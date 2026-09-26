"""Font fallback when an edit needs glyphs the embedded subset does not have."""

import pymupdf
import pytest

from pdfacil.pipeline.edits import DocumentEditor
from tests.fixtures.synthetic import build_digits_doc, subset_font

UNKNOWN = "Zqxvern"  # not in the fonts table and not on Google Fonts


def only_text(editor, page=0):
    return next(e for e in editor.page_model(page).elements.values() if e["type"] == "text")


def export_spans(pdf: bytes, page=0):
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    return [
        s
        for b in doc[page].get_text("dict")["blocks"]
        for ln in b.get("lines", [])
        for s in ln["spans"]
        if s["text"].strip()
    ]


def edit(editor, text, **extra):
    el = only_text(editor)
    runs = [{**el["runs"][0], "text": text}]
    return el, editor.apply([{"op": "editText", "id": el["id"], "runs": runs, **extra}])


@pytest.fixture
def open_sans(font_files):
    return font_files["OpenSans-Regular"]


def test_40_to_50_uses_one_font_for_whole_element(open_sans, registry):
    pdf = build_digits_doc(open_sans, ["40"])
    editor = DocumentEditor(pdf, registry)
    assert only_text(editor)["runs"][0]["font"].startswith(UNKNOWN)

    _, result = edit(editor, "50")
    out = editor.export()

    spans = export_spans(out)
    assert "".join(s["text"] for s in spans) == "50"
    fonts = {s["font"].split("+")[-1] for s in spans}
    assert len(fonts) == 1, f"mixed fonts in one element: {fonts}"
    assert UNKNOWN not in fonts.pop()

    warning = next(w for w in result.warnings if w["code"] == "font-substituted")
    assert warning["choice"] == "substitute"
    assert warning["choices"] == ["substitute", "keep", "upload"]
    assert warning["fonts"][0]["missing"] == ["5"]
    assert warning["fonts"][0]["substitute"]


def test_substitute_matches_classification_and_weight(open_sans, registry):
    registry.resolve("Merriweather-Regular")  # a serif candidate that must lose
    registry.resolve("Montserrat-Bold")  # wrong weight
    registry.resolve("OpenSans-Regular")
    editor = DocumentEditor(build_digits_doc(open_sans, ["40"]), registry)
    _, result = edit(editor, "50")
    warning = next(w for w in result.warnings if w["code"] == "font-substituted")
    assert warning["fonts"][0]["substitute"] == "OpenSans-Regular"


def test_keep_original_accepts_mismatched_characters(open_sans, registry):
    editor = DocumentEditor(build_digits_doc(open_sans, ["40"]), registry)
    _, result = edit(editor, "50", fontFallback="keep")
    spans = export_spans(editor.export())
    assert "".join(s["text"] for s in spans) == "50"
    assert any(UNKNOWN in s["font"] for s in spans)
    assert result.warnings[0]["code"] == "font-mismatch-kept"


def test_subsets_merged_across_pages(open_sans, registry):
    # Page 0 embeds a subset with "4" and "0"; page 1 a separate subset with "5".
    editor = DocumentEditor(build_digits_doc(open_sans, ["40", "5"]), registry)
    _, result = edit(editor, "50")
    assert not [w for w in result.warnings if w["code"] != "text-overflow"]
    spans = export_spans(editor.export())
    assert "".join(s["text"] for s in spans) == "50"
    assert all(UNKNOWN in s["font"] for s in spans)


def test_uploaded_font_renders_real_font(open_sans, registry):
    pdf = build_digits_doc(open_sans, ["40"])
    full = subset_font(open_sans, "0123456789", f"{UNKNOWN}-Regular")
    registry.register_file(full, owner="u1")
    editor = DocumentEditor(pdf, registry, owner="u1")
    _, result = edit(editor, "50")
    assert not result.warnings
    spans = export_spans(editor.export())
    assert {s["font"].split("+")[-1] for s in spans} == {f"{UNKNOWN} Regular"}


def test_covered_edit_keeps_original_font(open_sans, registry):
    editor = DocumentEditor(build_digits_doc(open_sans, ["40"]), registry)
    _, result = edit(editor, "404")
    assert not result.warnings
    spans = export_spans(editor.export())
    assert all(UNKNOWN in s["font"] for s in spans)
