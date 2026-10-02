"""Imperative route registration, and the net that catches what no adapter knows.

Four endpoints in a real service were invisible: registered with `blueprint.add_route`
rather than a decorator, so the adapter never looked at them, and not recorded as skipped
either. Framework coverage will always be incomplete; a route disappearing without anyone
knowing is the part that must not happen.
"""
from __future__ import annotations

from pathlib import Path

from fleetlens.adapters.python_web import PythonWebAdapter, looks_like_a_path


def _repo(tmp_path: Path, body: str) -> Path:
    (tmp_path / "app").mkdir(parents=True, exist_ok=True)
    (tmp_path / "app" / "routes.py").write_text(body)
    return tmp_path


def _found(tmp_path, body):
    skipped: list = []
    return PythonWebAdapter().discover(_repo(tmp_path, body), skipped), skipped


def test_sanic_add_route_with_version_and_url_prefix(tmp_path):
    """The real shape that went missing: imperative registration, and prefixes expressed
    as `version=` rather than a literal."""
    found, _ = _found(tmp_path, '''
from sanic import Blueprint

plain = Blueprint("plain")
versioned = Blueprint("versioned", version=4)
internal = Blueprint("internal", url_prefix="__internal__/v4")

async def send(request): ...

plain.add_route(send, "/send_notifications", methods=["POST"], name="a")
versioned.add_route(send, "/send_notifications", methods=["POST"], name="b")
versioned.add_route(send, "/send-event-notification", methods=["POST"], name="c")
internal.add_route(send, "/send_notifications", methods=["POST"], name="d")
''')
    assert {(i.method, i.path) for i in found} == {
        ("POST", "/send_notifications"),
        ("POST", "/v4/send_notifications"),
        ("POST", "/v4/send-event-notification"),
        ("POST", "/__internal__/v4/send_notifications"),
    }
    assert all(i.handler == "send" for i in found)


def test_flask_add_url_rule_takes_the_rule_first(tmp_path):
    """Argument order differs between frameworks, so the path is taken as the first string
    argument rather than by position."""
    found, _ = _found(tmp_path, '''
from flask import Flask
app = Flask(__name__)

def view(): ...

app.add_url_rule("/health", "health", view, methods=["GET"])
''')
    assert ("GET", "/health") in {(i.method, i.path) for i in found}


def test_fastapi_add_api_route(tmp_path):
    found, _ = _found(tmp_path, '''
from fastapi import APIRouter
router = APIRouter(prefix="/v1")

async def handler(): ...

router.add_api_route("/items", handler, methods=["POST"])
''')
    assert ("POST", "/v1/items") in {(i.method, i.path) for i in found}


def test_a_registration_style_nobody_has_implemented_is_still_reported(tmp_path):
    """The point of the net. An idiom released tomorrow should surface as unresolved, not
    as absent, so the count stays honest and the grounded LLM tier can resolve it."""
    found, skipped = _found(tmp_path, '''
from fastapi import FastAPI
app = FastAPI()

@app.get("/known")
def known(): ...

app.router.register_endpoint_handler("/invented/by/a/framework", known, verb="POST")
''')
    assert {(i.method, i.path) for i in found} == {("GET", "/known")}
    unknown = [s for s in skipped if s.reason == "unrecognised-route-registration"]
    assert len(unknown) == 1
    assert "/invented/by/a/framework" in unknown[0].expr


def test_string_handling_is_not_mistaken_for_a_route(tmp_path):
    """Every false positive in the first version of this detector was a separator passed to
    split or strip. A noisy unresolved count is worse than none, because nobody reads it."""
    _, skipped = _found(tmp_path, '''
from fastapi import FastAPI
app = FastAPI()

parts = "/a/b".split("/")
base = "/x/".strip("/")
head = some_url.rsplit("/d/", 1)
joined = "/".join(["a", "b"])
''')
    assert [s for s in skipped if s.reason == "unrecognised-route-registration"] == []


def test_what_counts_as_a_url_path():
    assert looks_like_a_path("/orders")
    assert looks_like_a_path("/v4/send_notifications")
    assert looks_like_a_path("/users/{id}")
    assert not looks_like_a_path("/")              # a separator, not a route
    assert not looks_like_a_path("relative")
    assert not looks_like_a_path("")


def test_a_computed_path_is_recorded_not_dropped(tmp_path):
    found, skipped = _found(tmp_path, '''
from sanic import Blueprint
bp = Blueprint("b")

async def h(request): ...

bp.add_route(h, SOME_CONSTANT, methods=["POST"])
''')
    assert found == []
    assert [s.reason for s in skipped] == ["non-literal-path"]


# --- registration the import allow-list could not see --------------------------------

def test_a_framework_we_have_never_heard_of_is_still_read(tmp_path):
    """A real gateway exposed 1190 endpoints and fleetlens reported none.

    The adapter admitted a file only when it imported fastapi, flask, sanic or starlette.
    This one imports an in-house framework, so the file was never opened: no interfaces,
    and no unresolved count either, which reads as a service with no API rather than as a
    gap. What a module *does* is the durable signal, not which package it imports.
    """
    found, skipped = _found(tmp_path, '''
from company_internal_framework import BaseRequestHandler, get, post, Request

class OrderHandler(BaseRequestHandler):
    @post(path="/v4/orders")
    async def create(self, request): ...

    @get(path="/v4/orders/{order_id}")
    async def fetch(self, request): ...
''')
    assert {f.name for f in found} == {"POST /v4/orders", "GET /v4/orders/{order_id}"}
    assert [f.handler for f in found if f.method == "POST"] == ["create"]
    assert skipped == []


def test_the_path_can_be_a_keyword_argument(tmp_path):
    """`@app.get(path="/x")` is ordinary FastAPI, and reading only `args[0]` missed it."""
    found, _ = _found(tmp_path, '''
from fastapi import APIRouter

router = APIRouter(prefix="/v1")

@router.get(path="/items")
async def items(): ...

@router.post("/items")
async def create(): ...
''')
    assert {f.name for f in found} == {"GET /v1/items", "POST /v1/items"}


def test_a_typed_path_parameter_is_still_a_path(tmp_path):
    """Sanic and Starlette write `{id:\\d+}`, Flask writes `<regex(...)>`. A path pattern
    that stopped at word characters read those as not-a-path and dropped the route."""
    assert looks_like_a_path(r"/v4/category/{udp_id:\d+}")
    assert looks_like_a_path('/x/<regex("[0-9]+"):y>')
    assert not looks_like_a_path("/")
    assert not looks_like_a_path("not a path")

    found, _ = _found(tmp_path, '''
from company_internal_framework import get

@get(path="/v4/category/{udp_id:\\\\d+}")
async def category(request): ...
''')
    assert [f.name for f in found] == [r"GET /v4/category/{udp_id:\d+}"]


def test_a_bare_decorator_is_not_a_route_just_because_of_its_name(tmp_path):
    """`get` and `post` are ordinary function names. Without a module that demonstrably
    registers routes this way, a decorator called `post` is just a decorator."""
    found, skipped = _found(tmp_path, '''
from celery import task

@task(path="computed")
def post(payload): ...

@task
def get(key): ...
''')
    assert found == []
    assert [s for s in skipped if s.kind == "interface" and s.reason == "non-literal-path"] == []


def test_a_computed_path_beside_a_literal_one_is_recorded_not_dropped(tmp_path):
    """Once a module has shown the idiom unambiguously, a sibling route whose path is
    built at import time is a route fleetlens could not read, which is the thing the
    unresolved count exists to report."""
    found, skipped = _found(tmp_path, '''
from company_internal_framework import get
PREFIX = "/v4"

@get(path="/v4/health")
async def health(request): ...

@get(path=PREFIX + "/orders")
async def orders(request): ...
''')
    assert [f.name for f in found] == ["GET /v4/health"]
    assert [(s.reason, s.method) for s in skipped] == [("non-literal-path", "GET")]


# --- the literal is not the path ----------------------------------------------------

def test_a_route_written_without_its_leading_slash_still_joins_on_one(tmp_path):
    """A real service reported `/v4prescriptions/status` for an endpoint served at
    `/v4/prescriptions/status`.

    Frameworks supply the missing slash before composing any prefix. Sanic says so in a
    comment -- "Fix case where the user did not prefix the URL with a /" -- Flask joins on
    the slash, and FastAPI requires one. Concatenating the literal as written was the only
    party that did not.
    """
    found, _ = _found(tmp_path, '''
from sanic import Blueprint

prescription = Blueprint("prescription", version=4)

@prescription.route("prescriptions/status", methods=["POST"])
async def status(request): ...

@prescription.route("/presigned_urls", methods=["POST"])
async def presigned(request): ...
''')
    assert {f.name for f in found} == {"POST /v4/prescriptions/status",
                                       "POST /v4/presigned_urls"}


def test_a_prefix_and_a_path_never_produce_a_double_slash(tmp_path):
    found, _ = _found(tmp_path, '''
from flask import Blueprint

bp = Blueprint("shop", __name__, url_prefix="/shop/")

@bp.route("/items")
def items(): ...
''')
    assert [f.name for f in found] == ["GET /shop/items"]


def test_a_mock_patch_target_is_not_a_patch_endpoint(tmp_path):
    """`mock.patch` shares its name with the HTTP verb, so `@patch("app.orders.fetch")`
    was reported as an endpoint. Normalising the leading slash would have made it worse by
    promoting it to `/app.orders.fetch`."""
    found, _ = _found(tmp_path, '''
from unittest import mock
from fastapi import APIRouter

router = APIRouter()

@router.get("/health")
async def health(): ...

@mock.patch("app.services.orders.fetch_order")
def test_fetch(m): ...

@router.patch("/orders/{order_id}")
async def amend(order_id: str): ...
''')
    assert {f.name for f in found} == {"GET /health", "PATCH /orders/{order_id}"}


def test_two_routes_collapsing_onto_one_path_are_recorded(tmp_path):
    """Only one route can serve a path, so the repeat collapses either way. What must not
    happen is it collapsing silently: a real service declared /merchant/generate_hash twice,
    once with a per-route version override we did not read, and the second endpoint left no
    trace anywhere. The surviving interface now carries both source lines, and the collision
    is a skipped site `fl doctor` can report.
    """
    from fleetlens.adapters.registry import discover_interfaces

    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "routes.py").write_text('''
from sanic import Blueprint

merchant = Blueprint("merchant", version=4)

@merchant.route("/merchant/generate_hash", methods=["POST"])
async def generate_hash(request): ...

@merchant.route("/merchant/generate_hash", methods=["POST"], version=5)
async def generate_hash_v5(request): ...
''')
    skipped: list = []
    found = discover_interfaces(tmp_path, skipped)

    assert [f.name for f in found] == ["POST /v4/merchant/generate_hash"]
    assert len(found[0].evidence) == 2                      # both declarations are kept
    dupes = [s for s in skipped if s.reason == "duplicate-path"]
    assert len(dupes) == 1 and dupes[0].method == "POST"
