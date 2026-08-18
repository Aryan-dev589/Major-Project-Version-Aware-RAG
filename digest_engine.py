"""
digest_engine.py
Module: Personalized Policy Digest ("For You This Week")

Existing notifications are generic broadcasts ("new policy published", sent
to everyone identically). This picks ONE most-relevant not-yet-acknowledged
policy per employee per week and delivers a short nudge instead — using
rule-based selection, no new infrastructure, and the existing Notification
model for delivery (same pattern as notify_user/notify_all_employees in
utils.py).

Selection order for each employee (first match wins):
  1. A not-yet-acknowledged policy in their own department
  2. A not-yet-acknowledged policy in a category they've recently searched
     (SearchHistory in the last 14 days, matched by category name appearing
     in the query text)
  3. Any not-yet-acknowledged mandatory policy
  4. The most recently updated active policy they haven't acknowledged

The digest message uses PolicyAIInsight.summary when available (LLM-backed,
optional), falling back to Policy.description exactly like the rest of the
app degrades when no LLM provider is configured.
"""
from datetime import datetime, timedelta, timezone

from models import (db, User, Policy, PolicyStatus, PolicyCategory,
                    SearchHistory, PolicyAIInsight, Notification, NotificationType)

DIGEST_TITLE_PREFIX = "For You This Week:"


def _acknowledged_policy_ids(user) -> set:
    return {a.policy_id for a in user.acknowledgements.all()}


def _recent_search_category_ids(user, days: int = 14) -> set:
    """Category ids implied by the employee's recent search queries, matched
    by category name appearing in the query text (no LLM needed)."""
    since = datetime.now(timezone.utc) - timedelta(days=days)
    recent = SearchHistory.query.filter(
        SearchHistory.user_id == user.id, SearchHistory.created_at >= since
    ).all()
    if not recent:
        return set()
    categories = PolicyCategory.query.all()
    hit_ids = set()
    for sh in recent:
        text = (sh.query_text or "").lower()
        for cat in categories:
            if cat.name.lower() in text:
                hit_ids.add(cat.id)
    return hit_ids


def pick_policy_for_user(user):
    """Returns the single most relevant not-yet-acknowledged active Policy
    for this employee this week, or None if nothing qualifies."""
    acked = _acknowledged_policy_ids(user)
    base = Policy.query.filter(Policy.status == PolicyStatus.ACTIVE)
    if acked:
        base = base.filter(~Policy.id.in_(acked))

    if user.department_id:
        p = base.filter(Policy.department_id == user.department_id) \
                .order_by(Policy.updated_at.desc()).first()
        if p:
            return p

    cat_ids = _recent_search_category_ids(user)
    if cat_ids:
        p = base.filter(Policy.category_id.in_(cat_ids)) \
                .order_by(Policy.updated_at.desc()).first()
        if p:
            return p

    p = base.filter(Policy.is_mandatory.is_(True)).order_by(Policy.updated_at.desc()).first()
    if p:
        return p

    return base.order_by(Policy.updated_at.desc()).first()


def _digest_message(policy) -> str:
    insight = PolicyAIInsight.query.filter_by(policy_id=policy.id).first()
    if insight and insight.summary:
        return insight.summary
    return policy.description or "Take a few minutes to read this one this week."


def send_weekly_digests() -> dict:
    """Send one 'For You This Week' notification per active employee. Safe
    to re-run — skips anyone who's already got this week's pick for the
    same policy so a manual re-run (or an extra cron tick) doesn't spam."""
    sent, skipped = 0, 0
    for user in User.query.filter_by(is_active=True).all():
        policy = pick_policy_for_user(user)
        if not policy:
            skipped += 1
            continue

        already_sent = Notification.query.filter(
            Notification.user_id == user.id,
            Notification.link == f"/policies/{policy.id}",
            Notification.title.like(f"{DIGEST_TITLE_PREFIX}%"),
        ).first()
        if already_sent:
            skipped += 1
            continue

        db.session.add(Notification(
            user_id=user.id, type=NotificationType.NEW_POLICY,
            title=f"{DIGEST_TITLE_PREFIX} {policy.title}",
            message=_digest_message(policy),
            link=f"/policies/{policy.id}",
        ))
        sent += 1

    db.session.commit()
    return {"sent": sent, "skipped": skipped}
