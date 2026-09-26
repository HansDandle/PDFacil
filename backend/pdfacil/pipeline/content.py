"""A small reader for page content streams: where each XObject is drawn, and with what CTM.

Images are moved by rewriting the exact ``/Name Do`` that draws them, so the edit keeps the
image bytes and its stacking order. PyMuPDF reports image placements (including ones nested
inside Form XObjects) but not where in the stream they come from; this module tracks the
graphics state (``q`` / ``Q`` / ``cm``) through the page's own content to find each ``Do``
and the transformation in effect there, so placements are matched by geometry, not by
counting names.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pymupdf

_WS = b" \t\r\n\f\x00"
_DELIM = b"()<>[]{}/%"
_NUMBER = re.compile(rb"[+-]?(?:\d+\.?\d*|\.\d+)$")


@dataclass
class DoCall:
    """A drawing command in the page's own content: an XObject ``Do`` or a painted path."""

    name: str  # XObject name for "do"; the paint operator (f, S, B, n...) for "path"
    ctm: pymupdf.Matrix  # maps the object's space to PDF user space
    stream: int  # xref of the content stream holding the command
    start: int  # byte span: "/Name Do", or first path-construction operand .. paint operator
    end: int
    kind: str = "do"  # "do" | "path"
    points: tuple = ()  # path points in the command's own space (for kind "path")
    owner: int | None = None  # xref of the Form XObject it sits in (None: page content)
    shared: bool = False  # inside a form drawn more than once: not editable in place
    target: int | None = None  # xref the Do draws (names are local to each form's resources)


PAINT_OPS = {"f", "F", "f*", "S", "s", "B", "B*", "b", "b*", "n"}
_PATH_OPS = {"m": 2, "l": 2, "c": 6, "v": 4, "y": 4, "re": 4, "h": 0}


def _skip_string(data: bytes, i: int) -> int:
    """i points after '('; return the index after the matching ')'."""
    depth = 1
    n = len(data)
    while i < n and depth:
        c = data[i]
        if c == 0x5C:  # backslash escape
            i += 2
            continue
        if c == 0x28:
            depth += 1
        elif c == 0x29:
            depth -= 1
        i += 1
    return i


def _skip_inline_image(data: bytes, i: int) -> int:
    """i points after the 'ID' operator; skip binary data up to and including 'EI'."""
    match = re.compile(rb"[\s]EI(?=[\s]|$)").search(data, i)
    return match.end() if match else len(data)


def tokens(data: bytes):
    """Yield (kind, value, start, end); kind is 'num', 'name', 'op' or 'other'."""
    i, n = 0, len(data)
    while i < n:
        c = data[i : i + 1]
        if c in (b" ", b"\t", b"\r", b"\n", b"\f", b"\x00"):
            i += 1
        elif c == b"%":
            j = data.find(b"\n", i)
            i = n if j < 0 else j + 1
        elif c == b"(":
            j = _skip_string(data, i + 1)
            yield "other", None, i, j
            i = j
        elif data.startswith(b"<<", i) or data.startswith(b">>", i):
            yield "other", None, i, i + 2
            i += 2
        elif c == b"<":
            j = data.find(b">", i)
            j = n if j < 0 else j + 1
            yield "other", None, i, j
            i = j
        elif c in (b"[", b"]", b"{", b"}"):
            yield "other", None, i, i + 1
            i += 1
        elif c == b"/":
            j = i + 1
            while j < n and data[j : j + 1] not in _WS and data[j : j + 1] not in _DELIM:
                j += 1
            yield "name", data[i + 1 : j].decode("latin-1"), i, j
            i = j
        else:
            j = i
            while j < n and data[j : j + 1] not in _WS and data[j : j + 1] not in _DELIM:
                j += 1
            if j == i:  # stray delimiter
                i += 1
                continue
            word = data[i:j]
            if _NUMBER.match(word):
                yield "num", float(word), i, j
            else:
                yield "op", word.decode("latin-1"), i, j
                if word == b"ID":
                    j = _skip_inline_image(data, j)
            i = j


_XOBJECT_REF = re.compile(r"/([^\s/<>\[\]()]+)\s+(\d+)\s+0\s+R")


def _xobjects(doc: pymupdf.Document, xref: int) -> dict[str, int]:
    """Resource name -> xref for the XObjects of a page or form."""
    kind, value = doc.xref_get_key(xref, "Resources/XObject")
    if kind == "xref":
        value = doc.xref_object(int(value.split()[0]), compressed=True)
    elif kind != "dict":
        return {}
    return {name: int(ref) for name, ref in _XOBJECT_REF.findall(value)}


def _form_matrix(doc: pymupdf.Document, xref: int) -> pymupdf.Matrix:
    kind, value = doc.xref_get_key(xref, "Matrix")
    if kind == "array":
        nums = [float(v) for v in value.strip("[]").split()]
        if len(nums) == 6:
            return pymupdf.Matrix(*nums)
    return pymupdf.Matrix(1, 0, 0, 1, 0, 0)


def _is_form(doc: pymupdf.Document, xref: int) -> bool:
    return doc.xref_get_key(xref, "Subtype")[1] == "/Form"


def page_do_calls(doc: pymupdf.Document, page: pymupdf.Page) -> list[DoCall]:
    """Every ``Do`` and painted path the page draws, in painting order, with the CTM in
    effect: the page's own content streams and, recursively, the Form XObjects they draw
    (Canva groups elements into forms). Calls inside a form that is drawn more than once
    are marked shared (editing them would change every copy). Indices into this list are
    stable under rewrites that wrap a command in ``q ... cm ... Q``."""
    calls: list[DoCall] = []
    uses: dict[int, int] = {}

    def scan(
        stream_xrefs: list[int], ctm: pymupdf.Matrix, resources: dict[str, int], depth: int, owner: int | None
    ):
        stack: list[pymupdf.Matrix] = []
        for xref in stream_xrefs:
            data = doc.xref_stream(xref) or b""
            operands: list = []
            path_start: int | None = None
            points: list[tuple[float, float]] = []
            for kind, value, start, end in tokens(data):
                if kind != "op":
                    operands.append((kind, value, start))
                    continue
                nums = [v for k, v, _ in operands if k == "num"]
                if value in _PATH_OPS:
                    if path_start is None:
                        path_start = operands[0][2] if operands else start
                    if value == "re" and len(nums) >= 4:
                        x, y, w, h = nums[-4:]
                        points += [(x, y), (x + w, y), (x, y + h), (x + w, y + h)]
                    else:
                        points += list(zip(nums[0::2], nums[1::2], strict=False))
                elif value in ("W", "W*"):
                    pass  # clip marker inside a path; the path still ends with a paint op
                elif value in PAINT_OPS:
                    if path_start is not None:
                        call = DoCall(
                            value, pymupdf.Matrix(ctm), xref, path_start, end, "path", tuple(points)
                        )
                        call.owner = owner
                        calls.append(call)
                    path_start, points = None, []
                elif value == "q":
                    stack.append(pymupdf.Matrix(ctm))
                elif value == "Q":
                    ctm = stack.pop() if stack else ctm
                elif value == "cm" and len(nums) >= 6:
                    ctm = pymupdf.Matrix(*nums[-6:]) * ctm  # PDF: CTM' = M x CTM (row vectors)
                elif value == "Do" and operands and operands[-1][0] == "name":
                    _, name, name_start = operands[-1]
                    call = DoCall(name, pymupdf.Matrix(ctm), xref, name_start, end)
                    call.owner = owner
                    target = resources.get(name)
                    call.target = target
                    calls.append(call)
                    if target is not None and depth < 12 and _is_form(doc, target):
                        uses[target] = uses.get(target, 0) + 1
                        inner = _xobjects(doc, target) or resources
                        scan([target], _form_matrix(doc, target) * ctm, inner, depth + 1, target)
                operands = []

    scan(page.get_contents(), pymupdf.Matrix(1, 0, 0, 1, 0, 0), _xobjects(doc, page.xref), 0, None)
    for call in calls:
        call.shared = call.owner is not None and uses.get(call.owner, 0) > 1
    return calls


def path_rect(call: DoCall, page: pymupdf.Page) -> pymupdf.Rect | None:
    """Page-space bounding box of a painted path's points (control points included)."""
    if not call.points:
        return None
    m = call.ctm * page.transformation_matrix
    pts = [pymupdf.Point(x, y) * m for x, y in call.points]
    return pymupdf.Rect(
        min(p.x for p in pts), min(p.y for p in pts), max(p.x for p in pts), max(p.y for p in pts)
    )


def match_path(
    calls: list[DoCall], page: pymupdf.Page, rect, after: int, curved: bool, tol: float = 1.0
) -> int | None:
    """Index of the painted path that draws a get_drawings() entry. Both lists are in painting
    order, so the search starts after the previous match (``after``)."""
    target = pymupdf.Rect(rect)
    # Bezier control points can lie outside the curve that get_drawings() measures.
    slack = tol + (0.25 * max(target.width, target.height) if curved else 0)
    for i in range(after + 1, len(calls)):
        call = calls[i]
        if call.kind != "path" or call.name == "n":
            continue
        r = path_rect(call, page)
        if r is None:
            continue
        inside = all(abs(a - b) <= tol for a, b in zip(r, target, strict=True))
        if inside or (
            curved and r.contains(target) and all(abs(a - b) <= slack for a, b in zip(r, target, strict=True))
        ):
            return i
    return None


def placement_rect(call: DoCall, page: pymupdf.Page) -> pymupdf.Rect:
    """Page-space (top-left origin) rectangle an image drawn by this Do covers."""
    return pymupdf.Rect(0, 0, 1, 1) * (call.ctm * page.transformation_matrix)


def match_placement(calls: list[DoCall], page: pymupdf.Page, xref: int, bbox, tol: float = 1.0) -> int | None:
    """Index of the Do that draws image ``xref`` at ``bbox`` (page content or a single-use form)."""
    target = pymupdf.Rect(bbox)
    for i, call in enumerate(calls):
        if call.kind != "do" or call.target != xref or call.shared:
            continue
        r = placement_rect(call, page)
        if all(abs(a - b) <= tol for a, b in zip(r, target, strict=True)):
            return i
    return None
