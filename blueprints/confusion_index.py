"""
blueprints/confusion_index.py
Module: Policy Confusion Index

Nobody was connecting the confusion signals the app already silently logs.
This combines four of them into one 0-100 score per policy, weighted:

  35%  QuizAttempt      average wrong-answer rate for that policy's quiz
  25%  WhatIfQuery       share of scenarios citing this policy that came
                         back DEPENDS/UNCLEAR or got flagged for HR
                         (via WhatIfQuery.applicable_policies_json)
  20%  Feedback          thumbs-down on chatbot answers that cited this
                         policy (via ChatMessage.citations_json)
  10%  PolicyComment     comment volume (proxy for active discussion)
  10%  SearchHistory     zero-result searches whose query text overlaps
                         this policy's title (heuristic keyword match —
                         SearchHistory has no policy_id column, so exact
                         zero-result searches can't be tied to one)

No LLM dependency — pure DB aggregation, ~1-2 hrs of surface area.
Only policies with at least one logged signal are shown, so the list is a
real rewrite priority queue, not a guess.

Routes:
  GET /admin/confusion-index   dashboard: ranked confusion scores
"""
import json
from collections import defaultdict

from flask import Blueprint, render_template
from flask_login import login_required

from models import (Policy, PolicyStatus, QuizAttempt, PolicyComment,
                    WhatIfQuery, WhatIfVerdict, Feedback, ChatMessage,
                    SearchHistory, UserRole)
from utils import role_required

confusion_index_bp = Blueprint("confusion_index", __name__, url_prefix="/admin")

_STOPWORDS = {"the", "a", "an", "of", "and", "for", "to", "is", "are", "policy",
             "policies", "what", "how", "do", "does", "i", "can", "my", "on",
             "in", "about", "our", "your", "me", "please", "need"}


def _keywords(text: str) -> set:
    cleaned = "".join(ch.lower() if ch.isalnum() else " " for ch in (text or ""))
    return {w for w in cleaned.split() if len(w) > 2 and w not in _STOPWORDS}


def _quiz_scores():
    """policy_id -> {attempts, wrong_rate 0-100}"""
    buckets = defaultdict(list)
    for qa in QuizAttempt.query.all():
        if qa.total:
            buckets[qa.policy_id].append(100 - qa.percentage)
    return {pid: {"attempts": len(v), "wrong_rate": round(sum(v) / len(v))}
           for pid, v in buckets.items()}


def _comment_counts():
    counts = defaultdict(int)
    for (pid,) in PolicyComment.query.with_entities(PolicyComment.policy_id).all():
        counts[pid] += 1
    return counts


def _whatif_signals():
    """policy_id -> {mentions, uncertain, flagged}"""
    signals = defaultdict(lambda: {"mentions": 0, "uncertain": 0, "flagged": 0})
    for wq in WhatIfQuery.query.all():
        is_uncertain = wq.verdict in (WhatIfVerdict.DEPENDS, WhatIfVerdict.UNCLEAR)
        referenced_ids = set()
        for ref in (wq.applicable_policies or []):
            try:
                pid = int(ref.get("policy_id"))
            except (TypeError, ValueError):
                continue
            referenced_ids.add(pid)
        for pid in referenced_ids:
            signals[pid]["mentions"] += 1
            if is_uncertain:
                signals[pid]["uncertain"] += 1
            if wq.flagged_for_hr:
                signals[pid]["flagged"] += 1
    return signals


def _feedback_downvotes():
    """policy_id -> distinct downvoted-message count, via ChatMessage.citations_json"""
    counts = defaultdict(int)
    down_msg_ids = [f.message_id for f in Feedback.query.filter_by(vote="down").all() if f.message_id]
    if not down_msg_ids:
        return counts
    messages = ChatMessage.query.filter(ChatMessage.id.in_(down_msg_ids)).all()
    for m in messages:
        try:
            citations = json.loads(m.citations_json) if m.citations_json else []
        except (ValueError, TypeError):
            citations = []
        seen_pids = set()
        for c in citations:
            try:
                pid = int(c.get("policy_id"))
            except (TypeError, ValueError):
                continue
            seen_pids.add(pid)
        for pid in seen_pids:
            counts[pid] += 1
    return counts


def _search_keyword_hits(policies):
    """policy_id -> count of zero-result searches whose keywords overlap the policy title."""
    zero_result = SearchHistory.query.filter(
        (SearchHistory.chunks_found == 0) | (SearchHistory.answered == False)
    ).all()
    if not zero_result:
        return {}
    policy_kw = {p.id: _keywords(p.title) for p in policies}
    counts = defaultdict(int)
    for sh in zero_result:
        qkw = _keywords(sh.query_text)
        if not qkw:
            continue
        for pid, kws in policy_kw.items():
            if qkw & kws:
                counts[pid] += 1
    return counts


def _compute():
    policies = Policy.query.filter_by(status=PolicyStatus.ACTIVE).all()
    if not policies:
        return {"rows": [], "policy_count": 0, "signal_totals": {}}

    quiz = _quiz_scores()
    comments = _comment_counts()
    whatif = _whatif_signals()
    downvotes = _feedback_downvotes()
    search_hits = _search_keyword_hits(policies)

    max_comments = max(comments.values(), default=0) or 1
    max_downvotes = max(downvotes.values(), default=0) or 1
    max_search = max(search_hits.values(), default=0) or 1
    max_mentions = max((v["mentions"] for v in whatif.values()), default=0) or 1

    rows = []
    for p in policies:
        q = quiz.get(p.id, {"attempts": 0, "wrong_rate": 0})
        wf = whatif.get(p.id, {"mentions": 0, "uncertain": 0, "flagged": 0})
        c = comments.get(p.id, 0)
        d = downvotes.get(p.id, 0)
        s = search_hits.get(p.id, 0)

        if not (q["attempts"] or wf["mentions"] or c or d or s):
            continue  # no logged confusion signal at all — nothing to rank

        quiz_component = q["wrong_rate"]
        whatif_component = round(100 * (
            0.6 * (wf["uncertain"] / wf["mentions"] if wf["mentions"] else 0) +
            0.4 * (wf["mentions"] / max_mentions)
        )) if wf["mentions"] else 0
        feedback_component = round(100 * d / max_downvotes) if d else 0
        comment_component = round(100 * c / max_comments) if c else 0
        search_component = round(100 * s / max_search) if s else 0

        score = round(
            0.35 * quiz_component + 0.25 * whatif_component +
            0.20 * feedback_component + 0.10 * comment_component + 0.10 * search_component
        )

        rows.append({
            "policy": p, "score": score,
            "quiz_attempts": q["attempts"], "quiz_wrong_rate": q["wrong_rate"],
            "whatif_mentions": wf["mentions"], "whatif_uncertain": wf["uncertain"],
            "whatif_flagged": wf["flagged"], "downvotes": d, "comments": c,
            "zero_result_searches": s,
        })

    rows.sort(key=lambda r: -r["score"])
    return {
        "rows": rows,
        "policy_count": len(policies),
        "signal_totals": {
            "quiz_attempts": sum(v["attempts"] for v in quiz.values()),
            "whatif_queries": WhatIfQuery.query.count(),
            "downvotes": sum(downvotes.values()),
            "comments": sum(comments.values()),
        },
    }


@confusion_index_bp.route("/confusion-index")
@login_required
@role_required(UserRole.ADMIN, UserRole.HR)
def dashboard():
    data = _compute()
    return render_template("admin/confusion_index.html", **data)
