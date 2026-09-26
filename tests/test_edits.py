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


def test_change_font_size_color_and_alignment(one_pager, registry):
    editor = DocumentEditor(one_pager, registry)
    head = find_text(editor, "Fall Underwriting")
    run = {"text": "Fall Packages", "font": "Playfair Display-Bold", "size": 40, "color": "#1A1A1A"}
    result = editor.apply([{"op": "editText", "id": head["id"], "runs": [run], "align": "left"}])
    assert not [w for w in result.warnings if w["code"] != "text-overflow"]
    spans = [s for s in spans_in(editor.export(), (0, 20, 612, 100)) if round(s["size"]) == 40]
    assert "".join(s["text"] for s in spans) == "Fall Packages"
    assert {font_key(s["font"]) for s in spans} == {"playfairdisplaybold"}
    assert {round(s["size"]) for s in spans} == {40}
    assert {s["color"] for s in spans} == {0x1A1A1A}
    # Left aligned: moves to the left edge of the free space (half an inch from the page edge;
    # the full-width orange band behind it is no limit). The top of the block stays put.
    assert min(s["bbox"][0] for s in spans) == pytest.approx(36, abs=2)
    assert min(s["bbox"][1] for s in spans) == pytest.approx(head["bbox"][1], abs=4)


@pytest.mark.parametrize("align", ["right", "center"])
def test_alignment_change_moves_single_line(one_pager, registry, align):
    editor = DocumentEditor(one_pager, registry)
    sub = find_text(editor, "Reach")  # left aligned, one line, nothing to its right
    editor.apply([{"op": "editText", "id": sub["id"], "runs": sub["runs"], "align": align}])
    spans = [s for s in spans_in(editor.export(), (0, 160, 612, 190)) if round(s["size"]) == 16]
    x0, x1 = min(s["bbox"][0] for s in spans), max(s["bbox"][2] for s in spans)
    if align == "right":
        assert x1 == pytest.approx(612 - 36, abs=1.5)
    else:
        assert (x0 + x1) / 2 == pytest.approx(306, abs=1.5)


@pytest.mark.parametrize("align", ["center", "right"])
def test_alignment_change_moves_short_lines_of_a_paragraph(one_pager, registry, align):
    editor = DocumentEditor(one_pager, registry)
    body = find_text(editor, "Our fall packages")
    editor.apply([{"op": "editText", "id": body["id"], "runs": body["runs"], "align": align}])
    spans = [s for s in spans_in(editor.export(), (40, 200, 340, 320)) if round(s["size"]) == 11]
    lines: dict[int, list] = {}
    for s in spans:
        lines.setdefault(round(s["origin"][1]), []).append(s)
    edges = [(min(s["bbox"][0] for s in ln), max(s["bbox"][2] for s in ln)) for ln in lines.values()]
    x0, _y0, x1, _y1 = body["bbox"]
    for left, right in edges:
        if align == "center":
            assert (left + right) / 2 == pytest.approx((x0 + x1) / 2, abs=1.5)
        else:
            assert right == pytest.approx(x1, abs=1.5)
    assert any(left > x0 + 10 for left, _ in edges)  # the short last line really moved


def test_moved_block_still_aligns_and_reports_its_box(one_pager, registry):
    editor = DocumentEditor(one_pager, registry)
    sub = find_text(editor, "Reach")
    x0, y0, x1, y1 = sub["bbox"]
    result = editor.apply([
        {"op": "transform", "id": sub["id"], "bbox": [x0, y0 + 20, x1, y1 + 20]},  # move only
        {"op": "editText", "id": sub["id"], "runs": sub["runs"], "align": "right"},
    ])  # fmt: skip
    box = result.boxes[sub["id"]]
    assert box[2] == pytest.approx(612 - 36, abs=1.5)
    assert box[1] == pytest.approx(y0 + 20, abs=3)


def test_alignment_inside_a_card(one_pager, registry):
    editor = DocumentEditor(one_pager, registry)
    title = find_text(editor, "Morning Drive")  # left aligned inside the 350-540 card
    editor.apply([{"op": "editText", "id": title["id"], "runs": title["runs"], "align": "center"}])
    spans = [s for s in spans_in(editor.export(), (350, 430, 540, 460)) if round(s["size"]) == 14]
    x0, x1 = min(s["bbox"][0] for s in spans), max(s["bbox"][2] for s in spans)
    assert (x0 + x1) / 2 == pytest.approx(445, abs=1.5)


def test_alignment_inside_resized_box(one_pager, registry):
    editor = DocumentEditor(one_pager, registry)
    sub = find_text(editor, "Reach")
    x0, y0, _x1, y1 = sub["bbox"]
    editor.apply([
        {"op": "transform", "id": sub["id"], "bbox": [x0, y0, 500, y1]},
        {"op": "editText", "id": sub["id"], "runs": sub["runs"], "align": "right"},
    ])  # fmt: skip
    spans = [s for s in spans_in(editor.export(), (0, 160, 612, 190)) if round(s["size"]) == 16]
    assert max(s["bbox"][2] for s in spans) == pytest.approx(500, abs=1.5)


def test_resizing_text_rewraps_to_new_width(one_pager, registry):
    editor = DocumentEditor(one_pager, registry)
    body = find_text(editor, "Our fall packages")
    x0, y0, _x1, y1 = body["bbox"]
    editor.apply([{"op": "transform", "id": body["id"], "bbox": [x0, y0, x0 + 150, y1]}])
    spans = [s for s in spans_in(editor.export(), (60, 200, 330, 400)) if round(s["size"]) == 11]
    assert max(s["bbox"][2] for s in spans) <= x0 + 150.5
    assert len({round(s["origin"][1]) for s in spans}) > len(body["lines"])


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


def test_move_image_and_edit_text_on_same_page(one_pager, registry):
    editor = DocumentEditor(one_pager, registry)
    photo = next(e for e in elements(editor).values() if e["type"] == "image" and e["bbox"][0] == 72)
    head = find_text(editor, "Fall Underwriting")
    circle = next(e for e in elements(editor).values() if e["type"] == "shape" and e["bbox"][0] == 360)
    editor.apply([
        {"op": "editText", "id": head["id"], "runs": [{**head["runs"][0], "text": "Winter"}]},
        {"op": "transform", "id": circle["id"], "bbox": [370, 600, 430, 660]},
        {"op": "transform", "id": photo["id"], "bbox": [80, 420, 308, 580]},
    ])  # fmt: skip
    page = pymupdf.open(stream=editor.export(), filetype="pdf")[0]
    xref = next(x[0] for x in page.get_images() if x[2] == 228)
    assert list(page.get_image_rects(xref)[0]) == pytest.approx([80, 420, 308, 580], abs=0.01)
    assert "Winter" in page.get_text()


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


def test_resizing_a_shape_keeps_it_behind_its_text(one_pager, registry):
    editor = DocumentEditor(one_pager, registry)
    band = next(
        e for e in elements(editor).values() if e["type"] == "shape" and e["bbox"] == [0.0, 0.0, 612.0, 130.0]
    )
    assert all(p.get("call") is not None for p in editor.page_model(0).drawings[band["id"]])
    result = editor.apply([{"op": "transform", "id": band["id"], "bbox": [0, 0, 612, 90]}])
    assert not result.notices  # no "shape-on-top" fallback
    out = editor.export()
    img = render(out)
    # Band now ends at 90 pt: cream below it, orange above it.
    assert tuple(img[int(100 * 2), 20][:3]) == pytest.approx((255, 248, 238), abs=3)
    assert img[int(80 * 2), 20][0] > 200 and img[int(80 * 2), 20][2] < 90
    # The white headline still paints over the band (it would be hidden if the band were
    # redrawn on top): some pixels inside the headline box are white.
    head = find_text(editor, "Fall Underwriting")
    x0, y0, x1, y1 = (int(v * 2) for v in head["bbox"])
    region = img[y0:y1, x0:x1]
    assert (region.min(axis=2) > 240).sum() > 500


def test_shapes_and_images_inside_a_form_are_edited_in_place(registry):
    """Canva groups elements into Form XObjects; edits must reach inside them."""
    from tests.fixtures.synthetic import _gradient

    group = pymupdf.open()
    gp = group.new_page(width=200, height=100)
    gp.draw_rect(pymupdf.Rect(10, 10, 110, 60), color=None, fill=(0.2, 0.4, 0.8))
    gp.insert_image(pymupdf.Rect(120, 10, 190, 80), pixmap=_gradient(70, 70))
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.show_pdf_page(pymupdf.Rect(50, 50, 250, 150), group, 0)  # drawn once, as a form
    page.insert_text((60, 200), "Caption", fontsize=12)
    pdf = doc.tobytes()

    editor = DocumentEditor(pdf, registry)
    els = editor.page_model(0).elements
    box = next(e for e in els.values() if e["type"] == "shape" and not e["locked"])
    img = next(e for e in els.values() if e["type"] == "image")
    assert not img["locked"]
    assert all(p.get("call") is not None for p in editor.page_model(0).drawings[box["id"]])

    result = editor.apply([
        {"op": "transform", "id": box["id"], "bbox": [60, 160, 160, 185]},
        {"op": "transform", "id": img["id"], "bbox": [300, 50, 370, 120]},
    ])  # fmt: skip
    assert not result.notices  # no remove-and-redraw fallback
    out = pymupdf.open(stream=editor.export(), filetype="pdf")[0]
    fills = [d["rect"] for d in out.get_drawings() if d.get("fill") and d["fill"][2] > 0.7]
    assert any(all(abs(a - b) < 0.5 for a, b in zip(r, (60, 160, 160, 185), strict=True)) for r in fills)
    assert not any(abs(r.x0 - 60) < 0.5 and abs(r.y0 - 55) < 0.5 for r in fills)
    images = [pymupdf.Rect(i["bbox"]) for i in out.get_image_info()]
    assert any(all(abs(a - b) < 0.5 for a, b in zip(r, (300, 50, 370, 120), strict=True)) for r in images)


def test_moving_a_clipped_image_moves_its_clip(registry):
    """Canva draws each element as 'q <clip> W n q <cm> /Im Do Q Q'; moving only the image
    would slide it out of its clip window and it would vanish."""
    from tests.fixtures.synthetic import _gradient

    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.insert_image(pymupdf.Rect(20, 20, 380, 26), pixmap=_gradient(360, 6))  # a thin divider
    xref = page.get_contents()[0]
    stream = doc.xref_stream(xref)
    # Wrap the image in a clip window exactly around it (PDF coordinates: y up).
    doc.update_stream(xref, b"q 20 274 360 6 re W* n\n" + stream + b"\nQ")
    pdf = doc.tobytes()

    editor = DocumentEditor(pdf, registry)
    img = next(e for e in editor.page_model(0).elements.values() if e["type"] == "image")
    editor.apply([{"op": "transform", "id": img["id"], "bbox": [20, 200, 380, 206]}])
    out = editor.export()
    pix = render(out, scale=1)
    assert pix[203, 200].tolist() != [255, 255, 255]  # visible at the new position
    assert pix[23, 200].tolist() == [255, 255, 255]  # gone from the old one


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
    editor.apply([{"op": "addText", "page": 0, "bbox": [120, 715, 300, 740], "runs": [run]}])
    spans = spans_in(editor.export(), (120, 712, 300, 745))
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
