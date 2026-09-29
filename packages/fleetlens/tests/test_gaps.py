"""Skipped-site recording + the grounded LLM gap-filler."""
from __future__ import annotations

import json

from fleetlens.adapters.outbound import discover_outbound
from fleetlens.adapters.python_web import PythonWebAdapter
from fleetlens.adapters.registry import build_interfaces
from fleetlens.enrich.gaps import fill_gaps
from fleetlens.loaders import interfaces as iface_loader
from fleetlens.store.models import KnowledgeObject
from fleetlens.store.sqlite import SqliteStore


def _write(root, name, code):
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(code)


_ROUTE = '''
from sanic import Blueprint
from app.models import SendApiModel
bp = Blueprint("bp")

@bp.route(SendApiModel.uri(), methods=[SendApiModel.http_method()])
async def send(request):
    return {}

@bp.get("/literal")
async def lit(request):
    return {}
'''

_MODEL = '''
class SendApiModel:
    _uri = "/send-notification"
    _method = "POST"

    @classmethod
    def uri(cls):
        return cls._uri

    @classmethod
    def http_method(cls):
        return cls._method
'''


def test_python_web_records_skipped_site(tmp_path):
    _write(tmp_path, "app/routes.py", _ROUTE)
    _write(tmp_path, "app/models.py", _MODEL)
    skipped = []
    ifaces = PythonWebAdapter().discover(tmp_path, skipped)
    assert [i.path for i in ifaces] == ["/literal"]
    assert len(skipped) == 1
    site = skipped[0]
    assert site.kind == "interface" and site.reason == "non-literal-path"
    assert site.file == "app/routes.py" and site.expr == "SendApiModel.uri()"
    assert {"SendApiModel", "uri", "http_method"} <= set(site.names)  # whole decorator
    assert site.method is None  # methods=[...] was non-literal too
    assert "@bp.route(" in site.snippet and "async def send" in site.snippet

    build_interfaces(tmp_path, "svc")
    doc = json.loads((tmp_path / ".context" / "skipped.json").read_text())
    assert doc["schema"] == "skipped/v1" and doc["sites"][0]["expr"] == "SendApiModel.uri()"


def test_outbound_records_only_http_looking_sites(tmp_path):
    _write(tmp_path, "c.py", '''
async def go(session, url, d, key):
    await session.post(url, json={})          # awaited -> recorded
    resp = await client.request("GET", url)   # .request(method, url) -> recorded, method known
    d.get(key)                                # dict lookup -> noise, not recorded
    requests.get(f"{base}/orders")            # f-string with non-path prefix -> recorded
''')
    skipped = []
    resolved = discover_outbound(tmp_path, skipped)
    # f"{base}/orders" carries a literal path even though the host is a variable, so it now
    # resolves rather than being recorded as a gap.
    assert {(c.verb, c.path) for c in resolved} == {("GET", "/orders")}
    got = {(s.line, s.method, s.expr) for s in skipped}
    assert got == {(3, "POST", "url"), (4, "GET", "url")}
    assert all(s.kind == "outbound" and s.reason == "non-literal-url" for s in skipped)


class ScriptedLLM:
    def __init__(self, replies):
        self.replies, self.prompts = list(replies), []

    def complete(self, prompt, *, system=None, max_tokens=64):
        self.prompts.append(prompt)
        return self.replies.pop(0)


def _symbol(slug, sid, path, kind, qual, start, end):
    return KnowledgeObject("code_symbol", f"{slug}:{sid}", qual.rsplit(".", 1)[-1], None, "unknown",
                           "static", "callgraph", None, None,
                           {"path": path, "qualname": qual, "kind": kind, "lang": "python",
                            "line_start": start, "line_end": end})


def _service(slug, root, skipped, outbound=None):
    return KnowledgeObject("service", slug, slug, None, "unknown", "static", "index", None, None,
                           {"root": str(root), "skipped": skipped, "outbound": outbound or []})


def _indexed_store(tmp_path):
    _write(tmp_path, "app/routes.py", _ROUTE)
    _write(tmp_path, "app/models.py", _MODEL)
    build_interfaces(tmp_path, "svc")
    sites = json.loads((tmp_path / ".context" / "skipped.json").read_text())["sites"]
    s = SqliteStore(":memory:")
    iface_loader.load(tmp_path / ".context", "svc", s)
    s.upsert_object(_symbol("svc", "app/models.py::SendApiModel", "app/models.py", "class",
                            "SendApiModel", 2, 12))
    s.upsert_object(_symbol("svc", "app/models.py::SendApiModel.uri", "app/models.py", "method",
                            "SendApiModel.uri", 6, 8))
    s.upsert_object(_symbol("svc", "app/routes.py::send", "app/routes.py", "function", "send", 6, 8))
    s.upsert_object(_service("svc", tmp_path, sites))
    s.commit()
    return s


def test_gap_filler_grounds_and_stores_llm_interface(tmp_path):
    store = _indexed_store(tmp_path)
    llm = ScriptedLLM(['{"resolved": true, "method": "POST", "path": "/send-notification"}'])
    r = fill_gaps(store, llm)
    assert r["resolved"] == 1 and r["interfaces"] == 1 and r["rejected"] == 0

    # one-hop context reached the class body, class listed before the accessor
    assert '_uri = "/send-notification"' in llm.prompts[0]
    assert llm.prompts[0].index("(class SendApiModel)") < llm.prompts[0].index("(method SendApiModel.uri)")

    obj = store.get("interface:svc:post-send-notification")
    assert obj and obj.source == "llm" and obj.payload["confidence"] == "llm-grounded"
    assert obj.payload["evidence"] == ["app/routes.py:6"]
    # anchored to the decorated function, like the static loader does
    assert obj.payload["handler"] == "send"
    edges = store.edges_of("interface:svc:post-send-notification")
    assert [(e.relationship, e.to_id, e.source) for e in edges] == \
        [("handled_by", "code_symbol:svc:app/routes.py::send", "llm")]
    assert store.get("service:svc").payload["interface_count"] == 1
    # static one untouched
    assert store.get("interface:svc:get-literal").source == "static"

    # gated: a second run makes no LLM calls
    llm2 = ScriptedLLM([])
    r2 = fill_gaps(store, llm2)
    assert r2["skipped"] == 1 and llm2.prompts == []
    assert store.get("service:svc").payload["gaps"]


def test_gap_filler_rejects_hallucinated_path(tmp_path):
    store = _indexed_store(tmp_path)
    llm = ScriptedLLM(['{"resolved": true, "method": "POST", "path": "/api/v1/notify"}'])
    r = fill_gaps(store, llm)
    assert r["rejected"] == 1 and r["interfaces"] == 0
    assert store.get("interface:svc:post-api-v1-notify") is None
    gaps = store.get("service:svc").payload["gaps"]
    assert list(gaps.values())[0]["status"] == "rejected"


def test_gap_filler_records_unresolvable_and_outbound(tmp_path):
    _write(tmp_path, "app/client.py", '''
async def call(self):
    url = urljoin(self._host, self._endpoint)
    return await self._session.post(url, json={})
''')
    _write(tmp_path, "app/paths.py", 'ORDERS = "/orders/{id}"\n')
    skipped = []
    discover_outbound(tmp_path, skipped)
    from dataclasses import asdict
    s = SqliteStore(":memory:")
    s.upsert_object(_service("svc", tmp_path, [asdict(x) for x in skipped]))
    s.commit()

    # unresolvable: config-driven
    llm = ScriptedLLM(['{"resolved": false, "reason": "host and endpoint come from config"}'])
    r = fill_gaps(s, llm)
    assert r["unresolved"] == 1 and r["outbound"] == 0
    rec = list(s.get("service:svc").payload["gaps"].values())[0]
    assert rec["status"] == "unresolved" and "config" in rec["reason"]

    # resolvable outbound lands on the service node tagged llm; grounded against paths.py
    s.upsert_object(_service("svc", tmp_path, [asdict(x) for x in skipped]))
    llm = ScriptedLLM(['{"resolved": true, "method": "POST", "path": "/orders/{id}", "host": "orders"}'])
    r = fill_gaps(s, llm)
    assert r["outbound"] == 1
    ob = s.get("service:svc").payload["outbound"]
    assert ob == [{"verb": "POST", "path": "/orders/{id}", "host": "orders",
                   "evidence": "app/client.py:4", "source": "llm"}]


def test_ts_adapters_record_skipped_sites(tmp_path):
    from fleetlens.adapters.ts_outbound import discover_outbound as ts_outbound
    from fleetlens.adapters.ts_web import TSWebAdapter
    _write(tmp_path, "src/routes.ts", '''
import express from "express";
import { ROUTES } from "./paths";
const app = express();
app.get(ROUTES.orders, (req, res) => res.json({}));
app.post("/literal", handler);
const m = new Map(); m.get(key);
''')
    _write(tmp_path, "src/client.ts", '''
export async function load(base: string, id: string) {
  const a = await axios.get(`${base}/orders/${id}`);
  const b = await fetch(url, { method: "POST" });
  cache.get(key);
  return apiClient.get(path);
}
''')
    skipped = []
    ifaces = TSWebAdapter().discover(tmp_path, skipped)
    assert [i.path for i in ifaces] == ["/literal"]
    assert [(s.kind, s.line, s.expr, s.method, s.names) for s in skipped] == [
        ("interface", 5, "ROUTES.orders", "GET", ["ROUTES", "orders"])]

    skipped = []
    ts_outbound(tmp_path, skipped)
    got = {(s.line, s.method, s.expr) for s in skipped if s.file == "src/client.ts"}
    assert len(skipped) == 3  # m.get(key) in routes.ts is noise, not recorded
    assert got == {(3, "GET", "`{}/orders/{}`"), (4, "GET", "url"), (6, "GET", "path")}


def test_context_falls_back_to_textual_definitions(tmp_path):
    from fleetlens.enrich.gaps import _context
    _write(tmp_path, "src/paths.ts", 'export const ROUTES = { orders: "/orders/:id" };\n')
    s = SqliteStore(":memory:")  # no code_symbol rows at all
    ctx = _context(s, "svc", tmp_path, ["ROUTES", "orders"])
    assert "# src/paths.ts:1" in ctx and '"/orders/:id"' in ctx


def test_index_repo_fills_gaps_when_llm_given(tmp_path, monkeypatch):
    """`fl index --fill-gaps`: gaps are resolved as part of indexing, so a re-index never
    silently drops LLM-resolved interfaces. Call-graph build is stubbed (SCIP is covered
    elsewhere)."""
    from fleetlens import indexing
    from fleetlens.callgraph import cli as cg_cli

    _write(tmp_path, "app/routes.py", _ROUTE)
    _write(tmp_path, "app/models.py", _MODEL)

    def fake_cg(argv):
        root = tmp_path / ".context"
        root.mkdir(exist_ok=True)
        (root / "callgraph.json").write_text(json.dumps({"nodes": [
            {"id": "app/models.py::SendApiModel", "path": "app/models.py", "qualname": "SendApiModel",
             "kind": "class", "lang": "python", "line_start": 2, "line_end": 12},
            {"id": "app/routes.py::send", "path": "app/routes.py", "qualname": "send",
             "kind": "function", "lang": "python", "line_start": 6, "line_end": 8}], "edges": []}))
        return 0
    monkeypatch.setattr(cg_cli, "main", fake_cg)

    store = SqliteStore(":memory:")
    llm = ScriptedLLM(['{"resolved": true, "method": "POST", "path": "/send-notification"}'])
    [summary] = indexing.index_repo(tmp_path, store, slug="svc", llm=llm)
    assert summary["skipped"] == 1 and summary["gaps"]["resolved"] == 1
    assert summary["interfaces"] == 2  # 1 static + 1 filled
    assert store.get("interface:svc:post-send-notification").source == "llm"
    assert store.get("service:svc").payload["interface_count"] == 2

    # without an llm, indexing is purely deterministic and never calls anything
    [summary] = indexing.index_repo(tmp_path, store, slug="svc")
    assert summary["interfaces"] == 1 and "gaps" not in summary
    assert store.get("interface:svc:post-send-notification") is None  # re-index wiped it


def test_inbound_request_accessors_are_not_outbound_calls(tmp_path):
    """`request.args.get(...)` reads the INBOUND request; recording it wasted 12 minutes of
    local-LLM time on non-URLs during benchmarking."""
    _write(tmp_path, "h.py", '''
async def handler(request, session, method, args):
    filter_ = request.args.get("filter")
    body = request.json.get("x")
    page = request.query_args.get("page")
    await session.request(method, *args)          # forwarding wrapper, not a URL
    return await session.get(request.args.get("cb"))
''')
    skipped = []
    discover_outbound(tmp_path, skipped)
    exprs = {s.expr for s in skipped}
    assert not any(e.startswith("*") for e in exprs), exprs
    assert '"filter"' not in exprs and "'filter'" not in exprs
    # the genuine outbound call is still recorded
    assert any("request.args.get" in e for e in exprs), exprs
