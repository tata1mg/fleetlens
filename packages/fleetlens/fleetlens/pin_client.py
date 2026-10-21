"""Thin client for the pin admin API, for `fl pin` and `fl unpin`.

Standard library only, like enrich/providers.py. `pip install fleetlens` with no extras must
be enough to pin from a laptop, so nothing here may import the server stack, numpy or a
parser. The server holds all the policy (expiry, caps); this module only sends requests and
formats answers.
"""
from __future__ import annotations

import getpass
import json
import os
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

#: The response shape this client understands. A newer server still works, because unknown
#: fields are ignored, and the user is told to upgrade.
API_VERSION = 1


class ClientError(Exception):
    """`status` is the HTTP status, or None when the server could not be reached."""

    def __init__(self, status, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _config_file(env) -> Path:
    base = env.get("XDG_CONFIG_HOME", "").strip()
    root = Path(base) if base else Path.home() / ".config"
    return root / "fleetlens" / "config.toml"


def _read_config(path: Path) -> dict:
    """Read flat `key = "value"` lines. Only `url` and `token` are supported.

    tomllib arrived in Python 3.11 and the package supports 3.10, so this reads the one
    shape the file needs rather than pulling in a dependency. Tables and arrays are not
    understood and are skipped.
    """
    try:
        text = path.read_text()
    except OSError:
        return {}
    found = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if key not in ("url", "token"):
            continue
        if value[:1] in ("'", '"'):
            end = value.find(value[0], 1)
            value = value[1:end] if end > 0 else value[1:]
        else:
            value = value.split("#", 1)[0].strip()
        found[key] = value
    return found


def resolve_connection(url, token, env=None, config_path=None) -> tuple:
    """The server URL and token: flag, then FLEETLENS_URL / FLEETLENS_TOKEN, then the config
    file at ~/.config/fleetlens/config.toml (or under $XDG_CONFIG_HOME)."""
    env = os.environ if env is None else env
    url = url or env.get("FLEETLENS_URL", "").strip() or None
    token = token or env.get("FLEETLENS_TOKEN", "").strip() or None
    if not (url and token):
        path = Path(config_path) if config_path else _config_file(env)
        cfg = _read_config(path)
        if cfg.get("token") and not token:
            try:
                mode = path.stat().st_mode
            except OSError:
                mode = 0
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                print(f"fl: warning: {path} holds a token and is readable by other users; "
                      f"run chmod 600 on it.", file=sys.stderr)
        url = url or cfg.get("url") or None
        token = token or cfg.get("token") or None
    if not (url and token):
        raise ClientError(None,
                          "no server url or token. Supply them one of three ways:\n"
                          "  flags:   --url URL --token TOKEN\n"
                          "  env:     FLEETLENS_URL and FLEETLENS_TOKEN\n"
                          "  file:    url = \"...\" and token = \"...\" in "
                          "~/.config/fleetlens/config.toml")
    return url.rstrip("/"), token


def request(method: str, base_url: str, path: str, token: str, body=None,
            timeout: float = 30) -> dict:
    """One JSON request. Raises ClientError; the token is never put in a message."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base_url.rstrip("/") + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            message = json.loads(exc.read()).get("error") or exc.reason
        except (ValueError, AttributeError):
            message = exc.reason
        raise ClientError(exc.code, str(message)) from None
    except (urllib.error.URLError, OSError) as exc:
        raise ClientError(None, f"cannot reach {base_url}: {getattr(exc, 'reason', exc)}") \
            from None
    try:
        out = json.loads(raw)
    except ValueError:
        raise ClientError(None, f"{base_url} did not return JSON; is it a fleetlens server?") \
            from None
    if not isinstance(out, dict):
        raise ClientError(None, f"unexpected response from {base_url}")
    return out


def newer_server_note(response: dict):
    """One line to print when the server speaks a newer API than this client, else None."""
    version = response.get("api_version")
    if isinstance(version, int) and version > API_VERSION:
        return (f"fl: the server speaks API version {version}, newer than this client's "
                f"{API_VERSION}; upgrade fleetlens to see everything it reports.")
    return None


def default_owner() -> str:
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - no passwd entry in a container, for example
        return "unknown"


# Note there is no duration anywhere below: the server decides how long a pin lives.
def create(base_url, token, alias, reason="", owner=None) -> dict:
    return request("POST", base_url, "/pins", token,
                   {"alias": alias, "reason": reason, "owner": owner or default_owner()})


def list_pins(base_url, token) -> dict:
    return request("GET", base_url, "/pins", token)


def extend(base_url, token, alias, days) -> dict:
    return request("PATCH", base_url, f"/pins/{urllib.parse.quote(alias, safe='')}", token,
                   {"days": days})


def delete(base_url, token, alias) -> dict:
    return request("DELETE", base_url, f"/pins/{urllib.parse.quote(alias, safe='')}", token)


def _day(iso) -> str:
    return str(iso or "")[:10]


def format_pinned(pin: dict) -> str:
    return (f'pinned build {pin.get("build_id")} as "{pin.get("alias")}", '
            f'expires {_day(pin.get("expires_at"))}')


def format_table(pins: list) -> str:
    """A fixed-width table: alias, build id, owner, reason, expires."""
    head = ("ALIAS", "BUILD", "OWNER", "REASON", "EXPIRES")
    rows = [(str(p.get("alias", "")), str(p.get("build_id", "")), str(p.get("owner", "")),
             str(p.get("reason", "")).replace("\n", " ")[:40], _day(p.get("expires_at")))
            for p in pins]
    widths = [max(len(r[i]) for r in [head, *rows]) for i in range(len(head))]
    lines = ["  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip() for r in [head, *rows]]
    return "\n".join(lines)
