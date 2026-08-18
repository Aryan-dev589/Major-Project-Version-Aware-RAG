"""
utils.py
Shared helpers used across blueprints.
"""
import json
import difflib
import uuid
from datetime import datetime, timezone
from functools import wraps
from flask import request, abort
from flask_login import current_user
from models import db, AuditLog, Notification, NotificationType, Policy, User


# ---------- Role guards ----------
def role_required(*roles):
    """Decorator: abort 403 if current user doesn't have one of the given roles."""
    def decorator(f):
        @wraps(f)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                abort(401)
            if current_user.role not in roles:
                abort(403)
            return f(*args, **kwargs)
        return wrapped
    return decorator


# ---------- Audit logging ----------
def audit(action: str, resource_type: str = None, resource_id: int = None, detail: dict = None):
    """Write an immutable audit log entry. Safe to call outside a request
    context too (e.g. from scripts/check_workflow_reminders.py via cron)."""
    from flask import has_request_context
    in_request = has_request_context()
    log = AuditLog(
        user_id=(current_user.id if in_request and current_user.is_authenticated else None),
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        detail=json.dumps(detail) if detail else None,
        ip_address=request.remote_addr if in_request else None,
        user_agent=request.headers.get("User-Agent", "")[:300] if in_request else "background-job",
    )
    db.session.add(log)
    db.session.commit()


# ---------- Notifications ----------
def notify_user(user_id: int, ntype: str, title: str, message: str = None, link: str = None):
    notif = Notification(user_id=user_id, type=ntype, title=title, message=message, link=link)
    db.session.add(notif)
    db.session.commit()


def notify_all_employees(ntype: str, title: str, message: str = None, link: str = None):
    """Notify every active employee (used when a mandatory policy is published)."""
    users = User.query.filter_by(is_active=True).all()
    for u in users:
        db.session.add(Notification(user_id=u.id, type=ntype, title=title, message=message, link=link))
    db.session.commit()


# ---------- Policy ID generator ----------
def generate_policy_id() -> str:
    """
    Generates sequential IDs like POL-2024-001, POL-2024-002 …
    Thread-safe because SQLAlchemy commits before we generate the next one.
    """
    year = datetime.now().year
    prefix = f"POL-{year}-"
    last = (
        Policy.query
        .filter(Policy.policy_id.like(f"{prefix}%"))
        .order_by(Policy.id.desc())
        .first()
    )
    if last:
        try:
            last_num = int(last.policy_id.split("-")[-1])
        except ValueError:
            last_num = 0
    else:
        last_num = 0
    return f"{prefix}{(last_num + 1):03d}"


# ---------- Version bump ----------
def next_version(current: str, major: bool = False) -> tuple[float, str]:
    """
    current = "1.0" → minor bump → (1.1, "v1.1")
               "1.0" → major bump → (2.0, "v2.0")
    """
    try:
        parts = str(current).split(".")
        major_v = int(parts[0])
        minor_v = int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        major_v, minor_v = 1, 0

    if major:
        major_v += 1
        minor_v = 0
    else:
        minor_v += 1

    num = float(f"{major_v}.{minor_v}")
    label = f"v{major_v}.{minor_v}"
    return num, label


# ---------- Diff engine (Phase 3 version comparison) ----------
def compute_diff(old_text: str, new_text: str) -> dict:
    """
    Produces a structured diff between two policy texts.
    Returns:
      {
        "added": [lines],
        "removed": [lines],
        "changed": [(old_line, new_line)],
        "html": "<span class='added'>...</span> ..." (inline HTML for UI),
        "stats": {"added": n, "removed": n, "changed": n}
      }
    """
    old_lines = old_text.splitlines(keepends=True)
    new_lines = new_text.splitlines(keepends=True)

    added, removed, html_parts = [], [], []
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for line in old_lines[i1:i2]:
                html_parts.append(f'<span class="diff-equal">{_esc(line)}</span>')
        elif tag == "insert":
            for line in new_lines[j1:j2]:
                added.append(line.rstrip())
                html_parts.append(f'<span class="diff-added">+ {_esc(line)}</span>')
        elif tag == "delete":
            for line in old_lines[i1:i2]:
                removed.append(line.rstrip())
                html_parts.append(f'<span class="diff-removed">- {_esc(line)}</span>')
        elif tag == "replace":
            for line in old_lines[i1:i2]:
                removed.append(line.rstrip())
                html_parts.append(f'<span class="diff-removed">- {_esc(line)}</span>')
            for line in new_lines[j1:j2]:
                added.append(line.rstrip())
                html_parts.append(f'<span class="diff-added">+ {_esc(line)}</span>')

    return {
        "added": added,
        "removed": removed,
        "html": "".join(html_parts),
        "stats": {"added": len(added), "removed": len(removed)},
    }


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------- Pagination helper ----------
def paginate(query, page: int, per_page: int = 20):
    return query.paginate(page=page, per_page=per_page, error_out=False)


# ---------- Date helpers ----------
def days_until(d) -> int | None:
    if d is None:
        return None
    today = datetime.now(timezone.utc).date()
    return (d - today).days


# ---------- Meeting code generator (Module 5) ----------
def generate_meeting_code() -> str:
    """Sequential codes like MTG-2026-001, MTG-2026-002 …"""
    from models import Meeting
    year = datetime.now().year
    prefix = f"MTG-{year}-"
    last = (
        Meeting.query
        .filter(Meeting.meeting_code.like(f"{prefix}%"))
        .order_by(Meeting.id.desc())
        .first()
    )
    if last:
        try:
            last_num = int(last.meeting_code.split("-")[-1])
        except ValueError:
            last_num = 0
    else:
        last_num = 0
    return f"{prefix}{(last_num + 1):03d}"


# ---------- ICS calendar export (Module 5 — "Generate Calendar Tasks") ----------
def _ics_escape(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace(",", "\\,").replace(";", "\\;").replace("\n", "\\n")


def build_action_items_ics(action_items, calendar_name: str = "Policy Ledger Action Items") -> str:
    """
    Builds a minimal .ics calendar file (VCALENDAR/VTODO) from a list of
    MeetingActionItem objects that have a due_date set. No external
    dependency required — VTODO/VEVENT is plain text per RFC 5545.
    """
    now_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Policy Ledger//Meeting Action Items//EN",
        f"X-WR-CALNAME:{_ics_escape(calendar_name)}",
    ]
    for item in action_items:
        if not item.due_date:
            continue
        due_str = item.due_date.strftime("%Y%m%d")
        uid = f"action-{item.id}-{uuid.uuid4().hex[:8]}@policyledger"
        priority_map = {"high": 1, "medium": 5, "low": 9}
        lines += [
            "BEGIN:VTODO",
            f"UID:{uid}",
            f"DTSTAMP:{now_stamp}",
            f"DUE;VALUE=DATE:{due_str}",
            f"SUMMARY:{_ics_escape(item.description[:200])}",
            f"DESCRIPTION:{_ics_escape('Owner: ' + item.owner_display)}",
            f"PRIORITY:{priority_map.get(item.priority, 5)}",
            f"STATUS:{'COMPLETED' if item.status == 'done' else 'NEEDS-ACTION'}",
            "END:VTODO",
        ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines)


# ---------- PDF export (Search / Module 14 — "give me the whole document") ----------
def generate_policy_pdf(policy, version):
    """
    Renders a policy version as a formatted PDF and returns an in-memory
    BytesIO ready for send_file(). Uses reportlab (pure Python, no
    external binary needed). Policy.download_count already existed in the
    schema for this purpose but nothing populated it before.
    """
    import io
    from reportlab.lib.pagesizes import LETTER
    from reportlab.lib.units import inch
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, HRFlowable
    from reportlab.lib import colors
    from reportlab.lib.enums import TA_LEFT

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=LETTER,
        topMargin=0.85 * inch, bottomMargin=0.85 * inch,
        leftMargin=0.9 * inch, rightMargin=0.9 * inch,
        title=policy.title, author="Policy Ledger",
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle("PLTitle", parent=styles["Title"], alignment=TA_LEFT, fontSize=18, spaceAfter=4)
    meta_style = ParagraphStyle("PLMeta", parent=styles["Normal"], fontSize=9, textColor=colors.HexColor("#5a5a52"))
    body_style = ParagraphStyle("PLBody", parent=styles["Normal"], fontSize=10.5, leading=15, spaceAfter=8)

    story = [
        Paragraph(policy.title, title_style),
        Paragraph(
            f"{policy.policy_id} &nbsp;·&nbsp; Version {version.version_label} &nbsp;·&nbsp; "
            f"Status: {policy.status.replace('_', ' ').title()} &nbsp;·&nbsp; "
            f"Category: {policy.category.name if policy.category else 'General'}",
            meta_style,
        ),
        Spacer(1, 4),
        HRFlowable(width="100%", thickness=0.75, color=colors.HexColor("#c9c4b4")),
        Spacer(1, 12),
    ]

    if policy.description:
        story.append(Paragraph(f"<i>{policy.description}</i>", body_style))
        story.append(Spacer(1, 6))

    for para in (version.content or "").split("\n"):
        para = para.strip()
        if not para:
            story.append(Spacer(1, 6))
            continue
        safe = para.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        story.append(Paragraph(safe, body_style))

    story.append(Spacer(1, 18))
    story.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#c9c4b4")))
    story.append(Spacer(1, 6))
    footer = (f"Effective: {version.effective_date or '—'} &nbsp;·&nbsp; "
             f"Generated by Policy Ledger on {datetime.now(timezone.utc).strftime('%Y-%m-%d')}")
    story.append(Paragraph(footer, meta_style))

    doc.build(story)
    buf.seek(0)
    return buf
