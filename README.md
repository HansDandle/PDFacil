# PDFacil

A hosted, lightweight design tool for everyday Canva-style work (one-sheets, rate cards, flyers, social graphics, proposals) that can also import and edit Canva-exported PDFs with their original fonts intact.

- Designs are stored as JSON and edited on a canvas in the browser.
- A FastAPI + PyMuPDF backend handles PDF import, direct PDF edits, exports, and OCR.
- Brand kits, templates with `{{fields}}`, and CSV batch generation cover sales workflows.

## Status

Backend PDF pipeline (extraction, font mapping, paragraph rebuild, edits, export) and the direct-edit API are working. Hosting, the editor UI, and design documents come next.

## Development

Requires [uv](https://docs.astral.sh/uv/). Python 3.12 is installed by uv.

```sh
uv sync                                   # install dependencies
uv run pytest                             # run the test suite (downloads a few Google Fonts once)
uv run uvicorn pdfacil.main:app --reload  # API on http://localhost:8000 (docs at /docs)
```

Try an edit on any PDF without the UI:

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
