"""
workflow_engine.py
Module 8: Workflow Automation — engine functions.

This sits alongside (not replacing) the original simple 3-stage approval
flow in blueprints/admin.py. It only activates once at least one
WorkflowTemplate exists in the system; policy_detail.html decides which
"Submit for Review" button to show based on that.

Key ideas:
  - A WorkflowTemplate is chosen for a policy based on category + priority
    conditions ("conditional workflow").
  - Each WorkflowStage can have multiple approvers (WorkflowStageApprover);
    approval_mode "any" clears the stage on the first approval, "all"
    requires every assigned approver to approve ("parallel approval").
  - SLA timers + escalation + reminders are handled by check_reminders_and_escalations(),
    callable from an admin button or a cron/Task Scheduler job
    (see scripts/check_workflow_reminders.py).
"""
from datetime import datetime, timedelta, timezone

from models import (db, WorkflowTemplate, WorkflowStageInstance, WorkflowApprovalAction,
                    WorkflowStageStatus, WorkflowApprovalMode, WorkflowInstanceStatus,
                    PolicyStatus, Notification, NotificationType, User, UserRole)
from utils import audit, notify_user


def select_template(policy):
    """
    Conditional workflow selection: prefer a template whose category+priority
    conditions match this policy; fall back to the system default template.
    """
    candidates = WorkflowTemplate.query.filter_by(is_active=True).all()
    specific = [t for t in candidates if t.category_id and t.matches(policy)]
    if specific:
        return specific[0]
    generic_matches = [t for t in candidates if not t.category_id and t.matches(policy)]
    if generic_matches:
        return generic_matches[0]
    defaults = [t for t in candidates if t.is_default]
    return defaults[0] if defaults else None


def _notify_stage_approvers(stage_instance, policy):
    actions = WorkflowApprovalAction.query.filter_by(
        stage_instance_id=stage_instance.id, status=WorkflowStageStatus.PENDING).all()
    notified_user_ids = set()
    for action in actions:
        if action.approver_type == "user" and action.assigned_user_id:
            notified_user_ids.add(action.assigned_user_id)
        elif action.approver_type == "role" and action.role:
            for u in User.query.filter_by(role=action.role, is_active=True).all():
                notified_user_ids.add(u.id)
    for uid in notified_user_ids:
        notify_user(uid, NotificationType.APPROVAL_NEEDED,
                   f"Approval needed: {policy.title} — {stage_instance.name}",
                   link=f"/admin/policies/{policy.id}")


def start_workflow(policy, version, template, started_by=None):
    """
    Instantiates all stages from `template` for this policy/version.
    Stage 1 becomes PENDING immediately (with SLA timer started); the rest
    start WAITING. Returns the list of created WorkflowStageInstance rows.
    """
    # Clear any previous run for this version (e.g. resubmission after rejection)
    WorkflowStageInstance.query.filter_by(version_id=version.id).delete()
    db.session.flush()

    instances = []
    for stage in template.stages:
        instance = WorkflowStageInstance(
            policy_id=policy.id, version_id=version.id, template_id=template.id,
            name=stage.name, order=stage.order, approval_mode=stage.approval_mode,
            sla_hours=stage.sla_hours,
            status=WorkflowStageStatus.WAITING,
        )
        db.session.add(instance)
        db.session.flush()

        for approver in stage.approvers:
            db.session.add(WorkflowApprovalAction(
                stage_instance_id=instance.id, approver_type=approver.approver_type,
                role=approver.role, assigned_user_id=approver.user_id,
                status=WorkflowStageStatus.PENDING,
            ))
        instances.append(instance)

    if instances:
        first = min(instances, key=lambda i: i.order)
        first.status = WorkflowStageStatus.PENDING
        if first.sla_hours:
            first.sla_due_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=first.sla_hours)

    db.session.commit()

    if instances:
        _notify_stage_approvers(min(instances, key=lambda i: i.order), policy)
        audit("workflow.start", "policy", policy.id, {"template": template.name, "stages": len(instances)})

    return instances


def _finish_stage(stage_instance, final_status):
    stage_instance.status = final_status
    stage_instance.completed_at = datetime.now(timezone.utc)
    # Any still-pending sibling actions in an "any" group get marked skipped
    for action in stage_instance.actions:
        if action.status == WorkflowStageStatus.PENDING:
            action.status = WorkflowStageStatus.SKIPPED


def record_action(stage_instance, actor, decision: str, comment: str = ""):
    """
    `decision` is "approve" or "reject". Returns a dict describing what
    happened: {stage_completed, workflow_status, next_stage} for the caller
    to flash/notify appropriately.
    """
    my_action = next((a for a in stage_instance.actions
                      if a.status == WorkflowStageStatus.PENDING and a.user_can_act(actor)), None)
    if not my_action:
        return {"error": "You are not an eligible approver for this stage, or it's already been acted on."}

    my_action.actor_id = actor.id
    my_action.comment = comment
    my_action.acted_at = datetime.now(timezone.utc)
    my_action.status = WorkflowStageStatus.APPROVED if decision == "approve" else WorkflowStageStatus.REJECTED

    policy = stage_instance.policy
    result = {"stage_completed": False, "workflow_status": None, "next_stage": None}

    if decision == "reject":
        _finish_stage(stage_instance, WorkflowStageStatus.REJECTED)
        policy.status = PolicyStatus.REJECTED
        stage_instance.version.status = "rejected"
        result.update(stage_completed=True, workflow_status=WorkflowInstanceStatus.REJECTED)
        db.session.commit()
        audit("workflow.reject", "policy", policy.id, {"stage": stage_instance.name})
        return result

    # decision == "approve" — check whether the stage is now cleared
    remaining = [a for a in stage_instance.actions if a.status == WorkflowStageStatus.PENDING]
    approved_count = sum(1 for a in stage_instance.actions if a.status == WorkflowStageStatus.APPROVED)
    stage_cleared = (
        (stage_instance.approval_mode == WorkflowApprovalMode.ANY and approved_count >= 1) or
        (stage_instance.approval_mode == WorkflowApprovalMode.ALL and not remaining)
    )

    if not stage_cleared:
        db.session.commit()
        audit("workflow.partial_approve", "policy", policy.id, {"stage": stage_instance.name})
        return result

    _finish_stage(stage_instance, WorkflowStageStatus.APPROVED)
    result["stage_completed"] = True

    # Activate next stage, if any
    next_stage = (WorkflowStageInstance.query
                 .filter_by(policy_id=policy.id, version_id=stage_instance.version_id)
                 .filter(WorkflowStageInstance.order > stage_instance.order)
                 .order_by(WorkflowStageInstance.order.asc()).first())

    if next_stage:
        next_stage.status = WorkflowStageStatus.PENDING
        if next_stage.sla_hours:
            next_stage.sla_due_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=next_stage.sla_hours)
        result["next_stage"] = next_stage.name
        result["workflow_status"] = WorkflowInstanceStatus.IN_PROGRESS
        db.session.commit()
        _notify_stage_approvers(next_stage, policy)
    else:
        # All stages cleared — publish
        policy.status = PolicyStatus.ACTIVE
        version = stage_instance.version
        version.status = "approved"
        version.approved_by_id = actor.id
        version.approved_at = datetime.now(timezone.utc)
        result["workflow_status"] = WorkflowInstanceStatus.APPROVED
        db.session.commit()

    audit("workflow.approve", "policy", policy.id, {"stage": stage_instance.name})
    return result


# ================================================================
# SLA reminders + escalation (Module 8 — "Auto reminders", "Escalation", "SLA timers")
# ================================================================
def check_reminders_and_escalations(reminder_lead_hours: int = 24):
    """
    Call periodically (admin button, or scripts/check_workflow_reminders.py via
    cron/Task Scheduler). For every PENDING stage instance:
      - if within `reminder_lead_hours` of its SLA deadline and no reminder sent yet,
        notify the pending approvers.
      - if past its SLA deadline and not yet escalated, mark escalated and notify
        all Admins (the escalation path, since this app has no per-user "manager" link).
    Returns a summary dict for display.
    """
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    reminders_sent, escalations_made = 0, 0

    pending = WorkflowStageInstance.query.filter_by(status=WorkflowStageStatus.PENDING).filter(
        WorkflowStageInstance.sla_due_at.isnot(None)).all()

    for stage in pending:
        policy = stage.policy
        if stage.is_overdue and not stage.escalated:
            stage.escalated = True
            stage.escalated_at = now
            for admin in User.query.filter_by(role=UserRole.ADMIN, is_active=True).all():
                notify_user(admin.id, NotificationType.APPROVAL_NEEDED,
                           f"⚠ Escalated: {policy.title} — {stage.name} is overdue",
                           link=f"/admin/policies/{policy.id}")
            audit("workflow.escalate", "policy", policy.id, {"stage": stage.name})
            escalations_made += 1
        elif not stage.escalated and not stage.reminder_sent_at:
            hours_left = (stage.sla_due_at - now).total_seconds() / 3600
            if 0 < hours_left <= reminder_lead_hours:
                stage.reminder_sent_at = now
                _notify_stage_approvers(stage, policy)
                reminders_sent += 1

    db.session.commit()
    return {"reminders_sent": reminders_sent, "escalations_made": escalations_made, "checked": len(pending)}
