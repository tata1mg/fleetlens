"""TS adapters: Express interface discovery, axios/fetch outbound, and a TS->Python edge."""
from __future__ import annotations

from fleetlens.adapters.registry import discover_interfaces
from fleetlens.adapters.ts_outbound import discover_outbound as ts_outbound
from fleetlens.adapters.ts_web import TSWebAdapter
from fleetlens.resolve import resolve
from fleetlens.store.models import KnowledgeObject
from fleetlens.store.sqlite import SqliteStore

_EXPRESS = '''
import express from "express";
const app = express();
const router = express.Router();

app.get("/orders/:id", async (req, res) => { res.json({}); });
router.post("/orders", createOrder);
map.get("thing");            // not a route (no leading slash) -> ignored
'''

_CLIENT = '''
import axios from "axios";
export async function load(id: string) {
  const a = await axios.get(`http://order-service/orders/${id}`);
  const b = await fetch("/health", { method: "POST" });
  return [a, b];
}
'''


def test_express_route_discovery(tmp_path):
    (tmp_path / "routes.ts").write_text(_EXPRESS)
    ifaces = {(i.method, i.path): i for i in TSWebAdapter().discover(tmp_path)}
    assert ("GET", "/orders/:id") in ifaces
    assert ("POST", "/orders") in ifaces
    assert ("GET", "thing") not in [(i.method, i.path) for i in ifaces.values()]
    o = ifaces[("POST", "/orders")]
    assert o.framework == "express" and o.handler == "createOrder"
    # canonical id normalizes the :id param
    assert ifaces[("GET", "/orders/:id")].id == "get-orders-id"


def test_registry_applies_ts_adapter(tmp_path):
    (tmp_path / "routes.ts").write_text(_EXPRESS)
    found = {(i.method, i.path) for i in discover_interfaces(tmp_path)}
    assert ("GET", "/orders/:id") in found  # TSWebAdapter is wired into the registry


def test_ts_outbound_axios_and_fetch(tmp_path):
    (tmp_path / "client.ts").write_text(_CLIENT)
    calls = {(c.verb, c.path): c for c in ts_outbound(tmp_path)}
    assert ("GET", "/orders/{}") in calls          # template URL -> path with {}
    assert calls[("GET", "/orders/{}")].host == "order-service"
    # fetch defaults to GET in v1 (parsing the {method:...} option is a later refinement)
    assert ("GET", "/health") in calls


def test_cross_language_edge_ts_consumer_python_provider():
    # a Python provider exposes GET /orders/{id}; a TS consumer calls it -> resolved edge
    s = SqliteStore(":memory:")
    s.upsert_object(KnowledgeObject("interface", "orders:get-orders-id", "GET /orders/{id}",
                                    None, "unknown", "static", "adapter", None, None,
                                    {"method": "GET", "path": "/orders/{id}"}))
    s.upsert_object(KnowledgeObject("service", "orders", "orders", None, "unknown", "static",
                                    "index", None, None, {"interface_count": 1, "outbound": []}))
    s.upsert_object(KnowledgeObject("service", "web-ui", "web-ui", None, "unknown", "static",
                                    "index", None, None, {"outbound": [
                                        {"verb": "GET", "path": "/orders/42", "host": "orders"}]}))
    s.commit()
    assert resolve(s, s, s)["edges"] == 1
    from fleetlens.service.relationships import RelationshipService
    down = RelationshipService(s, s).for_object("service:web-ui")["downstream"]
    assert down[0]["id"] == "service:orders"


def test_plain_javascript_express_routes(tmp_path):
    """A UI server written in `.js` was never read, so it indexed with no interfaces."""
    (tmp_path / "server").mkdir()
    (tmp_path / "server" / "server.js").write_text('''
const express = require("express")
const app = express()
app.get("/healthcheck", (req, res) => res.send("ok"))
app.get(["/logout", "/account/logout"], logout)
app.post(ROUTES.pay, payments)
''')
    (tmp_path / "server" / "auth.jsx").write_text('''
import express from "express"
const router = express.Router()
router.post("/create_token", createToken)
export default router
''')
    skipped: list = []
    found = {(i.method, i.path, i.evidence[0]) for i in discover_interfaces(tmp_path, skipped)}
    assert found == {("GET", "/healthcheck", "server/server.js:4"),
                     ("GET", "/logout", "server/server.js:5"),
                     ("GET", "/account/logout", "server/server.js:5"),
                     ("POST", "/create_token", "server/auth.jsx:4")}
    assert [(s.file, s.line, s.expr) for s in skipped] == [("server/server.js", 6, "ROUTES.pay")]


def test_a_request_client_is_not_a_router(tmp_path):
    """A UI codebase makes as many `api.get("/x")` requests as it declares routes. Only an
    Express app or router's verb calls are endpoints."""
    (tmp_path / "server.js").write_text('''
const express = require("express")
const app = express()
app.get("/health", health)
''')
    (tmp_path / "client.js").write_text('''
const api = axios.create({ baseURL: "/api" })
export const load = (id) => api.get("/orders", { params: { id } })
export const save = (body) => api.post("/orders", body)
export const lookup = (key) => store.get("/cache/" + key, onHit)
''')
    skipped: list = []
    found = {(i.method, i.path) for i in TSWebAdapter().discover(tmp_path, skipped)}
    assert found == {("GET", "/health")}
    assert skipped == []


def test_routes_on_an_app_passed_in(tmp_path):
    """`export function addRoutes(app) { ... }`: the app is created elsewhere, and is
    recognised by what it is called with, or in TypeScript by its type."""
    (tmp_path / "middleware.js").write_text('''
export function addMiddlewares(app) {
  app.use(cookieParser())
  app.get("/login", redirectLogin)
}
''')
    (tmp_path / "routes.ts").write_text('''
import type { Express, Router } from "express"
export default (app: Express, admin: Router) => {
  app.post("/orders", create)
  admin.delete("/orders/:id", remove)
}
''')
    found = {(i.method, i.path) for i in TSWebAdapter().discover(tmp_path)}
    assert found == {("GET", "/login"), ("POST", "/orders"), ("DELETE", "/orders/:id")}
