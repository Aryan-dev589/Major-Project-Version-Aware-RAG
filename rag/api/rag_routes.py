"""
rag/api/rag_routes.py
Flask blueprint for all RAG endpoints.
Register in app.py: app.register_blueprint(rag_bp)
"""
import uuid
from datetime import datetime
from flask import Blueprint, request, jsonify, render_template, session, Response, stream_with_context
from flask_login import login_required, current_user
from models import db, ChatSession, ChatMessage, SearchHistory, Feedback, IndexingJob, PolicyChunk

rag_bp = Blueprint("rag", __name__, url_prefix="/rag")


# ─── Employee chat ─────────────────────────────────────────────────────────────

@rag_bp.route("/chat")
@login_required
def chat_page():
    """Render the AI chat interface for employees."""
    unread_count = _unread_count()
    try:
        doc_count = db.session.query(PolicyChunk.policy_id).distinct().count()
    except Exception:
        doc_count = 0
    return render_template("employee/chat.html", unread_count=unread_count, doc_count=doc_count)


@rag_bp.route("/api/chat", methods=["POST"])
@login_required
def api_chat():
    data = request.get_json(force=True)
    query = (data.get("query") or "").strip()
    session_id = data.get("session_id") or _get_or_create_session()

    if not query:
        return jsonify({"error": "Empty query"}), 400

    if len(query) > 1000:
        return jsonify({"error": "Query too long (max 1000 chars)"}), 400

    from rag.chatbot.chat_service import answer as rag_answer
    dept = current_user.department.name if current_user.department else ""
    result = rag_answer(
        query=query,
        session_id=session_id,
        user_role=current_user.role,
        user_department=dept,
    )

    # Persist to DB
    message_id = _save_message(session_id, query, result["answer"], result["citations"], result["chunks_used"])
    _save_search_history(query, result)

    return jsonify({
        "answer": result["answer"],
        "citations": result["citations"],
        "chunks_used": result["chunks_used"],
        "session_id": session_id,
        "fallback": result["fallback"],
        "message_id": message_id,
    })


@rag_bp.route("/api/chat/stream", methods=["POST"])
@login_required
def api_chat_stream():
    data = request.get_json(force=True)
    query = (data.get("query") or "").strip()
    session_id = data.get("session_id") or _get_or_create_session()

    if not query:
        return jsonify({"error": "Empty query"}), 400

    if len(query) > 1000:
        return jsonify({"error": "Query too long (max 1000 chars)"}), 400

    from rag.chatbot.chat_service import generate_chat_stream
    dept = current_user.department.name if current_user.department else ""

    return Response(
        stream_with_context(
            generate_chat_stream(
                query=query,
                session_id=session_id,
                user_role=current_user.role,
                user_department=dept,
            )
        ),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
        },
    )


@rag_bp.route("/api/feedback", methods=["POST"])
@login_required
def api_feedback():
    data = request.get_json(force=True)
    vote = data.get("vote")  # "up" or "down"
    comment = (data.get("comment") or "").strip()

    if vote not in ("up", "down"):
        return jsonify({"error": "Invalid vote"}), 400

    # message_id must be a real ChatMessage row id (integer) or None —
    # never trust it blindly, since the FK column will reject anything else.
    try:
        msg_id = int(data.get("message_id"))
    except (TypeError, ValueError):
        msg_id = None

    fb = Feedback(
        user_id=current_user.id,
        message_id=msg_id,
        vote=vote,
        comment=comment,
        created_at=datetime.utcnow(),
    )
    db.session.add(fb)
    db.session.commit()
    return jsonify({"ok": True})


@rag_bp.route("/api/sessions/<session_id>/history")
@login_required
def api_session_history(session_id):
    msgs = ChatMessage.query.filter_by(session_id=session_id)\
        .order_by(ChatMessage.created_at).all()
    return jsonify([{
        "id": m.id, "role": m.role, "content": m.content,
        "citations": m.citations_json, "created_at": m.created_at.isoformat(),
    } for m in msgs])


@rag_bp.route("/api/sessions/clear", methods=["POST"])
@login_required
def api_clear_session():
    sid = request.get_json(force=True).get("session_id")
    from rag.chatbot.memory import clear_session
    clear_session(sid)
    return jsonify({"ok": True})


# ─── Admin RAG dashboard ────────────────────────────────────────────────────────

@rag_bp.route("/admin/dashboard")
@login_required
def admin_rag_dashboard():
    if not current_user.can_manage_policies():
        from flask import abort
        abort(403)

    from rag.vectordb.chroma import get_store
    from rag.embeddings.embedder import get_embedder
    from rag.llm.gemini import get_llm, GeminiClient, OllamaClient

    try:
        store = get_store()
        vec_stats = store.stats()
    except Exception as e:
        vec_stats = {"total_chunks": 0, "error": str(e)}

    embedder = get_embedder()
    llm = get_llm()
    if isinstance(llm, GeminiClient):
        llm_provider = f"Gemini via OpenRouter ({llm.model})"
    elif isinstance(llm, OllamaClient):
        llm_provider = f"Ollama ({llm.model})"
    else:
        llm_provider = "Extractive fallback (no LLM_PROVIDER / API key configured)"
    chunk_count = PolicyChunk.query.count()
    indexed_policies = db.session.query(PolicyChunk.policy_id).distinct().count()
    recent_jobs = IndexingJob.query.order_by(IndexingJob.completed_at.desc()).limit(10).all()
    failed_jobs = IndexingJob.query.filter_by(status="failed").count()

    # Search analytics
    total_queries = SearchHistory.query.count()
    failed_queries = SearchHistory.query.filter_by(answered=False).count()

    # Feedback stats
    thumbs_up = Feedback.query.filter_by(vote="up").count()
    thumbs_down = Feedback.query.filter_by(vote="down").count()

    # Top searched queries
    from sqlalchemy import func
    top_queries = db.session.query(
        SearchHistory.query_text,
        func.count(SearchHistory.id).label("count")
    ).group_by(SearchHistory.query_text).order_by(func.count(SearchHistory.id).desc()).limit(8).all()

    unread_count = _unread_count()

    return render_template("admin/rag_dashboard.html",
        vec_stats=vec_stats,
        embedder_model=embedder.model_name,
        llm_provider=llm_provider,
        chunk_count=chunk_count,
        indexed_policies=indexed_policies,
        recent_jobs=recent_jobs,
        failed_jobs=failed_jobs,
        total_queries=total_queries,
        failed_queries=failed_queries,
        thumbs_up=thumbs_up,
        thumbs_down=thumbs_down,
        top_queries=top_queries,
        unread_count=unread_count,
    )


@rag_bp.route("/admin/index/<int:policy_id>/<int:version_id>", methods=["POST"])
@login_required
def admin_index_policy(policy_id, version_id):
    if not current_user.can_manage_policies():
        return jsonify({"error": "Forbidden"}), 403
    from rag.indexing.index_policy import index_policy_version
    result = index_policy_version(policy_id, version_id)
    return jsonify(result)


@rag_bp.route("/admin/delete/<int:policy_id>", methods=["POST"])
@login_required
def admin_delete_index(policy_id):
    if not current_user.can_manage_policies():
        return jsonify({"error": "Forbidden"}), 403
    from rag.indexing.index_policy import delete_policy_from_index
    delete_policy_from_index(policy_id)
    return jsonify({"ok": True})


# ─── Helpers ───────────────────────────────────────────────────────────────────

def _get_or_create_session() -> str:
    key = "rag_session_id"
    if key not in session:
        sid = str(uuid.uuid4())
        session[key] = sid
        cs = ChatSession(
            id=sid,
            user_id=current_user.id,
            created_at=datetime.utcnow(),
        )
        db.session.add(cs)
        db.session.commit()
    return session[key]


def _save_message(session_id, query, answer, citations, chunks_used):
    import json
    try:
        msg_user = ChatMessage(
            session_id=session_id, role="user", content=query,
            created_at=datetime.utcnow(),
        )
        db.session.add(msg_user)
        db.session.flush()

        msg_asst = ChatMessage(
            session_id=session_id, role="assistant", content=answer,
            citations_json=json.dumps(citations),
            chunks_used=chunks_used,
            created_at=datetime.utcnow(),
        )
        db.session.add(msg_asst)
        db.session.commit()
        return msg_asst.id
    except Exception:
        pass


def _save_search_history(query, result):
    try:
        sh = SearchHistory(
            user_id=current_user.id,
            query_text=query[:500],
            answered=not result["fallback"],
            chunks_found=result["chunks_used"],
            created_at=datetime.utcnow(),
        )
        db.session.add(sh)
        db.session.commit()
    except Exception:
        pass


def _unread_count():
    from models import Notification
    try:
        return Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
    except Exception:
        return 0
