"""V2 search deadline around the complete downstream ASGI call."""
import asyncio
from time import perf_counter
from uuid import uuid4

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from cuekb.api.errors import ErrorResponse
from cuekb.config import get_settings
from cuekb.services.execution import (
    SearchDeadlineExceeded,
    SearchExecution,
    activate_execution,
)


class V2DeadlineMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope["path"] not in {
            "/v2/search", "/v2/evidence/query"
        }:
            await self.app(scope, receive, send)
            return

        state = scope.setdefault("state", {})
        trace_id = state.setdefault("trace_id", uuid4())
        execution = SearchExecution.create(get_settings().search_deadline_ms, trace_id)
        messages: list[Message] = []

        async def capture(message: Message) -> None:
            # Search is a bounded, non-streaming response. Hold its headers and
            # body until all downstream work and the final deadline check finish,
            # so an expired request cannot commit a partial or successful result.
            messages.append(message)

        # SearchExecution uses perf_counter; event-loop clocks (e.g. uvloop)
        # need not share its epoch. Transfer a duration, never the raw timestamp.
        timeout = asyncio.timeout(max(0.0, execution.deadline - perf_counter()))
        try:
            with activate_execution(execution):
                async with timeout:
                    # Unlike BaseHTTPMiddleware.call_next(), this awaits the
                    # actual dependency/endpoint task, so expiry cancels both.
                    await self.app(scope, receive, capture)
                    execution.checkpoint()
        except TimeoutError as exc:
            if not isinstance(exc, SearchDeadlineExceeded) and not timeout.expired():
                raise
            body = ErrorResponse(
                detail="search_deadline_exceeded", error_code="SEARCH_DEADLINE_EXCEEDED",
                trace_id=trace_id, request_id=state.get("request_id"),
            )
            response = JSONResponse(status_code=504, content=body.model_dump(mode="json"),
                headers={"X-Trace-ID": str(trace_id), "Cache-Control": "no-store"})
            await response(scope, receive, send)
        else:
            for message in messages:
                await send(message)
