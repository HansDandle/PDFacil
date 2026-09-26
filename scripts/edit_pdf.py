"""Edit one text block of a PDF through the API, with no UI (milestone 2 check).

    uv run python scripts/edit_pdf.py input.pdf --list
    uv run python scripts/edit_pdf.py input.pdf "Old headline start" "New headline" -o out.pdf

Runs the app in-process with local storage under ./storage and writes before/after PNGs next
to the output for a visual check.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import pymupdf
from fastapi.testclient import TestClient

from pdfacil.api.app import create_app


def block_text(element: dict) -> str:
    return "".join(r["text"] for r in element["runs"])


def replacement_runs(element: dict, text: str) -> list[dict]:
    """Style new text like the element: the dominant run's style, except characters the
    original drew in another run (Canva draws glyphs its font lacks, such as curly quotes,
    in a fallback font) keep that run's style."""
    style_keys = ("font", "size", "color")
    counts: dict[tuple, int] = {}
    per_char: dict[str, dict] = {}
    for run in element["runs"]:
        key = tuple(run[k] for k in style_keys)
        counts[key] = counts.get(key, 0) + len(run["text"].strip())
    dominant = dict(zip(style_keys, max(counts, key=counts.__getitem__), strict=True))
    for run in element["runs"]:
        if any(run[k] != dominant[k] for k in style_keys):
            for ch in run["text"]:
                per_char.setdefault(ch, {k: run[k] for k in style_keys})
    runs: list[dict] = []
    for ch in text:
        style = per_char.get(ch, dominant)
        if runs and all(runs[-1][k] == style[k] for k in style_keys):
            runs[-1]["text"] += ch
        else:
            runs.append({"text": ch, **style})
    return runs


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", type=Path)
    parser.add_argument("find", nargs="?", help="start of the text block to replace")
    parser.add_argument("replace", nargs="?", help="new text for the block")
    parser.add_argument("-o", "--out", type=Path)
    parser.add_argument("-p", "--page", type=int, help="only this page (default: all pages)")
    parser.add_argument("--list", action="store_true", help="list text blocks and fonts")
    parser.add_argument("--keep-font", action="store_true", help='use fontFallback "keep"')
    args = parser.parse_args()
    if not args.pdf.is_file():
        print(f"file not found: {args.pdf}", file=sys.stderr)
        return 1

    client = TestClient(create_app())
    r = client.post("/sessions", files={"file": (args.pdf.name, args.pdf.read_bytes())})
    if r.status_code != 200:
        print(r.json()["detail"], file=sys.stderr)
        return 1
    sid = r.json()["id"]
    if r.json()["notice"]:
        print("note:", r.json()["notice"])
    pages = [args.page] if args.page is not None else range(r.json()["pageCount"])
    texts = []
    for n in pages:
        page = client.get(f"/sessions/{sid}/pages/{n}").json()
        texts.extend(e for e in page["elements"] if e["type"] == "text")

    if args.list or not args.find or args.replace is None:
        for f in client.get(f"/sessions/{sid}/fonts").json()["fonts"]:
            target = f"{f['family']} ({f['source']})" if f["family"] else "-"
            print(f"font  {f['status']:<12} {f['name']}  ->  {target}")
        if not texts:
            print("no text on these pages (images or outlined text only)")
        for e in texts:
            lock = " [locked]" if e["locked"] else ""
            print(f"{e['id']:<8} {e['align']:<9} {e['fontStatus']:<12} {block_text(e)[:70]!r}{lock}")
        return 0

    target = next((e for e in texts if block_text(e).startswith(args.find)), None)
    if target is None:
        print(f"no text block starts with {args.find!r} (use --list)", file=sys.stderr)
        return 1
    op = {"op": "editText", "id": target["id"], "runs": replacement_runs(target, args.replace)}
    if args.keep_font:
        op["fontFallback"] = "keep"
    r = client.post(f"/sessions/{sid}/export", json={"ops": [op]})
    if r.status_code != 200:
        print(r.json()["detail"], file=sys.stderr)
        return 1
    out = args.out or args.pdf.with_name(f"{args.pdf.stem}-edited.pdf")
    out.write_bytes(r.content)
    report = json.loads(r.headers["X-PDFacil-Warnings"])
    for w in report["warnings"] + report["notices"]:
        print("warning:", w)
    page_no = int(target["id"].split("-")[0][1:])
    for label, data in (("before", args.pdf.read_bytes()), ("after", r.content)):
        doc = pymupdf.open(stream=data, filetype="pdf")
        doc[page_no].get_pixmap(dpi=144).save(out.with_name(f"{out.stem}-{label}.png"))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
