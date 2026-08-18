"""
blueprints/search.py
Module 14: Search Engine

Advanced multi-field search across Policies and Meetings, with saved
searches and (for admins) search analytics.

Routes:
  GET  /search/advanced              advanced search form + results
  POST /search/save                  save the current filter set
  GET  /search/saved                 list current user's saved searches
  GET  /search/saved/<id>/run        re-run a saved search (redirects with its filters)
  POST /search/saved/<id>/delete     delete a saved search
  GET  /admin/search-analytics       admin: top queries, zero-result queries, volume
"""
import json
from datetime import datetime, timezone

from flask import Blueprint, render_template, redirect, url_for, flash, request, abort
from flask_login import login_required, current_user
from sqlalchemy import func, or_

from models import (db, Policy, PolicyVersion, PolicyCategory, PolicyStatus,
                    Department, Tag, User, UserRole, Meeting, MeetingType,
                    MeetingStatus, SavedSearch, SearchHistory, Notification)
from utils import audit, paginate, role_required

search_bp = Blueprint("search", __name__)


def _unread_count():
    return Notification.query.filter_by(user_id=current_user.id, is_read=False).count()


# ================================================================
# Advanced Search
# ================================================================
FILTER_KEYS = [
    "q", "scope", "category_id", "department_id", "author_id", "tag",
    "priority", "status", "confidentiality", "mandatory", "version_label",
    "date_from", "date_to", "search_content",
    "meeting_type", "meeting_status", "organizer_id",
]


def _current_filters():
    return {k: request.args.get(k, "").strip() for k in FILTER_KEYS}


def _parse_date(value):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        return None


def _search_policies(f):
    q = Policy.query
    if f["q"]:
        term = f"%{f['q']}%"
        if f["search_content"]:
            # Join active version content too
            version_ids = [v.policy_id for v in PolicyVersion.query.filter(
                PolicyVersion.content.ilike(term)).all()]
            q = q.filter(or_(
                Policy.title.ilike(term),
                Policy.description.ilike(term),
                Policy.id.in_(version_ids) if version_ids else False,
            ))
        else:
            q = q.filter(or_(Policy.title.ilike(term), Policy.description.ilike(term)))
    if f["category_id"]:
        q = q.filter_by(category_id=int(f["category_id"]))
    if f["department_id"]:
        q = q.filter_by(department_id=int(f["department_id"]))
    if f["author_id"]:
        q = q.filter_by(author_id=int(f["author_id"]))
    if f["priority"]:
        q = q.filter_by(priority=f["priority"])
    if f["status"]:
        q = q.filter_by(status=f["status"])
    if f["confidentiality"]:
        q = q.filter_by(confidentiality=f["confidentiality"])
    if f["mandatory"] == "1":
        q = q.filter_by(is_mandatory=True)
    if f["version_label"]:
        ver_matches = [v.policy_id for v in PolicyVersion.query.filter(
            PolicyVersion.version_label.ilike(f"%{f['version_label']}%")).all()]
        q = q.filter(Policy.id.in_(ver_matches) if ver_matches else False)
    if f["tag"]:
        tag = Tag.query.filter_by(name=f["tag"]).first()
        q = q.filter(Policy.tags.contains(tag)) if tag else q.filter(False)
    date_from, date_to = _parse_date(f["date_from"]), _parse_date(f["date_to"])
    if date_from:
        q = q.filter(Policy.created_at >= date_from)
    if date_to:
        q = q.filter(Policy.created_at <= date_to)
    # Non-admins/HR only see active policies
    if not current_user.can_manage_policies():
        q = q.filter_by(status=PolicyStatus.ACTIVE)
    return q.order_by(Policy.updated_at.desc()).all()


def _search_meetings(f):
    q = Meeting.query
    if f["q"]:
        term = f"%{f['q']}%"
        q = q.filter(or_(Meeting.title.ilike(term), Meeting.agenda.ilike(term)))
    if f["department_id"]:
        q = q.filter_by(department_id=int(f["department_id"]))
    if f["organizer_id"]:
        q = q.filter_by(organizer_id=int(f["organizer_id"]))
    if f["meeting_type"]:
        q = q.filter_by(meeting_type=f["meeting_type"])
    if f["meeting_status"]:
        q = q.filter_by(status=f["meeting_status"])
    date_from, date_to = _parse_date(f["date_from"]), _parse_date(f["date_to"])
    if date_from:
        q = q.filter(Meeting.scheduled_at >= date_from)
    if date_to:
        q = q.filter(Meeting.scheduled_at <= date_to)
    return q.order_by(Meeting.scheduled_at.desc()).all()


@search_bp.route("/search/advanced")
@login_required
def advanced_search():
    f = _current_filters()
    scope = f["scope"] or "policies"
    has_query = any(v for k, v in f.items() if k not in ("scope", "search_content"))

    policy_results, meeting_results = [], []
    if has_query:
        if scope in ("policies", "all"):
            policy_results = _search_policies(f)
        if scope in ("meetings", "all"):
            meeting_results = _search_meetings(f)

        total = len(policy_results) + len(meeting_results)
        sh = SearchHistory(
            user_id=current_user.id,
            query_text=(f["q"] or f"[filters: {scope}]")[:500],
            answered=total > 0,
            chunks_found=total,
        )
        db.session.add(sh)
        db.session.commit()

    categories = PolicyCategory.query.order_by(PolicyCategory.name).all()
    departments = Department.query.order_by(Department.name).all()
    authors = User.query.filter_by(is_active=True).order_by(User.name).all()
    tags = Tag.query.order_by(Tag.name).all()

    return render_template("search/advanced.html",
        f=f, scope=scope, has_query=has_query,
        policy_results=policy_results, meeting_results=meeting_results,
        categories=categories, departments=departments, authors=authors, tags=tags,
        all_statuses=PolicyStatus.ALL, all_meeting_types=MeetingType.ALL,
        all_meeting_statuses=MeetingStatus.ALL,
        unread_count=_unread_count(),
    )


# ================================================================
# Saved Searches
# ================================================================
@search_bp.route("/search/save", methods=["POST"])
@login_required
def save_search():
    name = request.form.get("name", "").strip()
    scope = request.form.get("scope", "policies")
    if not name:
        flash("Give your saved search a name.", "warning")
        return redirect(request.referrer or url_for("search.advanced_search"))

    filters = {k: request.form.get(k, "") for k in FILTER_KEYS if k != "scope" and request.form.get(k, "")}
    saved = SavedSearch(user_id=current_user.id, name=name, scope=scope)
    saved.filters = filters
    db.session.add(saved)
    db.session.commit()
    audit("search.save", "saved_search", saved.id, {"name": name})
    flash(f'Search saved as "{name}".', "success")
    return redirect(url_for("search.advanced_search", **filters))


@search_bp.route("/search/saved")
@login_required
def saved_searches():
    searches = SavedSearch.query.filter_by(user_id=current_user.id)\
        .order_by(SavedSearch.created_at.desc()).all()
    return render_template("search/saved.html", searches=searches, unread_count=_unread_count())


@search_bp.route("/search/saved/<int:search_id>/run")
@login_required
def run_saved_search(search_id):
    saved = SavedSearch.query.get_or_404(search_id)
    if saved.user_id != current_user.id:
        abort(403)
    saved.use_count = (saved.use_count or 0) + 1
    saved.last_used_at = datetime.now(timezone.utc)
    db.session.commit()
    return redirect(url_for("search.advanced_search", scope=saved.scope, **saved.filters))


@search_bp.route("/search/saved/<int:search_id>/delete", methods=["POST"])
@login_required
def delete_saved_search(search_id):
    saved = SavedSearch.query.get_or_404(search_id)
    if saved.user_id != current_user.id:
        abort(403)
    db.session.delete(saved)
    db.session.commit()
    flash("Saved search deleted.", "info")
    return redirect(url_for("search.saved_searches"))


# ================================================================
# Search Analytics (admin)
# ================================================================
@search_bp.route("/admin/search-analytics")
@login_required
@role_required(UserRole.ADMIN, UserRole.HR)
def search_analytics():
    total_searches = SearchHistory.query.count()
    zero_result = SearchHistory.query.filter_by(answered=False).count()

    top_queries = (db.session.query(SearchHistory.query_text, func.count(SearchHistory.id).label("cnt"))
                  .group_by(SearchHistory.query_text)
                  .order_by(func.count(SearchHistory.id).desc())
                  .limit(10).all())

    zero_result_queries = (db.session.query(SearchHistory.query_text, func.count(SearchHistory.id).label("cnt"))
                           .filter_by(answered=False)
                           .group_by(SearchHistory.query_text)
                           .order_by(func.count(SearchHistory.id).desc())
                           .limit(10).all())

    recent = SearchHistory.query.order_by(SearchHistory.created_at.desc()).limit(20).all()
    most_saved = SavedSearch.query.order_by(SavedSearch.use_count.desc()).limit(8).all()

    return render_template("admin/search_analytics.html",
        total_searches=total_searches, zero_result=zero_result,
        success_rate=round(100 * (total_searches - zero_result) / total_searches) if total_searches else 0,
        top_queries=top_queries, zero_result_queries=zero_result_queries,
        recent=recent, most_saved=most_saved,
    )
