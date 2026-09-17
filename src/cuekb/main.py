from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from opensearchpy.exceptions import OpenSearchException
from sqlalchemy.exc import SQLAlchemyError

from cuekb import __version__
from cuekb.api.routes import router


def create_app() -> FastAPI:
    application = FastAPI(
        title="CueKB API",
        version=__version__,
        description="Cue-driven evidence retrieval service",
    )
    application.include_router(router)
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
        response = await call_next(request)
        if request.url.path == "/" or request.url.path.startswith("/portal"):
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
                "form-action 'self'; frame-ancestors 'none'"
            )
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @application.exception_handler(PermissionError)
    async def permission_error(_: Request, exc: PermissionError) -> JSONResponse:
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @application.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> Response:
        if request.url.path != "/v1/model-configuration":
            return await request_validation_exception_handler(request, exc)
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {"loc": item["loc"], "msg": item["msg"], "type": item["type"]}
                    for item in exc.errors()
                ]
            },
        )

    @application.exception_handler(SQLAlchemyError)
    async def database_unavailable(_: Request, __: SQLAlchemyError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": "database_unavailable"})

    @application.exception_handler(OpenSearchException)
    async def search_unavailable(_: Request, __: OpenSearchException) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": "search_backend_unavailable"})

    return application


app = create_app()
