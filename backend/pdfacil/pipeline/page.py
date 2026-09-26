"""Assembles stages 1-3 into the page JSON served to the editor."""

from __future__ import annotations

import pymupdf

from .extract import PageExtraction, extract_page
from .fonts import DocFont, font_key, worst_status
from .paragraphs import block_to_element, build_blocks, locked_line_element


def _overlaps(a, b, tol: float = 0.5) -> bool:
    return a[0] < b[2] + tol and b[0] < a[2] + tol and a[1] < b[3] + tol and b[1] < a[3] + tol


def _close(a, b, tol: float = 1.0) -> bool:
    return all(abs(x - y) <= tol for x, y in zip(a, b, strict=True))


def _paint_index(log, kinds: tuple[str, ...], match) -> int:
    for i, (kind, rect) in enumerate(log):
        if kind in kinds and match(rect):
            return i
    return len(log)


def build_page_elements(ex: PageExtraction, fonts: dict[str, DocFont]) -> list[dict]:
    """Every element on the page, in z-order (array order is paint order)."""
    n = ex.index
    keyed: list[tuple[int, dict]] = []

    text_kinds = ("fill-text", "stroke-text", "ignore-text")
    containers = [g.bbox for g in ex.drawings if not g.background and not g.outlined_text]
    markers = [
        b for b in containers
        if 0 < b[2] - b[0] <= 24 and 0 < b[3] - b[1] <= 24
        and 0.5 <= (b[2] - b[0]) / (b[3] - b[1]) <= 2
    ]  # fmt: skip
    for i, block in enumerate(build_blocks(ex.lines)):
        el = block_to_element(block, f"p{n}-t{i}", ex.width, containers, markers)
        keyed.append((_text_z(ex, el, text_kinds), el))
    rotated = [ln for ln in ex.lines if not ln.horizontal and ln.text.strip()]
    for i, line in enumerate(rotated):
        el = locked_line_element(line, f"p{n}-r{i}")
        keyed.append((_text_z(ex, el, text_kinds), el))

    by_key = {font_key(name): f for name, f in fonts.items()}
    for el in (e for _, e in keyed):
        statuses = [by_key[k].status for r in el["runs"] if (k := font_key(r["font"])) in by_key]
        el["fontStatus"] = worst_status(statuses)

    for i, img in enumerate(ex.images):
        el = {
            "id": f"p{n}-i{i}",
            "type": "image",
            "bbox": list(img.bbox),
            "xref": img.xref,
            "locked": img.in_form,
            "source": {"name": img.name, "occurrence": img.occurrence, "matrix": list(img.matrix)},
        }
        if img.in_form:
            el["lockedReason"] = "nested-image"
        z = _paint_index(ex.paint_log, ("fill-image",), lambda r, b=img.bbox: _close(r, b))
        keyed.append((z, el))

    for i, group in enumerate(ex.drawings):
        el = {
            "id": f"p{n}-s{i}",
            "type": "shape",
            "bbox": list(group.bbox),
            "fill": group.fill,
            "stroke": group.stroke,
            "locked": group.background or group.outlined_text,
            "source": {"seqnos": [p["seqno"] for p in group.paths]},
        }
        if group.background:
            el["role"] = "background"
            el["lockedReason"] = "page-background"
        if group.outlined_text:
            el["outlinedText"] = True
            el["lockedReason"] = "outlined-text"
        keyed.append((group.seqno, el))

    keyed.sort(key=lambda k: k[0])
    return [el for _, el in keyed]


def _text_z(ex: PageExtraction, el: dict, kinds) -> int:
    rects = el["source"]["spanRects"]
    return _paint_index(ex.paint_log, kinds, lambda r: any(_overlaps(r, s) for s in rects))


def build_page(page: pymupdf.Page, fonts: dict[str, DocFont], background_url: str) -> dict:
    ex = extract_page(page)
    return {
        "page": page.number,
        "width": round(ex.width, 3),
        "height": round(ex.height, 3),
        "background": background_url,
        "elements": build_page_elements(ex, fonts),
    }
