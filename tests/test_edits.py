"""Milestone 2: text edits and export through the pipeline (no UI)."""

import pymupdf
import pytest

from pdfacil.pipeline.edits import DocumentEditor, OpError
from tests.conftest import changed_outside, diff_mask, render


def font_key(name: str) -> str:
    return name.split("+")[-1].replace(" ", "").replace("-", "").lower()


def elements(editor: DocumentEditor, page: int = 0) -> dict[str, dict]:
    return editor.page_model(page).elements


def find_text(editor, prefix):
    return next(
        e for e in elements(editor).values()
        if e["type"] == "text" and "".join(r["text"] for r in e["runs"]).startswith(prefix)
    )  # fmt: skip


def spans_in(pdf: bytes, rect, page=0):
    doc = pymupdf.open(stream=pdf, filetype="pdf")
    out = []
    for block in doc[page].get_text("dict", clip=pymupdf.Rect(rect))["blocks"]:
        for line in block.get("lines", []):
            out.extend(s for s in line["spans"] if s["text"].strip())
    return out


def test_round_trip_without_edits(one_pager, registry):
    editor = DocumentEditor(one_pager, registry)
    editor.apply([])
    out = editor.export()
    assert diff_mask(render(one_pager), render(out)).sum() == 0


def test_headline_word_change(one_pager, registry):
    editor = DocumentEditor(one_pager, registry)
    head = find_text(editor, "Fall Underwriting")
    runs = [{**head["runs"][0], "text": "Fall Sponsorship Packages"}]
    result = editor.apply([{"op": "editText", "id": head["id"], "runs": runs}])
    out = editor.export()

    assert not [w for w in result.warnings if w["code"] != "text-overflow"]
    spans = spans_in(out, (0, 40, 612, 82))
    assert "".join(s["text"] for s in spans) == "Fall Sponsorship Packages"
    # Real full font, not a fallback like Helvetica.
    assert {font_key(s["font"]) for s in spans} == {"montserratbold"}
    # Still centered, on the original baseline.
    line = spans[0]
    assert line["origin"][1] == pytest.approx(72, abs=0.5)
    center = (min(s["bbox"][0] for s in spans) + max(s["bbox"][2] for s in spans)) / 2
    assert center == pytest.approx(306, abs=1.5)
    # Nothing outside the headline changed (the band and logo underneath are intact).
    before, after = render(one_pager), render(out)
    grow = [min(head["bbox"][0], min(s["bbox"][0] for s in spans)), head["bbox"][1],
            max(head["bbox"][2], max(s["bbox"][2] for s in spans)), head["bbox"][3]]  # fmt: skip
    assert changed_outside(before, after, [grow]) == 0


def test_saved_lines_are_placed_exactly(one_pager, registry):
    """Export places each saved line at its saved baseline within 0.5 pt."""
    editor = DocumentEditor(one_pager, registry)
    body = find_text(editor, "Our fall packages")
    lines = [
        {"x": 72, "baseline": 230 + i * 18, "runs": [{**body["runs"][0], "text": t}]}
        for i, t in enumerate(["First saved line", "Second one", "Third"])
    ]
    editor.apply([{"op": "editText", "id": body["id"], "runs": body["runs"], "lines": lines}])
    out = editor.export()
    spans = spans_in(out, (60, 200, 330, 320))
    for line in lines:
        span = next(s for s in spans if s["text"].strip() == line["runs"][0]["text"])
        assert span["origin"][0] == pytest.approx(line["x"], abs=0.5)
        assert span["origin"][1] == pytest.approx(line["baseline"], abs=0.5)


def test_paragraph_rewraps_inside_width(one_pager, registry):
    editor = DocumentEditor(one_pager, registry)
    body = find_text(editor, "Our fall packages")
    text = "Short new copy. " * 12
    runs = [{**body["runs"][0], "text": text.strip()}]
    editor.apply([{"op": "editText", "id": body["id"], "runs": runs}])
    out = editor.export()
    spans = [s for s in spans_in(out, (60, 200, 330, 330)) if round(s["size"]) == 11]
    assert len({round(s["origin"][1]) for s in spans}) > 1
    assert min(s["bbox"][0] for s in spans) == pytest.approx(72, abs=0.5)
    assert max(s["bbox"][2] for s in spans) <= body["bbox"][2] + 0.5
    assert {font_key(s["font"]) for s in spans} == {"opensansregular"}


def test_tight_paragraph_edit_does_not_clip_neighbors(one_pager, registry):
    editor = DocumentEditor(one_pager, registry)
    tight = find_text(editor, "Tight leading")
    second = tight["lines"][1]
    # Replace only the second line's words by editing the whole block with its layout,
    # keeping the other lines as saved.
    lines = [dict(ln) for ln in tight["lines"]]
    lines[1] = {**second, "runs": [{**r, "text": "EDITED LINE"} for r in second["runs"][:1]]}
    editor.apply([{"op": "editText", "id": tight["id"], "runs": tight["runs"], "lines": lines}])
    out = editor.export()
    got = [s["text"].strip() for s in spans_in(out, tight["bbox"])]
    for i, ln in enumerate(tight["lines"]):
        if i != 1:
            assert "".join(r["text"] for r in ln["runs"]).strip() in got
    assert "EDITED LINE" in got


def test_text_over_image_edit_keeps_photo(one_pager, registry):
    editor = DocumentEditor(one_pager, registry)
    cap = find_text(editor, "Photo:")
    editor.apply([{"op": "editText", "id": cap["id"], "runs": [{**cap["runs"][0], "text": "Photo: Elm St"}]}])
    out = editor.export()
    before, after = render(one_pager), render(out)
    assert changed_outside(before, after, [cap["bbox"]]) == 0
    # Photo pixels away from the caption are identical.
    assert not diff_mask(before, after)[850:1100, 150:590].any()


def test_move_image_reuses_data(one_pager, registry):
    editor = DocumentEditor(one_pager, registry)
    photo = next(e for e in elements(editor).values() if e["type"] == "image" and e["bbox"][0] == 72)
    new = [90, 430, 318, 590]
    editor.apply([{"op": "transform", "id": photo["id"], "bbox": new}])
    out = editor.export()
    doc = pymupdf.open(stream=out, filetype="pdf")
    page = doc[0]
    assert len(page.get_images()) == 2
    xref = next(x[0] for x in page.get_images() if x[2] == 228)
    rect = page.get_image_rects(xref)[0]
    assert list(rect) == pytest.approx(new, abs=0.01)
    # Caption still paints on top of the moved photo (z-order preserved).
    moved = DocumentEditor(out, registry).page_model(0).elements
    order = list(moved)
    img = next(i for i, e in moved.items() if e["type"] == "image" and e["bbox"][0] == pytest.approx(90))
    cap = next(
        i for i, e in moved.items() if e["type"] == "text" and e["runs"][0]["text"].startswith("Photo")
    )
    assert order.index(img) < order.index(cap)


def test_resize_transparent_logo_keeps_alpha(one_pager, registry):
    editor = DocumentEditor(one_pager, registry)
    logo = next(e for e in elements(editor).values() if e["type"] == "image" and e["bbox"][0] == 548)
    editor.apply([{"op": "transform", "id": logo["id"], "bbox": [530, 8, 600, 78]}])
    out = editor.export()
    img = render(out)
    # Corner of the new logo box is transparent: orange band shows through.
    corner = img[int(10 * 2), int(532 * 2)]
    assert corner[0] > 200 and corner[2] < 90
    # Center of the new box shows the logo.
    center = img[int(43 * 2), int(565 * 2)]
    assert abs(int(center[0]) - 228) > 30 or abs(int(center[2]) - 46) > 30


def test_single_line_headline_grows_instead_of_wrapping(one_pager, registry):
    editor = DocumentEditor(one_pager, registry)
    head = find_text(editor, "Fall Underwriting")
    # Wider than the original ink, still narrower than the room before the logo.
    run = {**head["runs"][0], "text": "Fall Underwriting Packages!!"}
    result = editor.apply([{"op": "editText", "id": head["id"], "runs": [run]}])
    assert not result.warnings
    spans = spans_in(editor.export(), (0, 30, 612, 90))
    assert len({round(s["origin"][1]) for s in spans}) == 1
    center = (min(s["bbox"][0] for s in spans) + max(s["bbox"][2] for s in spans)) / 2
    assert center == pytest.approx(306, abs=1.5)


def test_resize_shape_leaves_neighbors(one_pager, registry):
    editor = DocumentEditor(one_pager, registry)
    circle = next(e for e in elements(editor).values() if e["type"] == "shape" and e["bbox"][0] == 360)
    editor.apply([{"op": "transform", "id": circle["id"], "bbox": [360, 600, 440, 680]}])
    out = editor.export()
    doc = pymupdf.open(stream=out, filetype="pdf")
    rects = [
        d["rect"]
        for d in doc[0].get_drawings()
        if d.get("fill") and d["rect"].x0 == pytest.approx(360, abs=0.1)
    ]
    assert any(r.x1 == pytest.approx(440, abs=0.1) and r.y1 == pytest.approx(680, abs=0.1) for r in rects)
    assert changed_outside(render(one_pager), render(out), [[360, 600, 440, 680]]) == 0


def test_delete_text(one_pager, registry):
    editor = DocumentEditor(one_pager, registry)
    footer = find_text(editor, "KXYZ")
    editor.apply([{"op": "delete", "id": footer["id"]}])
    out = editor.export()
    assert spans_in(out, footer["bbox"]) == []
    assert changed_outside(render(one_pager), render(out), [footer["bbox"]]) == 0


def test_add_text(one_pager, registry):
    editor = DocumentEditor(one_pager, registry)
    run = {"text": "New line", "font": "Open Sans Regular", "size": 12, "color": "#1A1A1A"}
    editor.apply([{"op": "addText", "page": 0, "bbox": [72, 600, 300, 630], "runs": [run]}])
    spans = spans_in(editor.export(), (72, 598, 300, 640))
    assert [s["text"] for s in spans] == ["New line"]


def test_locked_and_unknown_elements_rejected(one_pager, registry):
    editor = DocumentEditor(one_pager, registry)
    with pytest.raises(OpError):
        editor.apply([{"op": "delete", "id": "p0-zzz"}])
    locked = next(e for e in elements(editor).values() if e["locked"])
    with pytest.raises(OpError):
        editor.apply([{"op": "delete", "id": locked["id"]}])


def test_background_png_removes_editable_text(one_pager, registry):
    editor = DocumentEditor(one_pager, registry)
    png = editor.background_png(0, scale=1)
    assert png[:4] == b"\x89PNG"
    assert pymupdf.Pixmap(png).width == 612
