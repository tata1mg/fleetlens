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
