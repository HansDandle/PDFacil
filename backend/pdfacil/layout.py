"""Reference text layout: wraps runs into lines with baselines.

The browser editor is the source of truth for line breaks in interactive editing. This module
lays out text on the server when there is no editor (API scripts, template batch fills) and
mirrors the editor's rules: greedy word wrap inside the box width, line pitch = lineHeight x
largest font size in the line, no kerning.
"""

from __future__ import annotations

import re
from collections.abc import Callable

import pymupdf

FontLoader = Callable[[str], pymupdf.Font]


def _pieces(runs: list[dict]) -> list[dict]:
    """Split runs into words (each keeping its trailing space) and hard breaks."""
    out = []
    for run in runs:
        for part in re.split(r"(\n)", run["text"]):
            if part == "\n":
                out.append({"break": True})
                continue
            for word in re.findall(r"\S+\s*|\s+", part):
                out.append({**run, "text": word})
    return out


def _width(pieces: list[dict], fonts: FontLoader, letter_spacing: float, trim: bool) -> float:
    total = 0.0
    for i, p in enumerate(pieces):
        text = p["text"].rstrip() if trim and i == len(pieces) - 1 else p["text"]
        total += fonts(p["font"]).text_length(text, fontsize=p["size"]) + letter_spacing * len(text)
    return total


def _merge(pieces: list[dict]) -> list[dict]:
    runs: list[dict] = []
    for p in pieces:
        if runs and all(runs[-1][k] == p.get(k) for k in ("font", "size", "color")):
            runs[-1]["text"] += p["text"]
        else:
            runs.append({k: p[k] for k in ("text", "font", "size", "color")})
    return runs


def layout_text(
    runs: list[dict],
    *,
    x0: float,
    x1: float,
    first_baseline: float,
    align: str = "left",
    line_height: float = 1.2,
    letter_spacing: float = 0.0,
    fonts: FontLoader,
    list_items: bool = False,
) -> list[dict]:
    """Lines with x/baseline/runs. With ``list_items``, the first line of every paragraph
    (each item of a list) is flagged ``listItem`` so the export draws its marker."""
    width = x1 - x0
    lines: list[list[dict]] = [[]]
    hard_end: list[bool] = []
    for piece in _pieces(runs):
        if piece.get("break"):
            hard_end.append(True)
            lines.append([])
            continue
        current = lines[-1]
        # Half a point of slack: extracted boxes come from ink extents, which can be a hair
        # narrower than the advance widths measured here.
        if current and _width([*current, piece], fonts, letter_spacing, trim=True) > width + 0.5:
            hard_end.append(False)
            lines.append([piece])
        else:
            current.append(piece)
    hard_end.append(True)

    out = []
    baseline = first_baseline
    for i, pieces in enumerate(lines):
        pieces = [p for p in pieces if p["text"]] or [{**runs[-1], "text": ""}]
        pieces[-1] = {**pieces[-1], "text": pieces[-1]["text"].rstrip()}
        size = max(p["size"] for p in pieces)
        if i > 0:
            baseline += line_height * size
        line_width = _width(pieces, fonts, letter_spacing, trim=True)
        word_spacing = 0.0
        if align == "center":
            x = x0 + (width - line_width) / 2
        elif align == "right":
            x = x1 - line_width
        else:
            x = x0
            spaces = sum(p["text"].count(" ") for p in pieces)
            if align == "justified" and not hard_end[i] and spaces:
                word_spacing = (width - line_width) / spaces
        line = {"x": round(x, 3), "baseline": round(baseline, 3), "runs": _merge(pieces)}
        if word_spacing:
            line["wordSpacing"] = round(word_spacing, 4)
        if list_items and (i == 0 or hard_end[i - 1]):
            line["listItem"] = True
        out.append(line)
    return out
