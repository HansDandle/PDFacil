"""Direct PDF edit sessions."""

from __future__ import annotations

import json
from pathlib import PurePath

from fastapi import APIRouter, Body, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response

from ..pipeline.edits import OpError
from ..pipeline.fonts import read_font_names
from ..pipeline.sanitize import PdfRejected
from .services import LoadedSession, NotFound, Services

router = APIRouter(prefix="/sessions", tags=["sessions"])

MAX_FONT_BYTES = 20 * 1024 * 1024


def services(request: Request) -> Services:
    return request.app.state.services


def current_owner(request: Request) -> str | None:
    return None  # replaced by the logged-in user once auth lands (hosting milestone)


def _load(request: Request, session_id: str) -> LoadedSession:
    try:
        return services(request).load(session_id, current_owner(request))
    except NotFound as exc:
        raise HTTPException(404, "session not found") from exc


async def _read_limited(upload: UploadFile, limit: int) -> bytes:
    chunks, size = [], 0
    while chunk := await upload.read(1024 * 1024):
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, f"file is larger than {limit // (1024 * 1024)} MB")
        chunks.append(chunk)
    return b"".join(chunks)


def _warnings_header(result) -> dict[str, str]:
    return {"X-PDFacil-Warnings": json.dumps({"warnings": result.warnings, "notices": result.notices})}


@router.post("")
async def create_session(request: Request, file: UploadFile = File(...)):
    svc = services(request)
    data = await _read_limited(file, svc.settings.max_pdf_bytes)
    try:
        row = svc.create_session(data, PurePath(file.filename or "upload.pdf").name, current_owner(request))
    except PdfRejected as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "id": row.id,
        "filename": row.filename,
        "pageCount": row.page_count,
        "isCanva": row.is_canva,
        "notice": None if row.is_canva else "Not a Canva PDF: it opens, but edit quality is not guaranteed.",
    }


@router.get("/{session_id}/pages/{n}")
def get_page(request: Request, session_id: str, n: int):
    s = _load(request, session_id)
    try:
        return services(request).page_json(s, n)
    except NotFound as exc:
        raise HTTPException(404, "page not found") from exc


@router.get("/{session_id}/pages/{n}/bg.png")
def get_background(request: Request, session_id: str, n: int, scale: float = 2.0):
    s = _load(request, session_id)
    if not 0 <= n < s.row.page_count:
        raise HTTPException(404, "page not found")
    png = services(request).editor(s).background_png(n, max(0.25, min(scale, 4.0)))
    return Response(png, media_type="image/png")


@router.get("/{session_id}/fonts")
def get_fonts(request: Request, session_id: str):
    s = _load(request, session_id)
    return {"fonts": [f.to_json() for f in services(request).font_report(s).values()]}


@router.post("/{session_id}/fonts/map")
async def map_font(
    request: Request,
    session_id: str,
    name: str = Form(...),
    target: str | None = Form(None),
    file: UploadFile | None = File(None),
):
    """Point an unmapped font name at a known font, or at an uploaded font file."""
    svc = services(request)
    s = _load(request, session_id)
    owner = s.row.owner
    if file is not None:
        data = await _read_limited(file, MAX_FONT_BYTES)
        try:
            read_font_names(data)
        except Exception as exc:
            raise HTTPException(400, "not a TrueType or OpenType font file") from exc
        font = svc.fonts.register_file(data, owner=owner, source="upload")
    elif target:
        font = svc.fonts.resolve(target, owner)
        if font is None:
            raise HTTPException(404, f"font {target!r} not found")
    else:
        raise HTTPException(400, "send either target or file")
    if font.postscript_name != name:
        svc.fonts.add_alias(name, font, owner)
    svc.fonts_changed(s)
    return {"name": name, "mappedTo": font.postscript_name, "family": font.family}


def _ops(request: Request, s: LoadedSession, body: dict | None) -> list[dict]:
    if body and "ops" in body:
        ops = body["ops"]
        if not isinstance(ops, list):
            raise HTTPException(400, "ops must be a list")
        services(request).save_ops(s, ops)
        return ops
    return s.row.ops or []


@router.put("/{session_id}/ops")
def put_ops(request: Request, session_id: str, body: dict = Body(...)):
    s = _load(request, session_id)
    ops = _ops(request, s, body)
    try:
        _, result = services(request).apply(s, ops)
    except OpError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"count": len(ops), "warnings": result.warnings, "notices": result.notices}


@router.post("/{session_id}/check")
def check(request: Request, session_id: str, body: dict | None = Body(None)):
    """Warnings (font substitutions, overflow) for the ops, without rendering anything."""
    s = _load(request, session_id)
    try:
        _, result = services(request).apply(s, _ops(request, s, body))
    except OpError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"warnings": result.warnings, "notices": result.notices}


@router.post("/{session_id}/preview")
def preview(request: Request, session_id: str, body: dict | None = Body(None)):
    s = _load(request, session_id)
    page = int((body or {}).get("page", 0))
    scale = float((body or {}).get("scale", 2.0))
    if not 0 <= page < s.row.page_count:
        raise HTTPException(404, "page not found")
    try:
        editor, result = services(request).apply(s, _ops(request, s, body))
    except OpError as exc:
        raise HTTPException(400, str(exc)) from exc
    png = editor.render_png(page, max(0.25, min(scale, 4.0)))
    return Response(png, media_type="image/png", headers=_warnings_header(result))


@router.post("/{session_id}/export")
def export(request: Request, session_id: str, body: dict | None = Body(None)):
    svc = services(request)
    s = _load(request, session_id)
    try:
        editor, result = svc.apply(s, _ops(request, s, body))
    except OpError as exc:
        raise HTTPException(400, str(exc)) from exc
    pdf = editor.export()
    name = f"{PurePath(s.row.filename).stem}-edited.pdf"
    svc.storage.put(f"sessions/{s.row.id}/exports/{name}", pdf)
    headers = {"Content-Disposition": f'attachment; filename="{name}"', **_warnings_header(result)}
    return Response(pdf, media_type="application/pdf", headers=headers)
