"""Stage 2: font mapping.

Resolves font names found in a PDF (for example ``ABCDEF+Montserrat-Bold``) to full font
files: first the ``fonts`` table, then the Google Fonts catalog (downloaded on first use and
stored in object storage). Unresolved names fall back to the subset embedded in the PDF.
"""

from __future__ import annotations

import hashlib
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pymupdf
from fontTools.ttLib import TTFont
from sqlalchemy import or_, select
from sqlalchemy.orm import sessionmaker

from ..db import Font
from ..storage import Storage

SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")

_WEIGHTS = {
    "thin": 100, "hairline": 100,
    "extralight": 200, "ultralight": 200,
    "light": 300,
    "": 400, "regular": 400, "normal": 400, "book": 400, "roman": 400,
    "medium": 500,
    "semibold": 600, "demibold": 600,
    "bold": 700,
    "extrabold": 800, "ultrabold": 800,
    "black": 900, "heavy": 900,
}  # fmt: skip


def strip_subset_prefix(name: str) -> str:
    return SUBSET_PREFIX.sub("", name)


def font_key(name: str) -> str:
    """Comparison key: PyMuPDF reports one font as "Open Sans Regular" in some APIs and
    "OpenSans-Regular" in others, with or without a subset prefix."""
    return re.sub(r"[\s\-_,]", "", strip_subset_prefix(name)).lower()


@dataclass(frozen=True)
class FontSpec:
    family: str
    weight: int = 400
    italic: bool = False


def split_camel(name: str) -> str:
    """``OpenSans`` -> ``Open Sans``, ``PTSans`` -> ``PT Sans``, ``DMSans`` -> ``DM Sans``."""
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", " ", name).strip()


def parse_postscript_name(name: str) -> FontSpec:
    """Guess family, weight and style from a PostScript name such as ``Montserrat-BoldItalic``."""
    name = re.sub(r"_\d+$", "", strip_subset_prefix(name))
    if "-" in name:
        family, style = name.split("-", 1)
    elif "," in name:
        family, style = name.split(",", 1)
    else:
        # "Montserrat Bold", "Open Sans SemiBold Italic": peel style words off the end.
        words = name.split()
        tail: list[str] = []
        while len(words) > 1 and (
            words[-1].lower() in _WEIGHTS or words[-1].lower() in ("italic", "oblique")
        ):
            tail.insert(0, words.pop())
        family, style = " ".join(words), "".join(tail)
    style = style.lower().replace(" ", "").replace("-", "")
    italic = "italic" in style or "oblique" in style
    style = style.replace("italic", "").replace("oblique", "")
    return FontSpec(split_camel(family), _WEIGHTS.get(style, 400), italic)


@dataclass(frozen=True)
class FontNames:
    postscript_name: str
    family: str
    weight: int
    italic: bool


def read_font_names(data: bytes) -> FontNames:
    tt = TTFont(io.BytesIO(data), lazy=True)
    names = tt["name"]
    ps = names.getDebugName(6) or ""
    family = names.getDebugName(16) or names.getDebugName(1) or ps
    os2 = tt["OS/2"] if "OS/2" in tt else None
    weight = os2.usWeightClass if os2 is not None else 400
    italic = bool(os2.fsSelection & 1) if os2 is not None else "italic" in ps.lower()
    return FontNames(ps, family, weight, italic)


class GoogleFonts:
    """Looks fonts up in the Google Fonts catalog and downloads static TTF files."""

    CSS_URL = "https://fonts.googleapis.com/css2"
    # An old user agent makes the CSS API return TrueType URLs instead of WOFF2.
    USER_AGENT = "Mozilla/4.0"

    def __init__(self, client: httpx.Client | None = None):
        self.client = client or httpx.Client(timeout=20, follow_redirects=True)

    def fetch(self, spec: FontSpec) -> bytes | None:
        axis = f"ital,wght@{int(spec.italic)},{spec.weight}"
        resp = self.client.get(
            self.CSS_URL,
            params={"family": f"{spec.family}:{axis}"},
            headers={"User-Agent": self.USER_AGENT},
        )
        if resp.status_code != 200:
            return None
        match = re.search(r"url\((https://[^)]+)\)", resp.text)
        if not match:
            return None
        font = self.client.get(match.group(1))
        font.raise_for_status()
        if font.content[:4] not in (b"\x00\x01\x00\x00", b"OTTO", b"true"):
            return None  # not a TrueType/OpenType file (e.g. WOFF2)
        return font.content


@dataclass(frozen=True)
class ResolvedFont:
    postscript_name: str
    family: str
    weight: int
    italic: bool
    object_key: str
    owner: str | None
    source: str


def _to_resolved(row: Font) -> ResolvedFont:
    return ResolvedFont(
        row.postscript_name, row.family, row.weight, row.style == "italic",
        row.object_key, row.owner, row.source,
    )  # fmt: skip


class FontRegistry:
    """The ``fonts`` table plus the font files in object storage."""

    def __init__(
        self,
        session_factory: sessionmaker,
        storage: Storage,
        google: GoogleFonts | None = None,
    ):
        self.session_factory = session_factory
        self.storage = storage
        self.google = google
        self._google_misses: set[str] = set()
        self._loaded: dict[str, pymupdf.Font] = {}

    def lookup(self, name: str, owner: str | None = None) -> ResolvedFont | None:
        with self.session_factory() as db:
            rows = db.scalars(
                select(Font).where(
                    Font.lookup_key == font_key(name),
                    or_(Font.owner.is_(None), Font.owner == owner) if owner else Font.owner.is_(None),
                )
            ).all()
        # A user's own font wins over a shared one with the same name.
        rows = sorted(rows, key=lambda r: r.owner is None)
        return _to_resolved(rows[0]) if rows else None

    def all_fonts(self, owner: str | None = None) -> list[ResolvedFont]:
        with self.session_factory() as db:
            cond = or_(Font.owner.is_(None), Font.owner == owner) if owner else Font.owner.is_(None)
            return [_to_resolved(r) for r in db.scalars(select(Font).where(cond)).all()]

    def resolve(self, name: str, owner: str | None = None) -> ResolvedFont | None:
        """Look a name up, fetching it from Google Fonts if it is not known yet."""
        name = strip_subset_prefix(name)
        found = self.lookup(name, owner)
        if found or self.google is None or font_key(name) in self._google_misses:
            return found
        spec = parse_postscript_name(name)
        try:
            data = self.google.fetch(spec)
        except httpx.HTTPError:
            data = None
        if data is None:
            self._google_misses.add(font_key(name))
            return None
        font = self.register_file(data, owner=None, source="google", license_note="Google Fonts")
        if font_key(font.postscript_name) != font_key(name):
            font = self.add_alias(name, font, owner=None)
        return font

    def register_file(
        self,
        data: bytes,
        owner: str | None,
        source: str = "upload",
        license_note: str | None = None,
    ) -> ResolvedFont:
        names = read_font_names(data)
        ext = "otf" if data[:4] == b"OTTO" else "ttf"
        digest = hashlib.sha256(data).hexdigest()[:20]
        key = f"fonts/{owner or 'shared'}/{digest}.{ext}"
        if not self.storage.exists(key):
            self.storage.put(key, data)
        existing = self.lookup(names.postscript_name, owner)
        if existing and existing.owner == owner and existing.object_key == key:
            return existing
        row = Font(
            postscript_name=names.postscript_name,
            lookup_key=font_key(names.postscript_name),
            family=names.family,
            weight=names.weight,
            style="italic" if names.italic else "normal",
            license_note=license_note,
            owner=owner,
            object_key=key,
            source=source,
        )
        with self.session_factory() as db:
            db.add(row)
            db.commit()
        return _to_resolved(row)

    def add_alias(self, name: str, target: ResolvedFont, owner: str | None) -> ResolvedFont:
        """Make ``name`` resolve to ``target``'s file (used for manual font mapping)."""
        row = Font(
            postscript_name=strip_subset_prefix(name),
            lookup_key=font_key(name),
            family=target.family,
            weight=target.weight,
            style="italic" if target.italic else "normal",
            owner=owner,
            object_key=target.object_key,
            source="alias",
        )
        with self.session_factory() as db:
            db.add(row)
            db.commit()
        return _to_resolved(row)

    def path(self, font: ResolvedFont) -> Path:
        return self.storage.local_path(font.object_key)

    def load(self, font: ResolvedFont) -> pymupdf.Font:
        cached = self._loaded.get(font.object_key)
        if cached is None:
            cached = pymupdf.Font(fontfile=str(self.path(font)))
            self._loaded[font.object_key] = cached
        return cached


# --- per-document font report ----------------------------------------------------------------

MAPPED, SUBSET_ONLY, MISSING = "mapped", "subset-only", "missing"
_STATUS_RANK = {MAPPED: 0, SUBSET_ONLY: 1, MISSING: 2}


def worst_status(statuses: list[str]) -> str:
    return max(statuses, key=_STATUS_RANK.__getitem__, default=MAPPED)


@dataclass
class DocFont:
    name: str
    status: str = MISSING
    pages: list[int] = field(default_factory=list)
    embedded_xref: int | None = None
    resolved: ResolvedFont | None = None

    def to_json(self) -> dict:
        return {
            "name": self.name,
            "status": self.status,
            "pages": self.pages,
            "mappedTo": self.resolved.postscript_name if self.resolved else None,
            "source": self.resolved.source if self.resolved else None,
        }


def font_report(
    doc: pymupdf.Document, registry: FontRegistry, owner: str | None = None
) -> dict[str, DocFont]:
    """Every font used in the document with its mapping status."""
    fonts: dict[str, DocFont] = {}
    for page in doc:
        for xref, ext, _type, basefont, *_ in page.get_fonts(full=True):
            name = strip_subset_prefix(basefont)
            entry = fonts.setdefault(name, DocFont(name))
            if page.number not in entry.pages:
                entry.pages.append(page.number)
            if ext != "n/a" and entry.embedded_xref is None:
                entry.embedded_xref = xref
    for entry in fonts.values():
        entry.resolved = registry.resolve(entry.name, owner)
        if entry.resolved:
            entry.status = MAPPED
        elif entry.embedded_xref is not None:
            entry.status = SUBSET_ONLY
        else:
            entry.status = MISSING
    return fonts


def missing_glyphs(font: pymupdf.Font, text: str) -> set[str]:
    return {ch for ch in set(text) if not ch.isspace() and not font.has_glyph(ord(ch))}
