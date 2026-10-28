"""App factory. Serves /api/*; the static SPA mount is added with the UI slice."""

from fastapi import FastAPI

from . import errors
from .routes import bulk, certificates, export, meta


def create_app() -> FastAPI:
    app = FastAPI(title="Certificates admin")
    errors.install(app)
    app.include_router(meta.router)
    # Before certificates: "/{cert_id}" would otherwise claim "/export.csv".
    app.include_router(export.router)
    app.include_router(certificates.router)
    app.include_router(bulk.router)
    return app


app = create_app()
