from __future__ import annotations

from fastapi import FastAPI

from ..config import Settings, get_settings
from ..pipeline.fonts import GoogleFonts
from . import sessions
from .services import Services


def create_app(settings: Settings | None = None, google: GoogleFonts | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="PDFacil")
    app.state.services = Services(settings, google)
    app.include_router(sessions.router)

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/source")
    def source():
        """AGPL: link to the source code of this deployment."""
        return {"url": settings.source_url, "license": "AGPL-3.0-or-later"}

    return app
