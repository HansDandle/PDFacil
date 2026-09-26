"""Shared services for the API: database, storage, fonts and edit sessions."""

from __future__ import annotations

import secrets
from collections import OrderedDict
from dataclasses import dataclass

import pymupdf
from sqlalchemy.orm import sessionmaker

from ..config import Settings
from ..db import EditSession, make_engine, make_session_factory
from ..pipeline.edits import ApplyResult, DocumentEditor
from ..pipeline.fonts import DocFont, FontRegistry, GoogleFonts, font_report
from ..pipeline.page import build_page
from ..pipeline.sanitize import sanitize_pdf
from ..storage import LocalStorage, Storage


class NotFound(LookupError):
    pass


@dataclass
class LoadedSession:
    row: EditSession
    pdf: bytes
    doc: pymupdf.Document
    fonts: dict[str, DocFont] | None = None


class Services:
    def __init__(self, settings: Settings, google: GoogleFonts | None = None):
        self.settings = settings
        self.db: sessionmaker = make_session_factory(make_engine(settings.database_url))
        if settings.storage_backend != "local":
            raise NotImplementedError("only local storage is available until the R2 backend lands")
        self.storage: Storage = LocalStorage(settings.storage_dir)
        if google is None and settings.google_fonts_enabled:
            google = GoogleFonts()
        self.fonts = FontRegistry(self.db, self.storage, google)
        self._loaded: OrderedDict[str, LoadedSession] = OrderedDict()

    # --- sessions ---------------------------------------------------------------------------

    def create_session(self, data: bytes, filename: str, owner: str | None) -> EditSession:
        clean = sanitize_pdf(data, self.settings.max_pdf_bytes, self.settings.max_pdf_pages)
        session_id = secrets.token_hex(8)
        key = f"sessions/{session_id}/original.pdf"
        self.storage.put(key, clean.data)
        row = EditSession(
            id=session_id,
            owner=owner,
            filename=filename,
            original_key=key,
            page_count=clean.page_count,
            is_canva=clean.is_canva,
            ops=[],
        )
        with self.db() as db:
            db.add(row)
            db.commit()
        return row

    def load(self, session_id: str, owner: str | None) -> LoadedSession:
        cached = self._loaded.get(session_id)
        if cached is None:
            with self.db() as db:
                row = db.get(EditSession, session_id)
            if row is None:
                raise NotFound(session_id)
            pdf = self.storage.get(row.original_key)
            cached = LoadedSession(row, pdf, pymupdf.open(stream=pdf, filetype="pdf"))
            self._loaded[session_id] = cached
            while len(self._loaded) > 16:
                self._loaded.popitem(last=False)
        self._loaded.move_to_end(session_id)
        if cached.row.owner != owner:
            raise NotFound(session_id)  # never reveal other users' sessions
        return cached

    def font_report(self, s: LoadedSession) -> dict[str, DocFont]:
        if s.fonts is None:
            s.fonts = font_report(s.doc, self.fonts, s.row.owner)
        return s.fonts

    def page_json(self, s: LoadedSession, n: int) -> dict:
        if not 0 <= n < s.doc.page_count:
            raise NotFound(f"page {n}")
        return build_page(s.doc[n], self.font_report(s), f"/sessions/{s.row.id}/pages/{n}/bg.png")

    def save_ops(self, s: LoadedSession, ops: list[dict]) -> None:
        with self.db() as db:
            row = db.get(EditSession, s.row.id)
            row.ops = ops
            db.commit()
            s.row = row

    def editor(self, s: LoadedSession) -> DocumentEditor:
        return DocumentEditor(s.pdf, self.fonts, s.row.owner)

    def apply(self, s: LoadedSession, ops: list[dict]) -> tuple[DocumentEditor, ApplyResult]:
        editor = self.editor(s)
        return editor, editor.apply(ops)

    def fonts_changed(self, s: LoadedSession) -> None:
        s.fonts = None
