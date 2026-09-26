"""Milestone 1: page JSON is correct for the synthetic fixture."""

import pymupdf
import pytest

from pdfacil.pipeline.fonts import font_report
from pdfacil.pipeline.page import build_page


@pytest.fixture
def page_json(one_pager, registry):
    doc = pymupdf.open(stream=one_pager, filetype="pdf")
    return build_page(doc[0], font_report(doc, registry), "/bg.png")


def texts(page_json):
    return {"".join(r["text"] for r in e["runs"]): e for e in page_json["elements"] if e["type"] == "text"}


def test_page_geometry(page_json):
    assert (page_json["width"], page_json["height"]) == (612, 792)


def test_headline_is_centered_single_block(page_json):
    el = texts(page_json)["Fall Underwriting Packages"]
    assert el["align"] == "center"
    assert el["runs"][0]["font"] == "Montserrat Bold"
    assert el["runs"][0]["size"] == pytest.approx(30)
    assert el["runs"][0]["color"] == "#FFFFFF"
    assert el["fontStatus"] == "mapped"
    assert el["lines"][0]["baseline"] == pytest.approx(72, abs=0.01)


def test_letter_spacing_measured(page_json):
    el = texts(page_json)["RATE CARD"]
    assert el["letterSpacing"] == pytest.approx(4, abs=0.05)
    assert el["align"] == "center"


def test_mixed_runs_kept_in_one_block(page_json):
    el = texts(page_json)["Reach thousands of listeners every week."]
    assert [r["font"] for r in el["runs"]] == [
        "Montserrat Regular", "Montserrat Bold", "Montserrat Regular"
    ]  # fmt: skip


def test_body_paragraph_merged(page_json):
    body = next(e for t, e in texts(page_json).items() if t.startswith("Our fall packages"))
    assert len(body["lines"]) >= 5
    assert body["align"] == "left"
    assert body["lineHeight"] == pytest.approx(1.4, abs=0.01)
    assert "  " not in "".join(r["text"] for r in body["runs"])
    tight = next(e for t, e in texts(page_json).items() if t.startswith("Tight leading"))
    assert tight["lineHeight"] == pytest.approx(1.0, abs=0.01)


def test_centered_two_line_block(page_json):
    el = texts(page_json)["Spots from $25 per week Custom packages available"]
    assert el["align"] == "center"
    assert len(el["lines"]) == 2


def test_list_items_keep_hard_breaks(page_json):
    el = next(e for t, e in texts(page_json).items() if t.startswith("Morning drive"))
    text = "".join(r["text"] for r in el["runs"])
    assert text == (
        "Morning drive mentions\n"
        "Streaming pre-roll on the app and website for all twelve weeks\n"
        "Weekly reporting"
    )


def test_wrapped_paragraph_has_no_hard_breaks(page_json):
    body = next(e for t, e in texts(page_json).items() if t.startswith("Our fall packages"))
    assert "\n" not in "".join(r["text"] for r in body["runs"])


def test_centered_inside_card(page_json):
    assert texts(page_json)["Mon-Fri 6-10am"]["align"] == "center"
    assert texts(page_json)["Morning Drive"]["align"] == "left"


def test_rotated_text_locked(page_json):
    el = texts(page_json)["SIDEBAR"]
    assert el["locked"] and el["lockedReason"] == "rotated-text"


def test_images(page_json):
    images = [e for e in page_json["elements"] if e["type"] == "image"]
    assert sorted(tuple(round(v) for v in e["bbox"]) for e in images) == [
        (72, 420, 300, 580), (548, 14, 596, 62)
    ]  # fmt: skip


def test_shapes_background_and_outlined_text(page_json):
    shapes = [e for e in page_json["elements"] if e["type"] == "shape"]
    assert shapes[0].get("role") == "background" and shapes[0]["locked"]
    outlined = [s for s in shapes if s.get("outlinedText")]
    assert len(outlined) == 1 and outlined[0]["locked"]
    card = next(s for s in shapes if [round(v) for v in s["bbox"]] == [350, 420, 540, 580])
    assert not card["locked"]


def test_z_order(page_json):
    ids = [e["id"] for e in page_json["elements"]]
    by_text = {t: e["id"] for t, e in texts(page_json).items()}
    photo = next(e["id"] for e in page_json["elements"] if e["type"] == "image" and e["bbox"][0] == 72)
    assert ids.index(photo) < ids.index(by_text["Photo: Main Street"])
    assert ids[0] == page_json["elements"][0]["id"] and page_json["elements"][0]["role"] == "background"
