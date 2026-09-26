# PDFacil

A hosted, lightweight design tool for everyday Canva-style work (one-sheets, rate cards, flyers, social graphics, proposals) that can also import and edit Canva-exported PDFs with their original fonts intact.

- Designs are stored as JSON and edited on a canvas in the browser.
- A FastAPI + PyMuPDF backend handles PDF import, direct PDF edits, exports, and OCR.
- Brand kits, templates with `{{fields}}`, and CSV batch generation cover sales workflows.

## Status

Backend PDF pipeline (extraction, font mapping, paragraph rebuild, lists, edits, export), the direct-edit API, and a simple browser page for editing text in existing PDFs are working. The full design editor, hosting, and templates come next.

## Run it

Requires [uv](https://docs.astral.sh/uv/). Python 3.12 is installed by uv.

```sh
uv sync
uv run pdfacil          # starts on http://127.0.0.1:8000 and opens your browser
```

Drop a PDF on the page, click a text block, edit it, and download the result. Everything runs on your computer; files are kept under `./storage`.

## Development

```sh
uv run pytest                             # run the test suite (downloads a few Google Fonts once)
uv run uvicorn pdfacil.main:app --reload  # dev server with reload (API docs at /docs)
```

Try an edit from the command line:

```sh
uv run python scripts/edit_pdf.py flyer.pdf --list
uv run python scripts/edit_pdf.py flyer.pdf "Fall Underwriting" "Winter Underwriting" -o out.pdf
```

This writes `out.pdf` plus before/after PNGs.

### Layout

| Path | What |
| --- | --- |
| `backend/pdfacil/pipeline/` | PDF stages: `extract`, `fonts`, `paragraphs`, `edits` (apply + export), `fallback` (font substitution), `sanitize` |
| `backend/pdfacil/layout.py` | Server-side text layout (wrapping, alignment) |
| `backend/pdfacil/api/` | FastAPI app and the `/sessions` direct-edit endpoints |
| `tests/fixtures/synthetic.py` | Synthetic Canva-like PDFs used by the public test suite |

## Repository rules

This repo is public. Never commit:

- secrets (use `.env`; see `.env.example`)
- font files (`*.ttf`, `*.otf`, `*.woff`, `*.woff2`)
- real client PDFs (keep them in `fixtures/private/`, which is ignored)

## License

[AGPL-3.0](LICENSE). The app uses PyMuPDF (AGPL); network users can get the source from this repository (`GET /source`).
