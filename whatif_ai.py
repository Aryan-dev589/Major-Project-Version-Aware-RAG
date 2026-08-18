"""
whatif_ai.py
AI engine for Module 25 — Policy Impact Simulator ("What-If" Compliance Checker).
Grounded in retrieved policy chunks, evaluated using local Ollama (Qwen 2.5 7B).
"""
import json
import re

from rag.embeddings.embedder import get_embedder
from rag.vectordb.chroma import get_store
from rag.reranker.reranker import get_reranker
from rag.llm.gemini import get_llm

RELEVANCE_THRESHOLD = 0.05  # Lower threshold to pass more context to LLM


def _build_scenario_payload(scenario: str, user_role: str = "employee", user_department: str = "",
                           top_k_retrieve: int = 20, top_k_rerank: int = 6):
    scenario = (scenario or "").strip()
    if not scenario:
        return {
            "citations": [],
            "top_chunks": [],
            "user_prompt": "",
            "flagged_for_hr": False,
            "fallback": True,
            "error": "No scenario provided.",
        }

    embedder = get_embedder()
    store = get_store()
    reranker = get_reranker()

    allowed_depts = None
    if user_role == "employee" and user_department:
        allowed_depts = [user_department, ""]

    q_vec = embedder.embed_query(scenario)
    hits = store.search(
        query_embedding=q_vec, query_text=scenario, top_k=top_k_retrieve,
        active_only=True, allowed_departments=allowed_depts,
    )
    hits = [h for h in hits if h.get("score", 0) >= RELEVANCE_THRESHOLD]
    top_chunks = reranker.rerank(scenario, hits, top_k=top_k_rerank) if hits else []

    citations = []
    seen = set()
    for c in top_chunks:
        key = (c.get("policy_name"), c.get("version"), c.get("section"))
        if key in seen:
            continue
        seen.add(key)
        citations.append({
            "policy_name": c.get("policy_name", "Unknown"),
            "version": c.get("version", "N/A"),
            "section": c.get("section", "General"),
            "page": c.get("page", "N/A"),
            "policy_id": c.get("policy_id", ""),
            "relevance_score": round(c.get("rerank_score", c.get("score", 0)), 3),
        })

    if top_chunks:
        excerpts = "\n\n---\n\n".join(
            f"[{c.get('policy_name')} — {c.get('section', 'General')}]\n{c.get('text', '')[:1200]}"
            for c in top_chunks
        )
    else:
        excerpts = "NO DIRECTLY RELEVANT POLICY EXCERPTS FOUND IN DATABASE."

    user_prompt = f"EMPLOYEE SCENARIO:\n{scenario}\n\nRELEVANT POLICY EXCERPTS:\n{excerpts}\n\nProvide the JSON verdict now."

    return {
        "citations": citations,
        "top_chunks": top_chunks,
        "user_prompt": user_prompt,
        "flagged_for_hr": False,
        "fallback": False,
        "error": None,
    }


def _extract_json(text: str) -> dict | None:
    """Robustly extract and parse JSON from LLM outputs, removing markdown/think tags."""
    if not text:
        return None
    # Strip thinking tags if present
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    # Strip markdown code blocks
    text = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r"\s*```$", "", text)
    
    # Direct attempt
    try:
        obj = json.loads(text.strip())
        if isinstance(obj, dict):
            return obj
    except (ValueError, TypeError):
        pass

    # Regex extraction for JSON object block
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            obj = json.loads(match.group(0))
            if isinstance(obj, dict):
                return obj
        except (ValueError, TypeError):
            pass

    return None


VERDICT_SYSTEM_PROMPT = """You are an enterprise compliance policy assistant. An employee describes a real-world scenario.
Evaluate the scenario strictly against the provided company policy excerpts.

Respond with ONLY a raw JSON object with this exact schema (no preamble, no markdown formatting):
{
  "verdict": "compliant" | "not_compliant" | "depends" | "unclear",
  "confidence": <integer 0-100>,
  "explanation": "3-5 sentence explanation directed to the employee",
  "required_actions": ["concrete action 1", "concrete action 2"],
  "applicable_sections": ["Policy Name - Section X"]
}

Rules:
- "compliant": clearly permitted by policy.
- "not_compliant": clearly violates policy.
- "depends": permitted only under conditions (e.g., approval, duration caps). Explain the conditions.
- "unclear": provided excerpts do not explicitly cover this scenario or context is missing.
"""


def _materialize_result(parsed: dict | None, top_chunks: list[dict]) -> dict:
    if not parsed:
        if not top_chunks:
            return {
                "verdict": "unclear", "confidence": 0,
                "explanation": "No specific policy content was found matching this scenario in the company handbook. Please consult HR directly.",
                "required_actions": ["Consult with HR or your department manager."],
                "applicable_sections": []
            }
        top = top_chunks[0]
        return {
            "verdict": "unclear", "confidence": 30,
            "explanation": f"Relevant content found under '{top.get('policy_name', 'Policy')}' ({top.get('section', 'General')}), but a definitive verdict could not be structured automatically.",
            "required_actions": ["Review the cited section with HR."],
            "applicable_sections": [f"{top.get('policy_name', 'Policy')} - {top.get('section', 'General')}"]
        }

    return {
        "verdict": parsed.get("verdict") if parsed.get("verdict") in ("compliant", "not_compliant", "depends", "unclear") else "unclear",
        "confidence": max(0, min(100, int(parsed.get("confidence", 0) or 0))),
        "explanation": str(parsed.get("explanation", "")).strip() or "No explanation generated.",
        "required_actions": [str(a) for a in (parsed.get("required_actions") or [])],
        "applicable_sections": [str(a) for a in (parsed.get("applicable_sections") or [])],
    }


def stream_evaluate_scenario(scenario: str, user_role: str = "employee", user_department: str = "",
                            top_k_retrieve: int = 20, top_k_rerank: int = 6):
    prepared = _build_scenario_payload(scenario, user_role, user_department, top_k_retrieve, top_k_rerank)
    if prepared.get("error"):
        result = {
            "verdict": "unclear",
            "confidence": 0,
            "explanation": prepared["error"],
            "required_actions": [],
            "citations": [],
            "flagged_for_hr": False,
            "chunks_used": 0,
        }
        yield f"data: {json.dumps({'done': True, 'result': result})}\n\n"
        return

    llm = get_llm()
    messages = [
        {"role": "system", "content": VERDICT_SYSTEM_PROMPT},
        {"role": "user", "content": prepared["user_prompt"]},
    ]

    buffer = ""
    try:
        stream_fn = getattr(llm, "stream_complete", None)
        if callable(stream_fn):
            for token in stream_fn(messages):
                if token is None:
                    continue
                token = str(token)
                buffer += token
                yield f"data: {json.dumps({'text': token})}\n\n"
        else:
            raw = llm.complete(messages)
            buffer = raw or ""
            for ch in raw or "":
                yield f"data: {json.dumps({'text': ch})}\n\n"
    except Exception as e:
        print(f"[What-If LLM Error]: {e}")
        buffer = ""

    parsed = _extract_json(buffer)
    result = _materialize_result(parsed, prepared["top_chunks"])
    flagged = (
        result["verdict"] in ("depends", "unclear") or
        result.get("confidence", 0) < 55 or
        result["verdict"] == "not_compliant"
    )
    final_result = {
        "verdict": result["verdict"],
        "confidence": result.get("confidence", 0),
        "explanation": result["explanation"],
        "required_actions": result.get("required_actions", []),
        "citations": prepared["citations"],
        "flagged_for_hr": flagged,
        "chunks_used": len(prepared["top_chunks"]),
    }
    yield f"data: {json.dumps({'done': True, 'result': final_result})}\n\n"


def evaluate_scenario(scenario: str, user_role: str = "employee", user_department: str = "",
                      top_k_retrieve: int = 20, top_k_rerank: int = 6) -> dict:
    scenario = (scenario or "").strip()
    if not scenario:
        return {
            "verdict": "unclear", "confidence": 0, "explanation": "No scenario provided.",
            "required_actions": [], "citations": [], "flagged_for_hr": False, "chunks_used": 0
        }

    prepared = _build_scenario_payload(scenario, user_role, user_department, top_k_retrieve, top_k_rerank)
    top_chunks = prepared["top_chunks"]
    citations = prepared["citations"]

    llm = get_llm()
    parsed = None
    try:
        raw = llm.complete([
            {"role": "system", "content": VERDICT_SYSTEM_PROMPT},
            {"role": "user", "content": prepared["user_prompt"]},
        ])
        parsed = _extract_json(raw)
    except Exception as e:
        print(f"[What-If LLM Error]: {e}")

    result = _materialize_result(parsed, top_chunks)
    flagged = (
        result["verdict"] in ("depends", "unclear") or
        result.get("confidence", 0) < 55 or
        result["verdict"] == "not_compliant"
    )

    return {
        "verdict": result["verdict"],
        "confidence": result.get("confidence", 0),
        "explanation": result["explanation"],
        "required_actions": result.get("required_actions", []),
        "citations": citations,
        "flagged_for_hr": flagged,
        "chunks_used": len(top_chunks),
    }