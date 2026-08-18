"""
rag/reranker/reranker.py
Cross-encoder reranker. Takes top-N semantic hits and reranks for precision.
Primary: BAAI/bge-reranker-base
Fallback: simple token-overlap scoring
"""
from typing import Optional

_reranker = None


class CrossEncoderReranker:
    def __init__(self, model_name: str = "BAAI/bge-reranker-base"):
        from sentence_transformers import CrossEncoder
        self._model = CrossEncoder(model_name)
        self._model_name = model_name

    def rerank(self, query: str, hits: list[dict], top_k: int = 5) -> list[dict]:
        if not hits:
            return []
        pairs = [(query, h["text"]) for h in hits]
        scores = self._model.predict(pairs)
        for hit, score in zip(hits, scores):
            hit["rerank_score"] = float(score)
        hits.sort(key=lambda h: h["rerank_score"], reverse=True)
        return hits[:top_k]


class FallbackReranker:
    """Token overlap reranker — no deps needed."""
    import re as _re

    def rerank(self, query: str, hits: list[dict], top_k: int = 5) -> list[dict]:
        import re
        q_tokens = set(re.findall(r"[a-z0-9]+", query.lower()))
        for hit in hits:
            doc_tokens = set(re.findall(r"[a-z0-9]+", hit["text"].lower()))
            overlap = len(q_tokens & doc_tokens) / max(len(q_tokens), 1)
            hit["rerank_score"] = hit.get("score", 0) * 0.7 + overlap * 0.3
        hits.sort(key=lambda h: h["rerank_score"], reverse=True)
        return hits[:top_k]


def get_reranker():
    global _reranker
    if _reranker:
        return _reranker
    try:
        _reranker = CrossEncoderReranker()
        print("[Reranker] Using BAAI/bge-reranker-base")
    except Exception:
        _reranker = FallbackReranker()
        print("[Reranker] Using fallback token-overlap reranker")
    return _reranker
