"""
rag/chunking/chunker.py
Heading-aware chunker that splits policy text into meaningful sections.

Strategy:
1. Detect section headings (numbered, ALL CAPS, markdown-style #)
2. Split on headings first → each section becomes a logical chunk
3. If a section is too long, sub-split with sliding window (500 tokens, 100 overlap)
4. Short sections are merged with the next to avoid tiny chunks

Each chunk carries full metadata for precise citations.
"""
import re
from dataclasses import dataclass, field
from typing import Optional


# Heading patterns — ordered by specificity
HEADING_PATTERNS = [
    re.compile(r"^#{1,4}\s+(.+)$", re.MULTILINE),              # Markdown: # Title
    re.compile(r"^\d+\.\s+[A-Z].{2,80}$", re.MULTILINE),       # 1. Purpose
    re.compile(r"^\d+\.\d+\s+[A-Z].{2,60}$", re.MULTILINE),    # 1.1 Eligibility
    re.compile(r"^[A-Z][A-Z\s]{5,60}$", re.MULTILINE),          # ALL CAPS HEADING
    re.compile(r"^\[Page \d+\]$", re.MULTILINE),                 # Page markers from parser
]

# Chunk size limits (in characters, not tokens — simpler, no tokenizer needed)
MIN_CHUNK_CHARS = 40
MAX_CHUNK_CHARS = 1200
OVERLAP_CHARS = 150


@dataclass
class Chunk:
    text: str
    section: str = "General"
    page: Optional[int] = None
    chunk_index: int = 0
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "text": self.text,
            "section": self.section,
            "page": self.page,
            "chunk_index": self.chunk_index,
            **self.metadata,
        }


def chunk_policy(
    text: str,
    policy_id: int,
    policy_name: str,
    version: str,
    department: str = "",
) -> list[Chunk]:
    """
    Main entry point. Returns a list of Chunk objects with full metadata.
    """
    # 1. Split into sections by heading detection
    sections = _split_into_sections(text)

    chunks: list[Chunk] = []
    chunk_index = 0
    current_page = None

    for section_title, section_text in sections:
        # Track page numbers from parser markers
        page_match = re.search(r"\[Page (\d+)\]", section_text)
        if page_match:
            current_page = int(page_match.group(1))
            section_text = re.sub(r"\[Page \d+[^\]]*\]\n?", "", section_text)

        section_text = section_text.strip()
        if not section_text:
            continue

        # 2. Sub-chunk long sections
        sub_chunks = _split_long_section(section_text)

        for sub_text in sub_chunks:
            sub_text = sub_text.strip()
            if len(sub_text) < MIN_CHUNK_CHARS:
                continue

            chunk = Chunk(
                text=sub_text,
                section=section_title,
                page=current_page,
                chunk_index=chunk_index,
                metadata={
                    "policy_id": policy_id,
                    "policy_name": policy_name,
                    "version": version,
                    "department": department,
                    "section": section_title,
                    "page": current_page,
                    "chunk_index": chunk_index,
                    "char_count": len(sub_text),
                },
            )
            chunks.append(chunk)
            chunk_index += 1

    # Safety net: if every candidate chunk got filtered out as "too short"
    # (common for brief test/short policies), don't silently return nothing —
    # index the whole cleaned text as a single chunk instead. Without this,
    # short policies never make it into the vector store and the AI assistant
    # will always report "couldn't find this information" for them.
    if not chunks and text.strip():
        chunks.append(
            Chunk(
                text=text.strip(),
                section="General",
                page=None,
                chunk_index=0,
                metadata={
                    "policy_id": policy_id,
                    "policy_name": policy_name,
                    "version": version,
                    "department": department,
                    "section": "General",
                    "page": None,
                    "chunk_index": 0,
                    "char_count": len(text.strip()),
                },
            )
        )

    return chunks


def _split_into_sections(text: str) -> list[tuple[str, str]]:
    """
    Identify heading positions and split text into (heading, content) pairs.
    """
    # Find all heading positions
    heading_spans: list[tuple[int, str]] = []
    for pattern in HEADING_PATTERNS:
        for m in pattern.finditer(text):
            heading_spans.append((m.start(), m.group(0).strip()))

    if not heading_spans:
        return [("General", text)]

    # Sort by position, deduplicate overlapping
    heading_spans.sort(key=lambda x: x[0])
    deduped: list[tuple[int, str]] = []
    last_pos = -1
    for pos, heading in heading_spans:
        if pos > last_pos + 10:
            deduped.append((pos, heading))
            last_pos = pos

    sections: list[tuple[str, str]] = []
    # Content before first heading
    if deduped and deduped[0][0] > 0:
        intro = text[: deduped[0][0]].strip()
        if intro:
            sections.append(("Introduction", intro))

    for i, (pos, heading) in enumerate(deduped):
        end = deduped[i + 1][0] if i + 1 < len(deduped) else len(text)
        content = text[pos:end]
        # Remove the heading line from the content itself
        content = content[len(heading):].strip()
        clean_heading = re.sub(r"^#+\s*", "", heading).strip()
        sections.append((clean_heading or "Section", content))

    return sections if sections else [("General", text)]


def _split_long_section(text: str) -> list[str]:
    """
    If a section exceeds MAX_CHUNK_CHARS, split with sliding window.
    Tries to break on sentence boundaries.
    """
    if len(text) <= MAX_CHUNK_CHARS:
        return [text]

    chunks = []
    start = 0
    while start < len(text):
        end = min(start + MAX_CHUNK_CHARS, len(text))

        # Try to end on a sentence boundary
        if end < len(text):
            last_period = text.rfind(".", start, end)
            last_newline = text.rfind("\n", start, end)
            boundary = max(last_period, last_newline)
            if boundary > start + MIN_CHUNK_CHARS:
                end = boundary + 1

        chunks.append(text[start:end])
        start = end - OVERLAP_CHARS  # sliding overlap
        if start >= len(text):
            break

    return [c for c in chunks if len(c.strip()) >= MIN_CHUNK_CHARS]
