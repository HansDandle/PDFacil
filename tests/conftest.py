from pathlib import Path

import numpy as np
import pymupdf
import pytest

from pdfacil.db import make_engine, make_session_factory
from pdfacil.pipeline.fonts import FontRegistry, FontSpec, GoogleFonts, parse_postscript_name
from pdfacil.storage import LocalStorage
from tests.fixtures.synthetic import FONT_NAMES, build_one_pager

CACHE = Path(__file__).resolve().parent.parent / ".cache" / "fonts"


class CachedGoogleFonts(GoogleFonts):
    """Google Fonts with an on-disk cache so the suite downloads each font once."""

    def fetch(self, spec: FontSpec) -> bytes | None:
        key = CACHE / f"{spec.family.replace(' ', '_')}-{spec.weight}-{int(spec.italic)}.ttf"
        miss = key.with_suffix(".miss")
        if key.exists():
            return key.read_bytes()
        if miss.exists():
            return None
        data = super().fetch(spec)
        CACHE.mkdir(parents=True, exist_ok=True)
        if data is None:
            miss.touch()
        else:
            key.write_bytes(data)
        return data


@pytest.fixture(scope="session")
def google() -> CachedGoogleFonts:
    return CachedGoogleFonts()


@pytest.fixture(scope="session")
def font_files(google) -> dict[str, Path]:
    paths = {}
    for name in FONT_NAMES:
        spec = parse_postscript_name(name)
        assert google.fetch(spec), f"could not download {name}"
        paths[name] = CACHE / f"{spec.family.replace(' ', '_')}-{spec.weight}-{int(spec.italic)}.ttf"
    return paths


@pytest.fixture
def registry(tmp_path, google) -> FontRegistry:
    engine = make_engine(f"sqlite:///{tmp_path / 'test.sqlite3'}")
    return FontRegistry(make_session_factory(engine), LocalStorage(tmp_path / "objects"), google)


@pytest.fixture(scope="session")
def one_pager(font_files) -> bytes:
    return build_one_pager(font_files)


def render(pdf: bytes, page: int = 0, scale: float = 2.0) -> np.ndarray:
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    pix = doc[page].get_pixmap(matrix=pymupdf.Matrix(scale, scale), alpha=False)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)


def diff_mask(a: np.ndarray, b: np.ndarray, threshold: int = 40) -> np.ndarray:
    return np.abs(a.astype(int) - b.astype(int)).max(axis=2) > threshold


def changed_outside(a, b, rects, scale=2.0, pad=2.0) -> int:
    """Number of pixels that differ outside the given rects (in points)."""
    mask = diff_mask(a, b)
    for x0, y0, x1, y1 in rects:
        mask[
            max(0, int((y0 - pad) * scale)) : int((y1 + pad) * scale) + 1,
            max(0, int((x0 - pad) * scale)) : int((x1 + pad) * scale) + 1,
        ] = False
    return int(mask.sum())
