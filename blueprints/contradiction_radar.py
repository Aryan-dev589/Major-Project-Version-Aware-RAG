"""
blueprints/contradiction_radar.py
Module: Contradiction Radar (continuous)

The AI Review's find_conflicts() (policy_ai.py) already checks one policy
against similar peers, but only when someone manually runs AI Review on
that specific policy during editing. This dashboard is the read-only view
over ContradictionFlag rows persisted by scripts/scan_contradictions.py,
which runs that same check across every same-category active pair on a
schedule — turning it into ongoing monitoring instead of a one-off check,
so drift introduced by independently-edited policies gets caught.

Degrades gracefully: if the scan has never run, or no LLM is configured
(find_conflicts always returns no conflicts without one), the dashboard
simply says "0 found — configure LLM for deeper checks."

Routes:
  GET  /admin/contradictions                dashboard: open + resolved flags
  POST /admin/contradictions/<id>/dismiss    mark a flag as a false positive
"""
from datetime import datetime, timezone

from flask import Blueprint, render_template, redirect, url_for, flash
from flask_login import login_required, current_user

from models import db, ContradictionFlag, ContradictionScanStatus, UserRole
from utils import role_required, audit

contradiction_radar_bp = Blueprint("contradiction_radar", __name__, url_prefix="/admin")


@contradiction_radar_bp.route("/contradictions")
@login_required
@role_required(UserRole.ADMIN, UserRole.HR)
def dashboard():
    open_flags = (ContradictionFlag.query.filter_by(status=ContradictionScanStatus.OPEN)
                 .order_by(ContradictionFlag.last_confirmed_at.desc()).all())
    resolved_flags = (ContradictionFlag.query
                     .filter(ContradictionFlag.status.in_(
                         [ContradictionScanStatus.RESOLVED, ContradictionScanStatus.DISMISSED]))
                     .order_by(ContradictionFlag.detected_at.desc()).limit(20).all())
    has_ever_scanned = ContradictionFlag.query.count() > 0
    return render_template("admin/contradictions.html",
        open_flags=open_flags, resolved_flags=resolved_flags, has_ever_scanned=has_ever_scanned)


@contradiction_radar_bp.route("/contradictions/<int:flag_id>/dismiss", methods=["POST"])
@login_required
@role_required(UserRole.ADMIN, UserRole.HR)
def dismiss(flag_id):
    flag = ContradictionFlag.query.get_or_404(flag_id)
    flag.status = ContradictionScanStatus.DISMISSED
    flag.resolved_at = datetime.now(timezone.utc)
    flag.resolved_by_id = current_user.id
    db.session.commit()
    audit("contradiction.dismiss", "contradiction_flag", flag.id)
    flash("Flag dismissed as a false positive.", "success")
    return redirect(url_for("contradiction_radar.dashboard"))
