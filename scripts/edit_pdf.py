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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("pdf", type=Path)
    parser.add_argument("find", nargs="?", help="start of the text block to replace")
    parser.add_argument("replace", nargs="?", help="new text for the block")
    parser.add_argument("-o", "--out", type=Path)
    parser.add_argument("-p", "--page", type=int, default=0)
    parser.add_argument("--list", action="store_true", help="list text blocks and fonts")
    parser.add_argument("--keep-font", action="store_true", help='use fontFallback "keep"')
    args = parser.parse_args()

    client = TestClient(create_app())
    r = client.post("/sessions", files={"file": (args.pdf.name, args.pdf.read_bytes())})
    if r.status_code != 200:
        print(r.json()["detail"], file=sys.stderr)
        return 1
    sid = r.json()["id"]
    page = client.get(f"/sessions/{sid}/pages/{args.page}").json()
    texts = [e for e in page["elements"] if e["type"] == "text"]

    if args.list or not args.find or args.replace is None:
        for f in client.get(f"/sessions/{sid}/fonts").json()["fonts"]:
            print(f"font  {f['status']:<12} {f['name']}  ->  {f['mappedTo']}")
        for e in texts:
            lock = " [locked]" if e["locked"] else ""
            print(f"{e['id']:<8} {e['align']:<9} {e['fontStatus']:<12} {block_text(e)[:70]!r}{lock}")
        return 0

    target = next((e for e in texts if block_text(e).startswith(args.find)), None)
    if target is None:
        print(f"no text block starts with {args.find!r} (use --list)", file=sys.stderr)
        return 1
    op = {"op": "editText", "id": target["id"], "runs": [{**target["runs"][0], "text": args.replace}]}
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
    for label, data in (("before", args.pdf.read_bytes()), ("after", r.content)):
        doc = pymupdf.open(stream=data, filetype="pdf")
        doc[args.page].get_pixmap(dpi=144).save(out.with_name(f"{out.stem}-{label}.png"))
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
