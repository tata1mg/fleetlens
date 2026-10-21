"""The /pins routes behind the real bearer middleware."""
from __future__ import annotations

import pytest

pytest.importorskip("starlette.testclient")
pytest.importorskip("mcp")

from fleetlens.pins import PinPolicy, PinStore  # noqa: E402
from fleetlens.server import http  # noqa: E402
from fleetlens.server.admin import pin_routes  # noqa: E402
from fleetlens.store.models import KnowledgeObject  # noqa: E402
from fleetlens.store.sqlite import SqliteStore  # noqa: E402
from starlette.applications import Starlette  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402
from starlette.routing import Route  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

TOKEN = "s3cret"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _obj(name):
    return KnowledgeObject(
        object_type="service", object_id=name, name=name, summary=None,
        version="1", source="static", generation_strategy="deterministic",
        last_generated_at=None, embed_text=None, payload={})


@pytest.fixture
def client(tmp_path):
    db = tmp_path / "fleet.db"
    st = SqliteStore(str(db))
    st.upsert_object(_obj("orders"))
    st.stamp_build({})
    st.commit()
    st.close()
    st = SqliteStore(str(db), read_only=True)     # as `fl serve --http` opens it
    store = PinStore(PinPolicy(tmp_path / "pins"))

    async def healthz(_request):
        return JSONResponse({"status": "ok", **st.index_info()})

    app = Starlette(routes=[Route("/healthz", healthz), *pin_routes(str(db), store)])
    app.add_middleware(http.build_auth_middleware(TOKEN))
    return TestClient(app)


def test_every_verb_without_a_token_is_rejected(client):
    """The admin surface must not be reachable without the shared token."""
    assert client.get("/pins").status_code == 401
    assert client.post("/pins", json={"alias": "a"}).status_code == 401
    assert client.patch("/pins/a", json={"days": 1}).status_code == 401
    assert client.delete("/pins/a").status_code == 401


def test_wrong_token_is_rejected(client):
    assert client.get("/pins", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_public_paths_did_not_grow():
    assert http.PUBLIC_PATHS == ("/healthz",)


def test_healthz_stays_public_and_reports_the_build(client):
    body = client.get("/healthz").json()
    assert body["status"] == "ok" and body["build_id"]


def test_create_then_list(client):
    r = client.post("/pins", headers=AUTH, json={"alias": "eval", "reason": "r", "owner": "me"})
    assert r.status_code == 201
    pin = r.json()
    assert pin["api_version"] == 1 and pin["alias"] == "eval" and pin["expires_at"]
    listing = client.get("/pins", headers=AUTH).json()
    assert [p["alias"] for p in listing["pins"]] == ["eval"]
    assert listing["pins"][0]["days_left"] == 30
    assert listing["retention"] == {"retain_days": 30, "extend_max_days": 10}
    assert listing["api_version"] == 1


def test_duration_in_create_is_ignored(client):
    pin = client.post("/pins", headers=AUTH,
                      json={"alias": "a", "days": 999, "expires_at": "2099-01-01"}).json()
    assert pin["days_left"] == 30


def test_duplicate_is_409_and_traversal_is_400(client):
    client.post("/pins", headers=AUTH, json={"alias": "a"})
    assert client.post("/pins", headers=AUTH, json={"alias": "a"}).status_code == 409
    r = client.post("/pins", headers=AUTH, json={"alias": "../../etc/passwd"})
    assert r.status_code == 400 and r.json()["api_version"] == 1
    assert client.post("/pins", headers=AUTH, json={}).status_code == 400


def test_patch_range_and_success(client):
    client.post("/pins", headers=AUTH, json={"alias": "a"})
    r = client.patch("/pins/a", headers=AUTH, json={"days": 11})
    assert r.status_code == 422 and "10" in r.json()["error"]
    ok = client.patch("/pins/a", headers=AUTH, json={"days": 5})
    assert ok.status_code == 200 and ok.json()["days_left"] == 35
    assert client.patch("/pins/zzz", headers=AUTH, json={"days": 1}).status_code == 404


def test_delete_then_gone(client):
    client.post("/pins", headers=AUTH, json={"alias": "a"})
    r = client.delete("/pins/a", headers=AUTH)
    assert r.status_code == 200 and r.json()["deleted"] == "a"
    assert client.delete("/pins/a", headers=AUTH).status_code == 404
    assert client.get("/pins", headers=AUTH).json()["pins"] == []


def test_bad_bodies_are_400(client):
    assert client.post("/pins", headers=AUTH, content=b"{nope").status_code == 400
    assert client.post("/pins", headers=AUTH, json=["a"]).status_code == 400


def test_unknown_fields_are_ignored(client):
    r = client.post("/pins", headers=AUTH, json={"alias": "a", "future_field": {"x": 1}})
    assert r.status_code == 201
