"""Stages 4 and 5: apply edit operations to a PDF and export it.

Operations are replayed from the original file every time, so the frontend's undo is just
dropping ops and redaction errors never compound. Per element only the final state matters
(deleted, new text/layout, new bbox), so ops are collapsed before anything touches the page.

Known z-order limitations (reported as notices):
- Rewritten text is drawn on top of the page, so text that sat under a shape or image ends up
  above it.
- Moved or resized shapes are redrawn on top of the page.
Images keep their exact stacking position: their placement matrix is rewritten in place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pymupdf

from ..layout import layout_text
from .content import page_do_calls
from .extract import extract_page
from .fallback import SUBSTITUTE, FontPlanner, Renderer
from .fonts import FontRegistry
from .lists import marker_text
from .page import build_page_elements

TEXT_BAND = (0.3, 0.7)  # vertical slice of each span box used for redaction
EDGE_INSET = 0.3


class OpError(ValueError):
    pass


def _rgb(color: str) -> tuple[float, float, float]:
    color = color.lstrip("#")
    return tuple(int(color[i : i + 2], 16) / 255 for i in (0, 2, 4))  # type: ignore[return-value]


def _text_band(rect) -> pymupdf.Rect:
    x0, y0, x1, y1 = rect
    h = y1 - y0
    return pymupdf.Rect(x0 + EDGE_INSET, y0 + TEXT_BAND[0] * h, x1 - EDGE_INSET, y0 + TEXT_BAND[1] * h)


def _move_matrix(old, new) -> pymupdf.Matrix:
    old, new = pymupdf.Rect(old), pymupdf.Rect(new)
    sx = new.width / old.width if old.width else 1
    sy = new.height / old.height if old.height else 1
    return pymupdf.Matrix(sx, 0, 0, sy, new.x0 - old.x0 * sx, new.y0 - old.y0 * sy)


@dataclass
class ElementState:
    deleted: bool = False
    text: dict | None = None
    bbox: list[float] | None = None


@dataclass
class PageModel:
    elements: dict[str, dict]
    drawings: dict[str, list[dict]]  # shape element id -> original paths
    all_paths: list[dict]


@dataclass
class ApplyResult:
    warnings: list[dict] = field(default_factory=list)
    notices: list[dict] = field(default_factory=list)
    boxes: dict[str, list[float]] = field(default_factory=dict)  # where edited text ended up


class DocumentEditor:
    def __init__(self, pdf: bytes, registry: FontRegistry, owner: str | None = None):
        self.original = pdf
        self.registry = registry
        self.owner = owner
        self.doc = pymupdf.open(stream=pdf, filetype="pdf")
        self.pages: dict[int, PageModel] = {}
        self.planner = FontPlanner(self.doc, registry, owner)

    def page_model(self, n: int) -> PageModel:
        if n not in self.pages:
            ex = extract_page(self.doc[n])
            elements = build_page_elements(ex, {})
            shapes = [e for e in elements if e["type"] == "shape"]
            drawings = {}
            for el in shapes:
                seqnos = set(el["source"]["seqnos"])
                drawings[el["id"]] = [p for g in ex.drawings for p in g.paths if p["seqno"] in seqnos]
            all_paths = [p for g in ex.drawings for p in g.paths]
            self.pages[n] = PageModel({e["id"]: e for e in elements}, drawings, all_paths)
        return self.pages[n]

    def element(self, element_id: str) -> tuple[int, dict]:
        match = re.match(r"p(\d+)-", element_id)
        if not match or int(match.group(1)) >= self.doc.page_count:
            raise OpError(f"unknown element {element_id}")
        n = int(match.group(1))
        el = self.page_model(n).elements.get(element_id)
        if el is None:
            raise OpError(f"unknown element {element_id}")
        return n, el

    # --- op collapsing -------------------------------------------------------------------------

    def _collapse(self, ops: list[dict]):
        states: dict[str, ElementState] = {}
        adds: list[dict] = []
        for op in ops:
            kind = op.get("op")
            if kind == "addText":
                if not 0 <= int(op.get("page", -1)) < self.doc.page_count:
                    raise OpError("addText needs a valid page")
                adds.append(op)
                continue
            if kind not in ("editText", "transform", "delete"):
                raise OpError(f"unknown op {kind!r}")
            _, el = self.element(op.get("id", ""))
            if el["locked"]:
                raise OpError(f"element {el['id']} is locked")
            state = states.setdefault(el["id"], ElementState())
            if kind == "delete":
                state.deleted = True
            elif kind == "editText":
                if el["type"] != "text":
                    raise OpError(f"editText on non-text element {el['id']}")
                state.text = op
            else:
                state.bbox = [float(v) for v in op["bbox"]]
        return states, adds

    # --- apply ---------------------------------------------------------------------------------

    def apply(self, ops: list[dict]) -> ApplyResult:
        result = ApplyResult()
        states, adds = self._collapse(ops)
        by_page: dict[int, list[tuple[dict, ElementState]]] = {}
        for element_id, state in states.items():
            n, el = self.element(element_id)
            by_page.setdefault(n, []).append((el, state))
        for n in sorted({*by_page, *(int(a["page"]) for a in adds)}):
            page_adds = [a for a in adds if int(a["page"]) == n]
            self._apply_page(n, by_page.get(n, []), page_adds, result)
        return result

    def _apply_page(self, n: int, items, adds: list[dict], result: ApplyResult) -> None:
        page = self.doc[n]
        if page.rotation:
            raise OpError("editing rotated pages is not supported yet")
        model = self.page_model(n)

        texts = [(el, st) for el, st in items if el["type"] == "text"]
        shapes = [(el, st) for el, st in items if el["type"] == "shape"]
        images = [(el, st) for el, st in items if el["type"] == "image"]

        # 0. Images first: rewrite each placement in the original content stream (keeps z-order
        #    and image bytes). Redactions below rewrite the stream and rename its resources.
        #    Shapes painted directly by the page get the same treatment, so a resized shape
        #    stays behind the text on it. Last command first, so a rewrite (or a deletion)
        #    never shifts the index of a command still to be rewritten.
        inplace: list[tuple[int, dict, ElementState, str | None]] = []
        for el, st in images:
            if st.deleted or st.bbox:
                inplace.append((el["source"]["do"], el, st, el["xref"]))
        fallback_shapes = []
        for el, st in shapes:
            calls = [p.get("call") for p in model.drawings[el["id"]]]
            if calls and all(c is not None for c in calls):
                inplace.extend((c, el, st, None) for c in calls)
            else:
                fallback_shapes.append((el, st))
        for index, el, st, name in sorted(inplace, key=lambda item: item[0], reverse=True):
            new_bbox = None if st.deleted else st.bbox
            if not self._rewrite_call(page, index, el["bbox"], new_bbox, name):
                raise OpError(f"could not locate the drawing command for {el['id']}")
        shapes = fallback_shapes

        # 1. Remove original text of edited, moved or deleted text elements.
        if texts:
            for el, _ in texts:
                for rect in el["source"]["spanRects"]:
                    page.add_redact_annot(_text_band(rect), fill=False, cross_out=False)
            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_NONE,
                graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                text=pymupdf.PDF_REDACT_TEXT_REMOVE,
            )

        # 2. Remove shapes (and the bullet shapes of edited lists); restore any other paths
        #    the redaction took with them.
        list_markers = [
            (rect, el["source"]["markerSeqnos"])
            for el, _ in texts
            if el.get("list", {}).get("marker") == "shape"
            for rect in el["source"]["markerRects"]
        ]
        if shapes or list_markers:
            edited_seqnos = {p["seqno"] for el, _ in shapes for p in model.drawings[el["id"]]}
            edited_seqnos |= {s for _, seqnos in list_markers for s in seqnos}
            areas = []
            rects = [el["bbox"] for el, _ in shapes] + [rect for rect, _ in list_markers]
            for rect in rects:
                area = pymupdf.Rect(rect) + (-0.5, -0.5, 0.5, 0.5)
                areas.append(area)
                page.add_redact_annot(area, fill=False, cross_out=False)
            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_NONE,
                graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                text=pymupdf.PDF_REDACT_TEXT_NONE,
            )
            collateral = [
                p for p in model.all_paths
                if p["seqno"] not in edited_seqnos and any(a.contains(p["rect"]) for a in areas)
            ]  # fmt: skip
            if collateral:
                _draw_paths(page, collateral, None)
                result.notices.append({"page": n, "code": "shapes-restacked"})
            for el, st in shapes:
                if st.deleted:
                    continue
                if st.bbox:
                    matrix = _move_matrix(el["bbox"], st.bbox)
                    _draw_paths(page, model.drawings[el["id"]], matrix)
                    result.notices.append({"id": el["id"], "code": "shape-on-top"})

        # 3. Write text.
        for el, st in texts:
            if st.deleted:
                continue
            self._write_element(page, el, st, result)
        for i, op in enumerate(adds):
            el = {
                "id": op.get("id") or f"p{n}-new{i}",
                "bbox": op["bbox"],
                "align": op.get("align", "left"),
                "lineHeight": op.get("lineHeight", 1.2),
                "letterSpacing": op.get("letterSpacing", 0),
                "opacity": op.get("opacity", 1),
                "runs": op["runs"],
                "lines": [],
            }
            self._write_element(page, el, ElementState(text=op), result, new=True)

    def _rewrite_call(self, page: pymupdf.Page, index, old_bbox, new_bbox, image_xref: int | None) -> bool:
        """Move/resize (new_bbox) or delete (None) one drawing command in place, in the page
        content or the form it sits in: an image ``Do`` (``image_xref`` given) or a path."""
        calls = page_do_calls(self.doc, page)
        if index is None or index >= len(calls):
            return False
        call = calls[index]
        if image_xref is not None and (call.kind != "do" or call.target != image_xref):
            return False
        if image_xref is None and call.kind != "path":
            return False
        stream = self.doc.xref_stream(call.stream)
        original = stream[call.start : call.end]
        if new_bbox is None:
            replacement = b""
        else:
            # The command's CTM C maps its own space to PDF space; page space is C * T. For a
            # page-space move D the new CTM is C * T * D * T^-1, i.e. X * C with
            # X = C * T * D * T^-1 * C^-1, applied by wrapping the command in "q X cm ... Q".
            c, t = call.ctm, page.transformation_matrix
            x = c * t * _move_matrix(old_bbox, new_bbox) * ~t * ~c
            nums = " ".join(f"{v:.6f}" for v in (x.a, x.b, x.c, x.d, x.e, x.f))
            replacement = f"q {nums} cm\n".encode() + original + b"\nQ"
        self.doc.update_stream(call.stream, stream[: call.start] + replacement + stream[call.end :])
        return True

    def _write_element(
        self, page: pymupdf.Page, el: dict, st: ElementState, result: ApplyResult, new=False
    ) -> None:
        op = st.text or {}
        align_changed = "align" in op and op["align"] != el.get("align")
        # Block-level style changes travel with the text op.
        el = {**el, **{k: op[k] for k in ("align", "lineHeight", "letterSpacing") if k in op}}
        runs = op.get("runs") or el["runs"]
        lines = op.get("lines")
        choice = op.get("fontFallback", SUBSTITUTE)
        lst = el.get("list")
        marker_runs = []
        if lst and lst["marker"] != "shape":
            style = lst.get("glyph") or lst.get("number")
            items = "".join(r["text"] for r in runs).count("\n") + 1
            if lines:
                items = max(items, sum(1 for ln in lines if ln.get("listItem")))
            marker_runs = [
                {"text": marker_text(lst, k), **{f: style[f] for f in ("font", "size", "color")}}
                for k in range(items)
            ]
        all_runs = runs + [r for ln in (lines or []) for r in ln["runs"]] + marker_runs
        plan, warnings = self.planner.plan(el["id"], all_runs, choice)
        result.warnings.extend(warnings)

        box = st.bbox or el["bbox"]
        dx, dy = box[0] - el["bbox"][0], box[1] - el["bbox"][1]
        resized = st.bbox is not None and abs((box[2] - box[0]) - (el["bbox"][2] - el["bbox"][0])) > 0.5
        shift = False
        if not lines:
            if st.text is None and not new and not resized:
                lines = el["lines"]  # moved only: reuse the original layout
                shift = True
            else:
                same_style = all(
                    (r["font"], r["size"]) in {(o["font"], o["size"]) for o in el["runs"]} for r in runs
                )
                if el["lines"] and same_style:
                    first = el["lines"][0]["baseline"] + dy
                else:
                    # New font or size: keep the top of the block where it was.
                    first = _first_baseline({**el, "bbox": box}, runs, plan)
                x0, x1 = box[0], box[2]
                if lst:
                    x0 = lst["textX"] + dx  # item text column; markers hang to the left
                elif not new and not resized and len(el["lines"]) == 1:
                    if align_changed:
                        # The box is only as wide as the ink, so a new alignment needs the free
                        # space around the block to mean anything.
                        span = self._free_span(page.number, el, margin=36.0)
                    else:
                        # Room to grow sideways (see _single_line_span).
                        span = self._single_line_span(page.number, el)
                    x0, x1 = (v + dx for v in span)
                lines = layout_text(
                    runs,
                    x0=x0,
                    x1=x1,
                    first_baseline=first,
                    align=el["align"],
                    line_height=el["lineHeight"],
                    letter_spacing=el["letterSpacing"],
                    fonts=lambda name: plan[name],
                    list_items=bool(lst),
                )
        if shift:
            lines = [
                {**ln, "x": ln["x"] + dx, "baseline": ln["baseline"] + dy,
                 "runs": [{**r, "x": r["x"] + dx} if "x" in r else r for r in ln["runs"]]}
                for ln in lines
            ]  # fmt: skip

        restyled = any(
            (r["font"], r["size"]) not in {(o["font"], o["size"]) for o in el.get("runs", [])} for r in runs
        )
        if lines and lines[-1]["baseline"] > box[3] + 0.5 and not restyled:
            # Deliberately bigger text is expected to grow; only warn when more words overflow.
            result.warnings.append({"id": el["id"], "code": "text-overflow"})
        if lst:
            lines = lines + self._draw_list_markers(page, el, lines, marker_runs)
        result.boxes[el["id"]] = _lines_bbox(lines, plan, el.get("letterSpacing", 0))
        write_lines(page, lines, plan, el.get("letterSpacing", 0), el.get("opacity", 1))

    def _draw_list_markers(self, page, el: dict, lines: list[dict], marker_runs: list[dict]):
        """One marker per item start (lines flagged listItem), at the original offset from the
        item text. Shape markers are drawn now; glyph and number markers are returned as extra
        text lines for write_lines."""
        lst = el["list"]
        off = lst["offset"]
        extra = []
        template = []
        if lst["marker"] == "shape":
            seqnos = set(el["source"]["markerTemplate"])
            template = [p for p in self.page_model(page.number).all_paths if p["seqno"] in seqnos]
            t_rect = pymupdf.Rect(template[0]["rect"])
            for p in template[1:]:
                t_rect |= p["rect"]
        k = 0
        for line in lines:
            if not line.get("listItem"):
                continue
            x, y = line["x"], line["baseline"]
            if lst["marker"] == "shape":
                target = pymupdf.Rect(x + off[0], y + off[1], x + off[2], y + off[3])
                _draw_paths(page, template, _move_matrix(t_rect, target))
            else:
                run = marker_runs[min(k, len(marker_runs) - 1)]
                if lst["marker"] == "number":
                    run = {**run, "text": marker_text(lst, k)}
                extra.append({"x": x + off[0], "baseline": y, "runs": [run]})
            k += 1
        return extra

    def _single_line_span(self, n: int, el: dict, margin: float = 18.0) -> tuple[float, float]:
        """Horizontal room for a single-line block.

        The PDF only records the ink width of a one-line block, not the Canva text box, so a
        longer edit would wrap immediately. Let it grow sideways (keeping its alignment anchor)
        up to the nearest element in the same vertical band, or the page margin.
        """
        x0, _y0, x1, _y1 = el["bbox"]
        left, right = self._free_span(n, el, margin)
        if el["align"] == "center":
            center = (x0 + x1) / 2
            half = min(center - left, right - center)
            return center - half, center + half
        if el["align"] == "right":
            return left, x1
        return x0, right

    def _free_span(self, n: int, el: dict, margin: float) -> tuple[float, float]:
        """The horizontal space a block could occupy: up to the nearest element in its vertical
        band on each side, inside the card or button it sits on (keeping its padding there),
        or ``margin`` from the page edge. Always includes the block itself."""
        x0, y0, x1, y1 = el["bbox"]
        page_w = self.doc[n].rect.width
        left, right = margin, page_w - margin
        container = None
        for other in self.page_model(n).elements.values():
            if other["id"] == el["id"] or other.get("role") == "background":
                continue
            ox0, oy0, ox1, oy1 = other["bbox"]
            if oy1 <= y0 or oy0 >= y1:
                continue  # not in this line's band
            if ox0 <= x0 and ox1 >= x1:
                # Something behind the text: a full-width band is no limit, a card is.
                if oy0 <= y0 and oy1 >= y1 and ox1 - ox0 < page_w - 2 * margin:
                    if container is None or (ox1 - ox0) < (container[2] - container[0]):
                        container = (ox0, oy0, ox1, oy1)
                continue
            if ox1 <= x0:
                left = max(left, ox1 + 2)
            elif ox0 >= x1:
                right = min(right, ox0 - 2)
        if container:
            pad = max(2.0, min(x0 - container[0], container[2] - x1))
            left, right = max(left, container[0] + pad), min(right, container[2] - pad)
        return min(left, x0), max(right, x1)

    # --- output ----------------------------------------------------------------------------------

    def export(self) -> bytes:
        self.doc.subset_fonts()
        return self.doc.tobytes(garbage=4, deflate=True)

    def render_png(self, n: int, scale: float = 2.0) -> bytes:
        return self.doc[n].get_pixmap(matrix=pymupdf.Matrix(scale, scale)).tobytes("png")

    def background_png(self, n: int, scale: float = 2.0) -> bytes:
        """Page render with every editable text element removed (the editor's locked layer)."""
        page = self.doc[n]
        editable = [
            el for el in self.page_model(n).elements.values() if el["type"] == "text" and not el["locked"]
        ]
        for el in editable:
            for rect in el["source"]["spanRects"]:
                page.add_redact_annot(_text_band(rect), fill=False, cross_out=False)
        page.apply_redactions(
            images=pymupdf.PDF_REDACT_IMAGE_NONE,
            graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
            text=pymupdf.PDF_REDACT_TEXT_REMOVE,
        )
        markers = [r for el in editable for r in el["source"].get("markerRects", [])]
        if markers:  # list bullets are drawn by the editor with their items
            for rect in markers:
                page.add_redact_annot(
                    pymupdf.Rect(rect) + (-0.5, -0.5, 0.5, 0.5), fill=False, cross_out=False
                )
            page.apply_redactions(
                images=pymupdf.PDF_REDACT_IMAGE_NONE,
                graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                text=pymupdf.PDF_REDACT_TEXT_NONE,
            )
        return self.render_png(n, scale)


def _first_baseline(el: dict, runs: list[dict], plan: dict[str, Renderer]) -> float:
    run = runs[0]
    font = plan[run["font"]].font_for(run["text"].strip()[:1] or "x")
    return el["bbox"][1] + font.ascender * run["size"]


def _lines_bbox(lines: list[dict], plan: dict[str, Renderer], letter_spacing: float) -> list[float]:
    """Bounding box of laid-out lines (advance widths, font ascent/descent)."""
    x0 = y0 = float("inf")
    x1 = y1 = float("-inf")
    for line in lines:
        x = line["x"]
        for run in line["runs"]:
            if not run["text"]:
                continue
            x = run.get("x", x)
            renderer = plan[run["font"]]
            font = renderer.font_for(run["text"].strip()[:1] or "x")
            width = renderer.text_length(run["text"], run["size"]) + letter_spacing * len(run["text"])
            width += line.get("wordSpacing", 0) * run["text"].count(" ")
            x0, x1 = min(x0, x), max(x1, x + width)
            y0 = min(y0, line["baseline"] - font.ascender * run["size"])
            y1 = max(y1, line["baseline"] - font.descender * run["size"])
            x += width
    return [round(v, 2) for v in (x0, y0, x1, y1)] if x0 != float("inf") else []


def write_lines(
    page: pymupdf.Page,
    lines: list[dict],
    plan: dict[str, Renderer],
    letter_spacing: float = 0,
    opacity: float = 1,
) -> None:
    writers: dict[str, pymupdf.TextWriter] = {}
    for line in lines:
        x, y = line["x"], line["baseline"]
        word_spacing = line.get("wordSpacing", 0)
        for run in line["runs"]:
            if "x" in run:
                x = run["x"]
            renderer = plan[run["font"]]
            writer = writers.setdefault(run["color"], pymupdf.TextWriter(page.rect))
            size = run["size"]
            single = renderer.single_font
            if (
                single is not None
                and not letter_spacing
                and not word_spacing
                and (all(single.has_glyph(ord(c)) or c.isspace() for c in run["text"]))
            ):
                _, end = writer.append((x, y), run["text"], font=single, fontsize=size)
                x = end.x
                continue
            for ch in run["text"]:
                font = renderer.font_for(ch)
                writer.append((x, y), ch, font=font, fontsize=size)
                x += font.text_length(ch, fontsize=size) + letter_spacing
                if ch == " ":
                    x += word_spacing
    for color, writer in writers.items():
        writer.write_text(page, color=_rgb(color), opacity=opacity if opacity < 1 else -1)


def _draw_paths(page: pymupdf.Page, paths: list[dict], matrix: pymupdf.Matrix | None) -> None:
    def t(p):
        return pymupdf.Point(p) * matrix if matrix else pymupdf.Point(p)

    for path in sorted(paths, key=lambda p: p["seqno"]):
        shape = page.new_shape()
        for item in path["items"]:
            kind = item[0]
            if kind == "l":
                shape.draw_line(t(item[1]), t(item[2]))
            elif kind == "c":
                shape.draw_bezier(t(item[1]), t(item[2]), t(item[3]), t(item[4]))
            elif kind == "re":
                shape.draw_rect(pymupdf.Rect(item[1]) * matrix if matrix else item[1])
            elif kind == "qu":
                shape.draw_quad(pymupdf.Quad(item[1]) * matrix if matrix else item[1])
        cap = path.get("lineCap") or (0,)
        shape.finish(
            fill=path.get("fill"),
            color=path.get("color"),
            width=path.get("width") or 0,
            fill_opacity=path.get("fill_opacity") or 1,
            stroke_opacity=path.get("stroke_opacity") or 1,
            even_odd=bool(path.get("even_odd")),
            closePath=bool(path.get("closePath")),
            lineCap=max(cap) if isinstance(cap, (tuple, list)) else int(cap),
            lineJoin=int(path.get("lineJoin") or 0),
            dashes=path.get("dashes") or None,
        )
        shape.commit()
