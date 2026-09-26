"""Font plans for text edits, including the fallback rules for fonts with no full file.

Order of preference for each font in an edited element:

1. A full font file from the fonts table or Google Fonts.
2. The embedded subsets of that font, merged across the whole PDF (Canva may split one font
   into several subsets on different pages). Subsets often lack a usable Unicode cmap, so one
   is rebuilt from the PDF's text trace (which records Unicode -> glyph id for every char).
3. If a needed glyph is still missing, the whole element switches to the closest available
   font (classification, weight, average character width) and gets a warning. Missing
   characters are never drawn alone in a different font unless the user explicitly chooses
   to keep the original font ("keep").
"""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

import pymupdf
from fontTools.pens.recordingPen import RecordingPen
from fontTools.ttLib import TTFont, newTable
from fontTools.ttLib.tables._c_m_a_p import CmapSubtable

from .fonts import FontRegistry, ResolvedFont, font_key, parse_postscript_name

SUBSTITUTE, KEEP = "substitute", "keep"
FALLBACK_CHOICES = [SUBSTITUTE, KEEP, "upload"]

SANS, SERIF, DISPLAY, MONO = "sans", "serif", "display", "mono"

GOOGLE_CATEGORIES = {
    "Sans Serif": SANS,
    "Serif": SERIF,
    "Display": DISPLAY,
    "Handwriting": DISPLAY,
    "Monospace": MONO,
}

# Fetched from Google Fonts when the fonts table has nothing in the right classification.
DEFAULT_POOL = {
    SANS: ["Open Sans", "Roboto", "Montserrat"],
    SERIF: ["Merriweather", "Lora"],
    DISPLAY: ["Oswald", "Bebas Neue"],
    MONO: ["Roboto Mono"],
}
_WEIGHT_NAMES = {
    100: "Thin", 200: "ExtraLight", 300: "Light", 400: "Regular", 500: "Medium",
    600: "SemiBold", 700: "Bold", 800: "ExtraBold", 900: "Black",
}  # fmt: skip


@dataclass
class FontProfile:
    classification: str | None  # None when unknown
    weight: int
    italic: bool


def classify_name(name: str) -> str | None:
    n = name.lower()
    # Not a bare "code": Canva's "Code" font is a display sans.
    if re.search(r"mono|source ?code|fira ?code|courier|consol", n):
        return MONO
    if "sans" in n or "grotesk" in n or "gothic" in n:
        return SANS
    if "serif" in n or any(k in n for k in ("times", "garamond", "georgia", "baskerville")):
        return SERIF
    if any(k in n for k in ("display", "script", "hand", "brush", "bebas", "oswald", "anton")):
        return DISPLAY
    return None


def _classify_by_glyphs(tt: TTFont) -> str | None:
    """Mono if "i" and "m" share an advance; serif if "I" is more than a plain bar."""
    cmap = tt.getBestCmap() or {}
    hmtx = tt["hmtx"] if "hmtx" in tt else None
    if hmtx and ord("i") in cmap and ord("m") in cmap:
        if hmtx[cmap[ord("i")]][0] == hmtx[cmap[ord("m")]][0]:
            return MONO
    for ch in "Il":
        ratio = _serif_ratio(tt, cmap.get(ord(ch)))
        if ratio is not None:
            return SERIF if ratio > 1.6 else SANS
    return None


def _serif_ratio(tt: TTFont, glyph_name: str | None) -> float | None:
    """Full width of a stem letter ("I", "l") divided by its stem width at mid-height.
    About 1 for sans (a plain bar), well above 1.6 when serifs stick out."""
    if glyph_name is None:
        return None
    pen = RecordingPen()
    tt.getGlyphSet()[glyph_name].draw(pen)
    contours, current = [], []
    for op, args in pen.value:
        if op == "moveTo":
            current = [args[0]]
        elif op in ("lineTo", "qCurveTo", "curveTo"):
            current.extend(args)  # control points approximate straight stems well enough
        elif op in ("closePath", "endPath") and current:
            contours.append(current)
            current = []
    points = [p for c in contours for p in c]
    if not points:
        return None
    ys = [p[1] for p in points]
    xs = [p[0] for p in points]
    mid = (min(ys) + max(ys)) / 2
    crossings = []
    for contour in contours:
        for (x0, y0), (x1, y1) in zip(contour, contour[1:] + contour[:1], strict=True):
            if (y0 - mid) * (y1 - mid) < 0:
                crossings.append(x0 + (mid - y0) * (x1 - x0) / (y1 - y0))
    if len(crossings) < 2:
        return None
    stem = max(crossings) - min(crossings)
    return (max(xs) - min(xs)) / stem if stem > 0 else None


def classify_font_data(data: bytes, name: str) -> str | None:
    hint = classify_name(name)
    if hint:
        return hint
    try:
        tt = TTFont(io.BytesIO(data), lazy=True)
        if "post" in tt and tt["post"].isFixedPitch:
            return MONO
        if "OS/2" in tt:
            family_class = tt["OS/2"].sFamilyClass >> 8
            if family_class in (1, 2, 3, 4, 5, 7):
                return SERIF
            if family_class == 8:
                return SANS
            if family_class in (9, 10):
                return DISPLAY
            serif_style = tt["OS/2"].panose.bSerifStyle
            if 2 <= serif_style <= 10:
                return SERIF
            if serif_style in (11, 12, 13):
                return SANS
        return _classify_by_glyphs(tt)
    except Exception:
        return None


def _ref(value: str) -> int | None:
    match = re.match(r"\[?\s*(\d+)\s+0\s+R", value)
    return int(match.group(1)) if match else None


def _descriptor_info(doc: pymupdf.Document, xref: int) -> tuple[int | None, int | None]:
    """(Flags, FontWeight) from the PDF font descriptor, following Type0 descendants."""
    kind, val = doc.xref_get_key(xref, "DescendantFonts")
    target = _ref(val) if kind in ("array", "xref") else None
    if kind == "array" and target is None:
        return None, None
    font_xref = target or xref
    kind, val = doc.xref_get_key(font_xref, "FontDescriptor")
    desc = _ref(val) if kind == "xref" else None
    if desc is None:
        return None, None
    flags = doc.xref_get_key(desc, "Flags")[1]
    weight = doc.xref_get_key(desc, "FontWeight")[1]
    return (
        int(flags) if flags.isdigit() else None,
        int(float(weight)) if re.fullmatch(r"[\d.]+", weight) else None,
    )


@dataclass
class Subset:
    xref: int
    font: pymupdf.Font
    coverage: set[str]


@dataclass
class EmbeddedFont:
    name: str
    subsets: list[Subset] = field(default_factory=list)
    profile: FontProfile | None = None

    @property
    def coverage(self) -> set[str]:
        return set().union(*(s.coverage for s in self.subsets)) if self.subsets else set()

    def font_for(self, ch: str) -> pymupdf.Font | None:
        for s in self.subsets:
            if ch in s.coverage:
                return s.font
        return None


def _rebuild_cmap(buffer: bytes, umap: dict[str, int]) -> bytes:
    tt = TTFont(io.BytesIO(buffer))
    order = tt.getGlyphOrder()
    sub = CmapSubtable.newSubtable(4)
    sub.platformID, sub.platEncID, sub.language = 3, 1, 0
    sub.cmap = {ord(ch): order[gid] for ch, gid in umap.items() if 0 < gid < len(order) and ord(ch) < 0x10000}
    table = newTable("cmap")
    table.tableVersion = 0
    table.tables = [sub]
    tt["cmap"] = table
    out = io.BytesIO()
    tt.save(out)
    return out.getvalue()


class EmbeddedFonts:
    """All embedded font programs in a document, grouped by (subset-prefix-free) name."""

    def __init__(self, doc: pymupdf.Document):
        self.doc = doc
        self._xrefs: dict[str, list[int]] = {}
        self._umaps: dict[int, dict[str, int]] = {}
        for page in doc:
            by_name: dict[str, list[int]] = {}
            for xref, ext, _t, basefont, *_ in page.get_fonts(full=True):
                if ext == "n/a":
                    continue
                key = font_key(basefont)
                if xref not in self._xrefs.setdefault(key, []):
                    self._xrefs[key].append(xref)
                by_name.setdefault(key, []).append(xref)
            for span in page.get_texttrace():
                xrefs = set(by_name.get(font_key(span["font"]), ()))
                if len(xrefs) != 1:
                    continue  # ambiguous: two subsets of one font on the same page
                umap = self._umaps.setdefault(xrefs.pop(), {})
                for uni, gid, *_ in span["chars"]:
                    if uni > 32 and gid > 0:
                        umap.setdefault(chr(uni), gid)
        self._cache: dict[str, EmbeddedFont] = {}

    def get(self, name: str) -> EmbeddedFont:
        key = font_key(name)
        if key in self._cache:
            return self._cache[key]
        ef = EmbeddedFont(name)
        flags = weight = None
        for xref in self._xrefs.get(key, []):
            _base, ext, _type, buffer = self.doc.extract_font(xref)
            if not buffer:
                continue
            umap = self._umaps.get(xref, {})
            if ext == "ttf" and umap:
                try:
                    buffer = _rebuild_cmap(buffer, umap)
                except Exception:
                    pass
            try:
                font = pymupdf.Font(fontbuffer=buffer)
            except Exception:
                continue
            chars = set(umap) or {chr(c) for c in font.valid_codepoints() if c > 32}
            coverage = {ch for ch in chars if font.has_glyph(ord(ch))}
            ef.subsets.append(Subset(xref, font, coverage))
            if flags is None:
                flags, weight = _descriptor_info(self.doc, xref)
        spec = parse_postscript_name(name)
        # Only a certain classification is recorded (name, or the PDF's FixedPitch/Serif/Script
        # flags; Canva just sets "symbolic"). Otherwise glyph comparison decides.
        classification = classify_name(name)
        if classification is None and flags:
            classification = MONO if flags & 1 else SERIF if flags & 2 else DISPLAY if flags & 8 else None
        ef.profile = FontProfile(classification, weight or spec.weight, spec.italic)
        self._cache[key] = ef
        return ef


def average_width(font: pymupdf.Font, chars: set[str]) -> float | None:
    usable = [ch for ch in chars if font.has_glyph(ord(ch))]
    if not usable:
        return None
    return sum(font.glyph_advance(ord(ch)) for ch in usable) / len(usable)


def _ink(font_for, chars: list[str], size: float = 36.0) -> bytes:
    """Grayscale render of chars, one per fixed cell on a shared baseline (0 = paper)."""
    cell = size * 1.2
    doc = pymupdf.open()
    page = doc.new_page(width=cell * len(chars), height=cell)
    writer = pymupdf.TextWriter(page.rect)
    for i, ch in enumerate(chars):
        font = font_for(ch)
        if font is not None:
            writer.append((i * cell + size * 0.1, size * 0.95), ch, font=font, fontsize=size)
    writer.write_text(page)
    pix = page.get_pixmap(colorspace=pymupdf.csGRAY, alpha=False)
    return bytes(255 - v for v in pix.samples)


def glyph_similarity(reference_font_for, candidate: pymupdf.Font, chars: list[str]) -> float:
    """Ink overlap (intersection over union) of the same characters in two fonts, 0..1."""
    a = _ink(reference_font_for, chars)
    b = _ink(lambda _ch: candidate, chars)
    inter = sum(min(x, y) for x, y in zip(a, b, strict=True))
    union = sum(max(x, y) for x, y in zip(a, b, strict=True))
    return inter / union if union else 0.0


# --- renderers ---------------------------------------------------------------------------------


class Renderer:
    """Chooses the font file for each character of one original font within one element."""

    def __init__(self, label: str, primary, secondary=None, fallback: pymupdf.Font | None = None):
        self.label = label  # font name the output will carry
        self._primary = primary  # pymupdf.Font or EmbeddedFont
        self._secondary = secondary  # EmbeddedFont used when a full font lacks a glyph
        self._fallback = fallback  # only used with the explicit "keep" choice

    def font_for(self, ch: str) -> pymupdf.Font:
        p = self._primary
        if isinstance(p, pymupdf.Font):
            if p.has_glyph(ord(ch)) or ch.isspace() or self._secondary is None:
                return p
            return self._secondary.font_for(ch) or p
        font = p.font_for(ch)
        if font is None and ch.isspace() and p.subsets:
            font = p.subsets[0].font
        return font or self._fallback or p.subsets[0].font

    @property
    def single_font(self) -> pymupdf.Font | None:
        return self._primary if isinstance(self._primary, pymupdf.Font) else None

    def text_length(self, text: str, fontsize: float) -> float:
        single = self.single_font
        if single is not None and self._secondary is None:
            return single.text_length(text, fontsize=fontsize)
        return sum(self.font_for(ch).text_length(ch, fontsize=fontsize) for ch in text)


class FontPlanner:
    def __init__(self, doc: pymupdf.Document, registry: FontRegistry, owner: str | None = None):
        self.registry = registry
        self.owner = owner
        self.embedded = EmbeddedFonts(doc)
        self._candidates: list[tuple[ResolvedFont, pymupdf.Font, FontProfile]] | None = None

    def full_font(self, name: str) -> ResolvedFont | None:
        return self.registry.resolve(name, self.owner)

    def _candidate_list(self):
        if self._candidates is None:
            self._candidates = []
            seen = set()
            for rf in self.registry.all_fonts(self.owner):
                if rf.object_key in seen:
                    continue
                seen.add(rf.object_key)
                self._candidates.append(self._candidate(rf))
        return self._candidates

    def _candidate(self, rf: ResolvedFont):
        entry = self.registry.google.entry(rf.family) if self.registry.google else None
        classification = GOOGLE_CATEGORIES.get(entry.category) if entry else None
        if classification is None:
            data = self.registry.path(rf).read_bytes()
            classification = classify_font_data(data, rf.postscript_name + " " + rf.family)
        profile = FontProfile(classification or SANS, rf.weight, rf.italic)
        return rf, self.registry.load(rf), profile

    def substitute_for(self, name: str, needed: set[str]) -> tuple[ResolvedFont, pymupdf.Font] | None:
        """Closest available font: weight, style, average character width and, above all, how
        much the letters actually look alike (classification counts only when certain)."""
        ef = self.embedded.get(name)
        profile = ef.profile or FontProfile(None, 400, False)
        sample = sorted(ch for ch in ef.coverage if ch.isalnum())[:24]
        ref_width = next((w for s in ef.subsets if (w := average_width(s.font, s.coverage))), None)

        def score(c) -> float:
            _rf, font, p = c
            value = abs(p.weight - profile.weight) / 100 + (0 if p.italic == profile.italic else 2)
            if profile.classification and p.classification != profile.classification:
                value += 5
            width = average_width(font, set(sample)) if sample else None
            if ref_width and width:
                value += 10 * abs(width / ref_width - 1)
            chars = [ch for ch in sample if font.has_glyph(ord(ch))]
            if chars:
                value += 10 * (1 - glyph_similarity(ef.font_for, font, chars))
            return value

        def covers(c) -> bool:
            return all(c[1].has_glyph(ord(ch)) for ch in needed if not ch.isspace())

        pool = [c for c in self._candidate_list() if covers(c)]
        wanted = [profile.classification] if profile.classification else [SANS, SERIF, DISPLAY]
        for classification in wanted:
            if not any(c[2].classification == classification for c in pool):
                fetched = self._fetch_pool_font(classification, profile)
                if fetched and covers(fetched):
                    pool.append(fetched)
        if not pool:
            return None
        best = min(pool, key=score)
        return best[0], best[1]

    def _fetch_pool_font(self, classification: str, profile: FontProfile):
        weight = min(_WEIGHT_NAMES, key=lambda w: abs(w - profile.weight))
        style = _WEIGHT_NAMES[weight] + ("Italic" if profile.italic else "")
        style = "Italic" if style == "RegularItalic" else style
        for family in DEFAULT_POOL[classification]:
            rf = self.registry.resolve(f"{family.replace(' ', '')}-{style}")
            if rf:
                self._candidates = None
                return self._candidate(rf)
        return None

    def plan(self, element_id: str, runs: list[dict], choice: str = SUBSTITUTE):
        """Renderers per original font name for one element, plus warnings for the UI."""
        chars: dict[str, set[str]] = {}
        for run in runs:
            chars.setdefault(run["font"], set()).update(run["text"])

        warnings: list[dict] = []
        full = {name: self.full_font(name) for name in chars}
        partial = [name for name in chars if full[name] is None]
        missing = {
            name: {ch for ch in chars[name] if not ch.isspace()} - self.embedded.get(name).coverage
            for name in partial
        }
        needs_fallback = {name: m for name, m in missing.items() if m}

        plan: dict[str, Renderer] = {}
        for name, rf in full.items():
            if rf is None:
                continue
            font = self.registry.load(rf)
            ef = self.embedded.get(name)
            plan[name] = Renderer(rf.postscript_name, font, secondary=ef)
            lacking = {
                ch for ch in chars[name]
                if not ch.isspace() and not font.has_glyph(ord(ch)) and ef.font_for(ch) is None
            }  # fmt: skip
            if lacking:
                warnings.append({
                    "id": element_id, "code": "glyphs-missing", "font": name,
                    "missing": sorted(lacking),
                })  # fmt: skip

        if not needs_fallback:
            for name in partial:
                plan[name] = Renderer(name, self.embedded.get(name))
            return plan, warnings

        substitutes: dict[str, str] = {}
        for name in partial:
            sub = self.substitute_for(name, chars[name])
            ef = self.embedded.get(name)
            if choice == KEEP:
                plan[name] = Renderer(name, ef, fallback=sub[1] if sub else None)
            elif sub is not None:
                plan[name] = Renderer(sub[0].postscript_name, sub[1])
                substitutes[name] = sub[0].postscript_name
            else:
                plan[name] = Renderer(name, ef)
        warnings.append({
            "id": element_id,
            "code": "font-mismatch-kept" if choice == KEEP else "font-substituted",
            "fonts": [
                {"font": name, "missing": sorted(needs_fallback.get(name, set())),
                 "substitute": substitutes.get(name)}
                for name in partial
            ],
            "choice": choice,
            "choices": FALLBACK_CHOICES,
        })  # fmt: skip
        if choice != KEEP and any(substitutes.get(n) is None for n in needs_fallback):
            warnings.append({"id": element_id, "code": "no-substitute-available"})
        return plan, warnings
