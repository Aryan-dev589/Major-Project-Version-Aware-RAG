"""
rag/retrieval/hybrid_search.py
Executes a Sequential Hybrid Search:
1. Dense Vector Search (ChromaDB) retrieves top candidate chunks.
2. BM25 Sparse Search scores only the retrieved candidate pool.
3. Fuses scores via Weighted Harmonic Mean (WHM).
4. Passes candidates through the Cross-Encoder reranker.
"""

from typing import Optional, List
from rank_bm25 import BM25Okapi
import re

from rag.vectordb.chroma import get_store
from rag.reranker.reranker import get_reranker


def _tokenize(text: str) -> list[str]:
    """Basic regex tokenizer for BM25 processing."""
    return re.findall(r"[a-z0-9]+", text.lower())


def _extract_policy_code_and_version(query_text: str) -> tuple[Optional[str], Optional[str]]:
    """Find explicit policy code and version label in the user query."""
    policy_match = re.search(r"\b(POL-[A-Za-z0-9.-]+)\b", query_text, re.IGNORECASE)
    version_match = re.search(r"\b(v\d+(?:\.\d+)?)\b", query_text, re.IGNORECASE)
    policy_code = policy_match.group(1).upper() if policy_match else None
    version_label = version_match.group(1).lower() if version_match else None
    return policy_code, version_label


def _resolve_policy_filters(query_text: str) -> tuple[Optional[int], Optional[str]]:
    """Map a query policy code/version to the internal policy_id and version label."""
    policy_code, version_label = _extract_policy_code_and_version(query_text)
    if not policy_code:
        return None, None

    try:
        from models import Policy, PolicyVersion
        policy = Policy.query.filter_by(policy_id=policy_code).first()
    except Exception:
        return None, None

    if not policy:
        return None, None

    if version_label:
        version = PolicyVersion.query.filter_by(
            policy_id=policy.id,
            version_label=version_label,
            is_active=True,
        ).first()
        if version:
            return policy.id, version.version_label

    return policy.id, None


def min_max_normalize(scores: dict[str, float]) -> dict[str, float]:
    """Squashes raw scores into a clean 0.0 to 1.0 scale."""
    if not scores:
        return {}
    
    min_val = min(scores.values())
    max_val = max(scores.values())
    
    if max_val == min_val:
        return {k: 1.0 for k in scores}
        
    return {k: (v - min_val) / (max_val - min_val) for k, v in scores.items()}


def calculate_whm(
    vec_scores: dict[str, float],
    bm25_scores: dict[str, float],
    w_vec: float = 0.5,
    w_bm25: float = 0.5,
    epsilon: float = 1e-6,
) -> dict[str, float]:
    """
    Fuses normalized vector and BM25 scores using Weighted Harmonic Mean.
    """
    norm_vec = min_max_normalize(vec_scores)
    norm_bm25 = min_max_normalize(bm25_scores)

    all_doc_ids = set(norm_vec.keys()) | set(norm_bm25.keys())
    fused_results = {}

    for doc_id in all_doc_ids:
        v = norm_vec.get(doc_id, 0.0) + epsilon
        b = norm_bm25.get(doc_id, 0.0) + epsilon

        whm_score = (w_vec + w_bm25) / ((w_vec / v) + (w_bm25 / b))
        fused_results[doc_id] = whm_score

    return dict(sorted(fused_results.items(), key=lambda item: item[1], reverse=True))


def hybrid_search(
    query_text: str,
    query_embedding: list[float],
    candidate_limit: int = 50,
    top_k: int = 5,
    policy_id: Optional[int] = None,
    department: Optional[str] = None,
    allowed_departments: Optional[List[str]] = None,
) -> list[dict]:
    
    store = get_store()
    query_policy_id, query_version = _resolve_policy_filters(query_text)
    if query_policy_id is not None:
        policy_id = query_policy_id

    # Step 1: Dense Retrieval via ChromaDB
    raw_hits = store.search(
        query_embedding=query_embedding,
        query_text=query_text,
        top_k=candidate_limit,
        policy_id=policy_id,
        version=query_version,
        department=department,
        allowed_departments=allowed_departments,
        active_only=True,
    )

    if not raw_hits:
        return []

    hit_map = {hit["id"]: hit for hit in raw_hits}
    vec_scores = {hit["id"]: hit["semantic_score"] for hit in raw_hits}

    # Step 2: BM25 Sparse Search on Candidate Corpus
    corpus = [_tokenize(hit["text"]) for hit in raw_hits]
    bm25 = BM25Okapi(corpus)
    tokenized_query = _tokenize(query_text)
    raw_bm25_scores = bm25.get_scores(tokenized_query)

    bm25_scores = {hit["id"]: float(score) for hit, score in zip(raw_hits, raw_bm25_scores)}

    # Step 3: Weighted Harmonic Mean Fusion
    fused_scores = calculate_whm(vec_scores, bm25_scores)

    candidates = []
    for doc_id in list(fused_scores.keys())[:candidate_limit]:
        item = hit_map[doc_id]
        item["whm_score"] = fused_scores[doc_id] # chat_service.py will now correctly find this
        candidates.append(item)

    # Step 4: Cross-Encoder Reranking
    reranker = get_reranker()
    final_results = reranker.rerank(query_text, candidates, top_k=top_k)

    return final_results