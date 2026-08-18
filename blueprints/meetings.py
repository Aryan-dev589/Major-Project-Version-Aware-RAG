"""
blueprints/meetings.py
Module 5: Meeting Management (MOM)

Routes:
  GET  /meetings                          list meetings (filterable)
  GET  /meetings/new, POST                schedule a meeting
  GET  /meetings/<id>                     meeting detail (agenda, participants, MOM, decisions, action items)
  POST /meetings/<id>/edit                update agenda/details
  POST /meetings/<id>/notes               save raw notes / transcript
  POST /meetings/<id>/generate-mom        AI: generate minutes, decisions, action items, follow-up email
  POST /meetings/<id>/status              mark completed / cancelled
  POST /meetings/<id>/attendance          mark who attended
  POST /meetings/<id>/action-items/new    manually add an action item
  POST /action-items/<id>/status          update an action item's status
  POST /action-items/<id>/delete          delete an action item
  POST /meetings/<id>/decisions/new       manually add a decision
  GET  /meetings/<id>/calendar.ics        download action items as a calendar file
  GET  /my-action-items                   employee-facing: action items assigned to me
"""
import json
from datetime import datetime, timezone

from flask import (Blueprint, render_template, redirect, url_for, flash,
                   request, jsonify, abort, Response, current_app)
from flask_login import login_required, current_user

from models import (db, Meeting, MeetingType, MeetingStatus, MeetingParticipant,
                    MeetingMinutes, MeetingDecision, MeetingActionItem, ActionItemStatus,
                    Priority, User, UserRole, Department, Policy, PolicyStatus, Notification, NotificationType)
from utils import (audit, notify_user, paginate, generate_meeting_code,
                   build_action_items_ics, days_until)
from meeting_ai import generate_mom, parse_due_date, match_owner

meetings_bp = Blueprint("meetings", __name__)


def _can_manage(meeting):
    return current_user.is_admin() or meeting.organizer_id == current_user.id or current_user.can_manage_policies()


def _unread_count():
    return Notification.query.filter_by(user_id=current_user.id, is_read=False).count()


# ================================================================
# List
# ================================================================
@meetings_bp.route("/meetings")
@login_required
def list_meetings():
    page = request.args.get("page", 1, type=int)
    status_filter = request.args.get("status", "")
    type_filter = request.args.get("type", "")
    when = request.args.get("when", "")  # "upcoming" | "past" | ""

    q = Meeting.query
    if status_filter:
        q = q.filter_by(status=status_filter)
    if type_filter:
        q = q.filter_by(meeting_type=type_filter)
    now = datetime.now(timezone.utc)
    if when == "upcoming":
        q = q.filter(Meeting.scheduled_at >= now)
    elif when == "past":
        q = q.filter(Meeting.scheduled_at < now)

    q = q.order_by(Meeting.scheduled_at.desc())
    meetings = paginate(q, page, 15)

    return render_template("meetings/list.html",
        meetings=meetings, status_filter=status_filter, type_filter=type_filter,
        when=when, all_statuses=MeetingStatus.ALL, all_types=MeetingType.ALL,
        unread_count=_unread_count(),
    )


# ================================================================
# Create
# ================================================================
@meetings_bp.route("/meetings/new", methods=["GET", "POST"])
@login_required
def create_meeting():
    departments = Department.query.order_by(Department.name).all()
    users = User.query.filter_by(is_active=True).order_by(User.name).all()
    policies = Policy.query.filter_by(status=PolicyStatus.ACTIVE).order_by(Policy.title).all()
    error = None

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        scheduled_at_raw = request.form.get("scheduled_at", "").strip()
        participant_ids = request.form.getlist("participant_ids", type=int)

        if not title:
            error = "Meeting title is required."
        elif not scheduled_at_raw:
            error = "Date & time is required."
        else:
            try:
                scheduled_at = datetime.strptime(scheduled_at_raw, "%Y-%m-%dT%H:%M")
            except ValueError:
                error = "Invalid date/time format."

        if not error:
            meeting = Meeting(
                meeting_code=generate_meeting_code(),
                title=title,
                meeting_type=request.form.get("meeting_type", MeetingType.OTHER),
                department_id=request.form.get("department_id") or None,
                organizer_id=current_user.id,
                scheduled_at=scheduled_at,
                location=request.form.get("location", "").strip(),
                agenda=request.form.get("agenda", "").strip(),
                status=MeetingStatus.SCHEDULED,
            )
            db.session.add(meeting)
            db.session.flush()

            # Organizer is always a participant
            all_participant_ids = set(participant_ids) | {current_user.id}
            for uid in all_participant_ids:
                db.session.add(MeetingParticipant(
                    meeting_id=meeting.id, user_id=uid, is_organizer=(uid == current_user.id)
                ))

            # Related policies
            policy_ids = request.form.getlist("policy_ids", type=int)
            if policy_ids:
                meeting.related_policies = Policy.query.filter(Policy.id.in_(policy_ids)).all()

            db.session.commit()

            for uid in all_participant_ids:
                if uid != current_user.id:
                    notify_user(uid, NotificationType.MEETING_INVITE, f"Invited: {meeting.title}",
                               f"{current_user.name} added you to a meeting on "
                               f"{scheduled_at.strftime('%d %b %Y, %H:%M')}.",
                               url_for("meetings.meeting_detail", meeting_id=meeting.id))

            audit("meeting.create", "meeting", meeting.id, {"title": title})
            flash(f'Meeting "{title}" scheduled.', "success")
            return redirect(url_for("meetings.meeting_detail", meeting_id=meeting.id))

    return render_template("meetings/form.html",
        meeting=None, departments=departments, users=users, policies=policies,
        error=error, all_types=MeetingType.ALL, unread_count=_unread_count(),
    )


# ================================================================
# Detail
# ================================================================
@meetings_bp.route("/meetings/<int:meeting_id>")
@login_required
def meeting_detail(meeting_id):
    meeting = Meeting.query.get_or_404(meeting_id)
    participants = meeting.participants.all()
    action_items = meeting.action_items.order_by(MeetingActionItem.status, MeetingActionItem.due_date).all()
    decisions = meeting.decisions.order_by(MeetingDecision.created_at).all()
    is_participant = any(p.user_id == current_user.id for p in participants)

    if not (is_participant or current_user.is_admin() or current_user.can_manage_policies()):
        flash("You don't have access to this meeting.", "warning")
        return redirect(url_for("meetings.list_meetings"))

    return render_template("meetings/detail.html",
        meeting=meeting, participants=participants, action_items=action_items,
        decisions=decisions, can_manage=_can_manage(meeting),
        unread_count=_unread_count(),
    )


# ================================================================
# Save raw notes / transcript
# ================================================================
@meetings_bp.route("/meetings/<int:meeting_id>/notes", methods=["POST"])
@login_required
def save_notes(meeting_id):
    meeting = Meeting.query.get_or_404(meeting_id)
    if not _can_manage(meeting):
        abort(403)
    meeting.raw_notes = request.form.get("raw_notes", "").strip()
    db.session.commit()
    audit("meeting.notes_saved", "meeting", meeting.id)
    flash("Notes saved.", "success")
    return redirect(url_for("meetings.meeting_detail", meeting_id=meeting.id))


# ================================================================
# AI: generate MOM
# ================================================================
@meetings_bp.route("/meetings/<int:meeting_id>/generate-mom", methods=["POST"])
@login_required
def generate_mom_route(meeting_id):
    meeting = Meeting.query.get_or_404(meeting_id)
    if not _can_manage(meeting):
        abort(403)

    raw_notes = request.form.get("raw_notes", meeting.raw_notes or "").strip()
    if not raw_notes:
        return jsonify({"success": False, "error": "Add meeting notes or a transcript first."}), 400

    meeting.raw_notes = raw_notes
    result = generate_mom(raw_notes, meeting_title=meeting.title, agenda=meeting.agenda or "")

    # Upsert minutes
    minutes = meeting.minutes or MeetingMinutes(meeting_id=meeting.id)
    minutes.summary = result["summary"]
    minutes.full_minutes = result["full_minutes"]
    minutes.key_points = result["key_points"]
    minutes.followup_email = result["followup_email"]
    minutes.generated_by_ai = True
    minutes.generated_at = datetime.now(timezone.utc)
    if not minutes.id:
        db.session.add(minutes)

    # Replace previous AI-sourced decisions/action items; keep manual ones
    MeetingDecision.query.filter_by(meeting_id=meeting.id, source="ai").delete()
    MeetingActionItem.query.filter_by(meeting_id=meeting.id, source="ai").delete()

    for d in result["decisions"]:
        db.session.add(MeetingDecision(meeting_id=meeting.id, description=d, source="ai"))

    candidate_users = [p.user for p in meeting.participants.all()] + [meeting.organizer]
    created_items = []
    for item in result["action_items"]:
        owner = match_owner(item["owner_name"], candidate_users)
        ai_item = MeetingActionItem(
            meeting_id=meeting.id,
            description=item["description"],
            owner_id=owner.id if owner else None,
            owner_name_raw=item["owner_name"] if not owner else None,
            due_date=parse_due_date(item["due_date"]),
            priority=item["priority"],
            source="ai",
        )
        db.session.add(ai_item)
        created_items.append((ai_item, owner))

    db.session.commit()

    for ai_item, owner in created_items:
        if owner:
            notify_user(owner.id, NotificationType.ACTION_ITEM_ASSIGNED, "New action item assigned",
                       ai_item.description[:150], url_for("meetings.meeting_detail", meeting_id=meeting.id))

    audit("meeting.generate_mom", "meeting", meeting.id, {
        "decisions": len(result["decisions"]), "action_items": len(result["action_items"]),
    })

    return jsonify({
        "success": True,
        "summary": result["summary"],
        "full_minutes": result["full_minutes"],
        "key_points": result["key_points"],
        "decisions": result["decisions"],
        "action_items_count": len(result["action_items"]),
        "followup_email": result["followup_email"],
    })


# ================================================================
# Status changes
# ================================================================
@meetings_bp.route("/meetings/<int:meeting_id>/status", methods=["POST"])
@login_required
def set_status(meeting_id):
    meeting = Meeting.query.get_or_404(meeting_id)
    if not _can_manage(meeting):
        abort(403)
    new_status = request.form.get("status")
    if new_status in MeetingStatus.ALL:
        meeting.status = new_status
        db.session.commit()
        audit("meeting.status_change", "meeting", meeting.id, {"status": new_status})
        flash(f"Meeting marked as {new_status}.", "info")
    return redirect(url_for("meetings.meeting_detail", meeting_id=meeting.id))


@meetings_bp.route("/meetings/<int:meeting_id>/attendance", methods=["POST"])
@login_required
def mark_attendance(meeting_id):
    meeting = Meeting.query.get_or_404(meeting_id)
    if not _can_manage(meeting):
        abort(403)
    attended_ids = set(request.form.getlist("attended_ids", type=int))
    for p in meeting.participants.all():
        p.attended = p.user_id in attended_ids
    db.session.commit()
    flash("Attendance updated.", "success")
    return redirect(url_for("meetings.meeting_detail", meeting_id=meeting.id))


# ================================================================
# Decisions (manual)
# ================================================================
@meetings_bp.route("/meetings/<int:meeting_id>/decisions/new", methods=["POST"])
@login_required
def add_decision(meeting_id):
    meeting = Meeting.query.get_or_404(meeting_id)
    if not _can_manage(meeting):
        abort(403)
    text = request.form.get("description", "").strip()
    if text:
        db.session.add(MeetingDecision(meeting_id=meeting.id, description=text, source="manual"))
        db.session.commit()
        audit("meeting.decision_add", "meeting", meeting.id)
    return redirect(url_for("meetings.meeting_detail", meeting_id=meeting.id))


# ================================================================
# Action items
# ================================================================
@meetings_bp.route("/meetings/<int:meeting_id>/action-items/new", methods=["POST"])
@login_required
def add_action_item(meeting_id):
    meeting = Meeting.query.get_or_404(meeting_id)
    if not _can_manage(meeting):
        abort(403)
    desc = request.form.get("description", "").strip()
    if not desc:
        flash("Description is required.", "warning")
        return redirect(url_for("meetings.meeting_detail", meeting_id=meeting.id))

    owner_id = request.form.get("owner_id") or None
    due_date_raw = request.form.get("due_date", "").strip()
    due_date = None
    if due_date_raw:
        try:
            due_date = datetime.strptime(due_date_raw, "%Y-%m-%d").date()
        except ValueError:
            pass

    item = MeetingActionItem(
        meeting_id=meeting.id, description=desc, owner_id=owner_id,
        due_date=due_date, priority=request.form.get("priority", Priority.MEDIUM),
        source="manual",
    )
    db.session.add(item)
    db.session.commit()

    if owner_id:
        notify_user(int(owner_id), NotificationType.ACTION_ITEM_ASSIGNED, "New action item assigned",
                   desc[:150], url_for("meetings.meeting_detail", meeting_id=meeting.id))

    audit("meeting.action_item_add", "meeting", meeting.id)
    flash("Action item added.", "success")
    return redirect(url_for("meetings.meeting_detail", meeting_id=meeting.id))


@meetings_bp.route("/action-items/<int:item_id>/status", methods=["POST"])
@login_required
def update_action_item_status(item_id):
    item = MeetingActionItem.query.get_or_404(item_id)
    meeting = item.meeting
    is_owner = item.owner_id == current_user.id
    if not (is_owner or _can_manage(meeting)):
        abort(403)

    new_status = request.form.get("status")
    if new_status in ActionItemStatus.ALL:
        item.status = new_status
        item.completed_at = datetime.now(timezone.utc) if new_status == ActionItemStatus.DONE else None
        db.session.commit()
        audit("meeting.action_item_status", "meeting", meeting.id, {"item_id": item.id, "status": new_status})

    if request.is_json or request.headers.get("X-Requested-With") == "XMLHttpRequest":
        return jsonify({"success": True, "status": item.status})
    return redirect(request.referrer or url_for("meetings.meeting_detail", meeting_id=meeting.id))


@meetings_bp.route("/action-items/<int:item_id>/delete", methods=["POST"])
@login_required
def delete_action_item(item_id):
    item = MeetingActionItem.query.get_or_404(item_id)
    meeting = item.meeting
    if not _can_manage(meeting):
        abort(403)
    db.session.delete(item)
    db.session.commit()
    audit("meeting.action_item_delete", "meeting", meeting.id)
    return redirect(url_for("meetings.meeting_detail", meeting_id=meeting.id))


# ================================================================
# Calendar export (Module 5 — "Generate Calendar Tasks")
# ================================================================
@meetings_bp.route("/meetings/<int:meeting_id>/calendar.ics")
@login_required
def calendar_export(meeting_id):
    meeting = Meeting.query.get_or_404(meeting_id)
    items = meeting.action_items.filter(MeetingActionItem.due_date.isnot(None)).all()
    ics = build_action_items_ics(items, calendar_name=f"{meeting.title} — Action Items")
    return Response(ics, mimetype="text/calendar",
                    headers={"Content-Disposition": f'attachment; filename="{meeting.meeting_code}-action-items.ics"'})


# ================================================================
# Employee-facing: My Action Items
# ================================================================
@meetings_bp.route("/my-action-items")
@login_required
def my_action_items():
    items = (MeetingActionItem.query
            .filter_by(owner_id=current_user.id)
            .order_by(MeetingActionItem.status, MeetingActionItem.due_date)
            .all())
    return render_template("meetings/my_action_items.html",
        items=items, days_until=days_until, unread_count=_unread_count())
