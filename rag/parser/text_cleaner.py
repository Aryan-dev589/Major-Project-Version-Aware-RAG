"""
rag/parser/text_cleaner.py
Cleans raw extracted text before chunking.
Removes noise, normalizes whitespace, fixes common PDF artifacts.
"""
import re


def clean_text(text: str) -> str:
    """Full cleaning pipeline."""
    text = _fix_encoding(text)
    text = _remove_headers_footers(text)
    text = _fix_hyphenation(text)
    text = _normalize_whitespace(text)
    text = _remove_noise(text)
    return text.strip()


def _fix_encoding(text: str) -> str:
    """Fix common encoding artifacts from PDF extraction."""
    replacements = {
        "\u2019": "'", "\u2018": "'", "\u201c": '"', "\u201d": '"',
        "\u2013": "-", "\u2014": "--", "\u00a0": " ", "\u2022": "•",
        "\ufb01": "fi", "\ufb02": "fl", "\u0000": "",
    }
    for bad, good in replacements.items():
        text = text.replace(bad, good)
    return text


def _remove_headers_footers(text: str) -> str:
    """
    Heuristically remove repeated page headers/footers.
    Lines shorter than 60 chars that repeat across pages are likely headers.
    """
    lines = text.split("\n")
    line_counts: dict[str, int] = {}
    for line in lines:
        stripped = line.strip()
        if 0 < len(stripped) < 60:
            line_counts[stripped] = line_counts.get(stripped, 0) + 1

    # Lines appearing 3+ times are probably headers/footers
    noise = {line for line, count in line_counts.items() if count >= 3}
    cleaned = [line for line in lines if line.strip() not in noise]
    return "\n".join(cleaned)


def _fix_hyphenation(text: str) -> str:
    """Join words broken across lines by PDF hyphenation: e.g. 'employ-\nee' → 'employee'."""
    return re.sub(r"(\w)-\n(\w)", r"\1\2", text)


def _normalize_whitespace(text: str) -> str:
    """Collapse multiple blank lines to max two, strip trailing spaces."""
    text = re.sub(r"[ \t]+", " ", text)          # multiple spaces → single
    text = re.sub(r"\n{3,}", "\n\n", text)         # 3+ newlines → 2
    text = re.sub(r"[ \t]+\n", "\n", text)         # trailing spaces
    return text


def _remove_noise(text: str) -> str:
    """Remove page numbers, watermarks, and other common PDF noise."""
    # Standalone page numbers like "— 8 —" or just "8" on a line
    text = re.sub(r"\n\s*[-–—]*\s*\d+\s*[-–—]*\s*\n", "\n", text)
    # Repeated "CONFIDENTIAL" watermarks
    text = re.sub(r"\bCONFIDENTIAL\b", "", text, flags=re.IGNORECASE)
    # Long sequences of dots (table of contents artifacts)
    text = re.sub(r"\.{5,}", " ", text)
    return text
