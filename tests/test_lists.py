"""Lists behave like word-processor lists: markers follow their items through edits."""

import pymupdf
import pytest

from pdfacil.pipeline.edits import DocumentEditor
from pdfacil.pipeline.fonts import font_report
from pdfacil.pipeline.page import build_page


@pytest.fixture
def page_json(one_pager, registry):
    doc = pymupdf.open(stream=one_pager, filetype="pdf")
    return build_page(doc[0], font_report(doc, registry), "/bg.png")


def element(page_or_editor, prefix):
    elements = (
        page_or_editor["elements"]
        if isinstance(page_or_editor, dict)
        else page_or_editor.page_model(0).elements.values()
    )
    return next(
        e for e in elements
        if e["type"] == "text" and "".join(r["text"] for r in e["runs"]).startswith(prefix)
    )  # fmt: skip


def bullet_dots(pdf: bytes, region):
    """Small filled dark shapes in the region (the shape-bullets of the synthetic list)."""
    page = pymupdf.open(stream=pdf, filetype="pdf")[0]
    r = pymupdf.Rect(region)
    return sorted(
        (d["rect"] for d in page.get_drawings()
         if d.get("fill") and d["rect"].width < 6 and r.contains(d["rect"])),
        key=lambda rect: rect.y0,
    )  # fmt: skip


def lines_in(pdf: bytes, region):
    page = pymupdf.open(stream=pdf, filetype="pdf")[0]
    out = []
    for b in page.get_text("dict", clip=pymupdf.Rect(region))["blocks"]:
        for ln in b.get("lines", []):
            text = "".join(s["text"] for s in ln["spans"]).strip()
            if text:
                out.append((round(ln["spans"][0]["origin"][1], 1), round(ln["bbox"][0], 1), text))
    return sorted(out)


def test_shape_bullets_become_part_of_the_list(page_json):
    el = element(page_json, "Morning drive")
    assert el["list"]["type"] == "bullet" and el["list"]["marker"] == "shape"
    assert [ln.get("listItem", False) for ln in el["lines"]] == [True, True, False, True]
    # The dots are no longer separate shapes on the page.
    shapes = [e for e in page_json["elements"] if e["type"] == "shape"]
    assert not any(s["bbox"][0] > 350 and s["bbox"][2] < 360 and 270 < s["bbox"][1] < 320 for s in shapes)


def test_glyph_and_number_markers_leave_the_text(page_json):
    glyph = element(page_json, "Free remote")
    assert glyph["list"]["marker"] == "glyph" and glyph["list"]["glyph"]["text"] == "•"
    assert "".join(r["text"] for r in glyph["runs"]) == "Free remote broadcast\nLogo on the event banner"
    numbered = element(page_json, "Sign the")
    assert numbered["list"]["type"] == "number" and numbered["list"]["number"]["start"] == 1
    assert "".join(r["text"] for r in numbered["runs"]).split("\n") == [
        "Sign the agreement", "Send your logo", "Approve the copy"
    ]  # fmt: skip


def test_adding_an_item_adds_a_bullet_at_its_line(one_pager, registry):
    editor = DocumentEditor(one_pager, registry)
    el = element(editor, "Morning drive")
    text = "Morning drive mentions\nEvening drive mentions\nStreaming pre-roll on the app and website for all twelve weeks\nWeekly reporting"
    editor.apply([{"op": "editText", "id": el["id"], "runs": [{**el["runs"][0], "text": text}]}])
    out = editor.export()

    dots = bullet_dots(out, (345, 260, 365, 360))
    lines = [ln for ln in lines_in(out, (358, 265, 545, 338)) if ln[1] >= 361]
    assert len(dots) == 4 and len(lines) == 5
    # Every bullet sits at its item's first line, at the original offset (centre 3.5 pt above
    # the baseline); the one line without a bullet is the wrapped continuation.
    by_baseline = {round(b - 3.5, 1): (b, x, t) for b, x, t in lines}
    for dot in dots:
        assert round((dot.y0 + dot.y1) / 2, 1) in by_baseline
    bulleted = {round(b - 3.5, 1) for b in (ln[0] for ln in lines)} & {
        round((d.y0 + d.y1) / 2, 1) for d in dots
    }
    continuation = [ln for ln in lines if round(ln[0] - 3.5, 1) not in bulleted]
    assert len(continuation) == 1
    assert continuation[0][1] == pytest.approx(362, abs=0.5)  # hanging indent


def test_removing_items_removes_bullets(one_pager, registry):
    editor = DocumentEditor(one_pager, registry)
    el = element(editor, "Morning drive")
    editor.apply([{"op": "editText", "id": el["id"], "runs": [{**el["runs"][0], "text": "Only item"}]}])
    out = editor.export()
    assert len(bullet_dots(out, (345, 260, 365, 360))) == 1


def test_moving_a_list_moves_its_bullets(one_pager, registry):
    editor = DocumentEditor(one_pager, registry)
    el = element(editor, "Morning drive")
    x0, y0, x1, y1 = el["bbox"]
    editor.apply([{"op": "transform", "id": el["id"], "bbox": [x0, y0 + 40, x1, y1 + 40]}])
    out = editor.export()
    assert bullet_dots(out, (345, 260, 365, 300)) == []
    assert len(bullet_dots(out, (345, 300, 365, 400))) == 3


def test_glyph_list_edit_redraws_glyphs(one_pager, registry):
    editor = DocumentEditor(one_pager, registry)
    el = element(editor, "Free remote")
    text = "Free remote broadcast\nLogo on the event banner\nTwo tickets"
    editor.apply([{"op": "editText", "id": el["id"], "runs": [{**el["runs"][0], "text": text}]}])
    lines = lines_in(editor.export(), (60, 600, 330, 646))
    texts = [t for _, _, t in lines]
    assert texts.count("•") == 3
    assert [t for t in texts if t != "•"] == [
        "Free remote broadcast", "Logo on the event banner", "Two tickets"
    ]  # fmt: skip
    glyph_x = {x for _, x, t in lines if t == "•"}
    assert glyph_x == {72.0}


def test_numbered_list_renumbers(one_pager, registry):
    editor = DocumentEditor(one_pager, registry)
    el = element(editor, "Sign the")
    text = "Sign the agreement\nApprove the copy"
    editor.apply([{"op": "editText", "id": el["id"], "runs": [{**el["runs"][0], "text": text}]}])
    joined = " ".join(t for _, _, t in lines_in(editor.export(), (60, 640, 330, 700)))
    assert "1." in joined and "2." in joined and "3." not in joined
    assert joined.index("1.") < joined.index("Sign") < joined.index("2.") < joined.index("Approve")


def test_background_png_omits_list_bullets(one_pager, registry):
    editor = DocumentEditor(one_pager, registry)
    png = editor.background_png(0, scale=2)
    pix = pymupdf.Pixmap(png)
    # Where the first shape bullet was (centre 355, 276.5) is now background.
    r, g, b = pix.pixel(int(355 * 2), int(276.5 * 2))[:3]
    assert min(r, g, b) > 200
