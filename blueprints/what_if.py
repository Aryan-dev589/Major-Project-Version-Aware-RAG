"""
blueprints/what_if.py
Module 25: Policy Impact Simulator ("What-If" Compliance Checker)
"""
import json
import traceback
from flask import Blueprint, render_template, redirect, url_for, flash, request, Response, stream_with_context
from flask_login import login_required, current_user

from models import db, WhatIfQuery, UserRole, Notification
from utils import audit, role_required
from whatif_ai import evaluate_scenario, stream_evaluate_scenario

what_if_bp = Blueprint("what_if", __name__)


def _unread_count():
    return Notification.query.filter_by(user_id=current_user.id, is_read=False).count()


@what_if_bp.route("/what-if", methods=["GET", "POST"])
@login_required
def simulator():
    scenario = ""
    result = None

    if request.method == "POST" or request.args.get("scenario"):
        if request.method == "POST":
            scenario = (request.form.get("scenario") or "").strip()
        else:
            scenario = (request.args.get("scenario") or "").strip()

        if not scenario:
            flash("Describe a scenario first — e.g. \"Can I expense a personal phone under $300?\"", "warning")
            return redirect(url_for("what_if.simulator"))
        if len(scenario) > 800:
            flash("Please keep the scenario under 800 characters.", "warning")
            return redirect(url_for("what_if.simulator"))

        dept = current_user.department.name if getattr(current_user, "department", None) else ""
        role_str = str(current_user.role.value if hasattr(current_user.role, 'value') else current_user.role).lower()

        try:
            ai_result = evaluate_scenario(
                scenario=scenario, user_role=role_str, user_department=dept,
            )
        except Exception as e:
            print(f"[What-If Blueprint Error]: {e}")
            traceback.print_exc()
            ai_result = {
                "verdict": "unclear", "confidence": 0,
                "explanation": "The simulator couldn't complete this check right now. Please try again shortly or ask HR directly.",
                "required_actions": [], "citations": [], "flagged_for_hr": True, "chunks_used": 0,
            }

        record = WhatIfQuery(
            user_id=current_user.id,
            scenario_text=scenario,
            verdict=ai_result["verdict"],
            confidence=ai_result["confidence"],
            explanation=ai_result["explanation"],
            flagged_for_hr=ai_result["flagged_for_hr"],
        )
        record.required_actions = ai_result["required_actions"]
        record.applicable_policies = ai_result["citations"]
        db.session.add(record)
        db.session.commit()
        audit("what_if.run", "what_if_query", record.id,
              {"verdict": record.verdict, "confidence": record.confidence})

        result = {
            "id": record.id, "verdict": ai_result["verdict"], "confidence": ai_result["confidence"],
            "explanation": ai_result["explanation"], "required_actions": ai_result["required_actions"],
            "citations": ai_result["citations"], "flagged_for_hr": ai_result["flagged_for_hr"],
        }

    return render_template("employee/what_if.html",
        scenario=scenario, result=result, unread_count=_unread_count())


@what_if_bp.route("/what-if/stream", methods=["POST"])
@login_required
def simulator_stream():
    data = request.get_json(force=True, silent=True) or {}
    scenario = (data.get("scenario") or "").strip()
    if not scenario:
        return Response("data: {\"error\": \"Empty scenario\"}\n\n", mimetype="text/event-stream")

    if len(scenario) > 800:
        return Response("data: {\"error\": \"Scenario too long\"}\n\n", mimetype="text/event-stream")

    dept = current_user.department.name if getattr(current_user, "department", None) else ""
    role_str = str(current_user.role.value if hasattr(current_user.role, 'value') else current_user.role).lower()

    def _stream():
        final_result = None
        try:
            for chunk in stream_evaluate_scenario(scenario=scenario, user_role=role_str, user_department=dept):
                payload = json.loads(chunk.replace('data: ', '', 1).strip())
                if payload.get('done'):
                    final_result = payload.get('result')
                yield chunk
        except Exception as e:
            print(f"[What-If Stream Error]: {e}")
            yield f"data: {json.dumps({'done': True, 'result': {'verdict': 'unclear', 'confidence': 0, 'explanation': 'The simulator could not complete this check right now.', 'required_actions': [], 'citations': [], 'flagged_for_hr': True, 'chunks_used': 0}})}\n\n"
            final_result = {
                'verdict': 'unclear', 'confidence': 0, 'explanation': 'The simulator could not complete this check right now.',
                'required_actions': [], 'citations': [], 'flagged_for_hr': True, 'chunks_used': 0,
            }

        if final_result:
            try:
                record = WhatIfQuery(
                    user_id=current_user.id,
                    scenario_text=scenario,
                    verdict=final_result["verdict"],
                    confidence=final_result["confidence"],
                    explanation=final_result["explanation"],
                    flagged_for_hr=final_result["flagged_for_hr"],
                )
                record.required_actions = final_result["required_actions"]
                record.applicable_policies = final_result["citations"]
                db.session.add(record)
                db.session.commit()
                audit("what_if.run", "what_if_query", record.id,
                      {"verdict": record.verdict, "confidence": record.confidence})
            except Exception:
                db.session.rollback()

    return Response(stream_with_context(_stream()), mimetype="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
    })


@what_if_bp.route("/what-if/history")
@login_required
def history():
    queries = WhatIfQuery.query.filter_by(user_id=current_user.id)\
        .order_by(WhatIfQuery.created_at.desc()).limit(50).all()
    return render_template("employee/what_if_history.html",
        queries=queries, unread_count=_unread_count())


# ================================================================
# HR / Admin queue of flagged (grey-area / risky) scenarios
# ================================================================
@what_if_bp.route("/admin/what-if-queue")
@login_required
@role_required(UserRole.ADMIN, UserRole.HR)
def review_queue():
    status = request.args.get("status", "open")
    q = WhatIfQuery.query.filter_by(flagged_for_hr=True)
    if status == "open":
        pass  # flagged_for_hr stays True until resolved
    queries = q.order_by(WhatIfQuery.created_at.desc()).limit(200).all()
    total_runs = WhatIfQuery.query.count()
    return render_template("admin/what_if_queue.html",
        queries=queries, total_runs=total_runs)


@what_if_bp.route("/admin/what-if/<int:query_id>/resolve", methods=["POST"])
@login_required
@role_required(UserRole.ADMIN, UserRole.HR)
def resolve(query_id):
    record = WhatIfQuery.query.get_or_404(query_id)
    record.flagged_for_hr = False
    db.session.commit()
    audit("what_if.resolve", "what_if_query", record.id)
    flash("Marked as reviewed.", "success")
    return redirect(url_for("what_if.review_queue"))