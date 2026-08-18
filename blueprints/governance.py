"""
blueprints/governance.py
Module 26: Policy Governance Health Score

New capability, distinct from the Compliance Center (Module 9, which scores
audit-readiness against external frameworks): this module scores the
*internal health of the policy portfolio itself* — is it stale, duplicated,
risky, or dangerously owned by a single person — and rolls it into one
number leadership can track over time, plus a department-level heatmap and
a ranked "fix these first" list. It reuses signals that already exist
scattered across other modules (AI Review risk scores, review/expiry
dates, duplicate/conflict detection, author/reviewer assignment) but never
combines them into a single portfolio-level verdict anywhere else in the app.

Composite Governance Health Index (0-100), weighted:
  35%  AI risk        — inverse of average PolicyAIReview.risk_score
  25%  Freshness      — % of active policies not overdue for review
  20%  Cleanliness    — % of active policies with no flagged duplicates/conflicts
  20%  Ownership       — % of active policies with both a reviewer AND approver
                         on record (no single-person "bus factor" risk)

Routes:
  GET /admin/governance   dashboard: health index, breakdown, department
                          heatmap, top-10 at-risk policies
"""
from datetime import date

from flask import Blueprint, render_template
from flask_login import login_required

from models import db, Policy, PolicyStatus, PolicyAIReview, Department, UserRole
from utils import role_required

governance_bp = Blueprint("governance", __name__, url_prefix="/admin")


def _policy_risk_map():
    """policy_id -> PolicyAIReview, for active policies that have one."""
    return {r.policy_id: r for r in PolicyAIReview.query.all()}


def _compute_governance_data():
    today = date.today()
    active_policies = Policy.query.filter_by(status=PolicyStatus.ACTIVE).all()
    risk_map = _policy_risk_map()
    n = len(active_policies)

    if n == 0:
        return {
            "health_index": 100, "sub_scores": {"risk": 100, "freshness": 100, "cleanliness": 100, "ownership": 100},
            "active_policy_count": 0, "reviewed_count": 0,
            "overdue_count": 0, "expired_count": 0, "duplicate_conflict_count": 0, "single_owner_count": 0,
            "dept_rows": [], "at_risk": [],
        }

    overdue = []
    expired = []
    dup_conflict = []
    single_owner = []
    risk_scores = []
    per_policy_rows = []

    for p in active_policies:
        review = risk_map.get(p.id)
        risk_score = review.risk_score if review else None
        has_dup_conflict = bool(review and (review.duplicates or review.conflicts)) if review else False
        is_overdue = bool(p.review_date and p.review_date < today)
        is_expired = bool(p.expiry_date and p.expiry_date < today)
        no_backup = not (p.reviewer_id and p.approver_id)

        if is_overdue:
            overdue.append(p)
        if is_expired:
            expired.append(p)
        if has_dup_conflict:
            dup_conflict.append(p)
        if no_backup:
            single_owner.append(p)
        if risk_score is not None:
            risk_scores.append(risk_score)

        reasons = []
        if risk_score is not None and risk_score >= 60:
            reasons.append(f"High AI risk score ({risk_score}/100)")
        if is_overdue:
            reasons.append("Review overdue")
        if is_expired:
            reasons.append("Past expiry date, still active")
        if has_dup_conflict:
            reasons.append("Flagged duplicate/conflict with another policy")
        if no_backup:
            reasons.append("No reviewer/approver on record (single point of failure)")

        if reasons:
            per_policy_rows.append({
                "policy": p,
                "risk_score": risk_score if risk_score is not None else 0,
                "reasons": reasons,
                "severity": (risk_score or 0) + 20 * is_overdue + 25 * is_expired + 15 * has_dup_conflict + 10 * no_backup,
            })

    avg_risk = round(sum(risk_scores) / len(risk_scores)) if risk_scores else 30  # neutral default, not reviewed yet
    risk_component = round(100 - avg_risk)
    freshness_component = round(100 * (n - len(overdue)) / n)
    cleanliness_component = round(100 * (n - len(dup_conflict)) / n)
    ownership_component = round(100 * (n - len(single_owner)) / n)

    health_index = round(
        0.35 * risk_component + 0.25 * freshness_component +
        0.20 * cleanliness_component + 0.20 * ownership_component
    )

    # Department heatmap
    dept_rows = []
    for dept in Department.query.order_by(Department.name).all():
        dept_policies = [p for p in active_policies if p.department_id == dept.id]
        if not dept_policies:
            continue
        d_risk = [risk_map[p.id].risk_score for p in dept_policies if p.id in risk_map]
        d_overdue = sum(1 for p in dept_policies if p.review_date and p.review_date < today)
        d_single = sum(1 for p in dept_policies if not (p.reviewer_id and p.approver_id))
        dept_rows.append({
            "department": dept, "policy_count": len(dept_policies),
            "avg_risk": round(sum(d_risk) / len(d_risk)) if d_risk else None,
            "overdue_count": d_overdue, "single_owner_count": d_single,
        })
    # Company-wide (no department) bucket
    no_dept = [p for p in active_policies if not p.department_id]
    if no_dept:
        d_risk = [risk_map[p.id].risk_score for p in no_dept if p.id in risk_map]
        dept_rows.append({
            "department": None, "policy_count": len(no_dept),
            "avg_risk": round(sum(d_risk) / len(d_risk)) if d_risk else None,
            "overdue_count": sum(1 for p in no_dept if p.review_date and p.review_date < today),
            "single_owner_count": sum(1 for p in no_dept if not (p.reviewer_id and p.approver_id)),
        })
    dept_rows.sort(key=lambda r: -(r["avg_risk"] or 0))

    per_policy_rows.sort(key=lambda r: -r["severity"])

    return {
        "health_index": health_index,
        "sub_scores": {"risk": risk_component, "freshness": freshness_component,
                      "cleanliness": cleanliness_component, "ownership": ownership_component},
        "active_policy_count": n, "reviewed_count": len(risk_scores),
        "overdue_count": len(overdue), "expired_count": len(expired),
        "duplicate_conflict_count": len(dup_conflict), "single_owner_count": len(single_owner),
        "dept_rows": dept_rows, "at_risk": per_policy_rows[:10],
    }


@governance_bp.route("/governance")
@login_required
@role_required(UserRole.ADMIN, UserRole.HR)
def dashboard():
    data = _compute_governance_data()
    return render_template("admin/governance.html", **data)
