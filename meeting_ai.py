"""
meeting_ai.py
AI generation for Module 5 (Meeting Management / MOM).

Reuses the existing rag/llm provider abstraction (Gemini via OpenRouter, Ollama,
or the zero-dependency extractive fallback) so no new API keys or dependencies
are required. Given raw meeting notes or a pasted transcript, produces:

  - executive summary
  - detailed minutes
  - key discussion points
  - decisions made
  - action items (description, owner, due date, priority)
  - a follow-up email draft to participants

If the configured LLM can't produce valid structured JSON (e.g. no API key is
set and the extractive fallback is active), a rule-based heuristic parser is
used instead so the feature still works out of the box.
"""
import json
import re
from datetime import datetime, timedelta, date

from rag.llm.gemini import get_llm

SYSTEM_PROMPT = """You are an assistant that converts raw meeting notes or transcripts into structured Minutes of Meeting (MOM) for a corporate policy/knowledge platform.

Respond with ONLY a single valid JSON object — no markdown fences, no commentary before or after. Use exactly this shape:

{
  "summary": "2-3 sentence executive summary of the meeting",
  "full_minutes": "A well-organized, multi-paragraph minutes document covering what was discussed",
  "key_points": ["short bullet point", "short bullet point", "..."],
  "decisions": ["decision made", "decision made", "..."],
  "action_items": [
    {"description": "what needs to be done", "owner_name": "person's name or empty string if unclear", "due_date": "YYYY-MM-DD or empty string if not mentioned", "priority": "low|medium|high"}
  ],
  "followup_email": "A short, professional follow-up email to participants summarizing decisions and action items, ready to send"
}

Rules:
- Base everything strictly on the provided notes. Do not invent facts, numbers, or names that are not implied by the notes.
- If the notes don't mention a due date for an action item, use an empty string for due_date.
- Keep key_points and decisions concise, one idea per entry.
- If there is genuinely nothing to extract for a field, use an empty list or empty string for it — never omit the key.
"""


def _strip_json_fences(text: str) -> str:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _extract_json_object(text: str) -> dict | None:
    """Try progressively looser strategies to pull a JSON object out of LLM output."""
    candidates = [text, _strip_json_fences(text)]
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        candidates.append(match.group(0))
    for c in candidates:
        try:
            obj = json.loads(c)
            if isinstance(obj, dict):
                return obj
        except (ValueError, TypeError):
            continue
    return None


def _heuristic_fallback(raw_notes: str) -> dict:
    """
    Zero-dependency rule-based extraction, used when the LLM is unavailable
    or doesn't return valid JSON. Looks for common note-taking conventions:
      "Decision: ..."          -> decisions
      "Action: ... - Name - date" / "TODO: ..." -> action items
      everything else meaningful -> key points
    """
    lines = [l.strip("-• \t") for l in raw_notes.splitlines() if l.strip()]
    decisions, action_items, key_points = [], [], []

    action_re = re.compile(r"^(action|todo|follow[- ]?up)\s*[:\-]\s*(.+)$", re.IGNORECASE)
    decision_re = re.compile(r"^decision\s*[:\-]\s*(.+)$", re.IGNORECASE)
    owner_re = re.compile(r"@(\w+)")
    date_re = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")

    for line in lines:
        dm = decision_re.match(line)
        am = action_re.match(line)
        if dm:
            decisions.append(dm.group(1).strip())
        elif am:
            body = am.group(2).strip()
            owner_match = owner_re.search(body)
            date_match = date_re.search(body)
            action_items.append({
                "description": owner_re.sub("", date_re.sub("", body)).strip(" -,"),
                "owner_name": owner_match.group(1) if owner_match else "",
                "due_date": date_match.group(1) if date_match else "",
                "priority": "medium",
            })
        else:
            if len(line) > 8:
                key_points.append(line)

    summary = " ".join(key_points[:2]) or "Meeting notes recorded; no summary could be generated automatically."
    full_minutes = "\n\n".join(key_points) or raw_notes.strip()

    return {
        "summary": summary[:600],
        "full_minutes": full_minutes,
        "key_points": key_points[:12],
        "decisions": decisions,
        "action_items": action_items,
        "followup_email": (
            f"Hi all,\n\nThanks for joining. Quick recap:\n\n"
            + "\n".join(f"- {p}" for p in key_points[:6])
            + ("\n\nAction items:\n" + "\n".join(f"- {a['description']}" for a in action_items) if action_items else "")
            + "\n\nBest regards"
        ),
    }


def generate_mom(raw_notes: str, meeting_title: str = "", agenda: str = "") -> dict:
    """
    Main entry point. Returns a dict matching the JSON shape described in
    SYSTEM_PROMPT, always with all keys present (never raises).
    """
    raw_notes = (raw_notes or "").strip()
    if not raw_notes:
        return {
            "summary": "", "full_minutes": "", "key_points": [],
            "decisions": [], "action_items": [], "followup_email": "",
        }

    llm = get_llm()
    user_prompt = (
        f"MEETING TITLE: {meeting_title or '(untitled)'}\n"
        f"AGENDA: {agenda or '(none provided)'}\n\n"
        f"RAW NOTES / TRANSCRIPT:\n{raw_notes}\n\n"
        "Produce the JSON object described in your instructions now."
    )
    raw_response = ""
    try:
        raw_response = llm.complete([
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ])
    except Exception:
        raw_response = ""

    parsed = _extract_json_object(raw_response) if raw_response else None

    if not parsed or "I couldn't find this information" in (raw_response or ""):
        parsed = _heuristic_fallback(raw_notes)

    # Normalize / defend against a slightly malformed LLM response
    result = {
        "summary": str(parsed.get("summary", "") or ""),
        "full_minutes": str(parsed.get("full_minutes", "") or ""),
        "key_points": [str(p) for p in (parsed.get("key_points") or []) if str(p).strip()],
        "decisions": [str(d) for d in (parsed.get("decisions") or []) if str(d).strip()],
        "followup_email": str(parsed.get("followup_email", "") or ""),
        "action_items": [],
    }
    for item in (parsed.get("action_items") or []):
        if not isinstance(item, dict):
            continue
        desc = str(item.get("description", "")).strip()
        if not desc:
            continue
        result["action_items"].append({
            "description": desc,
            "owner_name": str(item.get("owner_name", "") or "").strip(),
            "due_date": str(item.get("due_date", "") or "").strip(),
            "priority": item.get("priority") if item.get("priority") in ("low", "medium", "high") else "medium",
        })
    return result


def parse_due_date(due_date_str: str):
    """Best-effort conversion of a due_date string from the AI into a date object."""
    if not due_date_str:
        return None
    due_date_str = due_date_str.strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%B %d, %Y", "%d %B %Y"):
        try:
            return datetime.strptime(due_date_str, fmt).date()
        except ValueError:
            continue
    if due_date_str.lower() in ("today",):
        return date.today()
    if due_date_str.lower() in ("tomorrow",):
        return date.today() + timedelta(days=1)
    return None


def match_owner(owner_name: str, candidate_users: list):
    """
    Best-effort fuzzy match of an AI-suggested owner name against real Users
    (organizer + participants) so action items can be assigned automatically.
    Falls back to None (owner_name kept as free text) if no confident match.
    """
    if not owner_name:
        return None
    needle = owner_name.strip().lower()
    if not needle:
        return None
    for u in candidate_users:
        if u.name.strip().lower() == needle:
            return u
    for u in candidate_users:
        first_name = u.name.strip().lower().split()[0] if u.name.strip() else ""
        if needle == first_name or needle in u.name.lower() or u.name.lower() in needle:
            return u
    return None
