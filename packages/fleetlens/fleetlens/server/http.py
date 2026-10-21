"""Streamable-HTTP transport for the MCP server, with a shared-token auth boundary.

stdio is the right default for one engineer on one laptop: the client owns the process,
and the operating system is the security boundary. A server on a VM has neither, so this
adds the two things that changes: a network transport, and a check that the caller is on
the team.

The check is a single shared bearer token read from the environment. That is deliberately
modest. It is enough to keep an internal index off the open network, and it is the same
mechanism the team already runs for context-service, so there is nothing new to operate.
It does not identify individual engineers and it does not support rotation without a
restart. If you need either, replace `BearerAuth` with your identity provider's middleware;
nothing else in the server knows how the caller was authenticated.

The pin admin routes (/pins, see server/admin.py) are served on the same port and are
behind the same token. Only /healthz is public.
"""
from __future__ import annotations

import logging
import os
import secrets
import threading

log = logging.getLogger(__name__)

#: Endpoints served without a token, so a load balancer can probe the process.
PUBLIC_PATHS = ("/healthz",)


def _unauthorized():
    from starlette.responses import JSONResponse
    return JSONResponse({"error": "unauthorized"}, status_code=401)


def build_auth_middleware(token: str):
    """A Starlette middleware class closed over the expected token."""
    from starlette.middleware.base import BaseHTTPMiddleware

    class BearerAuth(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if request.url.path in PUBLIC_PATHS:
                return await call_next(request)
            header = request.headers.get("authorization", "")
            if not header.startswith("Bearer "):
                return _unauthorized()
            # constant-time, so a wrong token leaks nothing through response timing
            if not secrets.compare_digest(header[7:], token):
                return _unauthorized()
            return await call_next(request)

    return BearerAuth


def resolve_token(token_env: str, allow_insecure: bool) -> str:
    """Read the shared token, or fail loudly.

    Refusing to start without a token is the point. A server that silently comes up open
    is worse than one that does not come up, because nothing downstream will tell you.
    """
    token = os.environ.get(token_env, "").strip()
    if token:
        return token
    if allow_insecure:
        log.warning("serving with NO authentication (--insecure). Anyone who can reach "
                    "this port can read the whole index.")
        return ""
    raise SystemExit(
        f"fl serve --http: no token in ${token_env}.\n"
        f"  Set one:       export {token_env}=$(openssl rand -hex 32)\n"
        f"  Or, for a local trial only, pass --insecure to serve without auth.")


def build_reload_middleware(store):
    """Pick up a refreshed index without a restart.

    The refresh job renames a newly built file over the old one. Checking before each
    request costs a stat() and means operators never have to remember the restart, which
    is the kind of step that gets forgotten exactly once and then silently serves a
    month-old graph.
    """
    from starlette.middleware.base import BaseHTTPMiddleware

    class ReloadIndex(BaseHTTPMiddleware):
        async def dispatch(self, request, call_next):
            if store.reload_if_changed():
                log.info("index changed on disk, reopened")
            return await call_next(request)

    return ReloadIndex


def security_settings(host: str, allowed_hosts: list):
    """DNS-rebinding policy. Off unless the operator asks for it.

    The MCP SDK validates the Host header by default on a loopback bind, and decides that
    when the server object is constructed. That protection exists for a server with no
    authentication: it stops a web page the user happens to visit from reaching a service
    on their own machine, because a browser will send requests but cannot forge a Host.

    Here every request already needs a bearer token, which that page does not have. So the
    Host check guards nothing the token does not already guard, while rejecting ordinary
    clients that reach the server by its address or a DNS name. `--allowed-host` turns it
    back on for anyone who wants defence in depth.

    The one case where it would carry weight is `--insecure`, where there is no token. That
    flag already says in plain terms that anyone who can reach the port can read the whole
    index, which is the more important thing to understand.
    """
    from mcp.server.transport_security import TransportSecuritySettings

    if allowed_hosts:
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=[*allowed_hosts, *(f"{h}:*" for h in allowed_hosts)],
            allowed_origins=[f"{scheme}://{h}" for h in allowed_hosts
                             for scheme in ("http", "https")])
    return TransportSecuritySettings(enable_dns_rebinding_protection=False)


#: How often expired pins are swept. Hourly is plenty: expiry is counted in days.
_COLLECT_INTERVAL_SECONDS = 3600


def start_pin_collector(pins, stop: threading.Event,
                        interval: float = _COLLECT_INTERVAL_SECONDS) -> threading.Thread:
    """Sweep expired pins now and then every `interval` seconds until `stop` is set.

    A daemon thread, so it never holds the process open. It waits on the event rather than
    sleeping so a test can stop it promptly.
    """
    def loop() -> None:
        while True:
            try:
                pins.collect()
            except Exception:  # noqa: BLE001 - a failed sweep must not end the schedule
                log.exception("pin collection failed")
            if stop.wait(interval):
                return

    thread = threading.Thread(target=loop, name="pin-collector", daemon=True)
    thread.start()
    return thread


def serve(mcp, host: str, port: int, token: str, store, db_path: str, pins,
          registry=None) -> None:
    """Run the FastMCP streamable-HTTP app under uvicorn.

    `pins` is a pins.PinPolicy; `db_path` is the live index that pins are snapshots of.
    `registry`, when given, also serves each pin at /pins/<alias>/mcp off the same MCP
    server; see server/routing.py for why one server rather than one per pin."""
    try:
        import uvicorn
    except ImportError:  # pragma: no cover
        raise SystemExit("fl serve --http: uvicorn not installed — "
                         "pip install 'fleetlens[server]'")
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    mcp.settings.host, mcp.settings.port = host, port
    app = mcp.streamable_http_app()

    async def healthz(_request):
        # Unauthenticated on purpose: it reports liveness and index age, never content.
        return JSONResponse({"status": "ok", **store.index_info()})

    app.router.routes.append(Route("/healthz", healthz, methods=["GET"]))

    if registry is not None:
        # The MCP endpoint FastMCP already built, mounted a second time under each pin.
        # Starlette's {alias} never matches across a slash, so this cannot collide with the
        # admin routes below, and an alias containing a slash cannot exist anyway.
        from .routing import alias_scoped
        mcp_endpoint = next(r for r in app.router.routes
                            if getattr(r, "path", None) == mcp.settings.streamable_http_path)
        app.router.routes.append(
            Route("/pins/{alias}/mcp", endpoint=alias_scoped(mcp_endpoint.app, registry)))
    # Not added to PUBLIC_PATHS: the auth middleware below covers these like /mcp.
    from ..pins import PinStore
    from .admin import pin_routes
    pin_store = PinStore(pins, live_db=db_path,
                         # A pin being served must not vanish under an open connection, so
                         # the collector skips whatever the registry currently holds open.
                         in_use=(registry.open_aliases if registry is not None else None))
    app.router.routes.extend(pin_routes(db_path, pin_store))
    if not token:
        log.warning("pin management (/pins) is unauthenticated because --insecure is set")
    start_pin_collector(pin_store, threading.Event())
    # Order matters: middleware added last runs first, so reject unauthenticated callers
    # before doing any work on their behalf.
    app.add_middleware(build_reload_middleware(store))
    if token:
        app.add_middleware(build_auth_middleware(token))

    log.info("serving MCP (streamable-http) on %s:%s%s", host, port,
             "" if token else "  [NO AUTH]")
    uvicorn.run(app, host=host, port=port)
