"""
rag/parser/pdf_parser.py
Extracts text from PDF, DOCX, TXT, HTML files.
Supports scanned PDFs via OCR (pytesseract).
Libraries: PyMuPDF (fitz), pdfplumber, python-docx
"""
import os
import re
from pathlib import Path


def extract_text(filepath: str) -> str:
    """
    Auto-detect file type and extract clean text.
    Returns raw text string ready for chunking.
    """
    path = Path(filepath)
    ext = path.suffix.lower()

    if ext == ".pdf":
        return _extract_pdf(filepath)
    elif ext == ".docx":
        return _extract_docx(filepath)
    elif ext in (".txt", ".md"):
        return path.read_text(encoding="utf-8", errors="ignore")
    elif ext in (".html", ".htm"):
        return _extract_html(filepath)
    else:
        # Try plain text as fallback
        try:
            return path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            return ""


def _extract_pdf(filepath: str) -> str:
    """
    Primary: PyMuPDF (fast, good for digital PDFs).
    Fallback: pdfplumber (better for complex layouts).
    OCR fallback: pytesseract for scanned PDFs.
    """
    text = ""

    # Try PyMuPDF first
    try:
        import fitz  # PyMuPDF
        doc = fitz.open(filepath)
        pages = []
        for page_num, page in enumerate(doc, 1):
            page_text = page.get_text("text")
            if page_text.strip():
                pages.append(f"[Page {page_num}]\n{page_text}")
        text = "\n\n".join(pages)
        doc.close()
    except ImportError:
        pass
    except Exception:
        pass

    # If PyMuPDF got nothing, try pdfplumber
    if not text.strip():
        try:
            import pdfplumber
            with pdfplumber.open(filepath) as pdf:
                pages = []
                for page_num, page in enumerate(pdf.pages, 1):
                    page_text = page.extract_text() or ""
                    if page_text.strip():
                        pages.append(f"[Page {page_num}]\n{page_text}")
                text = "\n\n".join(pages)
        except ImportError:
            pass
        except Exception:
            pass

    # If still empty — scanned PDF, use OCR
    if not text.strip():
        text = _ocr_pdf(filepath)

    return text


def _ocr_pdf(filepath: str) -> str:
    """OCR pipeline for scanned PDFs: pdf2image → pytesseract."""
    try:
        from pdf2image import convert_from_path
        import pytesseract
        images = convert_from_path(filepath, dpi=300)
        pages = []
        for page_num, img in enumerate(images, 1):
            page_text = pytesseract.image_to_string(img, lang="eng")
            if page_text.strip():
                pages.append(f"[Page {page_num} — OCR]\n{page_text}")
        return "\n\n".join(pages)
    except ImportError:
        return "[OCR not available — install pdf2image and pytesseract]"
    except Exception as e:
        return f"[OCR failed: {e}]"


def _extract_docx(filepath: str) -> str:
    """Extract text from DOCX preserving heading structure."""
    try:
        from docx import Document
        doc = Document(filepath)
        lines = []
        for para in doc.paragraphs:
            if para.text.strip():
                # Mark headings for chunker to use
                if para.style.name.startswith("Heading"):
                    level = para.style.name.replace("Heading ", "")
                    lines.append(f"\n{'#' * int(level) if level.isdigit() else '#'} {para.text.strip()}")
                else:
                    lines.append(para.text.strip())
        return "\n".join(lines)
    except ImportError:
        return "[python-docx not installed — run: pip install python-docx]"
    except Exception as e:
        return f"[DOCX extraction failed: {e}]"


def _extract_html(filepath: str) -> str:
    """Strip HTML tags and extract readable text."""
    try:
        from html.parser import HTMLParser

        class _Stripper(HTMLParser):
            def __init__(self):
                super().__init__()
                self.parts = []
                self._skip = False

            def handle_starttag(self, tag, attrs):
                if tag in ("script", "style"):
                    self._skip = True

            def handle_endtag(self, tag):
                if tag in ("script", "style"):
                    self._skip = False
                if tag in ("p", "div", "h1", "h2", "h3", "h4", "li", "br"):
                    self.parts.append("\n")

            def handle_data(self, data):
                if not self._skip and data.strip():
                    self.parts.append(data)

        html = Path(filepath).read_text(encoding="utf-8", errors="ignore")
        stripper = _Stripper()
        stripper.feed(html)
        return " ".join(stripper.parts)
    except Exception as e:
        return f"[HTML extraction failed: {e}]"
