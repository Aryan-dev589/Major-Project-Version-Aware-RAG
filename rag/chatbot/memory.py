"""rag/chatbot/memory.py - in-memory conversation store keyed by session_id"""
from datetime import datetime
from collections import defaultdict

_sessions: dict[str, list[dict]] = defaultdict(list)
MAX_TURNS = 10


def add_message(session_id: str, role: str, content: str):
    _sessions[session_id].append({"role": role, "content": content, "time": datetime.now().isoformat()})
    if len(_sessions[session_id]) > MAX_TURNS * 2:
        _sessions[session_id] = _sessions[session_id][-(MAX_TURNS * 2):]


def get_history(session_id: str) -> list[dict]:
    return _sessions.get(session_id, [])


def clear_session(session_id: str):
    _sessions.pop(session_id, None)
