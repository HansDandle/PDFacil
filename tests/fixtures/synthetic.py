"""Synthetic Canva-like PDFs for the public test suite.

Real Canva exports contain client data and live in a private fixture set. These fixtures mimic
their structure: subset-embedded fonts, one text line per span, tracking via per-glyph
positions, mixed runs, images with alpha, text over images and shapes, outlined glyph text,
and rotated text.
"""

from __future__ import annotations

import io
from pathlib import Path

import pymupdf
from fontTools.pens.basePen import BasePen
from fontTools.subset import Options, Subsetter
from fontTools.ttLib import TTFont

from pdfacil.layout import layout_text

FONT_NAMES = ["Montserrat-Bold", "Montserrat-Regular", "OpenSans-Regular"]

ORANGE = (0.894, 0.341, 0.180)
DARK = (0.102, 0.102, 0.102)
WHITE = (1, 1, 1)
CREAM = (1.0, 0.973, 0.933)

BODY = (
    "Our fall packages combine on-air spots, streaming audio and event sponsorships so your "
    "message reaches listeners at home, at work and on the road. Every schedule includes a "
    "dedicated account manager and weekly reporting."
)
TIGHT = (
    "Tight leading test paragraph set solid so neighbouring lines nearly touch and redaction "
    "must not clip the line above or below the edited one."
)


def _hex(rgb) -> str:
    return "#" + "".join(f"{round(v * 255):02X}" for v in rgb)


def _write(page, x, y, text, font, size, color, tracking=0.0):
    tw = pymupdf.TextWriter(page.rect)
    if tracking:
        for ch in text:
            tw.append((x, y), ch, font=font, fontsize=size)
            x += font.text_length(ch, fontsize=size) + tracking
    else:
        tw.append((x, y), text, font=font, fontsize=size)
    tw.write_text(page, color=color)


def _centered_x(text, font, size, center, tracking=0.0):
    width = font.text_length(text, fontsize=size) + tracking * len(text)
    return center - width / 2


class _ShapePen(BasePen):
    """Draws glyph outlines onto a PyMuPDF Shape (font units -> page points)."""

    def __init__(self, glyphset, shape, x, y, scale):
        super().__init__(glyphset)
        self.shape, self.x, self.y, self.scale = shape, x, y, scale
        self.start = None

    def _pt(self, p):
        return pymupdf.Point(self.x + p[0] * self.scale, self.y - p[1] * self.scale)

    def _moveTo(self, p):
        self.start = self.current = self._pt(p)

    def _lineTo(self, p):
        q = self._pt(p)
        self.shape.draw_line(self.current, q)
        self.current = q

    def _curveToOne(self, p1, p2, p3):
        q = self._pt(p3)
        self.shape.draw_bezier(self.current, self._pt(p1), self._pt(p2), q)
        self.current = q

    def _qCurveToOne(self, p1, p2):
        c0 = self.current
        c1, c3 = self._pt(p1), self._pt(p2)
        a = c0 + (c1 - c0) * (2 / 3)
        b = c3 + (c1 - c3) * (2 / 3)
        self.shape.draw_bezier(c0, a, b, c3)
        self.current = c3

    def _closePath(self):
        if self.start is not None and self.current != self.start:
            self.shape.draw_line(self.current, self.start)
        self.current = self.start


def draw_outlined_text(page, text, font_path: Path, x, baseline, size, color):
    tt = TTFont(str(font_path))
    glyphs, cmap = tt.getGlyphSet(), tt.getBestCmap()
    scale = size / tt["head"].unitsPerEm
    for ch in text:
        name = cmap[ord(ch)]
        shape = page.new_shape()
        glyphs[name].draw(_ShapePen(glyphs, shape, x, baseline, scale))
        shape.finish(fill=color, color=None, even_odd=True, closePath=True)
        shape.commit()
        x += glyphs[name].width * scale + size * 0.04


def _gradient(width, height, alpha=False) -> pymupdf.Pixmap:
    channels = 4 if alpha else 3
    data = bytearray(width * height * channels)
    for y in range(height):
        for x in range(width):
            i = (y * width + x) * channels
            data[i] = int(40 + 200 * x / width)
            data[i + 1] = int(90 + 120 * y / height)
            data[i + 2] = 170
            if alpha:
                dx, dy = x - width / 2, y - height / 2
                data[i + 3] = 255 if dx * dx + dy * dy < (width / 2) ** 2 else 0
    return pymupdf.Pixmap(pymupdf.csRGB, width, height, bytes(data), alpha)


def build_one_pager(fonts: dict[str, Path]) -> bytes:
    F = {name: pymupdf.Font(fontfile=str(path)) for name, path in fonts.items()}
    bold, regular, body = F["Montserrat-Bold"], F["Montserrat-Regular"], F["OpenSans-Regular"]

    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.draw_rect(page.rect, color=None, fill=CREAM)
    page.draw_rect(pymupdf.Rect(0, 0, 612, 130), color=None, fill=ORANGE)

    # Headline on a colored band, centered on the page.
    headline = "Fall Underwriting Packages"
    _write(page, _centered_x(headline, bold, 30, 306), 72, headline, bold, 30, WHITE)
    # Letter-spaced label.
    label = "RATE CARD"
    _write(page, _centered_x(label, regular, 12, 306, 4), 104, label, regular, 12, WHITE, 4)

    # Subhead with one bold word.
    x = 72
    for text, font in (("Reach ", regular), ("thousands", bold), (" of listeners every week.", regular)):
        _write(page, x, 180, text, font, 16, DARK)
        x += font.text_length(text, fontsize=16)

    # Body paragraph, left aligned, one span per line like Canva.
    runs = [{"text": BODY, "font": "OpenSans-Regular", "size": 11, "color": _hex(DARK)}]
    for line in layout_text(runs, x0=72, x1=320, first_baseline=220, line_height=1.4,
                            fonts=F.__getitem__):  # fmt: skip
        _write(page, line["x"], line["baseline"], line["runs"][0]["text"], body, 11, DARK)

    # Tight paragraph (line height 1.0) in the right column.
    runs = [{"text": TIGHT, "font": "OpenSans-Regular", "size": 10, "color": _hex(DARK)}]
    for line in layout_text(runs, x0=350, x1=540, first_baseline=220, line_height=1.0,
                            fonts=F.__getitem__):  # fmt: skip
        _write(page, line["x"], line["baseline"], line["runs"][0]["text"], body, 10, DARK)

    # Centered two-line block.
    for i, text in enumerate(("Spots from $25 per week", "Custom packages available")):
        _write(page, _centered_x(text, regular, 14, 306), 340 + i * 20, text, regular, 14, ORANGE)

    # Photo with a caption over it.
    page.insert_image(pymupdf.Rect(72, 420, 300, 580), pixmap=_gradient(228, 160))
    _write(page, 80, 570, "Photo: Main Street", body, 10, WHITE)
    # Transparent logo (alpha channel) on the header band.
    page.insert_image(pymupdf.Rect(548, 14, 596, 62), pixmap=_gradient(80, 80, alpha=True))

    # A card shape with text on it, and a circle.
    page.draw_rect(pymupdf.Rect(350, 420, 540, 580), color=ORANGE, fill=WHITE, width=2, radius=0.08)
    _write(page, 366, 450, "Morning Drive", bold, 14, DARK)
    page.draw_circle((390, 630), 30, color=None, fill=ORANGE)

    # Headline exported as outlines (no text layer).
    draw_outlined_text(page, "SALE", fonts["Montserrat-Bold"], 440, 720, 36, ORANGE)

    # Rotated text along the left edge.
    tw = pymupdf.TextWriter(page.rect)
    tw.append((40, 700), "SIDEBAR", font=regular, fontsize=10)
    tw.write_text(page, color=DARK, morph=(pymupdf.Point(40, 700), pymupdf.Matrix(-90)))

    # Right-aligned footer.
    footer = "KXYZ 101.5 FM Austin"
    _write(page, 540 - body.text_length(footer, fontsize=9), 760, footer, body, 9, DARK)

    doc.set_metadata({"producer": "Canva", "creator": "Canva", "title": "Synthetic one-pager"})
    doc.subset_fonts()
    return doc.tobytes(garbage=3, deflate=True)


def subset_font(path: Path, chars: str, rename: str) -> bytes:
    """A subset of a font containing only ``chars``, renamed so no catalog can resolve it."""
    tt = TTFont(str(path))
    options = Options()
    options.name_IDs = ["*"]
    subsetter = Subsetter(options)
    subsetter.populate(text=chars)
    subsetter.subset(tt)
    family = rename.split("-")[0]
    for record in tt["name"].names:
        if record.nameID in (1, 16):
            record.string = family
        elif record.nameID in (4,):
            record.string = rename.replace("-", " ")
        elif record.nameID == 6:
            record.string = rename
    out = io.BytesIO()
    tt.save(out)
    return out.getvalue()


def build_digits_doc(font_path: Path, pages: list[str], rename="Zqxvern-Regular") -> bytes:
    """One page per string, each page embedding its own subset of the same (unknown) font."""
    doc = pymupdf.open()
    for i, text in enumerate(pages):
        page = doc.new_page(width=300, height=200)
        font = pymupdf.Font(fontbuffer=subset_font(font_path, text, rename))
        _write(page, 60 + i, 100, text, font, 48, DARK)
    doc.set_metadata({"producer": "Canva"})
    return doc.tobytes(garbage=3, deflate=True)
