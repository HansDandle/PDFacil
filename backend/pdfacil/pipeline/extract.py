"""Stage 1: extraction.

Pulls text spans (with per-character positions), image placements and vector drawings off a
page. Pure functions over a PyMuPDF page; no font lookups or storage access.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pymupdf

from .fonts import strip_subset_prefix

TEXT_FLAGS = (
    pymupdf.TEXT_PRESERVE_WHITESPACE
    | pymupdf.TEXT_PRESERVE_LIGATURES
    | pymupdf.TEXT_MEDIABOX_CLIP
    | pymupdf.TEXT_INHIBIT_SPACES  # tracking gaps must not become synthetic spaces
)

Rect = tuple[float, float, float, float]


def hex_color(value: int | tuple | list | None) -> str | None:
    if value is None:
        return None
    if isinstance(value, int):
        return f"#{value:06X}"
    if len(value) == 1:
        value = (value[0],) * 3
    if len(value) == 4:  # CMYK
        c, m, y, k = value
        value = ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
    return "#" + "".join(f"{round(max(0.0, min(1.0, v)) * 255):02X}" for v in value)


def rect_tuple(r) -> Rect:
    r = pymupdf.Rect(r)
    return (round(r.x0, 3), round(r.y0, 3), round(r.x1, 3), round(r.y1, 3))


@dataclass
class Span:
    text: str
    font: str
    size: float
    color: str
    flags: int
    origin: tuple[float, float]
    bbox: Rect
    alpha: int = 255
    char_origins: list[float] = field(default_factory=list)
    char_right: list[float] = field(default_factory=list)

    @property
    def letter_spacing(self) -> float:
        """Average extra space between characters, in points (Canva tracking)."""
        gaps = [self.char_origins[i + 1] - self.char_right[i] for i in range(len(self.char_origins) - 1)]
        return sum(gaps) / len(gaps) if gaps else 0.0

    @property
    def style(self) -> tuple[str, float, str]:
        return (self.font, round(self.size, 1), self.color)


@dataclass
class Line:
    spans: list[Span]
    bbox: Rect
    dir: tuple[float, float]

    @property
    def horizontal(self) -> bool:
        return abs(self.dir[1]) < 1e-3 and self.dir[0] > 0

    @property
    def baseline(self) -> float:
        return self.spans[0].origin[1]

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)


@dataclass
class ImagePlacement:
    xref: int
    name: str
    bbox: Rect
    matrix: tuple[float, ...]
    smask: int
    occurrence: int  # nth placement of this image on the page, in paint order
    in_form: bool  # placed inside a Form XObject rather than the page content


@dataclass
class DrawingGroup:
    paths: list[dict]
    bbox: Rect
    fill: str | None
    stroke: str | None
    background: bool = False
    outlined_text: bool = False

    @property
    def seqno(self) -> int:
        return min(p["seqno"] for p in self.paths)


@dataclass
class PageExtraction:
    index: int
    width: float
    height: float
    lines: list[Line]
    images: list[ImagePlacement]
    drawings: list[DrawingGroup]
    paint_log: list[tuple[str, Rect]]


def extract_spans(page: pymupdf.Page) -> list[Line]:
    raw = page.get_text("rawdict", flags=TEXT_FLAGS)
    lines: list[Line] = []
    for block in raw["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block["lines"]:
            spans = []
            for s in line["spans"]:
                chars = s["chars"]
                if not chars:
                    continue
                spans.append(
                    Span(
                        text="".join(c["c"] for c in chars),
                        font=strip_subset_prefix(s["font"]),
                        size=round(s["size"], 3),
                        color=hex_color(s["color"]) or "#000000",
                        flags=s["flags"],
                        origin=(round(s["origin"][0], 3), round(s["origin"][1], 3)),
                        bbox=rect_tuple(s["bbox"]),
                        alpha=s.get("alpha", 255),
                        char_origins=[c["origin"][0] for c in chars],
                        char_right=[c["bbox"][2] for c in chars],
                    )
                )
            if spans:
                lines.append(Line(spans, rect_tuple(line["bbox"]), tuple(line["dir"])))
    return lines


def extract_images(page: pymupdf.Page) -> list[ImagePlacement]:
    placements = []
    for xref, smask, _w, _h, _bpc, _cs, _alt, name, _filter, referencer in page.get_images(full=True):
        for k, (rect, matrix) in enumerate(page.get_image_rects(xref, transform=True)):
            if rect.is_empty or rect.is_infinite:
                continue
            placements.append(
                ImagePlacement(
                    xref=xref,
                    name=name,
                    bbox=rect_tuple(rect),
                    matrix=tuple(round(v, 6) for v in matrix),
                    smask=smask,
                    occurrence=k,
                    in_form=referencer != 0,
                )
            )
    return placements


def _style_key(path: dict) -> tuple:
    return (path.get("type"), hex_color(path.get("fill")), hex_color(path.get("color")))


def _is_glyph_like(path: dict) -> bool:
    r = path["rect"]
    curves = sum(1 for item in path["items"] if item[0] in ("c", "qu"))
    return (
        path.get("fill") is not None
        and path.get("type") == "f"
        and 0 < r.height < 120
        and r.width < 3 * r.height
        and curves + len(path["items"]) >= 4
    )


def _find_outlined_text(paths: list[dict], text_rects: list[pymupdf.Rect]) -> list[list[int]]:
    """Clusters of small filled glyph-shaped paths sitting on one baseline with no text layer."""
    candidates = [
        i for i, p in enumerate(paths)
        if _is_glyph_like(p) and not any(p["rect"].intersects(t) for t in text_rects)
    ]  # fmt: skip
    candidates.sort(key=lambda i: paths[i]["rect"].x0)
    clusters: list[list[int]] = []
    for i in candidates:
        r = paths[i]["rect"]
        for cluster in clusters:
            last = paths[cluster[-1]]["rect"]
            height = max(r.height, last.height)
            same_row = abs(r.y1 - last.y1) < 0.25 * height or abs(r.y0 - last.y0) < 0.25 * height
            if (
                same_row
                and 0 <= r.x0 - last.x1 < 0.8 * height
                and hex_color(paths[i]["fill"]) == hex_color(paths[cluster[-1]]["fill"])
            ):
                cluster.append(i)
                break
        else:
            clusters.append([i])
    return [c for c in clusters if len(c) >= 3]


def extract_drawings(page: pymupdf.Page, text_rects: list[pymupdf.Rect]) -> list[DrawingGroup]:
    paths = [p for p in page.get_drawings() if not p["rect"].is_empty or p["items"]]
    page_area = page.rect.width * page.rect.height

    outlined = _find_outlined_text(paths, text_rects)
    outlined_ids = {i for c in outlined for i in c}

    # Union paths that overlap and share fill and stroke style into one selectable shape.
    parent = list(range(len(paths)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    rest = [i for i in range(len(paths)) if i not in outlined_ids]
    for a_pos, a in enumerate(rest):
        ra = paths[a]["rect"]
        if ra.width * ra.height > 0.9 * page_area:
            continue
        for b in rest[a_pos + 1 :]:
            rb = paths[b]["rect"]
            if rb.width * rb.height > 0.9 * page_area:
                continue
            if _style_key(paths[a]) == _style_key(paths[b]) and ra.intersects(rb):
                parent[find(b)] = find(a)

    groups: dict[int, list[int]] = {}
    for i in rest:
        groups.setdefault(find(i), []).append(i)

    result = []
    for members in [*groups.values(), *outlined]:
        ps = [paths[i] for i in members]
        bbox = pymupdf.Rect(ps[0]["rect"])
        for p in ps[1:]:
            bbox |= p["rect"]
        is_outlined = members in outlined
        result.append(
            DrawingGroup(
                paths=ps,
                bbox=rect_tuple(bbox),
                fill=hex_color(ps[0].get("fill")),
                stroke=hex_color(ps[0].get("color")),
                background=(not is_outlined and bbox.width * bbox.height >= 0.95 * page_area),
                outlined_text=is_outlined,
            )
        )
    result.sort(key=lambda g: g.seqno)
    return result


def extract_page(page: pymupdf.Page) -> PageExtraction:
    lines = extract_spans(page)
    text_rects = [pymupdf.Rect(s.bbox) for line in lines for s in line.spans if s.text.strip()]
    return PageExtraction(
        index=page.number,
        width=page.rect.width,
        height=page.rect.height,
        lines=lines,
        images=extract_images(page),
        drawings=extract_drawings(page, text_rects),
        paint_log=[(kind, rect_tuple(r)) for kind, r in page.get_bboxlog()],
    )
