"""Validation and cleanup for uploaded PDFs (untrusted input)."""

from __future__ import annotations

from dataclasses import dataclass

import pymupdf


class PdfRejected(ValueError):
    """The upload is not acceptable; the message is safe to show to the user."""


@dataclass
class SanitizedPdf:
    data: bytes
    page_count: int
    is_canva: bool


def _action_text(doc: pymupdf.Document, kind: str, value: str) -> str:
    if kind == "xref":
        return doc.xref_object(int(value.split()[0]), compressed=True)
    return value


def _strip_catalog_actions(doc: pymupdf.Document) -> None:
    """scrub() leaves document-level actions alone: drop JavaScript and Launch ones."""
    catalog = doc.pdf_catalog()
    for key in ("OpenAction", "AA"):
        kind, value = doc.xref_get_key(catalog, key)
        if kind != "null" and any(
            s in _action_text(doc, kind, value) for s in ("/JavaScript", "/JS", "/Launch")
        ):
            doc.xref_set_key(catalog, key, "null")
    kind, value = doc.xref_get_key(catalog, "Names/JavaScript")
    if kind != "null":
        doc.xref_set_key(catalog, "Names/JavaScript", "null")


def sanitize_pdf(data: bytes, max_bytes: int, max_pages: int) -> SanitizedPdf:
    if len(data) > max_bytes:
        raise PdfRejected(f"PDF is larger than {max_bytes // (1024 * 1024)} MB")
    if b"%PDF-" not in data[:1024]:
        raise PdfRejected("File is not a PDF")
    try:
        doc = pymupdf.open(stream=data, filetype="pdf")
    except Exception as exc:
        raise PdfRejected("PDF is damaged and could not be opened") from exc
    if doc.needs_pass or doc.is_encrypted:
        raise PdfRejected("PDF is password-protected")
    if doc.page_count == 0:
        raise PdfRejected("PDF has no pages")
    if doc.page_count > max_pages:
        raise PdfRejected(f"PDF has more than {max_pages} pages")

    doc.scrub(
        attached_files=True,
        clean_pages=False,
        embedded_files=True,
        hidden_text=False,
        javascript=True,
        metadata=False,
        redactions=False,
        remove_links=False,
        reset_fields=False,
        reset_responses=False,
        thumbnails=True,
        xml_metadata=False,
    )
    _strip_catalog_actions(doc)
    for page in doc:
        for link in page.get_links():
            if link.get("kind") == pymupdf.LINK_LAUNCH:
                page.delete_link(link)

    meta = doc.metadata or {}
    is_canva = "canva" in f"{meta.get('producer', '')} {meta.get('creator', '')}".lower()
    return SanitizedPdf(doc.tobytes(garbage=1), doc.page_count, is_canva)
