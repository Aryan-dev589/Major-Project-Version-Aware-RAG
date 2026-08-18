"""
blueprints/workflow.py
Module 8: Workflow Automation

Routes:
  GET  /admin/workflows                     list templates
  GET/POST /admin/workflows/new             create a template (stages + approvers)
  GET/POST /admin/workflows/<id>/edit        edit a template
  POST /admin/workflows/<id>/delete
  POST /admin/workflows/<id>/set-default
  POST /admin/policies/<id>/workflow/submit  start a workflow run for a policy
  POST /admin/workflow-stage/<id>/act        approve/reject a stage instance
  POST /admin/workflows/check-reminders      manually trigger SLA reminders/escalation
  GET  /admin/workflow-analytics             bottlenecks, throughput, overdue/escalated
"""
import json
from datetime import datetime, timezone

from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify, abort
from flask_login import login_required, current_user
from sqlalchemy import func

from models import (db, WorkflowTemplate, WorkflowStage, WorkflowStageApprover,
                    WorkflowStageInstance, WorkflowApprovalAction, WorkflowStageStatus,
                    WorkflowApprovalMode, Policy, PolicyCategory, PolicyStatus,
                    PolicyVersion, User, UserRole, Priority)
from utils import audit, role_required
from workflow_engine import select_template, start_workflow, record_action, check_reminders_and_escalations

workflow_bp = Blueprint("workflow", __name__, url_prefix="/admin")


# ================================================================
# Template list / builder
# ================================================================
@workflow_bp.route("/workflows")
@login_required
@role_required(UserRole.ADMIN, UserRole.HR)
def template_list():
    templates = WorkflowTemplate.query.order_by(WorkflowTemplate.created_at.desc()).all()
    return render_template("admin/workflow_list.html", templates=templates)


def _save_stages_from_form(template):
    """Parses repeating stage/approver form fields and (re)builds template.stages."""
    WorkflowStage.query.filter_by(template_id=template.id).delete()
    db.session.flush()

    stage_names = request.form.getlist("stage_name[]")
    stage_modes = request.form.getlist("stage_mode[]")
    stage_slas = request.form.getlist("stage_sla[]")
    # approvers per stage arrive as parallel arrays keyed by stage index, e.g.
    # approver_stage_idx[] / approver_type[] / approver_role[] / approver_user_id[]
    approver_stage_idx = request.form.getlist("approver_stage_idx[]")
    approver_type = request.form.getlist("approver_type[]")
    approver_role = request.form.getlist("approver_role[]")
    approver_user_id = request.form.getlist("approver_user_id[]")

    created_stages = []
    for i, name in enumerate(stage_names):
        name = name.strip()
        if not name:
            continue
        stage = WorkflowStage(
            template_id=template.id, name=name, order=i + 1,
            approval_mode=stage_modes[i] if i < len(stage_modes) else WorkflowApprovalMode.ALL,
            sla_hours=int(stage_slas[i]) if i < len(stage_slas) and stage_slas[i].strip().isdigit() else None,
        )
        db.session.add(stage)
        db.session.flush()
        created_stages.append(stage)

    for j, idx_str in enumerate(approver_stage_idx):
        try:
            stage_idx = int(idx_str)
        except ValueError:
            continue
        if stage_idx >= len(created_stages):
            continue
        a_type = approver_type[j] if j < len(approver_type) else "role"
        role_val = approver_role[j] if j < len(approver_role) else ""
        user_id_val = approver_user_id[j] if j < len(approver_user_id) else ""
        if a_type == "user" and user_id_val:
            db.session.add(WorkflowStageApprover(stage_id=created_stages[stage_idx].id,
                                                 approver_type="user", user_id=int(user_id_val)))
        elif a_type == "role" and role_val:
            db.session.add(WorkflowStageApprover(stage_id=created_stages[stage_idx].id,
                                                 approver_type="role", role=role_val))


@workflow_bp.route("/workflows/new", methods=["GET", "POST"])
@login_required
@role_required(UserRole.ADMIN, UserRole.HR)
def template_create():
    categories = PolicyCategory.query.order_by(PolicyCategory.name).all()
    users = User.query.filter_by(is_active=True).order_by(User.name).all()
    error = None

    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            error = "Template name is required."
        else:
            template = WorkflowTemplate(
                name=name,
                description=request.form.get("description", "").strip(),
                category_id=request.form.get("category_id") or None,
                is_default=bool(request.form.get("is_default")),
                is_active=True,
                created_by_id=current_user.id,
            )
            priorities = request.form.getlist("applies_priorities")
            template.applies_priorities = priorities

            if template.is_default:
                WorkflowTemplate.query.filter_by(is_default=True).update({"is_default": False})

            db.session.add(template)
            db.session.flush()
            _save_stages_from_form(template)
            db.session.commit()

            audit("workflow.template_create", "workflow_template", template.id, {"name": name})
            flash(f'Workflow template "{name}" created.', "success")
            return redirect(url_for("workflow.template_list"))

    return render_template("admin/workflow_form.html",
        template=None, categories=categories, users=users, error=error,
        all_roles=UserRole.ALL, all_priorities=[Priority.LOW, Priority.MEDIUM, Priority.HIGH, Priority.CRITICAL],
    )


@workflow_bp.route("/workflows/<int:template_id>/edit", methods=["GET", "POST"])
@login_required
@role_required(UserRole.ADMIN, UserRole.HR)
def template_edit(template_id):
    template = WorkflowTemplate.query.get_or_404(template_id)
    categories = PolicyCategory.query.order_by(PolicyCategory.name).all()
    users = User.query.filter_by(is_active=True).order_by(User.name).all()
    error = None

    if request.method == "POST":
        template.name = request.form.get("name", "").strip() or template.name
        template.description = request.form.get("description", "").strip()
        template.category_id = request.form.get("category_id") or None
        template.applies_priorities = request.form.getlist("applies_priorities")
        make_default = bool(request.form.get("is_default"))
        if make_default and not template.is_default:
            WorkflowTemplate.query.filter_by(is_default=True).update({"is_default": False})
        template.is_default = make_default

        _save_stages_from_form(template)
        db.session.commit()
        audit("workflow.template_edit", "workflow_template", template.id)
        flash("Workflow template updated.", "success")
        return redirect(url_for("workflow.template_list"))

    return render_template("admin/workflow_form.html",
        template=template, categories=categories, users=users, error=error,
        all_roles=UserRole.ALL, all_priorities=[Priority.LOW, Priority.MEDIUM, Priority.HIGH, Priority.CRITICAL],
    )


@workflow_bp.route("/workflows/<int:template_id>/delete", methods=["POST"])
@login_required
@role_required(UserRole.ADMIN, UserRole.HR)
def template_delete(template_id):
    template = WorkflowTemplate.query.get_or_404(template_id)
    db.session.delete(template)
    db.session.commit()
    audit("workflow.template_delete", "workflow_template", template_id)
    flash("Workflow template deleted.", "info")
    return redirect(url_for("workflow.template_list"))


@workflow_bp.route("/workflows/<int:template_id>/set-default", methods=["POST"])
@login_required
@role_required(UserRole.ADMIN, UserRole.HR)
def template_set_default(template_id):
    WorkflowTemplate.query.filter_by(is_default=True).update({"is_default": False})
    template = WorkflowTemplate.query.get_or_404(template_id)
    template.is_default = True
    db.session.commit()
    flash(f'"{template.name}" is now the default workflow.', "success")
    return redirect(url_for("workflow.template_list"))


# ================================================================
# Submitting a policy through the workflow engine
# ================================================================
@workflow_bp.route("/policies/<int:policy_id>/workflow/submit", methods=["POST"])
@login_required
@role_required(UserRole.ADMIN, UserRole.HR, UserRole.MANAGER)
def submit_workflow(policy_id):
    policy = Policy.query.get_or_404(policy_id)
    version = policy.versions.filter_by(is_active=True).first()
    if not version:
        flash("No active version to submit.", "danger")
        return redirect(url_for("admin.policy_detail", policy_id=policy.id))

    template = select_template(policy)
    if not template:
        flash("No workflow template applies to this policy. Configure one under Workflows.", "warning")
        return redirect(url_for("admin.policy_detail", policy_id=policy.id))

    policy.status = PolicyStatus.HR_REVIEW
    db.session.commit()
    start_workflow(policy, version, template, started_by=current_user)

    flash(f'Submitted via "{template.name}" workflow ({len(template.stages)} stage(s)).', "success")
    return redirect(url_for("admin.policy_detail", policy_id=policy.id))


@workflow_bp.route("/workflow-stage/<int:stage_id>/act", methods=["POST"])
@login_required
def act_on_stage(stage_id):
    stage = WorkflowStageInstance.query.get_or_404(stage_id)
    decision = request.form.get("action")  # "approve" | "reject"
    comment = request.form.get("comment", "").strip()

    if decision not in ("approve", "reject"):
        abort(400)

    result = record_action(stage, current_user, decision, comment)
    if result.get("error"):
        flash(result["error"], "danger")
    elif decision == "reject":
        flash(f'Rejected at "{stage.name}".', "warning")
    elif result.get("next_stage"):
        flash(f'Approved. Now awaiting "{result["next_stage"]}".', "success")
    else:
        flash("All stages approved — policy is now Active.", "success")

    return redirect(url_for("admin.policy_detail", policy_id=stage.policy_id))


# ================================================================
# Reminders / escalation (manual trigger; also runnable via cron script)
# ================================================================
@workflow_bp.route("/workflows/check-reminders", methods=["POST"])
@login_required
@role_required(UserRole.ADMIN, UserRole.HR)
def check_reminders():
    summary = check_reminders_and_escalations()
    flash(f'Checked {summary["checked"]} pending stage(s): '
         f'{summary["reminders_sent"]} reminder(s) sent, '
         f'{summary["escalations_made"]} escalation(s) triggered.', "info")
    return redirect(url_for("workflow.workflow_analytics"))


# ================================================================
# Analytics
# ================================================================
@workflow_bp.route("/workflow-analytics")
@login_required
@role_required(UserRole.ADMIN, UserRole.HR)
def workflow_analytics():
    completed = WorkflowStageInstance.query.filter(
        WorkflowStageInstance.completed_at.isnot(None)).all()

    # Average time-in-stage, grouped by stage name (bottleneck detection)
    stage_times = {}
    for s in completed:
        hours = (s.completed_at.replace(tzinfo=None) - s.created_at).total_seconds() / 3600
        stage_times.setdefault(s.name, []).append(hours)
    bottlenecks = sorted(
        [{"name": name, "avg_hours": round(sum(v) / len(v), 1), "count": len(v)}
         for name, v in stage_times.items()],
        key=lambda x: -x["avg_hours"]
    )

    pending_count = WorkflowStageInstance.query.filter_by(status=WorkflowStageStatus.PENDING).count()
    overdue_count = sum(1 for s in WorkflowStageInstance.query.filter_by(
        status=WorkflowStageStatus.PENDING).all() if s.is_overdue)
    escalated_count = WorkflowStageInstance.query.filter_by(escalated=True).count()
    approved_count = WorkflowStageInstance.query.filter_by(status=WorkflowStageStatus.APPROVED).count()
    rejected_count = WorkflowStageInstance.query.filter_by(status=WorkflowStageStatus.REJECTED).count()

    pending_stages = (WorkflowStageInstance.query.filter_by(status=WorkflowStageStatus.PENDING)
                      .order_by(WorkflowStageInstance.sla_due_at.asc().nullslast()).all())

    templates = WorkflowTemplate.query.order_by(WorkflowTemplate.name).all()

    return render_template("admin/workflow_analytics.html",
        bottlenecks=bottlenecks, pending_count=pending_count, overdue_count=overdue_count,
        escalated_count=escalated_count, approved_count=approved_count, rejected_count=rejected_count,
        pending_stages=pending_stages, templates=templates,
    )
