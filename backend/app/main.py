"""App factory. Serves /api/*; the static SPA mount is added with the UI slice."""

from fastapi import FastAPI

from . import errors
from .routes import certificates, meta


def create_app() -> FastAPI:
    app = FastAPI(title="Certificates admin")
    errors.install(app)
    app.include_router(meta.router)
    app.include_router(certificates.router)
    return app


app = create_app()
