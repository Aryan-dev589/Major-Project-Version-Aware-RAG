"""
rag/indexing/index_policy.py
Indexes a policy version into the vector store.
Called after HR uploads/publishes a policy.
"""
import json
from datetime import datetime


def index_policy_version(policy_id: int, version_id: int, app=None) -> dict:
    """
    Full indexing pipeline for one policy version:
    1. Load policy + version from DB
    2. Extract text
    3. Clean text
    4. Chunk with metadata
    5. Embed chunks
    6. Upsert into ChromaDB
    7. Save chunk metadata to SQL (policy_chunks table)
    8. Log indexing job
    """
    result = {"success": False, "chunks": 0, "error": None}

    try:
        if app:
            ctx = app.app_context()
            ctx.push()

        from models import db, Policy, PolicyVersion
        from rag.parser.pdf_parser import extract_text
        from rag.parser.text_cleaner import clean_text
        from rag.chunking.chunker import chunk_policy
        from rag.embeddings.embedder import get_embedder
        from rag.vectordb.chroma import get_store

        policy = Policy.query.get(policy_id)
        version = PolicyVersion.query.get(version_id)

        if not policy or not version:
            result["error"] = "Policy or version not found"
            return result

        # Get text — from content field or uploaded file
        raw_text = version.content or ""
        if not raw_text and hasattr(version, "file_path") and version.file_path:
            raw_text = extract_text(version.file_path)

        clean = clean_text(raw_text)
        if not clean.strip():
            result["error"] = "No text to index"
            return result

        dept_name = policy.department.name if policy.department else ""
        chunks = chunk_policy(
            text=clean,
            policy_id=policy_id,
            policy_name=policy.title,
            version=version.version_label,
            department=dept_name,
        )

        if not chunks:
            result["error"] = "No chunks produced"
            return result

        # Add active flag to metadata
        for c in chunks:
            c.metadata["is_active"] = version.is_active

        embedder = get_embedder()
        texts = [c.text for c in chunks]
        embeddings = embedder.embed(texts)

        store = get_store()
        # Delete old chunks for this version first
        store.delete_policy_version(policy_id, version.version_label)
        # Upsert new chunks
        chunk_dicts = [c.to_dict() for c in chunks]
        store.upsert_chunks(chunk_dicts, embeddings)

        # Save chunk metadata to SQL
        _save_chunks_to_db(policy_id, version_id, chunks)

        result["success"] = True
        result["chunks"] = len(chunks)

        # Log job
        _log_indexing_job(policy_id, version_id, len(chunks), None)

    except Exception as e:
        result["error"] = str(e)
        _log_indexing_job(policy_id, version_id, 0, str(e))

    return result


def delete_policy_from_index(policy_id: int):
    """Remove all vectors for a policy."""
    from rag.vectordb.chroma import get_store
    store = get_store()
    store.delete_policy(policy_id)
    # Remove from SQL chunks table
    try:
        from models import db, PolicyChunk
        PolicyChunk.query.filter_by(policy_id=policy_id).delete()
        db.session.commit()
    except Exception:
        pass


def _save_chunks_to_db(policy_id: int, version_id: int, chunks):
    try:
        from models import db, PolicyChunk
        PolicyChunk.query.filter_by(policy_id=policy_id, version_id=version_id).delete()
        for chunk in chunks:
            pc = PolicyChunk(
                policy_id=policy_id,
                version_id=version_id,
                section=chunk.section,
                page=chunk.page,
                chunk_index=chunk.chunk_index,
                text_preview=chunk.text[:300],
                char_count=len(chunk.text),
            )
            db.session.add(pc)
        db.session.commit()
    except Exception:
        pass


def _log_indexing_job(policy_id, version_id, chunks_count, error):
    try:
        from models import db, IndexingJob
        job = IndexingJob(
            policy_id=policy_id,
            version_id=version_id,
            chunks_indexed=chunks_count,
            status="success" if not error else "failed",
            error_message=error,
            completed_at=datetime.utcnow(),
        )
        db.session.add(job)
        db.session.commit()
    except Exception:
        pass
