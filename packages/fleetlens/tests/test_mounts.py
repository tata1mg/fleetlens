"""A route's path depends on where its router is mounted, usually in another file.

A real service declared its blueprints bare and composed the prefixes through four layers
of `__init__.py`. fleetlens read one file at a time, so it reported `/auto-complete` for an
endpoint served at `/search/v1/test/auto-complete`, and two endpoints that differed only by
their prefix collapsed into one row.
"""
from __future__ import annotations

from pathlib import Path

from fleetlens.adapters.python_web import PythonWebAdapter


def _write(root: Path, files: dict) -> Path:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return root


def _paths(root: Path) -> set:
    return {f.name for f in PythonWebAdapter().discover(root)}


def test_sanic_blueprint_group_prefixes_compose_across_files(tmp_path):
    """The shape that was wrong: a bare blueprint, and three nested groups in packages
    above it each contributing a segment."""
    _write(tmp_path, {
        "app/modules/search/routes/v1/test.py": '''
from sanic.blueprints import Blueprint
test = Blueprint("search_test_blueprint_v1")

@test.route("/auto-complete", methods=["GET"])
async def autocomplete(request): ...

@test.route("/suggestions", methods=["GET"])
async def suggestions(request): ...
''',
        "app/modules/search/routes/v1/__init__.py": '''
from sanic.blueprints import Blueprint
from .test import test
test_blueprints = Blueprint.group(test, url_prefix="/test")
''',
        "app/modules/search/routes/__init__.py": '''
from sanic.blueprints import Blueprint
from .v1 import test_blueprints
v1_blueprints = Blueprint.group(test_blueprints, url_prefix="/v1")
''',
        "app/modules/search/__init__.py": '''
from sanic.blueprints import Blueprint
from .routes import v1_blueprints
search_blueprints = Blueprint.group(v1_blueprints, url_prefix="/search")
''',
    })
    assert _paths(tmp_path) == {"GET /search/v1/test/auto-complete",
                                "GET /search/v1/test/suggestions"}


def test_two_versions_of_one_handler_stay_two_endpoints(tmp_path):
    """The prefix is the only thing telling these apart. Dropping it made them the same
    path, and since an interface id is derived from its path, one overwrote the other."""
    _write(tmp_path, {
        "app/routes/v1/test.py": '''
from sanic.blueprints import Blueprint
test = Blueprint("v1")

@test.route("/<test_id:str>/dynamic", methods=["GET"])
async def dynamic(request): ...
''',
        "app/routes/v2/test.py": '''
from sanic.blueprints import Blueprint
test = Blueprint("v2")

@test.route("/<test_id:str>/dynamic", methods=["GET"])
async def dynamic(request): ...
''',
        "app/routes/__init__.py": '''
from sanic.blueprints import Blueprint
from .v1.test import test as v1_test
from .v2.test import test as v2_test
v1 = Blueprint.group(v1_test, url_prefix="/content/v1/test")
v2 = Blueprint.group(v2_test, url_prefix="/content/v2/test")
''',
    })
    found = _paths(tmp_path)
    assert found == {"GET /content/v1/test/<test_id:str>/dynamic",
                     "GET /content/v2/test/<test_id:str>/dynamic"}


def test_fastapi_include_router_prefix_adds_to_the_router_s_own(tmp_path):
    _write(tmp_path, {
        "api/orders.py": '''
from fastapi import APIRouter
router = APIRouter(prefix="/orders")

@router.get("/{order_id}")
async def fetch(order_id: str): ...
''',
        "api/main.py": '''
from fastapi import FastAPI
from .orders import router
app = FastAPI()
app.include_router(router, prefix="/api/v2")
''',
    })
    assert _paths(tmp_path) == {"GET /api/v2/orders/{order_id}"}


def test_flask_registration_prefix_replaces_the_blueprint_s_own(tmp_path):
    """Flask and FastAPI differ here, and both behaviours are the documented one. A Flask
    blueprint registered with a url_prefix serves under that prefix, not under both."""
    _write(tmp_path, {
        "shop/views.py": '''
from flask import Blueprint
bp = Blueprint("shop", __name__, url_prefix="/ignored")

@bp.route("/items")
def items(): ...
''',
        "shop/app.py": '''
from flask import Flask
from .views import bp
app = Flask(__name__)
app.register_blueprint(bp, url_prefix="/shop")
''',
    })
    assert _paths(tmp_path) == {"GET /shop/items"}


def test_a_router_mounted_twice_serves_both_paths(tmp_path):
    """Mounting one router under two prefixes is how a service exposes the same handlers
    publicly and internally. Both are real endpoints."""
    _write(tmp_path, {
        "api/common.py": '''
from fastapi import APIRouter
router = APIRouter()

@router.get("/health")
async def health(): ...
''',
        "api/main.py": '''
from fastapi import FastAPI
from .common import router
app = FastAPI()
app.include_router(router, prefix="/public")
app.include_router(router, prefix="/internal")
''',
    })
    assert _paths(tmp_path) == {"GET /public/health", "GET /internal/health"}


def test_an_unmounted_router_keeps_what_its_own_module_says(tmp_path):
    """Not every router is mounted somewhere fleetlens can follow: the mount may be built
    in a loop, or live in a package that is not in this repo. Falling back to the
    module-local prefix is what the adapter did before the graph existed, so an
    unresolvable chain costs nothing it was not already costing."""
    _write(tmp_path, {
        "svc/routes.py": '''
from fastapi import APIRouter
router = APIRouter(prefix="/v1")

@router.get("/ping")
async def ping(): ...
''',
    })
    assert _paths(tmp_path) == {"GET /v1/ping"}


def test_a_regex_match_group_is_not_a_blueprint_mount(tmp_path):
    """`.group` is also how you read a regex match, and it is far more common than Sanic's
    `Blueprint.group`. Reading one as the other would reparent routers arbitrarily."""
    _write(tmp_path, {
        "svc/routes.py": '''
import re
from sanic.blueprints import Blueprint
bp = Blueprint("svc", url_prefix="/v1")
m = re.match(r"(\\d+)", "42")
value = m.group(1)

@bp.route("/ping", methods=["GET"])
async def ping(request): ...
''',
    })
    assert _paths(tmp_path) == {"GET /v1/ping"}


def test_a_mounting_cycle_does_not_hang(tmp_path):
    """Nothing legitimate produces one, but a misread import could, and an indexer that
    hangs on one repository stalls the whole fleet sweep."""
    _write(tmp_path, {
        "svc/a.py": '''
from sanic.blueprints import Blueprint
from .b import b
a = Blueprint.group(b, url_prefix="/a")
''',
        "svc/b.py": '''
from sanic.blueprints import Blueprint
from .a import a
b = Blueprint.group(a, url_prefix="/b")

@b.route("/ping", methods=["GET"])
async def ping(request): ...
''',
    })
    found = _paths(tmp_path)
    assert all(f.endswith("/ping") for f in found)
