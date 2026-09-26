"""ASGI entry point: ``uvicorn pdfacil.main:app``."""

from .api.app import create_app

app = create_app()
