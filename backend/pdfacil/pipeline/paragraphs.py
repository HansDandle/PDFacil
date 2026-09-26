"""Stage 3: paragraph rebuild.

Canva exports every line separately. Group lines into editable text blocks, infer alignment and
line height, and keep mixed styling as runs. Each block also keeps its original laid-out lines
(the single source of truth for export) and the tight span rectangles used for redaction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import median

from .extract import Line, Span

EDGE_TOLERANCE = 1.5  # pt
PITCH_TOLERANCE = 0.15  # fraction of the block's line pitch


@dataclass
class VisualLine:
    spans: list[Span]

    @property
    def baseline(self) -> float:
        return self.spans[0].origin[1]

    @property
    def visible(self) -> list[Span]:
        return [s for s in self.spans if s.text.strip()] or self.spans

    @property
    def x0(self) -> float:
        return min(s.bbox[0] for s in self.visible)

    @property
    def x1(self) -> float:
        return max(s.bbox[2] for s in self.visible)

    @property
    def center(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def text(self) -> str:
        return "".join(s.text for s in self.spans)

    @property
    def style(self) -> tuple[str, float, str]:
        """The style covering the most characters (tolerates one bold word in a line)."""
        weights: dict[tuple, int] = {}
        for s in self.visible:
            weights[s.style] = weights.get(s.style, 0) + len(s.text.strip())
        return max(weights, key=weights.__getitem__)

    @property
    def size(self) -> float:
        return self.style[1]


def visual_lines(lines: list[Line]) -> list[VisualLine]:
    """Join horizontal line fragments that share a baseline and sit next to each other."""
    frags = sorted(
        (VisualLine(list(line.spans)) for line in lines if line.horizontal),
        key=lambda v: (round(v.baseline, 1), v.x0),
    )
    out: list[VisualLine] = []
    for frag in frags:
        prev = out[-1] if out else None
        if (
            prev
            and abs(prev.baseline - frag.baseline) < 0.5
            and -0.5 < frag.x0 - prev.x1 < max(prev.size, frag.size) * 1.2
        ):
            prev.spans.extend(frag.spans)
        else:
            out.append(frag)
    return [v for v in out if v.text.strip()]


def _edges(a: VisualLine, b: VisualLine) -> set[str]:
    found = set()
    if abs(a.x0 - b.x0) <= EDGE_TOLERANCE:
        found.add("left")
    if abs(a.center - b.center) <= EDGE_TOLERANCE:
        found.add("center")
    if abs(a.x1 - b.x1) <= EDGE_TOLERANCE:
        found.add("right")
    return found


@dataclass
class Block:
    lines: list[VisualLine]
    align: set[str] = field(default_factory=lambda: {"left", "center", "right"})

    @property
    def pitch(self) -> float | None:
        if len(self.lines) < 2:
            return None
        return median(b.baseline - a.baseline for a, b in zip(self.lines, self.lines[1:], strict=False))

    def accepts(self, line: VisualLine) -> set[str] | None:
        last = self.lines[-1]
        a, b = last.style, line.style
        if a[0] != b[0] or a[2] != b[2] or abs(a[1] - b[1]) > 0.25:
            return None
        pitch = line.baseline - last.baseline
        if not 0.7 * last.size <= pitch <= 2.5 * last.size:
            return None
        if self.pitch is not None and abs(pitch - self.pitch) > PITCH_TOLERANCE * self.pitch:
            return None
        first = self.lines[0]
        if min(first.x1, line.x1) - max(first.x0, line.x0) <= 0:
            return None
        common = self.align & _edges(first, line)
        return common or None


def build_blocks(lines: list[Line]) -> list[Block]:
    blocks: list[Block] = []
    open_blocks: list[Block] = []
    for line in visual_lines(lines):
        candidates = [(b, b.accepts(line)) for b in open_blocks]
        candidates = [(b, a) for b, a in candidates if a]
        if candidates:
            # Nearest block above wins when columns sit side by side.
            block, align = min(candidates, key=lambda c: line.baseline - c[0].lines[-1].baseline)
            block.lines.append(line)
            block.align = align
        else:
            block = Block([line])
            blocks.append(block)
            open_blocks.append(block)
        open_blocks = [b for b in open_blocks if line.baseline - b.lines[-1].baseline < 3 * b.lines[-1].size]
    return blocks


def infer_alignment(block: Block, page_width: float) -> str:
    lines = block.lines
    if len(lines) == 1:
        return "center" if abs(lines[0].center - page_width / 2) <= 3 else "left"
    first = lines[0]
    body = set.intersection(*[_edges(first, ln) for ln in lines[1:-1]]) if len(lines) > 2 else None
    widest = max(ln.x1 - ln.x0 for ln in lines)
    last = lines[-1]
    if (
        body is not None
        and {"left", "right"} <= body
        and "left" in _edges(first, last)
        and (last.x1 - last.x0) < widest - EDGE_TOLERANCE
    ):
        return "justified"
    if len(block.align) == 1:
        return next(iter(block.align))
    if "center" in block.align and abs(first.center - page_width / 2) <= 2:
        return "center"
    for side in ("left", "center", "right"):
        if side in block.align:
            return side
    return "left"


def _runs_for(spans: list[Span], with_positions: bool) -> list[dict]:
    runs: list[dict] = []
    for s in spans:
        style = {"font": s.font, "size": s.size, "color": s.color}
        prev = runs[-1] if runs else None
        if prev and all(prev[k] == v for k, v in style.items()) and not with_positions:
            prev["text"] += s.text
            continue
        run = {"text": s.text, **style}
        if with_positions:
            run["x"] = s.origin[0]
        runs.append(run)
    return runs


def _merge_runs(runs: list[dict]) -> list[dict]:
    merged: list[dict] = []
    for run in runs:
        run = {k: v for k, v in run.items() if k != "x"}
        prev = merged[-1] if merged else None
        if prev and all(prev[k] == run[k] for k in ("font", "size", "color")):
            prev["text"] += run["text"]
        else:
            merged.append(run)
    return merged


def block_to_element(block: Block, element_id: str, page_width: float) -> dict:
    spans = [s for ln in block.lines for s in ln.spans]
    x0 = min(ln.x0 for ln in block.lines)
    x1 = max(ln.x1 for ln in block.lines)
    y0 = min(s.bbox[1] for s in spans)
    y1 = max(s.bbox[3] for s in spans)

    # Paragraph text: lines joined with spaces (Canva does not distinguish wraps from breaks).
    flat: list[dict] = []
    for i, ln in enumerate(block.lines):
        line_runs = _runs_for(ln.spans, with_positions=False)
        line_runs[0]["text"] = line_runs[0]["text"].lstrip()
        line_runs[-1]["text"] = line_runs[-1]["text"].rstrip()
        if i < len(block.lines) - 1 and not line_runs[-1]["text"].endswith("-"):
            line_runs[-1]["text"] += " "
        flat.extend(r for r in line_runs if r["text"])

    size = block.lines[0].size
    pitch = block.pitch
    letter_spacings = [s.letter_spacing for s in spans if len(s.text) > 1]
    return {
        "id": element_id,
        "type": "text",
        "bbox": [round(v, 2) for v in (x0, y0, x1, y1)],
        "rotation": 0,
        "opacity": 1,
        "align": infer_alignment(block, page_width),
        "lineHeight": round(pitch / size, 3) if pitch else 1.2,
        "letterSpacing": round(median(letter_spacings), 2) if letter_spacings else 0,
        "locked": False,
        "runs": _merge_runs(flat),
        "lines": [
            {
                "x": ln.spans[0].origin[0],
                "baseline": ln.baseline,
                "runs": _runs_for(ln.spans, with_positions=True),
            }
            for ln in block.lines
        ],  # fmt: skip
        "source": {"spanRects": [list(s.bbox) for s in spans if s.text.strip()]},
    }


def locked_line_element(line: Line, element_id: str) -> dict:
    """Rotated or vertical text: visible but not editable in v1."""
    spans = line.spans
    return {
        "id": element_id,
        "type": "text",
        "bbox": list(line.bbox),
        "rotation": 0,
        "opacity": 1,
        "align": "left",
        "lineHeight": 1.2,
        "letterSpacing": 0,
        "locked": True,
        "lockedReason": "rotated-text",
        "runs": _merge_runs(_runs_for(spans, with_positions=False)),
        "source": {"spanRects": [list(s.bbox) for s in spans]},
    }
