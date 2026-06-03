"""
pdf_extract.py — PDF text extraction for the Well Report Analyzer.

Extracts text and tables from PDF pages using pdfplumber, with an OCR
fallback (pdf2image + pytesseract) for scanned/image-only pages.
"""
import os

OCR_THRESHOLD = 20   # pages with fewer selectable chars than this use OCR

# ── Windows PATH fix for Tesseract and Poppler ────────────────────────────────
# These paths are specific to one development machine.  Each block checks
# whether its path actually exists before doing anything, so on every other
# machine (different Windows user, Mac, Linux) both blocks are silently skipped
# and cause no side-effects whatsoever.
#
# If YOU are on Windows and can't edit your system PATH (e.g. locked-down
# university or corporate laptop), set TESSERACT_EXE and POPPLER_BIN in your
# .env file to point at your local installs (see README OCR section).
_TESSERACT_EXE = os.environ.get("TESSERACT_EXE", "")
if os.path.exists(_TESSERACT_EXE):
    try:
        import pytesseract
        pytesseract.pytesseract.tesseract_cmd = _TESSERACT_EXE
    except ImportError:
        pass  # pytesseract not installed — OCR fallback simply won't be available.

_POPPLER_BIN = os.environ.get("POPPLER_BIN", "")
if os.path.exists(_POPPLER_BIN):
    os.environ["PATH"] += ";" + _POPPLER_BIN


def _format_table(table: list) -> str:
    """Convert a pdfplumber list-of-lists table into pipe-delimited text."""
    rows = []
    for row in table:
        cells = [str(c).strip() if c is not None else "" for c in row]
        rows.append(" | ".join(cells))
    return "\n".join(rows)


def extract_text_from_pdf(pdf_path: str) -> tuple:
    """
    Extract all text and table content from a PDF file.

    CASE A (digital pages)  : pdfplumber extracts text + tables directly.
    CASE B (scanned pages)  : pdf2image converts to image, pytesseract OCRs it.

    Returns (text, meta) where text is a combined plain-text string for the
    entire document and meta is {"page_count": int, "ocr_used": bool}.
    """
    try:
        import pdfplumber
    except ImportError as e:
        raise RuntimeError("pdfplumber is required.  pip install pdfplumber") from e

    try:
        import shutil
        import pdf2image
        import pytesseract
        # Verify the underlying binaries are actually reachable at runtime.
        # The Python packages import fine even when the executables are missing,
        # which would cause a hard crash later — check now so we can fall back cleanly.
        _poppler_ok   = bool(shutil.which("pdftoppm") or shutil.which("pdftoppm.exe"))
        _tess_cmd     = pytesseract.pytesseract.tesseract_cmd  # may be custom path set above
        _tesseract_ok = (
            os.path.isfile(_tess_cmd)
            if _tess_cmd and _tess_cmd != "tesseract"
            else bool(shutil.which("tesseract") or shutil.which("tesseract.exe"))
        )
        ocr_available = _poppler_ok and _tesseract_ok
    except ImportError:
        ocr_available = False

    all_text          = []
    ocr_used          = False
    ocr_pages_done    = 0   # pages successfully processed by OCR
    ocr_pages_skipped = 0   # pages that needed OCR but binaries were unavailable

    with pdfplumber.open(pdf_path) as pdf:
        page_count = len(pdf.pages)
        for page_num, page in enumerate(pdf.pages, start=1):
            page_parts = []

            try:
                probe = page.extract_text() or ""
            except (ValueError, AttributeError, OSError, RuntimeError):
                probe = ""

            if len(probe.strip()) >= OCR_THRESHOLD:
                # CASE A: digital / selectable text
                try:
                    raw_text = page.extract_text() or ""
                    if raw_text.strip():
                        page_parts.append(raw_text)
                except (ValueError, AttributeError, OSError, RuntimeError) as e:
                    page_parts.append(f"[TEXT ERROR page {page_num}: {e}]")

                try:
                    tables = page.extract_tables()
                    for t_idx, tbl in enumerate(tables or [], start=1):
                        formatted = _format_table(tbl)
                        page_parts.append(
                            f"\n[TABLE {t_idx} - Page {page_num}]\n"
                            f"{'-'*50}\n{formatted}\n{'-'*50}\n"
                        )
                except (ValueError, AttributeError, OSError) as e:
                    page_parts.append(f"[TABLE ERROR page {page_num}: {e}]")

            else:
                # CASE B: scanned — OCR fallback
                if not ocr_available:
                    ocr_pages_skipped += 1
                    page_parts.append(
                        f"[Page {page_num}: scanned - OCR unavailable "
                        "(Tesseract/Poppler not found in PATH)]"
                    )
                else:
                    ocr_used = True
                    try:
                        images = pdf2image.convert_from_path(
                            pdf_path, dpi=300,
                            first_page=page_num, last_page=page_num
                        )
                        if images:
                            ocr_text = pytesseract.image_to_string(
                                images[0], config="--psm 3"
                            )
                            if ocr_text.strip():
                                page_parts.append(ocr_text)
                        ocr_pages_done += 1
                    except Exception as e:
                        page_parts.append(f"[OCR ERROR page {page_num}: {e}]")

            header = f"\n{'='*60}\nPAGE {page_num}\n{'='*60}\n"
            all_text.append(header + "\n".join(page_parts))

    return "\n".join(all_text), {
        "page_count":        page_count,
        "ocr_used":          ocr_used,
        "ocr_pages_done":    ocr_pages_done,
        "ocr_pages_skipped": ocr_pages_skipped,
    }
