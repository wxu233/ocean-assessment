"""App factory. Serves /api/* and the built SPA (frontend/dist) with an index.html fallback."""

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from . import errors
from .errors import ApiError, not_found
from .routes import bulk, certificates, export, meta

DIST_DIR = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def _install_spa(app: FastAPI) -> None:
    """Registered last so every /api route wins. Unknown /api/* paths stay JSON 404s;
    any other path is a built file if one exists, else index.html (client-side view)."""

    @app.api_route("/api/{rest:path}", include_in_schema=False,
                   methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
    def api_not_found(rest: str):
        raise not_found()

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa(full_path: str):
        if full_path == "api":
            raise not_found()
        index = DIST_DIR / "index.html"
        if not index.is_file():
            raise ApiError(404, "ui_not_built",
                           "Frontend not built. Run ./start.sh (or: cd frontend && npm run build).")
        candidate = (DIST_DIR / full_path).resolve()
        if full_path and candidate.is_file() and candidate.is_relative_to(DIST_DIR):
            return FileResponse(candidate)
        return FileResponse(index)


def create_app() -> FastAPI:
    app = FastAPI(title="Certificates admin")
    errors.install(app)
    app.include_router(meta.router)
    # Before certificates: "/{cert_id}" would otherwise claim "/export.csv".
    app.include_router(export.router)
    app.include_router(certificates.router)
    app.include_router(bulk.router)
    _install_spa(app)
    return app


app = create_app()
