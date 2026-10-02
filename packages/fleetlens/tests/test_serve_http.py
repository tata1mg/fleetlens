"""Read-only serving, the auth boundary, and index freshness reporting."""
from __future__ import annotations

import pytest
from fleetlens.server.http import build_auth_middleware, resolve_token
from fleetlens.store.models import KnowledgeObject
from fleetlens.store.sqlite import SqliteStore


def _obj(name):
    return KnowledgeObject(
        object_type="service", object_id=name, name=name, summary=None,
        version="1", source="static", generation_strategy="deterministic",
        last_generated_at=None, embed_text=None, payload={})


def _seed(path):
    st = SqliteStore(str(path))
    st.upsert_object(_obj("orders"))
    st.commit()
    st.close()


def test_read_only_store_cannot_write(tmp_path):
    db = tmp_path / "fleet.db"
    _seed(db)
    ro = SqliteStore(str(db), read_only=True)
    assert ro.list_objects("service")[0].name == "orders"
    with pytest.raises(Exception):
        ro.upsert_object(_obj("x"))
        ro.commit()
    ro.close()


def test_read_only_refuses_to_invent_an_index(tmp_path):
    """Serving a path that does not exist must fail, not come up empty. A server that
    answers "no services" for a typo'd path is indistinguishable from a broken index."""
    with pytest.raises(FileNotFoundError):
        SqliteStore(str(tmp_path / "absent.db"), read_only=True)


def test_index_info_reports_coverage(tmp_path):
    db = tmp_path / "fleet.db"
    _seed(db)
    info = SqliteStore(str(db), read_only=True).index_info()
    assert info["services"] == 1
    assert info["indexed_at"]


def test_serve_http_refuses_to_start_without_a_token(monkeypatch):
    monkeypatch.delenv("FLEETLENS_TOKEN", raising=False)
    with pytest.raises(SystemExit):
        resolve_token("FLEETLENS_TOKEN", allow_insecure=False)


def test_insecure_is_opt_in(monkeypatch):
    monkeypatch.delenv("FLEETLENS_TOKEN", raising=False)
    assert resolve_token("FLEETLENS_TOKEN", allow_insecure=True) == ""


def test_token_comes_from_the_named_env_var(monkeypatch):
    monkeypatch.setenv("MY_TOKEN", "s3cret")
    assert resolve_token("MY_TOKEN", allow_insecure=False) == "s3cret"


@pytest.mark.parametrize("header,expected", [
    ("Bearer s3cret", 200),
    ("Bearer wrong", 401),
    ("s3cret", 401),
    ("", 401),
])
def test_auth_boundary(header, expected):
    pytest.importorskip("starlette")
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    app = Starlette(routes=[
        Route("/mcp", lambda r: PlainTextResponse("ok")),
        Route("/healthz", lambda r: PlainTextResponse("ok")),
    ])
    app.add_middleware(build_auth_middleware("s3cret"))
    client = TestClient(app)
    headers = {"authorization": header} if header else {}
    assert client.get("/mcp", headers=headers).status_code == expected
    # the probe endpoint stays open so a load balancer does not need the token
    assert client.get("/healthz").status_code == 200


def test_fleetlens_home_places_the_index_under_it(monkeypatch):
    """A deployment owns one directory; commands should not have to repeat the path."""
    import importlib

    import fleetlens.cli as cli
    monkeypatch.setenv("FLEETLENS_HOME", "/srv/fleetlens")
    assert importlib.reload(cli)._default_db() == "/srv/fleetlens/data/fleetlens.db"
    monkeypatch.delenv("FLEETLENS_HOME")
    assert importlib.reload(cli)._default_db() == "fleetlens.db"


def test_renamed_index_is_picked_up_without_a_restart(tmp_path):
    """The refresh job builds a new file and renames it over the old one. Without this the
    server keeps the unlinked inode open and serves the previous graph indefinitely."""
    import os

    db = tmp_path / "fleet.db"
    _seed(db)
    ro = SqliteStore(str(db), read_only=True)
    assert ro.index_info()["services"] == 1

    fresh = tmp_path / "fleet.db.new"
    st = SqliteStore(str(fresh))
    st.upsert_object(_obj("orders"))
    st.upsert_object(_obj("billing"))
    st.commit()
    st.close()
    os.replace(fresh, db)

    assert ro.reload_if_changed() is True
    assert ro.index_info()["services"] == 2
    assert ro.reload_if_changed() is False  # steady state does not churn connections
    ro.close()


def test_reload_keeps_serving_when_the_file_goes_missing(tmp_path):
    """A half-finished refresh must not take the server down."""
    import os

    db = tmp_path / "fleet.db"
    _seed(db)
    ro = SqliteStore(str(db), read_only=True)
    os.remove(db)
    assert ro.reload_if_changed() is False
    assert ro.index_info()["services"] == 1  # still answering from the open descriptor
    ro.close()


def test_a_broken_dependency_is_not_reported_as_an_mcp_version_problem(capsys, monkeypatch):
    """mcp failing to import because pydantic is v1 is not an mcp version problem, and
    saying it is sends someone to reinstall the package that was working."""
    import argparse
    import builtins

    from fleetlens import cli

    real_import = builtins.__import__

    def boom(name, *a, **kw):
        if name.startswith("mcp"):
            raise ImportError("cannot import name 'TypeAdapter' from 'pydantic'",
                              name="pydantic")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", boom)
    rc = cli._cmd_serve(argparse.Namespace(
        db=":memory:", http=True, host="127.0.0.1", port=8081, embed_model="",
        embed_provider="ollama", base_url="", api_key_env="", token_env="T", insecure=True))

    err = capsys.readouterr().err
    assert rc == 2
    assert "pydantic" in err                     # names what actually failed
    assert "mcp>=1.2.0,<2.0" not in err          # and does not blame mcp's version


def test_only_the_token_gates_access_whatever_the_bind_address(monkeypatch):
    """Host and Origin are not checked. The SDK validates Host by default on a loopback
    bind, which protects a server with no authentication from a web page the user happens
    to visit. Every request here already needs a bearer token that such a page does not
    have, so the check rejects ordinary clients without guarding anything extra."""
    from fleetlens.server.http import security_settings

    for host in ("0.0.0.0", "127.0.0.1", "localhost", "::1"):
        s = security_settings(host, [])
        assert s.enable_dns_rebinding_protection is False, host


def test_an_operator_can_still_pin_the_accepted_hosts():
    """Off by default is a default, not a policy. Defence in depth stays available."""
    from fleetlens.server.http import security_settings

    s = security_settings("0.0.0.0", ["fleetlens.internal", "10.1.7.242"])
    assert s.enable_dns_rebinding_protection is True
    assert "fleetlens.internal" in s.allowed_hosts
    assert "fleetlens.internal:*" in s.allowed_hosts     # any port on that host
    assert "10.1.7.242:*" in s.allowed_hosts
    assert "https://fleetlens.internal" in s.allowed_origins
