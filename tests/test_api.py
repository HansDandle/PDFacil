import json

import pymupdf
import pytest
from fastapi.testclient import TestClient

from pdfacil.api.app import create_app
from pdfacil.config import Settings
from tests.fixtures.synthetic import build_digits_doc, subset_font


@pytest.fixture
def client(tmp_path, google):
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'api.sqlite3'}",
        storage_dir=tmp_path / "objects",
        max_pdf_bytes=5 * 1024 * 1024,
        max_pdf_pages=3,
    )
    return TestClient(create_app(settings, google))


def upload(client, data: bytes, name="flyer.pdf"):
    return client.post("/sessions", files={"file": (name, data, "application/pdf")})


def test_editor_page_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "text/html" in r.headers["content-type"]
    assert "PDFacil" in r.text and "/sessions" in r.text


def test_healthz_and_source(client):
    assert client.get("/healthz").json() == {"ok": True}
    assert "github.com" in client.get("/source").json()["url"]


def test_session_flow(client, one_pager):
    r = upload(client, one_pager)
    assert r.status_code == 200, r.text
    session = r.json()
    assert session["pageCount"] == 1 and session["isCanva"] and session["notice"] is None
    sid = session["id"]

    page = client.get(f"/sessions/{sid}/pages/0").json()
    head = next(
        e for e in page["elements"]
        if e["type"] == "text" and e["runs"][0]["text"].startswith("Fall")
    )  # fmt: skip
    assert head["fontStatus"] == "mapped"
    assert page["background"] == f"/sessions/{sid}/pages/0/bg.png"

    bg = client.get(page["background"], params={"scale": 1})
    assert bg.headers["content-type"] == "image/png"
    assert pymupdf.Pixmap(bg.content).width == 612

    fonts = client.get(f"/sessions/{sid}/fonts").json()["fonts"]
    assert {f["status"] for f in fonts} == {"mapped"}

    run = {**head["runs"][0], "text": "Winter Packages"}
    ops = [{"op": "editText", "id": head["id"], "runs": [run]}]
    assert client.put(f"/sessions/{sid}/ops", json={"ops": ops}).json()["count"] == 1

    preview = client.post(f"/sessions/{sid}/preview", json={"page": 0, "scale": 1})
    assert preview.status_code == 200 and preview.content[:4] == b"\x89PNG"
    assert json.loads(preview.headers["X-PDFacil-Warnings"])["warnings"] == []

    exported = client.post(f"/sessions/{sid}/export")
    assert exported.status_code == 200
    assert 'filename="flyer-edited.pdf"' in exported.headers["content-disposition"]
    doc = pymupdf.open(stream=exported.content, filetype="pdf")
    assert "Winter Packages" in doc[0].get_text()


def test_font_fallback_warning_and_upload(client, font_files):
    sid = upload(client, build_digits_doc(font_files["OpenSans-Regular"], ["40"])).json()["id"]
    el = client.get(f"/sessions/{sid}/pages/0").json()["elements"][0]
    assert el["fontStatus"] == "subset-only"
    ops = [{"op": "editText", "id": el["id"], "runs": [{**el["runs"][0], "text": "50"}]}]
    warnings = client.post(f"/sessions/{sid}/check", json={"ops": ops}).json()["warnings"]
    assert warnings[0]["code"] == "font-substituted"

    # Choice 3: upload the real font; the element re-renders in it with no warning.
    full = subset_font(font_files["OpenSans-Regular"], "0123456789", "Zqxvern-Regular")
    r = client.post(
        f"/sessions/{sid}/fonts/map",
        data={"name": el["runs"][0]["font"]},
        files={"file": ("Zqxvern-Regular.ttf", full, "font/ttf")},
    )
    assert r.status_code == 200, r.text
    assert client.post(f"/sessions/{sid}/check").json()["warnings"] == []
    fonts = client.get(f"/sessions/{sid}/fonts").json()["fonts"]
    assert fonts[0]["status"] == "mapped"


def test_bad_ops_rejected(client, one_pager):
    sid = upload(client, one_pager).json()["id"]
    r = client.put(f"/sessions/{sid}/ops", json={"ops": [{"op": "delete", "id": "p0-nope"}]})
    assert r.status_code == 400


def test_unknown_session_404(client):
    assert client.get("/sessions/doesnotexist/pages/0").status_code == 404


@pytest.mark.parametrize(
    "data, status",
    [
        (b"hello world", 400),
        (b"%PDF-1.7\n garbage garbage", 400),
        (b"%PDF-1.7\n" + b"0" * (6 * 1024 * 1024), 413),
    ],
    ids=["not-pdf", "malformed", "oversize"],
)
def test_rejects_bad_uploads(client, data, status):
    assert upload(client, data).status_code == status


def test_rejects_encrypted_and_too_many_pages(client):
    doc = pymupdf.open()
    doc.new_page()
    enc = doc.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256, user_pw="x", owner_pw="y")
    assert "password" in upload(client, enc).json()["detail"]
    big = pymupdf.open()
    for _ in range(4):
        big.new_page()
    assert "pages" in upload(client, big.tobytes()).json()["detail"]


def test_strips_javascript(client):
    doc = pymupdf.open()
    doc.new_page()
    doc.xref_set_key(doc.pdf_catalog(), "OpenAction", "<</S/JavaScript/JS(app.alert(1))>>")
    sid = upload(client, doc.tobytes()).json()["id"]
    stored = client.app.state.services.storage.get(f"sessions/{sid}/original.pdf")
    assert b"app.alert" not in pymupdf.open(stream=stored, filetype="pdf").tobytes(expand=255)
