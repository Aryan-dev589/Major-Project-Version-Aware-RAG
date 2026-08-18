"""rag/chatbot/citations.py"""

def build_citations(chunks: list[dict]) -> list[dict]:
    seen, citations = set(), []
    for c in chunks:
        key = (c.get("policy_name"), c.get("version"), c.get("section"))
        if key not in seen:
            seen.add(key)
            citations.append({
                "policy_name": c.get("policy_name", "Unknown"),
                "version": c.get("version", "N/A"),
                "section": c.get("section", "General"),
                "page": c.get("page", "N/A"),
                "department": c.get("department", ""),
                "policy_id": c.get("policy_id", ""),
                "relevance_score": round(c.get("rerank_score", c.get("score", 0)), 3),
            })
    return citations


def format_citations_text(citations: list[dict]) -> str:
    if not citations:
        return ""
    lines = ["\n\n**Sources:**"]
    for i, c in enumerate(citations, 1):
        lines.append(
            f"{i}. **{c['policy_name']}** v{c['version']} — "
            f"Section: {c['section']}"
            + (f", Page {c['page']}" if c['page'] and c['page'] != "None" else "")
        )
    return "\n".join(lines)
