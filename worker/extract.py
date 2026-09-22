"""Text extraction from uploaded attachments.

Images get OCR'd directly. PDFs try direct text extraction first (fast,
exact) and only fall back to rasterize-and-OCR when the PDF turns out to
have no real text layer (a scanned document saved as PDF). .docx reads
paragraph text via python-docx. Plain .txt/.rtf are read as-is.

Nothing here raises out to the caller — a failure is returned as
("", <message>) so main.py can record it on the attachment row without a
try/except at every call site, mirroring the "never let this stage block
the pipeline" philosophy used throughout this project's Ollama calls.
"""

import os
import subprocess
import tempfile

import pytesseract
from PIL import Image
from pypdf import PdfReader

try:
    import docx  # python-docx
except ImportError:  # pragma: no cover — always installed via requirements.txt
    docx = None

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".tiff", ".bmp"}
MAX_OCR_PAGES = 20  # cap on a scanned PDF's page-by-page OCR fallback, so one huge document can't stall the worker for minutes


def extract_text(abs_path: str, filename: str) -> tuple[str, str | None]:
    """Returns (extracted_text, error). error is None on success — even an
    image with no legible text is a success (empty string), not a failure."""
    ext = os.path.splitext(filename)[1].lower()
    if not os.path.isfile(abs_path):
        return "", f"File missing on disk: {abs_path}"
    try:
        if ext in IMAGE_EXTENSIONS:
            return _ocr_image(abs_path), None
        if ext == ".pdf":
            return _extract_pdf(abs_path), None
        if ext == ".docx":
            return _extract_docx(abs_path), None
        if ext in (".txt", ".rtf"):
            with open(abs_path, "r", errors="replace") as f:
                return f.read(), None
        return "", f"Unsupported file type for extraction: {ext or '(none)'}"
    except Exception as exc:
        return "", str(exc)


def _ocr_image(path: str) -> str:
    return pytesseract.image_to_string(Image.open(path))


def _extract_pdf(path: str) -> str:
    reader = PdfReader(path)
    text = "\n".join((page.extract_text() or "") for page in reader.pages)
    if len(text.strip()) >= 20:
        return text
    # Too little/no embedded text -- almost certainly a scanned PDF with no
    # real text layer. Rasterize pages (poppler-utils' pdftoppm) and OCR each.
    return _ocr_pdf(path)


def _ocr_pdf(path: str) -> str:
    with tempfile.TemporaryDirectory() as tmpdir:
        prefix = os.path.join(tmpdir, "page")
        subprocess.run(
            ["pdftoppm", "-png", "-r", "200", "-l", str(MAX_OCR_PAGES), path, prefix],
            check=True, capture_output=True, timeout=120,
        )
        pages = sorted(f for f in os.listdir(tmpdir) if f.endswith(".png"))
        texts = [pytesseract.image_to_string(Image.open(os.path.join(tmpdir, p))) for p in pages]
    return "\n\n".join(texts)


def _extract_docx(path: str) -> str:
    """Paragraphs AND tables, in document order.

    `Document.paragraphs` walks only the top-level body and skips every table
    cell, which for this app is exactly the wrong half to lose: a leaked ledger,
    a roster, a badge log or a payment schedule is a table, and the paragraphs
    around it are usually just a heading. Extracting only those produced a
    document that looked like it had been read successfully while the substance
    — every name, date and figure — was silently gone.

    Walking the body's XML children rather than `d.paragraphs` + `d.tables`
    keeps a table next to the sentence that introduces it, which matters
    because the model reads this as one narrative.
    """
    if docx is None:
        raise RuntimeError("python-docx not installed")
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    d = docx.Document(path)
    parts = []
    for child in d.element.body.iterchildren():
        tag = child.tag.split("}")[-1]
        if tag == "p":
            text = Paragraph(child, d).text.strip()
            if text:
                parts.append(text)
        elif tag == "tbl":
            for row in Table(child, d).rows:
                # Tab-separated: a row read as one line keeps "Trombley" next
                # to the amount he approved, which is the whole point of a
                # ledger. Cells are de-duplicated because a merged cell repeats
                # its text across every column it spans.
                cells, seen = [], set()
                for cell in row.cells:
                    value = cell.text.strip()
                    if value and id(cell._tc) not in seen:
                        seen.add(id(cell._tc))
                        cells.append(value)
                if cells:
                    parts.append("\t".join(cells))
    return "\n".join(parts)
