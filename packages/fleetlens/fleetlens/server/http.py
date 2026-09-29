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
"""
from __future__ import annotations

import logging
import os
import secrets

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


def serve(mcp, host: str, port: int, token: str, store) -> None:
    """Run the FastMCP streamable-HTTP app under uvicorn."""
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
    # Order matters: middleware added last runs first, so reject unauthenticated callers
    # before doing any work on their behalf.
    app.add_middleware(build_reload_middleware(store))
    if token:
        app.add_middleware(build_auth_middleware(token))

    log.info("serving MCP (streamable-http) on %s:%s%s", host, port,
             "" if token else "  [NO AUTH]")
    uvicorn.run(app, host=host, port=port)
