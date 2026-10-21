"""`fl pin` against a fake admin server. Stdlib only: this file runs on a base install."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fleetlens import cli, pin_client

TOKEN = "tok-very-secret"


class _Fake:
    """Records requests and replays one canned response."""

    def __init__(self):
        self.requests = []
        self.status = 200
        self.body = {"api_version": 1}

    def start(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def _do(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                outer.requests.append({"method": self.command, "path": self.path,
                                       "auth": self.headers.get("Authorization"),
                                       "body": json.loads(raw) if raw else None})
                data = json.dumps(outer.body).encode()
                self.send_response(outer.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PATCH = do_DELETE = _do

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake(monkeypatch, tmp_path):
    for k in ("FLEETLENS_URL", "FLEETLENS_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    f = _Fake().start()
    yield f
    f.stop()


def _pin(alias="eval", expires="2026-11-20T00:00:00+00:00"):
    return {"alias": alias, "build_id": "20261021T071755Z", "reason": "baseline",
            "owner": "me", "created_at": "2026-10-21T00:00:00+00:00", "expires_at": expires}


def _run(fake, *argv):
    return cli.main([*argv[:2 if argv[0] == "pin" else 1], *argv[2 if argv[0] == "pin" else 1:],
                     "--url", fake.url, "--token", TOKEN])


def test_create_sends_post_without_a_duration_and_prints_the_line(fake, capsys):
    fake.status, fake.body = 201, {"api_version": 1, **_pin()}
    assert _run(fake, "pin", "create", "--as", "eval", "--reason", "baseline",
                "--owner", "me") == 0
    req = fake.requests[0]
    assert (req["method"], req["path"], req["auth"]) == ("POST", "/pins", f"Bearer {TOKEN}")
    assert req["body"] == {"alias": "eval", "reason": "baseline", "owner": "me"}
    assert capsys.readouterr().out.strip() == (
        'pinned build 20261021T071755Z as "eval", expires 2026-11-20')


def test_create_has_no_duration_option(fake):
    with pytest.raises(SystemExit):
        cli.main(["pin", "create", "--as", "x", "--days", "5", "--url", fake.url,
                  "--token", TOKEN])


def test_list_prints_a_table(fake, capsys):
    fake.body = {"api_version": 1, "pins": [_pin("a"), _pin("bb")]}
    assert _run(fake, "pin", "list") == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == ["ALIAS", "BUILD", "OWNER", "REASON", "EXPIRES"]
    assert out[1].startswith("a ") and out[2].startswith("bb") and "2026-11-20" in out[2]


def test_list_when_empty(fake, capsys):
    fake.body = {"api_version": 1, "pins": []}
    assert _run(fake, "pin", "list") == 0
    assert "no pins" in capsys.readouterr().out


def test_extend_sends_patch_with_days(fake, capsys):
    fake.body = {"api_version": 1, **_pin()}
    assert _run(fake, "pin", "extend", "eval", "--days", "3") == 0
    req = fake.requests[0]
    assert (req["method"], req["path"], req["body"]) == ("PATCH", "/pins/eval", {"days": 3})


def test_delete_and_unpin_send_delete(fake):
    fake.body = {"api_version": 1, "deleted": "eval"}
    assert _run(fake, "pin", "delete", "eval") == 0
    assert _run(fake, "unpin", "eval") == 0
    assert [(r["method"], r["path"]) for r in fake.requests] == [("DELETE", "/pins/eval")] * 2


def test_connection_precedence_flag_then_env_then_config(monkeypatch, tmp_path):
    cfg = tmp_path / "config.toml"
    cfg.write_text('url = "http://cfg"\ntoken = "cfgtok"\n')
    cfg.chmod(0o600)
    env = {"FLEETLENS_URL": "http://env", "FLEETLENS_TOKEN": "envtok"}
    assert pin_client.resolve_connection("http://flag", "flagtok", env, cfg) == (
        "http://flag", "flagtok")
    assert pin_client.resolve_connection(None, None, env, cfg) == ("http://env", "envtok")
    assert pin_client.resolve_connection(None, None, {}, cfg) == ("http://cfg", "cfgtok")


def test_config_respects_xdg_and_trims_trailing_slash(tmp_path):
    d = tmp_path / "fleetlens"
    d.mkdir()
    (d / "config.toml").write_text('url = "http://h:1/"\ntoken = "t"\n')
    (d / "config.toml").chmod(0o600)
    got = pin_client.resolve_connection(None, None, {"XDG_CONFIG_HOME": str(tmp_path)})
    assert got == ("http://h:1", "t")


def test_config_parser_handles_comments_quotes_and_missing_file(tmp_path):
    cfg = tmp_path / "c.toml"
    cfg.write_text('# note\n\n[table]\nurl = \'http://a\'  \ntoken = plain # trailing\n'
                   'other = "x"\n')
    assert pin_client._read_config(cfg) == {"url": "http://a", "token": "plain"}
    assert pin_client._read_config(tmp_path / "missing.toml") == {}


def test_group_readable_config_with_a_token_warns(tmp_path, capsys):
    cfg = tmp_path / "c.toml"
    cfg.write_text('url = "http://a"\ntoken = "t"\n')
    cfg.chmod(0o644)
    pin_client.resolve_connection(None, None, {}, cfg)
    err = capsys.readouterr().err
    assert "readable by other users" in err and '"t"' not in err


def test_missing_url_and_token_exit_2_with_guidance(fake, capsys):
    assert cli.main(["pin", "list"]) == 2
    err = capsys.readouterr().err
    assert "FLEETLENS_URL" in err and "--url" in err and "config.toml" in err


def test_server_error_prints_the_message_and_exits_1(fake, capsys):
    fake.status, fake.body = 409, {"api_version": 1, "error": "a pin named 'x' already exists"}
    assert _run(fake, "pin", "create", "--as", "x") == 1
    assert "a pin named 'x' already exists" in capsys.readouterr().err


def test_unreachable_server_exits_2_without_a_traceback(capsys):
    import socket
    with socket.socket() as sock:           # a port that was free a moment ago
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    assert cli.main(["pin", "list", "--url", f"http://127.0.0.1:{port}",
                     "--token", TOKEN]) == 2
    err = capsys.readouterr().err
    assert "cannot reach" in err and "Traceback" not in err


def test_newer_api_version_still_works_and_prints_one_note(fake, capsys):
    fake.body = {"api_version": 99, "pins": [_pin()], "brand_new": {"x": 1}}
    assert _run(fake, "pin", "list") == 0
    cap = capsys.readouterr()
    assert "2026-11-20" in cap.out
    assert cap.err.count("upgrade") == 1


def test_token_never_appears_in_output(fake, capsys):
    fake.status, fake.body = 401, {"api_version": 1, "error": "unauthorized"}
    _run(fake, "pin", "list")
    _run(fake, "pin", "create", "--as", "x")
    cap = capsys.readouterr()
    assert TOKEN not in cap.out + cap.err


def test_pin_help_does_not_load_the_server_stack():
    """`pip install fleetlens` with no extras must be enough for `fl pin`."""
    code = (
        "import sys\n"
        "import fleetlens.cli as c\n"
        "try:\n    c.main(['pin', '--help'])\nexcept SystemExit:\n    pass\n"
        "bad = [m for m in ('mcp', 'numpy', 'tree_sitter', 'starlette', 'uvicorn')"
        " if m in sys.modules]\n"
        "print('LOADED', bad)\n")
    out = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True,
                         env={"PYTHONPATH": str(__import__("pathlib").Path(cli.__file__)
                                                .parent.parent)})
    assert "LOADED []" in out.stdout, out.stdout + out.stderr
