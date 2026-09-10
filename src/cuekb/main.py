from fastapi import FastAPI

from cuekb import __version__
from cuekb.api.routes import router


def create_app() -> FastAPI:
    application = FastAPI(
        title="CueKB API",
        version=__version__,
        description="Cue-driven evidence retrieval service",
    )
    application.include_router(router)
    return application


app = create_app()
