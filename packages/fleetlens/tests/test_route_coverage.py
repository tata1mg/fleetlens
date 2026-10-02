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
