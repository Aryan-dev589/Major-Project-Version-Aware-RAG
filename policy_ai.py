"""
policy_ai.py
AI engine for Module 3 (AI Policy Assistant).

Reuses the existing rag/llm provider abstraction. Three families of tools:

  Writer    — generate_policy_draft, rewrite_text (rewrite/simplify/grammar/
              tone/translate all funnel through one instruction-driven call),
              explain_clause
  Review    — review_policy (missing sections, compliance flags, risk score,
              legal-language issues, suggestions) + find_duplicates (heuristic,
              no LLM needed) + find_conflicts (LLM, only run against the
              top duplicate candidates to keep cost bounded)
  Insights  — generate_insights (summary, executive summary, FAQ, quiz, key
              points, impact analysis, reading time) + summarize_changes
              (diff-based "what changed" between two versions)

Every LLM-backed function degrades gracefully (returns a clearly-labeled
partial/fallback result) rather than raising, so the feature stays usable
without an API key configured — consistent with meeting_ai.py.
"""
import json
import re
import difflib
from datetime import datetime

from rag.llm.gemini import get_llm

WORDS_PER_MINUTE = 200


# ================================================================
# Shared helpers
# ================================================================
def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _extract_json(text: str) -> dict | None:
    for candidate in (text, _strip_fences(text)):
        try:
            obj = json.loads(candidate)
            if isinstance(obj, dict):
                return obj
        except (ValueError, TypeError):
            continue
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if match:
        try:
            obj = json.loads(match.group(0))
            if isinstance(obj, dict):
                return obj
        except (ValueError, TypeError):
            pass
    return None


_REFUSAL_MARKER = "I couldn't find this information"


def _llm_text(system_prompt: str, user_prompt: str) -> str:
    llm = get_llm()
    try:
        result = llm.complete([
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]).strip()
    except Exception:
        return ""
    # The zero-dependency ExtractiveClient fallback (used when no LLM provider is
    # configured) is shaped for the RAG chat prompt format and always refuses
    # outside it — treat that as "no result" so callers use their own fallback.
    if _REFUSAL_MARKER in result:
        return ""
    return result


def reading_time_minutes(content: str) -> int:
    words = len((content or "").split())
    return max(1, round(words / WORDS_PER_MINUTE))


# ================================================================
# AI Writer
# ================================================================
def generate_policy_draft(title: str, description: str = "", category_name: str = "",
                          key_points: str = "") -> str:
    """Module 3 — 'Generate policy'. Drafts a new policy from a topic/title."""
    system = ("You are a corporate policy writer. Write clear, professional, well-structured "
             "policy documents with numbered sections (Purpose, Scope, Policy Details, "
             "Responsibilities, Compliance, Definitions as relevant). Plain text output only, "
             "no markdown formatting, no commentary before or after the policy text itself.")
    user = (f"Write a complete policy document titled '{title}'.\n"
           f"Category: {category_name or 'General'}\n"
           f"Description: {description or '(not provided)'}\n"
           f"Key points to include: {key_points or '(use your judgment based on the title/category)'}\n\n"
           "Produce the full policy text now.")
    result = _llm_text(system, user)
    if result:
        return result
    return (
        f"{title}\n\n"
        "1. Purpose\n[Describe why this policy exists.]\n\n"
        "2. Scope\n[Describe who and what this policy applies to.]\n\n"
        "3. Policy Details\n"
        f"{key_points or '[Add the specific rules and requirements here.]'}\n\n"
        "4. Responsibilities\n[Describe who is responsible for what.]\n\n"
        "5. Compliance\n[Describe consequences of non-compliance.]\n\n"
        "(AI draft unavailable — this is a starter template. Configure an LLM provider for full AI generation.)"
    )


REWRITE_INSTRUCTIONS = {
    "rewrite": "Rewrite the following policy text to be clearer and better organized, preserving all rules and meaning exactly.",
    "simplify": "Simplify the following policy text into plain, easy-to-understand language suitable for a general employee audience, at roughly an 8th-grade reading level. Preserve all rules and meaning.",
    "grammar": "Correct all grammar, spelling, and punctuation errors in the following policy text. Do not change the meaning, tone, or structure otherwise.",
    "tone": "Rewrite the following policy text in a more professional, formal, corporate tone. Preserve all rules and meaning.",
}


def rewrite_text(content: str, mode: str, target_language: str = "") -> str:
    """Module 3 — 'Rewrite / Simplify / Improve grammar / Professional tone / Translate policy'."""
    if not (content or "").strip():
        return ""
    if mode == "translate":
        instruction = f"Translate the following policy text into {target_language or 'Spanish'}, preserving formatting and section structure."
    else:
        instruction = REWRITE_INSTRUCTIONS.get(mode, REWRITE_INSTRUCTIONS["rewrite"])

    system = "You are an expert corporate policy editor. Output ONLY the resulting text — no preamble, no explanation, no markdown fences."
    user = f"{instruction}\n\n---\n{content}\n---"
    result = _llm_text(system, user)
    return result or content


def explain_clause(content: str, clause_text: str) -> str:
    """Module 3 — 'Explain clauses'."""
    system = "You are a helpful compliance assistant. Explain policy clauses in plain, simple language for employees."
    user = (f"Here is the full policy for context:\n---\n{content[:4000]}\n---\n\n"
           f"Explain this specific clause in plain language, including what it means practically for an employee:\n\n\"{clause_text}\"")
    result = _llm_text(system, user)
    return result or "AI explanation unavailable right now. Please configure an LLM provider or ask HR directly."


# ================================================================
# AI Review
# ================================================================
REVIEW_SYSTEM_PROMPT = """You are a corporate policy compliance reviewer. Analyze the given policy text and respond with ONLY a single valid JSON object, no markdown fences, no commentary:

{
  "missing_sections": ["section name that should exist but doesn't, e.g. 'Definitions'", "..."],
  "compliance_issues": ["potential compliance gap or risk, referencing common frameworks like GDPR/ISO 27001/labour law where relevant", "..."],
  "legal_language_issues": ["vague, ambiguous, or legally weak phrasing found in the text, quoted briefly", "..."],
  "suggestions": ["concrete improvement suggestion", "..."],
  "risk_score": <integer 0-100, higher = more risk/gaps>
}

Rules:
- Base findings strictly on the text provided. Use empty lists / a moderate risk_score if nothing notable is found.
- Keep each entry short (one sentence).
"""


def _heuristic_review(content: str) -> dict:
    lower = content.lower()
    common_sections = ["purpose", "scope", "responsibilit", "compliance", "definition", "policy detail"]
    missing = [s.title() for s in common_sections if s not in lower]
    vague_terms = ["as appropriate", "if necessary", "from time to time", "reasonable efforts", "may vary"]
    legal_issues = [f'Vague phrase found: "{t}"' for t in vague_terms if t in lower]
    risk = min(100, 20 + len(missing) * 10 + len(legal_issues) * 8)
    return {
        "missing_sections": missing,
        "compliance_issues": [],
        "legal_language_issues": legal_issues,
        "suggestions": ["Configure an LLM provider for deeper compliance analysis."],
        "risk_score": risk,
    }


def review_policy(content: str) -> dict:
    """Module 3 — 'AI Review': missing sections, compliance check, risk scoring, legal language checker, suggestions."""
    if not (content or "").strip():
        return {"missing_sections": [], "compliance_issues": [], "legal_language_issues": [],
               "suggestions": [], "risk_score": 0}

    raw = _llm_text(REVIEW_SYSTEM_PROMPT, f"POLICY TEXT:\n{content}\n\nProduce the JSON now.")
    parsed = _extract_json(raw) if raw else None
    if not parsed:
        parsed = _heuristic_review(content)

    return {
        "missing_sections": [str(s) for s in (parsed.get("missing_sections") or [])],
        "compliance_issues": [str(s) for s in (parsed.get("compliance_issues") or [])],
        "legal_language_issues": [str(s) for s in (parsed.get("legal_language_issues") or [])],
        "suggestions": [str(s) for s in (parsed.get("suggestions") or [])],
        "risk_score": max(0, min(100, int(parsed.get("risk_score", 50) or 0))),
    }


def find_duplicates(target_content: str, candidates: list, threshold: float = 0.55) -> list:
    """
    Module 3 — 'Duplicate detection'. Pure heuristic (difflib), no LLM needed —
    fast, deterministic, and works with zero configuration.
    `candidates` is a list of (policy_id, title, content) tuples to compare against.
    Returns a list of {policy_id, title, similarity} sorted by similarity desc.
    """
    matches = []
    for policy_id, title, content in candidates:
        if not content:
            continue
        ratio = difflib.SequenceMatcher(None, target_content, content).quick_ratio()
        if ratio >= threshold:
            matches.append({"policy_id": policy_id, "title": title, "similarity": round(ratio * 100)})
    return sorted(matches, key=lambda m: -m["similarity"])[:5]


def find_conflicts(target_title: str, target_content: str, candidates: list) -> list:
    """
    Module 3 — 'Conflict detection'. Only called against a short list of
    already-similar candidates (from find_duplicates) to keep LLM cost bounded.
    `candidates` is a list of (policy_id, title, content) tuples.
    Returns a list of {policy_id, title, conflict_description}.
    """
    conflicts = []
    system = ("You compare two corporate policies for direct contradictions (e.g. one says "
             "3 remote days, the other says 2). Respond with ONLY JSON: "
             '{"conflict": true/false, "description": "one sentence describing the contradiction, or empty string"}')
    for policy_id, title, content in candidates[:3]:
        user = (f"POLICY A ('{target_title}'):\n{target_content[:2500]}\n\n"
               f"POLICY B ('{title}'):\n{content[:2500]}\n\nCompare them now.")
        raw = _llm_text(system, user)
        parsed = _extract_json(raw) if raw else None
        if parsed and parsed.get("conflict"):
            conflicts.append({
                "policy_id": policy_id, "title": title,
                "conflict_description": str(parsed.get("description", "")).strip() or "Potential contradiction detected.",
            })
    return conflicts


# ================================================================
# AI Insights
# ================================================================
INSIGHTS_SYSTEM_PROMPT = """You generate employee-facing insights for a corporate policy. Respond with ONLY a single valid JSON object, no markdown fences:

{
  "summary": "1-2 sentence plain-language summary",
  "executive_summary": "3-4 sentence summary for leadership, focusing on business impact and risk",
  "key_points": ["short key point", "..."],
  "faq": [{"question": "...", "answer": "..."}],
  "quiz": [{"question": "...", "options": ["A","B","C","D"], "correct_index": 0}],
  "impact_analysis": "1-2 sentences on who/what is affected by this policy and how"
}

Generate 3-5 FAQ entries and 3 quiz questions grounded strictly in the text. Keep answers concise.
"""


def _heuristic_insights(content: str, title: str) -> dict:
    first_lines = [l.strip() for l in content.splitlines() if l.strip()][:5]
    return {
        "summary": f"{title}: see full policy text for details (AI summary unavailable).",
        "executive_summary": "AI executive summary unavailable — configure an LLM provider.",
        "key_points": first_lines,
        "faq": [],
        "quiz": [],
        "impact_analysis": "AI impact analysis unavailable — configure an LLM provider.",
    }


def generate_insights(content: str, title: str) -> dict:
    """Module 3 — 'AI Insights': policy summary, executive summary, FAQ, quiz, key points, impact analysis, reading time."""
    if not (content or "").strip():
        return {"summary": "", "executive_summary": "", "key_points": [], "faq": [], "quiz": [],
               "impact_analysis": "", "reading_time_minutes": 0}

    raw = _llm_text(INSIGHTS_SYSTEM_PROMPT, f"POLICY TITLE: {title}\n\nPOLICY TEXT:\n{content}\n\nProduce the JSON now.")
    parsed = _extract_json(raw) if raw else None
    if not parsed:
        parsed = _heuristic_insights(content, title)

    faq = []
    for item in (parsed.get("faq") or []):
        if isinstance(item, dict) and item.get("question"):
            faq.append({"question": str(item["question"]), "answer": str(item.get("answer", ""))})

    quiz = []
    for item in (parsed.get("quiz") or []):
        if isinstance(item, dict) and item.get("question") and item.get("options"):
            quiz.append({
                "question": str(item["question"]),
                "options": [str(o) for o in item["options"]][:6],
                "correct_index": int(item.get("correct_index", 0) or 0),
            })

    return {
        "summary": str(parsed.get("summary", "") or ""),
        "executive_summary": str(parsed.get("executive_summary", "") or ""),
        "key_points": [str(p) for p in (parsed.get("key_points") or [])],
        "faq": faq,
        "quiz": quiz,
        "impact_analysis": str(parsed.get("impact_analysis", "") or ""),
        "reading_time_minutes": reading_time_minutes(content),
    }


def summarize_changes(old_content: str, new_content: str) -> str:
    """Module 3 — 'Important changes' (used alongside version diffing)."""
    if not old_content:
        return "Initial version — no prior version to compare."
    system = "You summarize what changed between two policy versions, in plain language, for employees who already know the old version."
    user = f"OLD VERSION:\n{old_content}\n\nNEW VERSION:\n{new_content}\n\nSummarize the important changes in 2-4 bullet points (plain text, one per line, no markdown)."
    result = _llm_text(system, user)
    if result:
        return result
    diff_lines = list(difflib.unified_diff(old_content.splitlines(), new_content.splitlines(), lineterm=""))
    added = sum(1 for l in diff_lines if l.startswith("+") and not l.startswith("+++"))
    removed = sum(1 for l in diff_lines if l.startswith("-") and not l.startswith("---"))
    return f"{added} line(s) added, {removed} line(s) removed. (AI summary unavailable — configure an LLM provider for a plain-language explanation.)"
