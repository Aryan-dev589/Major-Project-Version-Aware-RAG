"""
blueprints/audit_center.py
Module 10: Audit Center

Routes:
  GET /admin/audit-center              dashboard: volume, top actions, activity chart
  GET /admin/audit-center/timeline     filterable chronological event log
  GET /admin/audit-center/user/<id>    one user's full activity history ("replay")
  GET /admin/audit-center/security     failed logins, suspicious activity, MFA adoption
  GET /admin/audit-center/export       CSV export of the (filtered) timeline
"""
import csv
import io
import json
from datetime import datetime, timedelta, timezone
from collections import Counter, defaultdict

from flask import Blueprint, render_template, request, Response
from flask_login import login_required
from sqlalchemy import func

from models import db, AuditLog, User, UserRole
from utils import role_required, paginate

audit_center_bp = Blueprint("audit_center", __name__, url_prefix="/admin")


def _apply_filters(query):
    action = request.args.get("action", "").strip()
    resource_type = request.args.get("resource_type", "").strip()
    user_id = request.args.get("user_id", "").strip()
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()

    if action:
        query = query.filter(AuditLog.action.like(f"{action}%"))
    if resource_type:
        query = query.filter_by(resource_type=resource_type)
    if user_id:
        query = query.filter_by(user_id=int(user_id))
    if date_from:
        try:
            query = query.filter(AuditLog.timestamp >= datetime.strptime(date_from, "%Y-%m-%d"))
        except ValueError:
            pass
    if date_to:
        try:
            query = query.filter(AuditLog.timestamp <= datetime.strptime(date_to, "%Y-%m-%d") + timedelta(days=1))
        except ValueError:
            pass
    return query


@audit_center_bp.route("/audit-center")
@login_required
@role_required(UserRole.ADMIN, UserRole.HR)
def audit_dashboard():
    now = datetime.now(timezone.utc)
    thirty_days_ago = now - timedelta(days=30)

    total_events = AuditLog.query.count()
    events_today = AuditLog.query.filter(
        AuditLog.timestamp >= now.replace(hour=0, minute=0, second=0, microsecond=0)).count()
    events_30d = AuditLog.query.filter(AuditLog.timestamp >= thirty_days_ago).count()

    top_actions = (db.session.query(AuditLog.action, func.count(AuditLog.id).label("cnt"))
                  .filter(AuditLog.timestamp >= thirty_days_ago)
                  .group_by(AuditLog.action).order_by(func.count(AuditLog.id).desc()).limit(10).all())

    top_resource_types = (db.session.query(AuditLog.resource_type, func.count(AuditLog.id).label("cnt"))
                          .filter(AuditLog.timestamp >= thirty_days_ago, AuditLog.resource_type.isnot(None))
                          .group_by(AuditLog.resource_type).order_by(func.count(AuditLog.id).desc()).all())

    top_users = (db.session.query(AuditLog.user_id, func.count(AuditLog.id).label("cnt"))
                .filter(AuditLog.timestamp >= thirty_days_ago, AuditLog.user_id.isnot(None))
                .group_by(AuditLog.user_id).order_by(func.count(AuditLog.id).desc()).limit(10).all())
    top_users_data = []
    for uid, cnt in top_users:
        u = User.query.get(uid)
        if u:
            top_users_data.append({"user": u, "count": cnt})

    recent_rows = AuditLog.query.filter(AuditLog.timestamp >= now - timedelta(days=14)).all()
    by_day = defaultdict(int)
    for r in recent_rows:
        by_day[r.timestamp.date()] += 1
    activity_trend = []
    for i in range(13, -1, -1):
        d = (now - timedelta(days=i)).date()
        activity_trend.append({"label": d.strftime("%d %b"), "value": by_day.get(d, 0)})

    return render_template("admin/audit_dashboard.html",
        total_events=total_events, events_today=events_today, events_30d=events_30d,
        top_actions=top_actions, top_resource_types=top_resource_types,
        top_users=top_users_data, activity_trend=activity_trend,
    )


@audit_center_bp.route("/audit-center/timeline")
@login_required
@role_required(UserRole.ADMIN, UserRole.HR)
def timeline():
    page = request.args.get("page", 1, type=int)
    query = _apply_filters(AuditLog.query).order_by(AuditLog.timestamp.desc())
    logs = paginate(query, page, 40)

    distinct_actions = [a[0] for a in db.session.query(AuditLog.action).distinct().order_by(AuditLog.action).all()]
    distinct_resource_types = [r[0] for r in db.session.query(AuditLog.resource_type).distinct()
                               .filter(AuditLog.resource_type.isnot(None)).all()]
    users = User.query.order_by(User.name).all()

    return render_template("admin/audit_timeline.html",
        logs=logs, distinct_actions=distinct_actions,
        distinct_resource_types=distinct_resource_types, users=users,
        filters={k: request.args.get(k, "") for k in
                ["action", "resource_type", "user_id", "date_from", "date_to"]},
    )


@audit_center_bp.route("/audit-center/user/<int:user_id>")
@login_required
@role_required(UserRole.ADMIN, UserRole.HR)
def user_activity(user_id):
    user = User.query.get_or_404(user_id)
    page = request.args.get("page", 1, type=int)
    query = AuditLog.query.filter_by(user_id=user_id).order_by(AuditLog.timestamp.desc())
    logs = paginate(query, page, 50)

    action_counts = Counter(l.action for l in AuditLog.query.filter_by(user_id=user_id).all())
    top_actions = action_counts.most_common(8)

    return render_template("admin/audit_user_activity.html", user=user, logs=logs, top_actions=top_actions)


@audit_center_bp.route("/audit-center/security")
@login_required
@role_required(UserRole.ADMIN, UserRole.HR)
def security_report():
    now = datetime.now(timezone.utc)
    thirty_days_ago = now - timedelta(days=30)

    failed_logins = AuditLog.query.filter(
        AuditLog.action == "auth.login_failed", AuditLog.timestamp >= thirty_days_ago).all()
    failed_by_email = Counter()
    failed_by_ip = Counter()
    for f in failed_logins:
        try:
            detail = json.loads(f.detail) if f.detail else {}
        except (ValueError, TypeError):
            detail = {}
        if detail.get("email"):
            failed_by_email[detail["email"]] += 1
        if f.ip_address:
            failed_by_ip[f.ip_address] += 1

    # Suspicious: 5+ failed attempts from the same email or IP in the window
    suspicious_emails = [{"email": e, "count": c} for e, c in failed_by_email.items() if c >= 5]
    suspicious_ips = [{"ip": ip, "count": c} for ip, c in failed_by_ip.items() if c >= 5]

    total_users = User.query.filter_by(is_active=True).count()
    mfa_users = User.query.filter_by(is_active=True, mfa_enabled=True).count()
    mfa_adoption = round(100 * mfa_users / total_users) if total_users else 0

    successful_logins_30d = AuditLog.query.filter(
        AuditLog.action == "auth.login_success", AuditLog.timestamp >= thirty_days_ago).count()

    return render_template("admin/audit_security.html",
        failed_login_count=len(failed_logins), suspicious_emails=suspicious_emails,
        suspicious_ips=suspicious_ips, mfa_adoption=mfa_adoption, mfa_users=mfa_users,
        total_users=total_users, successful_logins_30d=successful_logins_30d,
    )


@audit_center_bp.route("/audit-center/export")
@login_required
@role_required(UserRole.ADMIN, UserRole.HR)
def export_csv():
    query = _apply_filters(AuditLog.query).order_by(AuditLog.timestamp.desc()).limit(5000)
    rows = query.all()

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Timestamp", "User", "Action", "Resource Type", "Resource ID", "IP Address", "Detail"])
    for r in rows:
        user_name = r.user.name if r.user else ""
        writer.writerow([
            r.timestamp.strftime("%Y-%m-%d %H:%M:%S"), user_name, r.action,
            r.resource_type or "", r.resource_id or "", r.ip_address or "", r.detail or "",
        ])

    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=audit_log_export.csv"})
