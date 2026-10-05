"""PythonWebAdapter: route discovery across FastAPI/Flask/Sanic patterns + interface loader."""
from __future__ import annotations

from fleetlens.adapters.base import Interface, _slug
from fleetlens.adapters.python_web import PythonWebAdapter
from fleetlens.adapters.registry import build_interfaces
from fleetlens.loaders import interfaces as iface_loader
from fleetlens.store.sqlite import SqliteStore


def test_slug_and_id():
    assert _slug("/events/{id:int}") == "events-id"
    assert _slug("/") == "root"
    assert Interface(method="POST", path="/orders/{id}").id == "post-orders-id"


def _write(tmp_path, name, code):
    (tmp_path / name).write_text(code)


def test_fastapi_and_flask_routes(tmp_path):
    _write(tmp_path, "api.py", '''
from fastapi import APIRouter
router = APIRouter(prefix="/v1")

@router.get("/orders/{id}")
async def get_order(id):
    """Fetch one order."""
    return {}

@router.post("/orders")
async def create_order():
    return {}
''')
    _write(tmp_path, "web.py", '''
from flask import Blueprint
bp = Blueprint("bp", __name__, url_prefix="/admin")

@bp.route("/health", methods=["GET", "POST"])
def health():
    return "ok"
''')
    found = {(i.method, i.path): i for i in PythonWebAdapter().discover(tmp_path)}
    # prefix composition + verb decorators (FastAPI) and methods= (Flask)
    assert ("GET", "/v1/orders/{id}") in found
    assert ("POST", "/v1/orders") in found
    assert ("GET", "/admin/health") in found and ("POST", "/admin/health") in found
    o = found[("GET", "/v1/orders/{id}")]
    assert o.handler == "get_order" and o.summary == "Fetch one order." and o.framework == "fastapi"
    assert o.evidence and o.evidence[0].startswith("api.py:")


def test_non_literal_path_skipped(tmp_path):
    _write(tmp_path, "d.py", '''
from fastapi import FastAPI
app = FastAPI()
P = "/dyn"
@app.get(P)
def dynamic():
    return {}
''')
    assert PythonWebAdapter().discover(tmp_path) == []  # non-literal path -> honest skip


def test_build_and_load_interfaces_and_uniqueness(tmp_path):
    _write(tmp_path, "api.py", '''
from fastapi import FastAPI
app = FastAPI()
@app.get("/x")
def a():
    return {}
@app.get("/x")   # duplicate (method,path) is de-duped by the registry
def b():
    return {}
''')
    build_interfaces(tmp_path, "svc")
    store = SqliteStore(":memory:")
    summary = iface_loader.load(tmp_path / ".context", "svc", store)
    assert summary["interfaces"] == 1
    objs = store.list_objects("interface")
    assert [o.id for o in objs] == ["interface:svc:get-x"]
    assert objs[0].payload["method"] == "GET" and objs[0].payload["path"] == "/x"


def test_awaiting_a_cache_or_an_orm_is_not_an_outbound_request(tmp_path):
    """`await` was treated as evidence of HTTP, which it is not in an async service: nearly
    everything is awaited there, including every cache read and every ORM write. On one
    fleet that misread 383 calls, and each one whose argument was not a literal became an
    unresolved site the LLM tier was then asked to invent a URL for. One of them was a
    Redis read, and the model duly proposed a plausible path that did not exist.
    """
    from fleetlens.adapters.outbound import discover_outbound

    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "svc.py").write_text('''
async def handler(request, key, order_id):
    cached = await RedisCache.get(key)
    await queue.put(key)
    await Corporates.all().delete()
    await db.get(order_id)
    result = await self.client.get(f"/v1/orders/{order_id}")
    warm = await CacheWarmingHttpClient.get("/v1/warm")
    return cached, result, warm
''')
    calls = discover_outbound(tmp_path)
    paths = sorted(c.path for c in calls)

    # an interpolated segment normalises to {}, which is what makes a caller's
    # path joinable against a provider's route
    assert paths == ["/v1/orders/{}", "/v1/warm"]


def test_a_receiver_naming_itself_http_is_believed_over_its_storage_word(tmp_path):
    """The exclusion is on what the receiver is, not on a word appearing anywhere in it.
    A cache that warms itself over HTTP is still making requests, and `dbx_api_client` is
    not a database."""
    from fleetlens.adapters.outbound import _not_a_client

    for name in ("RedisCache", "BaseCache", "redis_client", "queue", "db",
                 "Corporates.all()", "ServiceConfigCache"):
        assert _not_a_client(name), name
    for name in ("self.client", "http_client", "session", "SearchServiceClient",
                 "CacheWarmingHttpClient", "dbx_api_client", "self.api_client", "cls"):
        assert not _not_a_client(name), name
