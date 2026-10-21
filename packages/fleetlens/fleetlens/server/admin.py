"""Admin routes for pinned snapshots, served on the same port and token as the MCP endpoint.

These are plain HTTP routes and deliberately not MCP tools. Pinning is an operator action
taken by an engineer or a harness script, not something an agent should decide to do in the
middle of answering a question, and keeping it out of the tool list keeps it out of the
model's context.

They sit behind the existing bearer middleware because they are appended to the same app;
nothing here adds to PUBLIC_PATHS and nothing here checks credentials itself. The `owner`
field is a label the client chooses, trusted as such: with one shared token there is no
identity to verify it against.
"""
from __future__ import annotations

from ..pins import PinError, PinStore

#: Bumped when a response changes shape in a way an older client could misread. Clients
#: compare it and say so when they are older than the server.
API_VERSION = 1


def pin_routes(db_path: str, pin_store: PinStore) -> list:
    """The /pins routes, closed over the live index path and the pin store."""
    from starlette.concurrency import run_in_threadpool
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    def reply(body: dict, status: int = 200):
        return JSONResponse({"api_version": API_VERSION, **body}, status_code=status)

    def fail(exc: PinError):
        return reply({"error": exc.message}, exc.status)

    async def body_of(request) -> dict:
        try:
            body = await request.json()
        except ValueError:
            raise PinError(400, "request body must be JSON") from None
        if not isinstance(body, dict):
            raise PinError(400, "request body must be a JSON object")
        return body   # unknown fields are ignored, so an older server accepts a newer client

    async def list_pins(_request):
        # Collect first so a listing never shows a pin that has already expired.
        await run_in_threadpool(pin_store.collect)
        pins = await run_in_threadpool(pin_store.list)
        policy = pin_store.policy
        return reply({"pins": pins, "retention": {
            "retain_days": policy.retain_days, "extend_max_days": policy.extend_max_days}})

    async def create_pin(request):
        try:
            body = await body_of(request)
            # Any duration in the body is ignored on purpose: expiry is the server's call.
            pin = await run_in_threadpool(
                pin_store.create, db_path, body.get("alias"),
                str(body.get("reason") or ""), str(body.get("owner") or ""))
        except PinError as exc:
            return fail(exc)
        except OSError as exc:
            # Typically a pins directory on a read-only or full volume. Say so, rather
            # than returning a bare 500 the operator has to dig the cause out of.
            return fail(PinError(500, f"could not write the pin: {exc.strerror or exc}"))
        return reply(pin, 201)

    async def extend_pin(request):
        try:
            body = await body_of(request)
            pin = await run_in_threadpool(
                pin_store.extend, request.path_params["alias"], body.get("days"))
        except PinError as exc:
            return fail(exc)
        return reply(pin)

    async def delete_pin(request):
        alias = request.path_params["alias"]
        try:
            await run_in_threadpool(pin_store.delete, alias)
        except PinError as exc:
            return fail(exc)
        return reply({"deleted": alias})

    return [
        Route("/pins", list_pins, methods=["GET"]),
        Route("/pins", create_pin, methods=["POST"]),
        Route("/pins/{alias}", extend_pin, methods=["PATCH"]),
        Route("/pins/{alias}", delete_pin, methods=["DELETE"]),
    ]
