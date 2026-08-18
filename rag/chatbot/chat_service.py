"""
rag/chatbot/chat_service.py
Full RAG pipeline: question → embed → retrieve → rerank → prompt → LLM → cite
"""
import json

from rag.embeddings.embedder import get_embedder
from rag.retrieval.hybrid_search import hybrid_search
from rag.llm.prompt_builder import build_prompt
from rag.llm.gemini import get_llm
from rag.chatbot.citations import build_citations, format_citations_text
from rag.chatbot.memory import get_history, add_message

RELEVANCE_THRESHOLD = 0.05


def generate_chat_stream(
    query: str,
    session_id: str,
    user_role: str = "employee",
    user_department: str = "",
    top_k_retrieve: int = 20,
    top_k_rerank: int = 5,
):
    """Yield a stream of SSE-formatted tokens for the RAG answer."""
    embedder = get_embedder()
    llm = get_llm()

    try:
        model_name = getattr(llm, "model", "unknown")
    except Exception:
        model_name = "unknown"
    print(f"[RAG] Using LLM client={llm.__class__.__name__} model={model_name}")

    allowed_depts = None
    if user_role == "employee" and user_department:
        allowed_depts = [user_department, ""]
    elif user_role in ("hr", "admin"):
        allowed_depts = None

    q_vec = embedder.embed_query(query)
    top_chunks = hybrid_search(
        query_text=query,
        query_embedding=q_vec,
        candidate_limit=top_k_retrieve,
        top_k=top_k_rerank,
        allowed_departments=allowed_depts,
    )
    valid_chunks = [c for c in top_chunks if c.get("rerank_score", c.get("whm_score", 0)) >= RELEVANCE_THRESHOLD]

    if not valid_chunks:
        answer_text = "I couldn't find this information in the available policies."
        add_message(session_id, "user", query)
        add_message(session_id, "assistant", answer_text)
        yield f"data: {json.dumps({'text': answer_text, 'done': True, 'fallback': True, 'chunks_used': 0, 'session_id': session_id})}\n\n"
        return

    history = get_history(session_id)
    messages = build_prompt(query, valid_chunks, chat_history=history)
    citations = build_citations(valid_chunks)
    final_tokens = []

    try:
        stream_fn = getattr(llm, "stream_complete", None)
        if callable(stream_fn):
            for token in stream_fn(messages):
                if token is None:
                    continue
                final_tokens.append(str(token))
                yield f"data: {json.dumps({'text': str(token)})}\n\n"
        else:
            answer_text = llm.complete(messages)
            for token in answer_text:
                final_tokens.append(token)
                yield f"data: {json.dumps({'text': token})}\n\n"
    except Exception:
        answer_text = "I couldn't find this information in the available policies."
        final_tokens.append(answer_text)
        yield f"data: {json.dumps({'text': answer_text})}\n\n"

    answer_text = "".join(final_tokens).strip() or "I couldn't find this information in the available policies."
    add_message(session_id, "user", query)
    add_message(session_id, "assistant", answer_text)
    yield f"data: {json.dumps({'done': True, 'text': '', 'answer': answer_text, 'citations': citations, 'chunks_used': len(valid_chunks), 'session_id': session_id, 'fallback': False})}\n\n"


def answer(
    query: str,
    session_id: str,
    user_role: str = "employee",
    user_department: str = "",
    top_k_retrieve: int = 20,
    top_k_rerank: int = 5,
) -> dict:
    """
    Full pipeline. Returns:
    {
        "answer": str,
        "citations": list[dict],
        "chunks_used": int,
        "session_id": str,
        "fallback": bool,
    }
    """
    embedder = get_embedder()
    llm = get_llm()
    # Log the active LLM client and model for debugging
    try:
        model_name = getattr(llm, "model", "unknown")
    except Exception:
        model_name = "unknown"
    print(f"[RAG] Using LLM client={llm.__class__.__name__} model={model_name}")

    # Role-based department filtering
    allowed_depts = None
    if user_role == "employee" and user_department:
        allowed_depts = [user_department, ""]  # own dept + company-wide
    elif user_role in ("hr", "admin"):
        allowed_depts = None  # no restriction

    # 1. Embed query
    q_vec = embedder.embed_query(query)

    # 2. Retrieve & Rerank via Hybrid Search (Dense + BM25 + WHM + Cross-Encoder)
    top_chunks = hybrid_search(
        query_text=query,
        query_embedding=q_vec,
        candidate_limit=top_k_retrieve,
        top_k=top_k_rerank,
        allowed_departments=allowed_depts,
    )
    
    # 3. Filter by relevance threshold
    # Look for the rerank_score, fallback to whm_score
    valid_chunks = [c for c in top_chunks if c.get("rerank_score", c.get("whm_score", 0)) >= RELEVANCE_THRESHOLD]

    print(f"DEBUG: Found {len(valid_chunks)} chunks above threshold!")  # DEBUG PRINT 1

    if not valid_chunks:
        answer_text = "I couldn't find this information in the available policies."
        add_message(session_id, "user", query)
        add_message(session_id, "assistant", answer_text)
        return {
            "answer": answer_text,
            "citations": [],
            "chunks_used": 0,
            "session_id": session_id,
            "fallback": True,
        }

    # 4. Build prompt with conversation memory
    history = get_history(session_id)
    messages = build_prompt(query, valid_chunks, chat_history=history)

    print(f"DEBUG FINAL MESSAGES: {messages}")  # DEBUG PRINT 2

    # 5. Generate answer
    answer_text = llm.complete(messages)

    # 6. Citations
    citations = build_citations(valid_chunks)

    # 7. Update memory
    add_message(session_id, "user", query)
    add_message(session_id, "assistant", answer_text)

    return {
        "answer": answer_text,
        "citations": citations,
        "chunks_used": len(valid_chunks),
        "session_id": session_id,
        "fallback": False,
    }