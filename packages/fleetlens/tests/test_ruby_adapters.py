"""Rails route discovery, controller anchoring, and Ruby outbound calls."""
from __future__ import annotations

from fleetlens.adapters.ruby_outbound import discover_outbound
from fleetlens.adapters.ruby_web import RailsRouteAdapter


def _routes(tmp_path, body, name="routes.rb"):
    f = tmp_path / "config" / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(body)
    return tmp_path


def test_namespaces_nest_into_the_path(tmp_path):
    _routes(tmp_path, """
Rails.application.routes.draw do
  namespace :api do
    namespace :v1 do
      get '/status', to: 'health#status'
      post 'orders/bulk', to: 'orders#bulk'
    end
  end
  root to: 'home#index'
end
""")
    got = {(i.method, i.path, i.handler) for i in RailsRouteAdapter().discover(tmp_path)}
    assert got == {("GET", "/api/v1/status", "health#status"),
                   ("POST", "/api/v1/orders/bulk", "orders#bulk"),
                   ("GET", "/", "home#index")}


def test_resources_expands_to_restful_routes(tmp_path):
    _routes(tmp_path, """
Rails.application.routes.draw do
  resources :orders
end
""")
    got = {(i.method, i.path) for i in RailsRouteAdapter().discover(tmp_path)}
    assert got == {
        ("GET", "/orders"), ("POST", "/orders"), ("GET", "/orders/new"),
        ("GET", "/orders/:id"), ("GET", "/orders/:id/edit"),
        ("PUT", "/orders/:id"), ("PATCH", "/orders/:id"), ("DELETE", "/orders/:id"),
    }


def test_resources_honours_only_and_except(tmp_path):
    _routes(tmp_path, """
Rails.application.routes.draw do
  resources :phlebos, only: [:index]
  resources :lfs_centers, only: [:index, :show]
  resources :widgets, except: [:destroy, :new, :edit, :update, :create]
end
""")
    got = {(i.method, i.path) for i in RailsRouteAdapter().discover(tmp_path)}
    assert ("GET", "/phlebos") in got
    assert not any(p.startswith("/phlebos/") for _, p in got)
    assert ("GET", "/lfs_centers") in got and ("GET", "/lfs_centers/:id") in got
    assert ("GET", "/widgets") in got and ("GET", "/widgets/:id") in got
    assert ("DELETE", "/widgets/:id") not in got


def test_member_and_collection_blocks(tmp_path):
    _routes(tmp_path, """
Rails.application.routes.draw do
  resources :orders, only: [] do
    member do
      get 'status'
    end
    collection do
      post 'search'
    end
  end
end
""")
    got = {(i.method, i.path) for i in RailsRouteAdapter().discover(tmp_path)}
    assert ("GET", "/orders/:id/status") in got      # member nests under :id
    assert ("POST", "/orders/search") in got          # collection does not
    assert ("GET", "/orders") not in got              # only: [] suppressed the defaults


def test_routes_split_across_config_routes_directory(tmp_path):
    """The main file usually just pulls in modules; the routes live in config/routes/."""
    _routes(tmp_path, """
Rails.application.routes.draw do
  get 'ping', to: 'static#ping'
  extend OdinRoutes
end
""")
    _routes(tmp_path, """
module OdinRoutes
  def self.extended(router)
    router.instance_eval do
      namespace :api do
        resources :phlebos, only: [:index]
      end
    end
  end
end
""", name="routes/odin_routes.rb")
    got = {(i.method, i.path) for i in RailsRouteAdapter().discover(tmp_path)}
    assert ("GET", "/ping") in got and ("GET", "/api/phlebos") in got


def test_non_route_config_files_are_not_scanned(tmp_path):
    """An initializer's `resource '*'` is CORS config, not a route."""
    _routes(tmp_path, "Rails.application.routes.draw do\n  get '/real', to: 'a#b'\nend\n")
    init = tmp_path / "config" / "initializers"
    init.mkdir(parents=True)
    (init / "cors.rb").write_text(
        "Rails.application.config.middleware.insert_before 0, Rack::Cors do\n"
        "  allow do\n    resource '*', headers: :any\n  end\nend\n")
    got = {i.path for i in RailsRouteAdapter().discover(tmp_path)}
    assert got == {"/real"}


def test_controller_action_is_resolved_so_endpoints_trace_into_code(tmp_path):
    _routes(tmp_path, "Rails.application.routes.draw do\n  get '/', to: 'home#index'\nend\n")
    c = tmp_path / "app" / "controllers"
    c.mkdir(parents=True)
    (c / "home_controller.rb").write_text(
        "class HomeController < ApplicationController\n"
        "  def index\n    render json: {}\n  end\nend\n")
    iface = RailsRouteAdapter().discover(tmp_path)[0]
    # evidence carries both the route declaration and the controller action, and the
    # interface loader anchors handled_by from the latter
    assert iface.evidence[0].startswith("config/routes.rb:")
    assert iface.evidence[1] == "app/controllers/home_controller.rb:2"


def test_computed_paths_are_recorded_not_guessed(tmp_path):
    _routes(tmp_path, """
Rails.application.routes.draw do
  get SOME_CONSTANT, to: 'a#b'
  mount Sidekiq::Web => '/sidekiq'
end
""")
    skipped = []
    RailsRouteAdapter().discover(tmp_path, skipped)
    assert {s.reason for s in skipped} == {"non-literal-path", "mounted-engine"}


def test_ruby_outbound_http(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "client.rb").write_text("""
class Client
  def fetch(id)
    RestClient.get('http://orders-svc/orders/123')
    HTTParty.post('/billing/charge')
    params.get('not_http')
    conn.get(build_path(id))
  end
end
""")
    skipped = []
    got = {(c.verb, c.path) for c in discover_outbound(tmp_path, skipped)}
    assert got == {("GET", "/orders/123"), ("POST", "/billing/charge")}
    # params.get is a hash read, not a request
    assert all("params" not in s.expr for s in skipped)
    assert any(s.reason == "non-literal-url" for s in skipped)


def test_a_nested_resource_hangs_off_its_parent_s_id(tmp_path):
    """`resources :orders do resources :items end` serves /orders/:order_id/items. Dropping
    the parent's id segment produced a path the service does not answer on, and shortened
    every route under a nesting -- fifty-one sites across three real Rails services.

    `member` and `collection` are computed from the bare resource, so they are unaffected:
    a member route keeps its own `:id` and a collection route has none.
    """
    (tmp_path / "config").mkdir(parents=True)
    (tmp_path / "config" / "routes.rb").write_text('''
Rails.application.routes.draw do
  namespace :api do
    resources :orders, only: [] do
      resources :sample_collection_pools, only: [:index] do
        collection do
          get 'fetch_payment_modes'
        end
      end
      member do
        get 'slots'
      end
      collection do
        get 'recent'
      end
    end
    resource :profile, only: [] do
      resources :avatars, only: [:index]
    end
  end
end
''')
    found = {f"{i.method} {i.path}" for i in RailsRouteAdapter().discover(tmp_path)}

    assert "GET /api/orders/:order_id/sample_collection_pools" in found
    assert ("GET /api/orders/:order_id/sample_collection_pools/fetch_payment_modes"
            in found)
    assert "GET /api/orders/:id/slots" in found          # member keeps its own :id
    assert "GET /api/orders/recent" in found             # collection has none
    assert "GET /api/profile/avatars" in found           # singular parent adds no id
