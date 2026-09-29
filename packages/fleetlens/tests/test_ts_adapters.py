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
