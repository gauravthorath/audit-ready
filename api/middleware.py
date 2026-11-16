"""The request-id middleware: where the trace begins on the server (NFR-5, NFR-6).

Every request gets an id - the one the browser sent in `X-Request-ID`, if it
looks like an id, or a fresh one - bound into the trace context for the whole
request, echoed back in the response header, and logged once with method,
path, status and elapsed milliseconds.

*Why a raw ASGI middleware and not `BaseHTTPMiddleware`.* Starlette's
convenience base class runs the downstream app in a separate task and buffers
the response through a queue. That is fine for a JSON route and awkward for
the SSE progress stream, and it has a history of losing context set inside the
endpoint. A plain ASGI callable wraps `send`, sets the context before calling
the app in the *same* task, and gets out of the way. Sync routes run on
anyio's worker threads, which copy the context in - the tests prove it by
finding the request id on a line logged from inside a route.

*Why the access log is replaced rather than added to.* Uvicorn's line has no
id. Two lines per request, one of them useless, is how people stop reading
logs. `core.tracing.configure_logging` disables it; this one line is the
replacement, and it is the line `grep <request_id>` finds first.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from starlette.datastructures import Headers, MutableHeaders

from core.tracing import bind, clean_id, elapsed_ms, log_event, new_id

log = logging.getLogger("auditready.api")

REQUEST_ID_HEADER = "X-Request-ID"

Scope = dict[str, Any]
Message = dict[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = clean_id(Headers(scope=scope).get(REQUEST_ID_HEADER.lower())) or new_id()
        status: dict[str, int] = {}
        started = time.perf_counter()

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
                MutableHeaders(scope=message).append(REQUEST_ID_HEADER, request_id)
            await send(message)

        with bind(request_id=request_id):
            try:
                await self.app(scope, receive, send_with_id)
            except Exception:
                # The framework will turn this into a 500 upstream; log the
                # request as failed here so the line exists even then.
                log_event(log, "http.request", logging.ERROR, method=scope["method"],
                          path=scope["path"], status=status.get("code", 500),
                          elapsed_ms=elapsed_ms(started))
                raise
            log_event(log, "http.request",
                      logging.WARNING if status.get("code", 500) >= 500 else logging.INFO,
                      method=scope["method"], path=scope["path"],
                      status=status.get("code", 0), elapsed_ms=elapsed_ms(started))
