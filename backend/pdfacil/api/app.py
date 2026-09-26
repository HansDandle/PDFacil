from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse

from ..config import Settings, get_settings
from ..pipeline.fonts import GoogleFonts
from . import sessions
from .services import Services

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


def create_app(settings: Settings | None = None, google: GoogleFonts | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(title="PDFacil")
    app.state.services = Services(settings, google)
    app.include_router(sessions.router)

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/fonts/catalog")
    def font_catalog():
        """Open Google Fonts families (most popular first) for the font picker."""
        google = app.state.services.fonts.google
        entries = sorted(google.catalog().values(), key=lambda e: e.popularity) if google else []
        return {
            "families": [
                {"family": e.family, "category": e.category, "styles": sorted(e.styles)} for e in entries
            ]
        }

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/source")
    def source():
        """AGPL: link to the source code of this deployment."""
        return {"url": settings.source_url, "license": "AGPL-3.0-or-later"}

    return app
