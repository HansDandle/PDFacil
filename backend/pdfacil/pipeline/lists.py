"""Bulleted and numbered lists.

In a PDF a bullet is just a dot drawn at fixed coordinates next to some text, so editing the
text leaves the dots behind. Here list blocks are rebuilt as real lists: the markers become
part of the text element (a template marker plus its offset from each item's first line), and
exports draw one marker per item wherever the items end up, like a word processor. Wrapped
continuation lines get no marker and align with the item text (hanging indent).

Three marker kinds:
- "shape": a small vector dot/square left of the line (Canva, ReportLab).
- "glyph": a bullet character at the start of the line ("• ", "– ").
- "number": "1." / "2)" / "a." at the start of the line; renumbered on export.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

STRONG_BULLETS = "•◦▪▫‣⁃●○■□►▸"
WEAK_BULLETS = "–-*"  # also used as punctuation: need a following space and two items
# PDFs often position the item text instead of emitting a space after the marker.
_GLYPH_RE = re.compile(rf"^\s*(?:([{STRONG_BULLETS}])\s*|([{re.escape(WEAK_BULLETS)}])\s+)(?=\S)")
_NUMBER_RE = re.compile(r"^\s*(\d{1,3})([.)])\s*(?=[^\d\s])")


class _GlyphMatch:
    def __init__(self, match: re.Match):
        self._m = match

    def group(self, _n: int = 1) -> str:
        return self._m.group(1) or self._m.group(2)

    def end(self) -> int:
        return self._m.end()


def glyph_prefix(text: str) -> _GlyphMatch | None:
    match = _GLYPH_RE.match(text)
    return _GlyphMatch(match) if match else None


def number_prefix(text: str) -> re.Match | None:
    return _NUMBER_RE.match(text)


def shape_marker_for(line, markers: list[tuple[int, tuple]]) -> int | None:
    """Index of a small shape sitting just left of the line's first character, if any."""
    top, bottom = line.baseline - line.size, line.baseline
    x0 = line.x0
    for idx, m in markers:
        if x0 - 3 * line.size <= m[0] and m[2] <= x0 + 1 and m[1] < bottom and m[3] > top:
            return idx
    return None


@dataclass
class ListInfo:
    kind: str  # "bullet" | "number"
    marker: str  # "shape" | "glyph" | "number"
    item_starts: set[int]  # indices of visual lines that start an item
    prefix_len: dict[int, int] = field(default_factory=dict)  # chars to strip per item line
    shape_indices: list[int] = field(default_factory=list)
    template: int | None = None
    text_x: float = 0.0
    offset: list[float] = field(default_factory=list)
    style: dict | None = None  # font/size/color for glyph and number markers
    glyph: str | None = None
    separator: str = "."
    start: int = 1

    def to_json(self) -> dict:
        data = {"type": self.kind, "marker": self.marker, "textX": round(self.text_x, 3)}
        data["offset"] = [round(v, 3) for v in self.offset]
        if self.marker == "glyph":
            data["glyph"] = {"text": self.glyph, **(self.style or {})}
        if self.marker == "number":
            data["number"] = {"format": "{n}" + self.separator, "start": self.start, **(self.style or {})}
        return data


def _text_start(line, prefix_len: int) -> tuple[float, dict | None, float | None]:
    """(x of the first char after the prefix, style of the marker span, x of the marker)."""
    seen = 0
    marker_style = marker_x = None
    for s in line.spans:
        for ch, x in zip(s.text, s.char_origins, strict=False):
            if seen >= prefix_len:
                return x, marker_style, marker_x
            if not ch.isspace() and marker_x is None:
                marker_x = x
                marker_style = {"font": s.font, "size": s.size, "color": s.color}
            seen += 1
    return line.x0, marker_style, marker_x


def detect_list(lines, markers: list[tuple[int, tuple]]) -> ListInfo | None:
    """Recognise a list in a block's visual lines. The first line must start an item."""
    if not lines:
        return None

    shapes = {i: shape_marker_for(ln, markers) for i, ln in enumerate(lines)}
    shapes = {i: m for i, m in shapes.items() if m is not None}
    if 0 in shapes:
        first = next(m for idx, m in markers if idx == shapes[0])
        text_x = lines[0].spans[0].origin[0]
        base = lines[0].baseline
        return ListInfo(
            kind="bullet",
            marker="shape",
            item_starts=set(shapes),
            shape_indices=list(shapes.values()),
            template=shapes[0],
            text_x=text_x,
            offset=[first[0] - text_x, first[1] - base, first[2] - text_x, first[3] - base],
        )

    glyphs = {i: m for i, ln in enumerate(lines) if (m := glyph_prefix(ln.text))}
    if 0 in glyphs:
        char = glyphs[0].group(1)
        same = {i: m for i, m in glyphs.items() if m.group(1) == char}
        # "- " or "* " at the start of a single line is too weak a signal on its own.
        if char in STRONG_BULLETS or len(same) >= 2:
            x, style, marker_x = _text_start(lines[0], same[0].end())
            return ListInfo(
                kind="bullet",
                marker="glyph",
                item_starts=set(same),
                prefix_len={i: m.end() for i, m in same.items()},
                text_x=x,
                offset=[(marker_x if marker_x is not None else x) - x],
                style=style,
                glyph=char,
            )

    numbers = {i: m for i, ln in enumerate(lines) if (m := number_prefix(ln.text))}
    if 0 in numbers and len(numbers) >= 2:
        sep = numbers[0].group(2)
        values = [int(numbers[i].group(1)) for i in sorted(numbers)]
        sequential = all(b == a + 1 for a, b in zip(values, values[1:], strict=False))
        if sequential and all(m.group(2) == sep for m in numbers.values()):
            x, style, marker_x = _text_start(lines[0], numbers[0].end())
            return ListInfo(
                kind="number",
                marker="number",
                item_starts=set(numbers),
                prefix_len={i: m.end() for i, m in numbers.items()},
                text_x=x,
                offset=[(marker_x if marker_x is not None else x) - x],
                style=style,
                separator=sep,
                start=values[0],
            )
    return None


def strip_prefix(runs: list[dict], count: int) -> list[dict]:
    """Remove the first ``count`` characters (a "• " or "1. " marker) from a line's runs."""
    out = []
    for run in runs:
        if count <= 0:
            out.append(run)
            continue
        text = run["text"]
        cut = min(count, len(text))
        count -= cut
        if text[cut:]:
            out.append({**run, "text": text[cut:]})
    return out


def marker_text(info: dict, n: int) -> str:
    """Text for item n (0-based) of a glyph or number list."""
    if info["marker"] == "glyph":
        return info["glyph"]["text"]
    return info["number"]["format"].format(n=info["number"]["start"] + n)
