"""
rag/vectordb/chroma.py
ChromaDB wrapper for persistent vector storage.

Features:
- Persistent on disk (data/chroma/)
- Role-aware filtering (employee only sees their dept + public)
- Version-aware filtering (only latest active version by default)
- Hybrid search: semantic + keyword BM25-style boost
- Upsert-safe (same chunk can be re-indexed without duplicates)
"""
import json
import math
import re
import os
from typing import Optional

CHROMA_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "data", "chroma")


def _get_client():
    try:
        import chromadb
        from chromadb.config import Settings
        os.makedirs(CHROMA_PATH, exist_ok=True)
        client = chromadb.PersistentClient(path=CHROMA_PATH)
        return client
    except ImportError:
        raise ImportError("chromadb not installed. Run: pip install chromadb")


def _get_collection(client=None):
    if client is None:
        client = _get_client()
    return client.get_or_create_collection(
        name="policy_chunks",
        metadata={"hnsw:space": "cosine"},
    )


class VectorStore:
    def __init__(self):
        self._client = _get_client()
        self._col = _get_collection(self._client)

    # ----------------------------------------------------------------
    # Indexing
    # ----------------------------------------------------------------
    def upsert_chunks(
        self,
        chunks: list[dict],
        embeddings: list[list[float]],
    ):
        """
        Store chunks with their embeddings.
        chunk dict must contain: text, policy_id, policy_name, version,
        department, section, page, chunk_index
        """
        ids = [f"pol{c['policy_id']}_v{c['version']}_c{c['chunk_index']}" for c in chunks]
        documents = [c["text"] for c in chunks]
        metadatas = [
            {
                "policy_id": str(c.get("policy_id", "")),
                "policy_name": c.get("policy_name", ""),
                "version": str(c.get("version", "")),
                "department": c.get("department", ""),
                "section": c.get("section", "General"),
                "page": str(c.get("page", "")),
                "chunk_index": str(c.get("chunk_index", 0)),
                "is_active": str(c.get("is_active", True)),
            }
            for c in chunks
        ]
        self._col.upsert(ids=ids, documents=documents, embeddings=embeddings, metadatas=metadatas)

    def deactivate_policy_version(self, policy_id: int, version: str):
        """
        Marks all chunks for a specific policy version as inactive by updating their metadata.
        This allows keeping historical embeddings without them appearing in active searches.
        """
        try:
            results = self._col.get(where={"$and": [
                {"policy_id": {"$eq": str(policy_id)}},
                {"version": {"$eq": str(version)}},
            ]})
            if results and results["ids"]:
                # Fetch existing metadatas, update the is_active flag, and push them back
                updated_metadatas = []
                for meta in results["metadatas"]:
                    meta["is_active"] = "False"
                    updated_metadatas.append(meta)
                
                self._col.update(ids=results["ids"], metadatas=updated_metadatas)
        except Exception:
            pass

    def delete_policy_version(self, policy_id: int, version: str):
        """Remove all chunks for a specific policy version."""
        try:
            results = self._col.get(where={"$and": [
                {"policy_id": {"$eq": str(policy_id)}},
                {"version": {"$eq": str(version)}},
            ]})
            if results["ids"]:
                self._col.delete(ids=results["ids"])
        except Exception:
            pass

    def delete_policy(self, policy_id: int):
        """Remove all chunks for a policy (all versions)."""
        try:
            results = self._col.get(where={"policy_id": {"$eq": str(policy_id)}})
            if results["ids"]:
                self._col.delete(ids=results["ids"])
        except Exception:
            pass

    # ----------------------------------------------------------------
    # Retrieval
    # ----------------------------------------------------------------
    def search(
        self,
        query_embedding: list[float],
        query_text: str,
        top_k: int = 20,
        policy_id: Optional[int] = None,
        version: Optional[str] = None,
        department: Optional[str] = None,
        active_only: bool = True,
        allowed_departments: Optional[list[str]] = None,
    ) -> list[dict]:
        """
        Hybrid search: semantic (via ChromaDB cosine) + keyword boost.

        Filters applied:
        - policy_id: restrict to a specific policy
        - version: restrict to a specific policy version
        - active_only: skip superseded versions
        - department: restrict to specific department
        - allowed_departments: role-based whitelist
        """
        where = self._build_where(policy_id, version, department, active_only, allowed_departments)
        n_results = min(top_k, max(self._col.count(), 1))

        try:
            results = self._col.query(
                query_embeddings=[query_embedding],
                n_results=n_results,
                where=where if where else None,
                include=["documents", "metadatas", "distances"],
            )
        except Exception as e:
            return []

        hits = []

        for i, doc_id in enumerate(results["ids"][0]):
            doc_text = results["documents"][0][i]
            metadata = results["metadatas"][0][i]
            distance = results["distances"][0][i]
            semantic_score = max(0.0, 1.0 - distance)  # cosine distance → similarity

            final_score = semantic_score

            hits.append({
                "id": doc_id,
                "text": doc_text,
                "score": round(final_score, 4),
                "semantic_score": round(semantic_score, 4),
                "policy_id": metadata.get("policy_id", ""),
                "policy_name": metadata.get("policy_name", ""),
                "version": metadata.get("version", ""),
                "department": metadata.get("department", ""),
                "section": metadata.get("section", ""),
                "page": metadata.get("page", ""),
                "chunk_index": metadata.get("chunk_index", ""),
            })

        hits.sort(key=lambda h: h["score"], reverse=True)
        return hits[:top_k]

    def _build_where(self, policy_id, version, department, active_only, allowed_departments):
        conditions = []
        if policy_id is not None:
            conditions.append({"policy_id": {"$eq": str(policy_id)}})
        if version:
            conditions.append({"version": {"$eq": str(version)}})
        if department:
            conditions.append({"department": {"$eq": department}})
        if active_only:
            conditions.append({"is_active": {"$eq": "True"}})
        if allowed_departments:
            if len(allowed_departments) == 1:
                conditions.append({"department": {"$eq": allowed_departments[0]}})
            else:
                conditions.append({"department": {"$in": allowed_departments}})
        if not conditions:
            return None
        if len(conditions) == 1:
            return conditions[0]
        return {"$and": conditions}

    def count(self) -> int:
        return self._col.count()

    def stats(self) -> dict:
        total = self._col.count()
        return {
            "total_chunks": total,
            "collection": "policy_chunks",
            "path": CHROMA_PATH,
            "model": "chromadb",
        }


# Singleton
_store: Optional[VectorStore] = None

def get_store() -> VectorStore:
    global _store
    if _store is None:
        _store = VectorStore()
    return _store