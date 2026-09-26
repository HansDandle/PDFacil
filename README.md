# PDFacil

Edit Canva PDFs without going back to Canva. Open a PDF exported from Canva, change the text, fonts, sizes, colors and layout, and download a new PDF that keeps the original fonts and artwork.

PDFacil runs on your own computer. It opens in your web browser, but your files never leave your machine (the only thing it downloads is free Google Fonts, the first time a font is used).

## What you can do

- **Edit text** in any block: headlines, paragraphs, prices, fine print. Text rewraps inside its box like it does in Canva.
- **Keep the original fonts.** Fonts embedded in the PDF are reused; Google Fonts are downloaded automatically. If a character is missing from a font, you choose: switch the block to a close match, keep the original, or upload the real font file.
- **Change the font, style, size, color and alignment** of a block (any Google Font).
- **Bulleted and numbered lists** behave like a word processor: add or remove items and the bullets follow; numbers renumber.
- **Move, resize and delete** text, images and shapes. Resized shapes stay behind the text on them.
- **See the real result** as you go: the page preview is the exact PDF you will download.

## Install (once)

PDFacil needs **uv**, a small tool that installs the right version of Python for it.

**Windows** — open PowerShell and run:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**macOS / Linux** — open Terminal and run:

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Close and reopen the window afterwards so the `uv` command is available.

Then get PDFacil, either:

- **Download:** on the GitHub page click **Code → Download ZIP**, and unzip it somewhere like `Documents\PDFacil`, or
- **Git:** `git clone https://github.com/HansDandle/PDFacil.git`

## Run it

Open PowerShell (or Terminal) in the PDFacil folder and run:

```sh
uv run pdfacil
```

The first start takes a minute while uv sets things up; after that it starts in a few seconds. Your browser opens at **http://127.0.0.1:8000**. Keep the window open while you work, and press **Ctrl+C** in it to stop PDFacil.

Tip (Windows): in File Explorer, open the PDFacil folder, type `powershell` in the address bar and press Enter to get a PowerShell window already in the right folder.

## Using the editor

1. Drop a PDF onto the page (or click **Open PDF**).
2. Click a text block, image or shape to select it.
   - **Text:** edit it in the panel on the right, change font, style, size, color or alignment, then click **Apply**. For lists, put one item per line.
   - **Move:** drag it. **Resize:** drag the handles (images keep their proportions; text boxes set the width the text wraps and aligns in).
   - A dashed side means the element continues past the edge of the page (Canva "bleed").
3. Click **Download PDF**. The file is saved as `<name>-edited.pdf`; your original is never changed.

| Keys | Action |
| --- | --- |
| Arrow keys | Nudge the selection 1 pt (Shift: 10 pt) |
| Delete | Delete the selection |
| Ctrl+Z | Undo |
| Esc | Deselect |

An orange **!** on a block means a font warning; select the block to see your options.

## Updating

- **Downloaded ZIP:** download the new ZIP and replace the folder (your settings and fonts live in its `storage` folder; copy that across if you want to keep downloaded fonts).
- **Git:** `git pull`, then `uv run pdfacil` as usual.

## Good to know

- Works best with PDFs exported from Canva. Other PDFs open too, with a notice that the result may not be as faithful.
- Canva's own fonts (like Canva Sans) are only partly embedded in its PDFs. You can edit text in them, but if you type a letter that wasn't in the original, PDFacil offers a close substitute or lets you upload the real font file.
- Text that Canva turned into shapes (outlined text) and rotated text are shown but can't be edited yet.
- Styling applies to a whole block; bolding a single word isn't supported yet.
- Everything PDFacil stores (uploaded PDFs, downloaded fonts) is in the `storage` folder inside PDFacil. Delete it any time to start fresh.

## Troubleshooting

- **`uv` is not recognized:** close and reopen PowerShell after installing uv.
- **The page doesn't open:** go to http://127.0.0.1:8000 yourself. If port 8000 is taken, run `uv run pdfacil --port 8001`.
- **A font doesn't download:** check your internet connection; Google Fonts are fetched the first time each font is used.
- **The first run seems stuck:** give it a minute. uv downloads Python and the PDF engine once.

## Development

```sh
uv sync                                   # install dependencies
uv run pytest                             # run the test suite (downloads a few Google Fonts once)
uv run uvicorn pdfacil.main:app --reload  # dev server that restarts on code changes (API docs at /docs)
```

Edit a PDF from the command line:

```sh
uv run python scripts/edit_pdf.py flyer.pdf --list
uv run python scripts/edit_pdf.py flyer.pdf "Fall Underwriting" "Winter Underwriting" -o out.pdf
```

| Path | What |
| --- | --- |
| `backend/pdfacil/pipeline/` | PDF stages: `extract`, `fonts`, `paragraphs`, `lists`, `content` (drawing-command reader), `edits` (apply + export), `fallback` (font substitution), `sanitize` |
| `backend/pdfacil/layout.py` | Text layout (wrapping, alignment) |
| `backend/pdfacil/api/` | FastAPI app and the `/sessions` edit endpoints |
| `backend/pdfacil/web/index.html` | The browser editor |
| `tests/fixtures/synthetic.py` | Synthetic Canva-like PDFs used by the public test suite |

This repo is public. Never commit secrets, font files (`*.ttf`, `*.otf`, `*.woff`, `*.woff2`) or real client PDFs (keep those in `fixtures/private/`, which is ignored).

## License

[AGPL-3.0](LICENSE). PDFacil uses PyMuPDF, which is also AGPL. The source code is always available from this repository.
