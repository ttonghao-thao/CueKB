from pathlib import Path
import asyncio
from contextlib import asynccontextmanager
import logging
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.openapi.utils import get_openapi
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from opensearchpy.exceptions import OpenSearchException
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException as StarletteHTTPException

from cuekb import __version__
from cuekb.adapters.generations import MaintenanceBusy
from cuekb.api.routes import router
from cuekb.api.errors import ErrorResponse, ServiceError
from cuekb.api.v2 import router as v2_router
from cuekb.api.v2_dependencies import V2RuntimePool
from cuekb.config import get_settings
from cuekb.services.execution import (
    SearchConfigurationChanged, SearchDeadlineExceeded, SearchExecution, activate_execution,
)

logger = logging.getLogger(__name__)


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        app.state.v2_runtime_pool = V2RuntimePool(get_settings().v2_max_inflight)
        try:
            yield
        finally:
            await app.state.v2_runtime_pool.close()

    application = FastAPI(
        title="CueKB API",
        version=__version__,
        description="Cue-driven evidence retrieval service",
        lifespan=lifespan,
    )
    application.include_router(router)
    application.include_router(v2_router)

    def public_openapi():
        if application.openapi_schema is None:
            schema = get_openapi(title=application.title, version=application.version,
                                 description=application.description, routes=application.routes)
            schema.setdefault("components", {}).setdefault("securitySchemes", {})["ApiKeyBearer"] = {
                "type": "http", "scheme": "bearer", "bearerFormat": "CueKB API key",
            }
            for path, methods in schema["paths"].items():
                for operation in methods.values():
                    if isinstance(operation, dict) and "responses" in operation:
                        if path not in {"/v1/health", "/v1/ready"}:
                            operation["security"] = [{"ApiKeyBearer": []}]
                        for response in operation["responses"].values():
                            headers = response.setdefault("headers", {})
                            headers["X-Trace-ID"] = {
                                "description": "Server trace; matches trace_id when present in the body.",
                                "schema": {"type": "string", "format": "uuid"},
                            }
                            if path.startswith("/v2/"):
                                headers["Cache-Control"] = {
                                    "description": "Evidence and processing responses are not cacheable.",
                                    "schema": {"type": "string", "const": "no-store"},
                                }
            application.openapi_schema = schema
        return application.openapi_schema

    application.openapi = public_openapi
    portal_dir = Path(__file__).with_name("portal")
    application.mount("/portal/assets", StaticFiles(directory=portal_dir), name="portal-assets")

    @application.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse("/portal/", status_code=307)

    @application.get("/portal/", include_in_schema=False)
    def portal() -> FileResponse:
        return FileResponse(portal_dir / "index.html")

    @application.middleware("http")
    async def portal_security_headers(request: Request, call_next):
        started = perf_counter()
        request.state.trace_id = uuid4()
        request.state.request_id = None
        if request.url.path == "/v2/search":
            execution = SearchExecution.create(get_settings().search_deadline_ms, request.state.trace_id)
            try:
                with activate_execution(execution):
                    async with asyncio.timeout(execution.remaining_ms() / 1000):
                        response = await call_next(request)
            except (SearchDeadlineExceeded, TimeoutError):
                response = error(request, 504, "search_deadline_exceeded", "SEARCH_DEADLINE_EXCEEDED")
        else:
            response = await call_next(request)
        response.headers["X-Trace-ID"] = str(request.state.trace_id)
        if request.url.path.startswith("/v2/"):
            response.headers["Cache-Control"] = "no-store"
            logger.info("api_request_finished", extra={
                "trace_id": str(request.state.trace_id), "status_code": response.status_code,
                "duration_ms": (perf_counter() - started) * 1000,
                "response_bytes": response.headers.get("content-length"),
            })
        if request.url.path == "/" or request.url.path.startswith("/portal"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
                "form-action 'self'; frame-ancestors 'none'"
            )
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def error(request, status, detail, code):
        body = ErrorResponse(
            detail=detail, error_code=code, trace_id=getattr(request.state, "trace_id", uuid4()),
            request_id=getattr(request.state, "request_id", None),
        )
        headers = {"X-Trace-ID": str(body.trace_id)}
        if request.url.path.startswith("/v2/"):
            headers["Cache-Control"] = "no-store"
        return JSONResponse(status_code=status, content=body.model_dump(mode="json"), headers=headers)

    @application.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        codes = {
            400: "BAD_REQUEST", 401: "INVALID_API_KEY", 403: "ACCESS_DENIED", 404: "NOT_FOUND",
            405: "METHOD_NOT_ALLOWED", 409: "CONFLICT", 413: "FILE_TOO_LARGE",
            415: "UNSUPPORTED_FILE_TYPE", 422: "INVALID_REQUEST", 503: "DEPENDENCY_UNAVAILABLE",
        }
        detail = exc.detail if isinstance(exc.detail, (str, list)) else "request_failed"
        response = error(request, exc.status_code, detail, codes.get(exc.status_code, "REQUEST_FAILED"))
        if exc.headers:
            response.headers.update(exc.headers)
        return response

    @application.exception_handler(ServiceError)
    async def service_error(request: Request, exc: ServiceError) -> JSONResponse:
        return error(request, exc.status_code, exc.detail, exc.error_code)

    @application.exception_handler(SearchDeadlineExceeded)
    async def deadline(request: Request, _: SearchDeadlineExceeded) -> JSONResponse:
        return error(request, 504, "search_deadline_exceeded", "SEARCH_DEADLINE_EXCEEDED")

    @application.exception_handler(SearchConfigurationChanged)
    async def binding_changed(request: Request, _: SearchConfigurationChanged) -> JSONResponse:
        return error(request, 409, "search_configuration_changed", "SEARCH_CONFIGURATION_CHANGED")

    @application.exception_handler(MaintenanceBusy)
    async def maintenance_busy(request: Request, exc: MaintenanceBusy) -> JSONResponse:
        return error(request, 409, str(exc), "INDEX_MAINTENANCE_IN_PROGRESS")

    @application.exception_handler(PermissionError)
    async def permission_error(request: Request, exc: PermissionError) -> JSONResponse:
        return error(request, 403, str(exc), "ACCESS_DENIED")

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> Response:
        detail = [{"loc": list(item["loc"]), "msg": item["msg"], "type": item["type"]} for item in exc.errors()]
        return error(request, 422, detail, "INVALID_REQUEST")

    @application.exception_handler(SQLAlchemyError)
    async def database_unavailable(request: Request, _: SQLAlchemyError) -> JSONResponse:
        return error(request, 503, "database_unavailable", "DATABASE_UNAVAILABLE")

    @application.exception_handler(OpenSearchException)
    async def search_unavailable(request: Request, _: OpenSearchException) -> JSONResponse:
        return error(request, 503, "search_backend_unavailable", "SEARCH_BACKEND_UNAVAILABLE")

    @application.exception_handler(Exception)
    async def unexpected(request: Request, exc: Exception) -> JSONResponse:
        logger.error("api_unhandled_error", extra={"trace_id": str(getattr(request.state, "trace_id", "")), "error_type": type(exc).__name__})
        return error(request, 500, "internal_error", "INTERNAL_ERROR")

    return application


app = create_app()
