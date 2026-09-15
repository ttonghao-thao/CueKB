from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
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

    @application.exception_handler(PermissionError)
    async def permission_error(_: Request, exc: PermissionError) -> JSONResponse:
        return JSONResponse(status_code=403, content={"detail": str(exc)})

    @application.exception_handler(SQLAlchemyError)
    async def database_unavailable(_: Request, __: SQLAlchemyError) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": "database_unavailable"})

    @application.exception_handler(OpenSearchException)
    async def search_unavailable(_: Request, __: OpenSearchException) -> JSONResponse:
        return JSONResponse(status_code=503, content={"detail": "search_backend_unavailable"})

    return application


app = create_app()
