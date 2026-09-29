"""Outbound extraction + cross-repo resolver: consumer outbound -> provider interface edge."""
from __future__ import annotations

from fleetlens.adapters.outbound import discover_outbound
from fleetlens.resolve import normalize_path, resolve
from fleetlens.service.relationships import RelationshipService
from fleetlens.store.models import KnowledgeObject
from fleetlens.store.sqlite import SqliteStore


def test_normalize_path():
    assert normalize_path("/orders/{id}") == "/orders/{}"
    assert normalize_path("/orders/<id:int>/items") == "/orders/{}/items"
    assert normalize_path("/x/") == "/x"


def test_outbound_extraction_shapes(tmp_path):
    (tmp_path / "client.py").write_text('''
import httpx
async def a():
    r = await httpx.get("http://order-service/orders/42")   # literal URL -> path + host
    return r

class Client:
    async def get_order(self, oid):
        path = "/orders/{}".format(oid)                       # path var + verb(path)
        return await self.get(path)
    async def make(self):
        return await self.post("/orders")                     # literal path
''')
    calls = {(c.verb, c.path): c for c in discover_outbound(tmp_path)}
    assert ("GET", "/orders/42") in calls
    assert calls[("GET", "/orders/42")].host == "order-service"
    assert ("GET", "/orders/{}") in calls
    assert ("POST", "/orders") in calls


def _svc(slug, outbound=None, ic=0):
    return KnowledgeObject("service", slug, slug, None, "unknown", "static", "index", None,
                           None, {"outbound": outbound or [], "interface_count": ic})


def _iface(slug, iid, method, path):
    return KnowledgeObject("interface", f"{slug}:{iid}", f"{method} {path}", None, "unknown",
                           "static", "adapter", None, None, {"method": method, "path": path})


def test_resolver_matches_outbound_to_interface():
    s = SqliteStore(":memory:")
    # provider 'orders' exposes GET /orders/{id}; consumer 'gateway' calls it
    s.upsert_object(_iface("orders", "get-orders-id", "GET", "/orders/{id}"))
    s.upsert_object(_svc("orders", ic=1))
    s.upsert_object(_svc("gateway", outbound=[
        {"verb": "GET", "path": "/orders/99", "host": "orders"},   # host corroborates
        {"verb": "POST", "path": "/unknown", "host": None},        # no provider -> unresolved
    ]))
    s.commit()

    r = resolve(s, s, s)
    assert r["edges"] == 1 and r["unresolved_calls"] == 1

    rel = RelationshipService(s, s)
    down = rel.for_object("service:gateway")["downstream"]
    assert len(down) == 1
    edge = down[0]
    assert edge["id"] == "service:orders" and edge["relationship"] == "calls"
    assert edge["metadata"]["confidence"] == "corroborated"
    # inverse
    up = rel.for_object("service:orders")["upstream"]
    assert [e["id"] for e in up] == ["service:gateway"]


def test_resolver_skips_self_calls():
    s = SqliteStore(":memory:")
    s.upsert_object(_iface("orders", "get-x", "GET", "/x"))
    s.upsert_object(_svc("orders", outbound=[{"verb": "GET", "path": "/x", "host": None}], ic=1))
    s.commit()
    assert resolve(s, s, s)["edges"] == 0  # a service calling its own endpoint is not an edge


def test_external_host_is_not_a_fleet_dependency():
    """A third-party URL whose path collides with a fleet route must not invent an edge."""
    from fleetlens.resolve import is_external_host
    slugs = {"athena_service", "notifyone-core"}
    assert is_external_host("drive.google.com", slugs)          # the real false positive
    assert is_external_host("api.stripe.com", slugs)
    assert not is_external_host("athena-service.internal", slugs)   # ours, dotted
    assert not is_external_host("notifyone-core", slugs)            # ours, bare
    assert not is_external_host("localhost", slugs)
    assert not is_external_host("", slugs)


def test_env_prefix_stripped_so_one_logical_queue_joins():
    from fleetlens.resolve import strip_env_prefix
    assert strip_env_prefix("stag-diagnostics-test_inventories") == "diagnostics-test_inventories"
    assert strip_env_prefix("pluto-diagnostics-test_inventories") == "diagnostics-test_inventories"
    # only strip while at least two segments remain, so the key stays specific
    assert strip_env_prefix("data_service-skus") == "data_service-skus"
    assert strip_env_prefix("orders") == "orders"


def test_resolver_drops_external_and_root_only_calls():
    from fleetlens.store.models import KnowledgeObject
    from fleetlens.store.sqlite import SqliteStore
    s = SqliteStore(":memory:")
    s.upsert_object(KnowledgeObject("interface", "provider:get-uc", "GET /uc", None, "unknown",
                                    "static", "adapter", None, None,
                                    {"type": "rest", "method": "GET", "path": "/uc"}))
    s.upsert_object(KnowledgeObject("interface", "provider:get-root", "GET /", None, "unknown",
                                    "static", "adapter", None, None,
                                    {"type": "rest", "method": "GET", "path": "/"}))
    s.upsert_object(KnowledgeObject("service", "provider", "provider", None, "unknown",
                                    "static", "index", None, None, {}))
    s.upsert_object(KnowledgeObject("service", "consumer", "consumer", None, "unknown",
                                    "static", "index", None, None,
                                    {"outbound": [
                                        {"verb": "GET", "path": "/uc", "host": "drive.google.com"},
                                        {"verb": "GET", "path": "/", "host": None},
                                    ]}))
    s.commit()
    r = resolve(s, s, s)
    assert r["edges"] == 0
    assert r["external_calls"] == 1 and r["unspecific_calls"] == 1


def test_env_prefixed_queues_produce_one_async_edge():
    from fleetlens.store.models import KnowledgeObject
    from fleetlens.store.sqlite import SqliteStore
    s = SqliteStore(":memory:")
    for slug in ("dexter", "merch"):
        s.upsert_object(KnowledgeObject("service", slug, slug, None, "unknown", "static",
                                        "index", None, None, {}))
    s.upsert_object(KnowledgeObject("interface", "dexter:publish-q", "PUBLISH q", None, "unknown",
                                    "static", "adapter", None, None,
                                    {"type": "event", "method": "PUBLISH", "framework": "config",
                                     "path": "stag-diagnostics-test_inventories"}))
    s.upsert_object(KnowledgeObject("interface", "merch:consume-q", "CONSUME q", None, "unknown",
                                    "static", "adapter", None, None,
                                    {"type": "event", "method": "CONSUME", "framework": "config",
                                     "path": "pluto-diagnostics-test_inventories"}))
    s.commit()
    r = resolve(s, s, s)
    assert r["async_edges"] == 1
    e = [x for x in s.edges_of("service:dexter", "both") if x.relationship == "publishes_to"]
    assert e and e[0].to_id == "service:merch"


def test_path_recovered_when_host_comes_from_config(tmp_path):
    from fleetlens.adapters.outbound import discover_outbound
    (tmp_path / "c.py").write_text(
        "from urllib.parse import urljoin\n"
        "async def go(self, base, client):\n"
        "    await client.post(urljoin(self._host, '/prepare-notification'), json={})\n"
        "    await client.get(f'{base}/orders/123')\n"
        "    await client.put(base + '/inventory/sync', json={})\n")
    got = {(c.verb, c.path) for c in discover_outbound(tmp_path)}
    assert got == {("POST", "/prepare-notification"), ("GET", "/orders/123"),
                   ("PUT", "/inventory/sync")}


# --- pluggable host resolution -------------------------------------------
def _b(value, key=""):
    from fleetlens.adapters.hosts import HostBinding
    return HostBinding(key=key, value=value)


def _ctx(slugs, **kw):
    from fleetlens.adapters.hosts import ResolveContext
    return ResolveContext(slugs=set(slugs), **kw)


def test_slug_match_requires_whole_tokens_not_substrings():
    from fleetlens.adapters.hosts import SlugMatchResolver
    r = SlugMatchResolver()
    ctx = _ctx({"orders", "orders_service", "payments"})
    assert r.resolve(_b("orders-svc"), ctx) == "orders"
    assert r.resolve(_b("orders-service"), ctx) == "orders_service"   # most specific wins
    assert r.resolve(_b("reorders-legacy"), ctx) is None              # not a whole token
    assert r.resolve(_b("s3.amazonaws.com"), ctx) is None             # known third party


def test_kubernetes_dns_resolver():
    from fleetlens.adapters.hosts import KubernetesDNSResolver
    r = KubernetesDNSResolver()
    ctx = _ctx({"orders"})
    assert r.resolve(_b("orders.default.svc.cluster.local"), ctx) == "orders"
    assert r.resolve(_b("orders-svc"), ctx) is None      # not its job; SlugMatch handles it


def test_manifest_declaration_is_the_escape_hatch():
    """Addresses no heuristic could map are declared, so a team never patches fleetlens."""
    from fleetlens.adapters.hosts import resolve_host
    assert resolve_host(_b("internal-lb-7.example.com"), _ctx({"orders"})) is None
    declared = {"internal-lb-7.example.com": "orders", "*.payments.internal": "payments"}
    assert resolve_host(_b("internal-lb-7.example.com"), _ctx({"orders"}, declared=declared)) == "orders"
    assert resolve_host(_b("eu.payments.internal"), _ctx({"orders"}, declared=declared)) == "payments"


def test_custom_resolver_plugs_in_without_touching_core():
    from fleetlens.adapters import hosts as H

    class LegacyPrefix:
        name = "legacy"
        def resolve(self, binding, ctx):
            return "orders" if binding.host.startswith("legacy-") else None

    H.HOST_RESOLVERS.insert(0, LegacyPrefix())
    try:
        assert H.resolve_host(_b("legacy-box-3"), _ctx({"orders"})) == "orders"
    finally:
        H.HOST_RESOLVERS.pop(0)


def test_port_resolver_handles_loopback_dev_configs():
    """notifyone: gateway config says localhost:9402, core declares PORT 9402."""
    from fleetlens.adapters.hosts import PortResolver
    r = PortResolver()
    ctx = _ctx({"core", "gateway"}, ports={"9402": "core"})
    assert r.resolve(_b("http://localhost:9402"), ctx) == "core"
    assert r.resolve(_b("http://127.0.0.1:9402"), ctx) == "core"
    assert r.resolve(_b("http://localhost:9999"), ctx) is None
    # a real hostname is the better signal; do not guess from a shared port like 8080
    assert r.resolve(_b("http://orders-svc:9402"), ctx) is None


def test_key_name_resolver_uses_the_key_when_the_value_is_uninformative():
    from fleetlens.adapters.hosts import KeyNameResolver
    r = KeyNameResolver()
    ctx = _ctx({"notifyone-core"}, aliases={"notification-core": "notifyone-core"})
    assert r.resolve(_b("http://localhost:9402", "NOTIFICATION_CORE.HOST"), ctx) == "notifyone-core"
    assert r.resolve(_b("http://localhost:1", "NOTIFYONE_CORE_URL"), ctx) == "notifyone-core"
    # a real host wins over the key name
    assert r.resolve(_b("http://other-svc:80", "NOTIFICATION_CORE.HOST"), ctx) is None


def test_service_identity_reads_own_port_and_name(tmp_path):
    import json as _json

    from fleetlens.adapters.hosts import service_identity
    (tmp_path / "config_template.json").write_text(_json.dumps(
        {"NAME": "notification_core", "PORT": 9402, "REDIS": {"PORT": 6379}}))
    ident = service_identity(tmp_path)
    assert ident["port"] == "9402" and ident["name"] == "notification_core"


def test_config_hosts_extraction(tmp_path):
    import json as _json

    from fleetlens.adapters.hosts import config_hosts
    (tmp_path / "config.json").write_text(_json.dumps({
        "ORDERS": {"HOST": "http://orders-svc:8080"},
        "PAYMENTS_BASE_URL": "https://payments.internal/v1",
        "DB": {"PASSWORD": "hunter2"},          # not a host key
        "HOST": "0.0.0.0",                       # own bind address, skipped
        "BILLING": {"HOST": "localhost:9500"},   # kept: PortResolver needs loopback
    }))
    got = {(b.key, b.host) for b in config_hosts(tmp_path)}
    assert got == {("ORDERS.HOST", "orders-svc"), ("PAYMENTS_BASE_URL", "payments.internal"),
                   ("BILLING.HOST", "localhost")}
    assert [b.port for b in config_hosts(tmp_path) if b.key == "BILLING.HOST"] == ["9500"]


def test_slug_match_is_anchored_at_the_first_token():
    """USER_GENERATED_CONTENT_SERVICE contains content_service's tokens but is not it."""
    from fleetlens.adapters.hosts import SlugMatchResolver
    r = SlugMatchResolver()
    ctx = _ctx({"content_service", "orders", "orders_service"})
    assert r.resolve(_b("content-service.internal"), ctx) == "content_service"
    assert r.resolve(_b("user-generated-content-service"), ctx) is None
    assert r.resolve(_b("orders-svc"), ctx) == "orders"
    assert r.resolve(_b("orders-service:8080"), ctx) == "orders_service"


def test_config_declared_edges_and_corroboration():
    from fleetlens.store.models import KnowledgeObject
    from fleetlens.store.sqlite import SqliteStore
    s = SqliteStore(":memory:")
    s.upsert_object(KnowledgeObject("service", "orders", "orders", None, "unknown", "static",
                                    "index", None, None, {}))
    s.upsert_object(KnowledgeObject("interface", "orders:get-items", "GET /items", None,
                                    "unknown", "static", "adapter", None, None,
                                    {"type": "rest", "method": "GET", "path": "/items"}))
    # web declares the address but no observed call -> config-confidence edge
    s.upsert_object(KnowledgeObject("service", "web", "web", None, "unknown", "static",
                                    "index", None, None,
                                    {"host_bindings": [{"key": "ORDERS.HOST", "host": "orders-svc"}]}))
    # api declares it AND has a matching request path -> corroborated
    s.upsert_object(KnowledgeObject("service", "api", "api", None, "unknown", "static",
                                    "index", None, None,
                                    {"host_bindings": [{"key": "ORDERS.HOST", "host": "orders-svc"}],
                                     "outbound": [{"verb": "GET", "path": "/items", "host": None}]}))
    s.commit()
    r = resolve(s, s, s)
    assert r["config_declared_edges"] == 2
    conf = {e.to_id: e.metadata.get("confidence")
            for e in s.edges_of("service:web", "both") + s.edges_of("service:api", "both")
            if e.relationship == "calls"}
    assert conf["service:orders"] in ("config", "corroborated")
    web = [e for e in s.edges_of("service:web", "both") if e.relationship == "calls"]
    api = [e for e in s.edges_of("service:api", "both") if e.relationship == "calls"]
    assert web[0].metadata["confidence"] == "config"
    assert api[0].metadata["confidence"] == "corroborated"
