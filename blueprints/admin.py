"""
blueprints/admin.py
Phase 1-3 Admin routes:
- Dashboard with stats
- Policy CRUD (create, edit, delete, archive, duplicate)
- Version management
- User management
- Approval workflow
- Audit logs
"""
import json
from datetime import datetime, timezone, date
from flask import (Blueprint, render_template, redirect, url_for,
                   flash, request, jsonify, abort, current_app)
from flask_login import login_required, current_user
from models import (db, Policy, PolicyVersion, PolicyCategory, PolicyStatus,
                    User, UserRole, Department, Tag, ApprovalWorkflow,
                    ApprovalStage, ApprovalStatus, Notification,
                    NotificationType, AuditLog, PolicyAcknowledgement,
                    WorkflowTemplate, WorkflowStageInstance)
from utils import (role_required, audit, notify_user, notify_all_employees,
                   generate_policy_id, next_version, compute_diff, paginate, days_until)
from rag.parser.pdf_parser import extract_text
from rag.parser.text_cleaner import clean_text
from rag.chunking.chunker import chunk_policy
from rag.embeddings.embedder import get_embedder
from rag.vectordb.chroma import get_store
from blueprints.employee import build_onboarding_checklist

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def _extract_pdf_text_from_upload(file_storage):
    if not file_storage or not getattr(file_storage, "filename", None):
        return None
    filename = file_storage.filename.lower()
    if not filename.endswith(".pdf"):
        return None

    import os
    import tempfile

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            file_storage.save(tmp.name)
            tmp_path = tmp.name
        return extract_text(tmp_path) or ""
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _sync_policy_version_to_chroma(policy, version_label, content, old_version_label=None):
    if not content:
        return

    try:
        store = get_store()
        if old_version_label:
            store.deactivate_policy_version(policy.id, old_version_label)

        clean = clean_text(content)
        if not clean.strip():
            return

        chunks = chunk_policy(
            text=clean,
            policy_id=policy.id,
            policy_name=policy.title,
            version=version_label,
            department=policy.department.name if policy.department else "",
        )
        if not chunks:
            return

        embeddings = get_embedder().embed([c.text for c in chunks])
        store.upsert_chunks([c.to_dict() for c in chunks], embeddings)
    except Exception as e:
        current_app.logger.warning(f"ChromaDB sync failed for policy {policy.id} {version_label}: {e}")


def hr_required(f):
    from functools import wraps
    @wraps(f)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.can_manage_policies():
            abort(403)
        return f(*args, **kwargs)
    return wrapped


# ================================================================
# Dashboard
# ================================================================
@admin_bp.route("/")
@login_required
@hr_required
def dashboard():
    total = Policy.query.count()
    active = Policy.query.filter_by(status=PolicyStatus.ACTIVE).count()
    draft = Policy.query.filter_by(status=PolicyStatus.DRAFT).count()
    archived = Policy.query.filter_by(status=PolicyStatus.ARCHIVED).count()
    in_review = Policy.query.filter(Policy.status.in_(PolicyStatus.IN_REVIEW)).count()
    total_users = User.query.filter_by(is_active=True).count()

    # Expiring soon (within 30 days)
    today = date.today()
    from datetime import timedelta
    soon = date.today() + timedelta(days=30)
    expiring = Policy.query.filter(
        Policy.expiry_date <= soon,
        Policy.expiry_date >= today,
        Policy.status == PolicyStatus.ACTIVE
    ).all()

    # Recent activity
    recent_policies = Policy.query.order_by(Policy.updated_at.desc()).limit(8).all()
    recent_logs = AuditLog.query.order_by(AuditLog.timestamp.desc()).limit(10).all()

    # Category breakdown
    categories = PolicyCategory.query.all()
    cat_data = []
    for c in categories:
        count = Policy.query.filter_by(category_id=c.id, status=PolicyStatus.ACTIVE).count()
        if count:
            cat_data.append({"name": c.name, "count": count, "icon": c.icon, "color": c.color})
    cat_data.sort(key=lambda x: x["count"], reverse=True)

    # Pending approvals for this user
    pending_approvals = ApprovalWorkflow.query.filter_by(
        actor_id=current_user.id, status=ApprovalStatus.PENDING
    ).all()

    return render_template("admin/dashboard.html",
        total=total, active=active, draft=draft, archived=archived,
        in_review=in_review, total_users=total_users, expiring=expiring,
        recent_policies=recent_policies, recent_logs=recent_logs,
        cat_data=cat_data, pending_approvals=pending_approvals,
    )


# ================================================================
# Policy List
# ================================================================
@admin_bp.route("/policies")
@login_required
@hr_required
def policy_list():
    page = request.args.get("page", 1, type=int)
    status_filter = request.args.get("status", "")
    cat_filter = request.args.get("category", "", type=int)
    dept_filter = request.args.get("department", "", type=int)
    search = request.args.get("q", "").strip()

    q = Policy.query
    if status_filter:
        q = q.filter_by(status=status_filter)
    if cat_filter:
        q = q.filter_by(category_id=cat_filter)
    if dept_filter:
        q = q.filter_by(department_id=dept_filter)
    if search:
        q = q.filter(Policy.title.ilike(f"%{search}%"))
    q = q.order_by(Policy.updated_at.desc())
    policies = paginate(q, page, 15)

    categories = PolicyCategory.query.order_by(PolicyCategory.name).all()
    departments = Department.query.order_by(Department.name).all()
    return render_template("admin/policy_list.html",
        policies=policies, categories=categories, departments=departments,
        status_filter=status_filter, cat_filter=cat_filter,
        dept_filter=dept_filter, search=search,
        all_statuses=PolicyStatus.ALL,
    )


# ================================================================
# Create Policy
# ================================================================
@admin_bp.route("/policies/new", methods=["GET", "POST"])
@login_required
@hr_required
def policy_create():
    categories = PolicyCategory.query.order_by(PolicyCategory.name).all()
    departments = Department.query.order_by(Department.name).all()
    hr_users = User.query.filter(User.role.in_([UserRole.HR, UserRole.ADMIN])).all()
    error = None

    if request.method == "POST":
        title = request.form.get("title", "").strip()
        pdf_text = _extract_pdf_text_from_upload(request.files.get("policy_file"))
        content = pdf_text.strip() if pdf_text is not None else request.form.get("content", "").strip()

        if not title:
            error = "Title is required."
        elif pdf_text is not None and not content:
            error = "Uploaded PDF did not contain extractable text."
        elif not content:
            error = "Policy content is required."
        else:
            policy = Policy(
                policy_id=generate_policy_id(),
                title=title,
                description=request.form.get("description", "").strip(),
                category_id=request.form.get("category_id") or None,
                department_id=request.form.get("department_id") or None,
                author_id=current_user.id,
                reviewer_id=request.form.get("reviewer_id") or None,
                approver_id=request.form.get("approver_id") or None,
                status=PolicyStatus.DRAFT,
                priority=request.form.get("priority", "medium"),
                confidentiality=request.form.get("confidentiality", "internal"),
                is_mandatory=bool(request.form.get("is_mandatory")),
            )
            # Dates
            for field in ("effective_date", "review_date", "expiry_date"):
                val = request.form.get(field)
                if val:
                    try:
                        setattr(policy, field, datetime.strptime(val, "%Y-%m-%d").date())
                    except ValueError:
                        pass

            # Tags
            tag_names = [t.strip() for t in request.form.get("tags", "").split(",") if t.strip()]
            for tn in tag_names:
                tag = Tag.query.filter_by(name=tn).first() or Tag(name=tn)
                if not tag.id:
                    db.session.add(tag)
                policy.tags.append(tag)

            db.session.add(policy)
            db.session.flush()  # get policy.id

            # First version
            version = PolicyVersion(
                policy_id=policy.id,
                version_num=1.0,
                version_label="v1.0",
                content=content,
                summary=request.form.get("change_summary", "Initial version"),
                change_reason="Policy created",
                created_by_id=current_user.id,
                is_active=True,
                status="draft",
                effective_date=policy.effective_date,
            )
            db.session.add(version)
            db.session.commit()

            audit("policy.create", "policy", policy.id, {"title": title, "policy_id": policy.policy_id})
            flash(f'Policy "{title}" created as draft.', "success")
            return redirect(url_for("admin.policy_detail", policy_id=policy.id))

    return render_template("admin/policy_form.html",
        categories=categories, departments=departments,
        hr_users=hr_users, error=error, policy=None,
    )


# ================================================================
# Policy Detail
# ================================================================
@admin_bp.route("/policies/<int:policy_id>")
@login_required
@hr_required
def policy_detail(policy_id):
    policy = Policy.query.get_or_404(policy_id)
    versions = policy.versions.order_by(PolicyVersion.version_num.desc()).all()
    approvals = policy.approvals.order_by(ApprovalWorkflow.order).all()
    ack_count = policy.acknowledgements.filter(
        PolicyAcknowledgement.acknowledged_at.isnot(None)
    ).count()
    total_employees = User.query.filter_by(role=UserRole.EMPLOYEE, is_active=True).count()

    # Days until expiry/review
    expiry_days = days_until(policy.expiry_date)
    review_days = days_until(policy.review_date)

    # Module 8: Workflow Automation — only surfaces once at least one template exists
    workflow_engine_active = WorkflowTemplate.query.filter_by(is_active=True).count() > 0
    active_ver = policy.versions.filter_by(is_active=True).first()
    workflow_stages = []
    if active_ver:
        workflow_stages = (WorkflowStageInstance.query
                           .filter_by(version_id=active_ver.id)
                           .order_by(WorkflowStageInstance.order).all())

    return render_template("admin/policy_detail.html",
        policy=policy, versions=versions, approvals=approvals,
        ack_count=ack_count, total_employees=total_employees,
        expiry_days=expiry_days, review_days=review_days,
        workflow_engine_active=workflow_engine_active, workflow_stages=workflow_stages,
    )


# ================================================================
# Edit Policy
# ================================================================
@admin_bp.route("/policies/<int:policy_id>/edit", methods=["GET", "POST"])
@login_required
@hr_required
def policy_edit(policy_id):
    policy = Policy.query.get_or_404(policy_id)
    categories = PolicyCategory.query.order_by(PolicyCategory.name).all()
    departments = Department.query.order_by(Department.name).all()
    hr_users = User.query.filter(User.role.in_([UserRole.HR, UserRole.ADMIN])).all()
    error = None

    if request.method == "POST":
        policy.title = request.form.get("title", policy.title).strip()
        policy.description = request.form.get("description", "").strip()
        policy.category_id = request.form.get("category_id") or None
        policy.department_id = request.form.get("department_id") or None
        policy.reviewer_id = request.form.get("reviewer_id") or None
        policy.approver_id = request.form.get("approver_id") or None
        policy.priority = request.form.get("priority", policy.priority)
        policy.confidentiality = request.form.get("confidentiality", policy.confidentiality)
        policy.is_mandatory = bool(request.form.get("is_mandatory"))

        for field in ("effective_date", "review_date", "expiry_date"):
            val = request.form.get(field)
            if val:
                try:
                    setattr(policy, field, datetime.strptime(val, "%Y-%m-%d").date())
                except ValueError:
                    pass

        pdf_text = _extract_pdf_text_from_upload(request.files.get("policy_file"))
        content = None
        if pdf_text is not None:
            content = pdf_text.strip()
            if not content:
                error = "Uploaded PDF did not contain extractable text."
        elif "content" in request.form:
            content = request.form.get("content", "").strip()

        # Tags
        policy.tags.clear()
        tag_names = [t.strip() for t in request.form.get("tags", "").split(",") if t.strip()]
        for tn in tag_names:
            tag = Tag.query.filter_by(name=tn).first() or Tag(name=tn)
            if not tag.id:
                db.session.add(tag)
            policy.tags.append(tag)

        if error:
            return render_template("admin/policy_form.html",
                policy=policy, categories=categories, departments=departments,
                hr_users=hr_users, error=error,
            )

        new_version_label = None
        old_version_label = None
        if content:
            current_ver = policy.versions.filter_by(is_active=True).first()
            old_version_label = current_ver.version_label if current_ver else None
            diff = compute_diff(current_ver.content, content) if current_ver else {}

            if current_ver:
                current_ver.is_active = False
                current_ver.status = "superseded"

            ver_num, ver_label = next_version(policy.current_version, major=False)
            new_version = PolicyVersion(
                policy_id=policy.id,
                version_num=ver_num,
                version_label=ver_label,
                content=content,
                summary=request.form.get("change_summary", "Updated policy version"),
                diff_json=json.dumps(diff),
                change_reason="Policy edited",
                created_by_id=current_user.id,
                is_active=True,
                status="draft",
                effective_date=policy.effective_date,
            )
            policy.current_version = ver_label
            policy.status = PolicyStatus.DRAFT
            db.session.add(new_version)
            new_version_label = ver_label

        db.session.commit()

        if new_version_label:
            _sync_policy_version_to_chroma(policy, new_version_label, content, old_version_label)
            audit("policy.edit", "policy", policy.id, {"title": policy.title, "version": new_version_label})
            flash(f"Policy updated and saved as version {new_version_label}.", "success")
        else:
            audit("policy.edit", "policy", policy.id, {"title": policy.title})
            flash("Policy metadata updated.", "success")

        return redirect(url_for("admin.policy_detail", policy_id=policy.id))

    return render_template("admin/policy_form.html",
        policy=policy, categories=categories, departments=departments,
        hr_users=hr_users, error=error,
    )


# ================================================================
# New Version (Phase 3 USP)
# ================================================================
@admin_bp.route("/policies/<int:policy_id>/versions/new", methods=["GET", "POST"])
@login_required
@hr_required
def version_create(policy_id):
    policy = Policy.query.get_or_404(policy_id)
    current_ver = policy.versions.filter_by(is_active=True).first()
    error = None

    if request.method == "POST":
        pdf_text = _extract_pdf_text_from_upload(request.files.get("policy_file"))
        content = pdf_text.strip() if pdf_text is not None else request.form.get("content", "").strip()
        summary = request.form.get("summary", "").strip()
        change_reason = request.form.get("change_reason", "").strip()
        bump_type = request.form.get("bump_type", "minor")  # "minor" or "major"

        if not content:
            error = "Version content is required."
        elif not summary:
            error = "Please describe what changed in this version."
        else:
            ver_num, ver_label = next_version(policy.current_version, major=(bump_type == "major"))

            # Compute diff vs previous version
            diff = {}
            if current_ver:
                diff = compute_diff(current_ver.content, content)

            # Deactivate old version
            if current_ver:
                current_ver.is_active = False
                current_ver.status = "superseded"

            new_ver = PolicyVersion(
                policy_id=policy.id,
                version_num=ver_num,
                version_label=ver_label,
                content=content,
                summary=summary,
                diff_json=json.dumps(diff),
                change_reason=change_reason,
                created_by_id=current_user.id,
                is_active=True,
                status="draft",
            )
            eff_date = request.form.get("effective_date")
            if eff_date:
                try:
                    new_ver.effective_date = datetime.strptime(eff_date, "%Y-%m-%d").date()
                except ValueError:
                    pass

            policy.current_version = ver_label
            policy.status = PolicyStatus.DRAFT  # back to draft after new version

            db.session.add(new_ver)
            db.session.commit()

            _sync_policy_version_to_chroma(policy, ver_label, content, current_ver.version_label if current_ver else None)

            audit("policy.new_version", "policy", policy.id,
                  {"version": ver_label, "changes": diff.get("stats", {})})
            flash(f"New version {ver_label} created.", "success")
            return redirect(url_for("admin.policy_detail", policy_id=policy.id))

    return render_template("admin/version_form.html",
        policy=policy, current_ver=current_ver, error=error,
    )


# ================================================================
# Version Diff (Phase 3 comparison)
# ================================================================
@admin_bp.route("/policies/<int:policy_id>/versions/compare")
@login_required
@hr_required
def version_compare(policy_id):
    policy = Policy.query.get_or_404(policy_id)
    v1_id = request.args.get("v1", type=int)
    v2_id = request.args.get("v2", type=int)

    versions = policy.versions.order_by(PolicyVersion.version_num.desc()).all()
    v1 = PolicyVersion.query.get(v1_id) if v1_id else (versions[1] if len(versions) > 1 else None)
    v2 = PolicyVersion.query.get(v2_id) if v2_id else (versions[0] if versions else None)

    diff = {}
    if v1 and v2:
        diff = compute_diff(v1.content, v2.content)

    return render_template("admin/version_compare.html",
        policy=policy, versions=versions, v1=v1, v2=v2, diff=diff,
    )


# ================================================================
# Publish / Archive / Delete
# ================================================================
@admin_bp.route("/policies/<int:policy_id>/publish", methods=["POST"])
@login_required
@hr_required
def policy_publish(policy_id):
    policy = Policy.query.get_or_404(policy_id)
    policy.status = PolicyStatus.ACTIVE

    active_ver = policy.versions.filter_by(is_active=True).first()
    if active_ver:
        active_ver.status = "approved"
        active_ver.approved_by_id = current_user.id
        active_ver.approved_at = datetime.now(timezone.utc)

    db.session.commit()
    audit("policy.publish", "policy", policy.id)

    # Notify employees
    link = url_for("employee.policy_view", policy_id=policy.id)
    if policy.is_mandatory:
        notify_all_employees(
            NotificationType.MANDATORY_READ,
            f"Mandatory: {policy.title}",
            "This policy requires your acknowledgement.",
            link,
        )
    else:
        notify_all_employees(
            NotificationType.NEW_POLICY,
            f"New policy published: {policy.title}",
            link=link,
        )

    flash(f'"{policy.title}" is now published and active.', "success")

    # Auto-index into vector store
    try:
        from rag.indexing.index_policy import index_policy_version
        active_ver2 = policy.versions.filter_by(is_active=True).first()
        if active_ver2:
            index_policy_version(policy.id, active_ver2.id)
    except Exception as e:
        flash(f"Warning: RAG indexing failed — {e}", "warning")

    return redirect(url_for("admin.policy_detail", policy_id=policy.id))


@admin_bp.route("/policies/<int:policy_id>/archive", methods=["POST"])
@login_required
@hr_required
def policy_archive(policy_id):
    policy = Policy.query.get_or_404(policy_id)
    policy.status = PolicyStatus.ARCHIVED
    db.session.commit()
    audit("policy.archive", "policy", policy.id)
    flash(f'"{policy.title}" archived.', "info")
    return redirect(url_for("admin.policy_list"))


@admin_bp.route("/policies/<int:policy_id>/restore", methods=["POST"])
@login_required
@hr_required
def policy_restore(policy_id):
    policy = Policy.query.get_or_404(policy_id)
    policy.status = PolicyStatus.DRAFT
    db.session.commit()
    audit("policy.restore", "policy", policy.id)
    flash(f'"{policy.title}" restored to draft.', "success")
    return redirect(url_for("admin.policy_detail", policy_id=policy.id))


@admin_bp.route("/policies/<int:policy_id>/delete", methods=["POST"])
@login_required
@hr_required
def policy_delete(policy_id):
    policy = Policy.query.get_or_404(policy_id)
    title = policy.title
    db.session.delete(policy)
    db.session.commit()
    audit("policy.delete", "policy", policy_id, {"title": title})
    flash(f'"{title}" permanently deleted.', "warning")
    return redirect(url_for("admin.policy_list"))


@admin_bp.route("/policies/<int:policy_id>/duplicate", methods=["POST"])
@login_required
@hr_required
def policy_duplicate(policy_id):
    orig = Policy.query.get_or_404(policy_id)
    orig_ver = orig.versions.filter_by(is_active=True).first()

    new_policy = Policy(
        policy_id=generate_policy_id(),
        title=f"Copy of {orig.title}",
        description=orig.description,
        category_id=orig.category_id,
        department_id=orig.department_id,
        author_id=current_user.id,
        priority=orig.priority,
        confidentiality=orig.confidentiality,
        status=PolicyStatus.DRAFT,
    )
    db.session.add(new_policy)
    db.session.flush()

    if orig_ver:
        new_ver = PolicyVersion(
            policy_id=new_policy.id,
            version_num=1.0,
            version_label="v1.0",
            content=orig_ver.content,
            summary=f"Duplicated from {orig.policy_id} {orig.current_version}",
            created_by_id=current_user.id,
            is_active=True,
            status="draft",
        )
        db.session.add(new_ver)

    db.session.commit()
    audit("policy.duplicate", "policy", new_policy.id, {"source": orig.policy_id})
    flash(f'Duplicated as "{new_policy.title}".', "success")
    return redirect(url_for("admin.policy_detail", policy_id=new_policy.id))


# ================================================================
# Approval Workflow
# ================================================================
@admin_bp.route("/policies/<int:policy_id>/submit-review", methods=["POST"])
@login_required
@hr_required
def submit_for_review(policy_id):
    policy = Policy.query.get_or_404(policy_id)
    active_ver = policy.versions.filter_by(is_active=True).first()
    if not active_ver:
        flash("No active version to submit.", "danger")
        return redirect(url_for("admin.policy_detail", policy_id=policy_id))

    policy.status = PolicyStatus.HR_REVIEW

    # Clear old approvals for this version
    ApprovalWorkflow.query.filter_by(version_id=active_ver.id).delete()

    stages = [
        (ApprovalStage.HR_REVIEW, 1),
        (ApprovalStage.LEGAL_REVIEW, 2),
        (ApprovalStage.MANAGEMENT, 3),
    ]
    for stage, order in stages:
        db.session.add(ApprovalWorkflow(
            policy_id=policy.id,
            version_id=active_ver.id,
            stage=stage,
            status=ApprovalStatus.PENDING if order == 1 else "waiting",
            order=order,
        ))

    db.session.commit()
    audit("policy.submit_review", "policy", policy.id)

    # Notify HR users
    for hr_user in User.query.filter_by(role=UserRole.HR).all():
        notify_user(hr_user.id, NotificationType.APPROVAL_NEEDED,
                    f'Review needed: {policy.title}',
                    link=url_for("admin.policy_detail", policy_id=policy.id))

    flash("Policy submitted for HR review.", "success")
    return redirect(url_for("admin.policy_detail", policy_id=policy.id))


@admin_bp.route("/approvals/<int:approval_id>/act", methods=["POST"])
@login_required
@hr_required
def approval_act(approval_id):
    approval = ApprovalWorkflow.query.get_or_404(approval_id)
    action = request.form.get("action")  # "approve" or "reject"
    comment = request.form.get("comment", "").strip()

    approval.actor_id = current_user.id
    approval.comment = comment
    approval.acted_at = datetime.now(timezone.utc)

    if action == "approve":
        approval.status = ApprovalStatus.APPROVED
        # Activate next stage
        next_stage = ApprovalWorkflow.query.filter_by(
            policy_id=approval.policy_id,
            order=approval.order + 1
        ).first()
        if next_stage:
            next_stage.status = ApprovalStatus.PENDING
            # Update policy status
            stage_status_map = {
                ApprovalStage.LEGAL_REVIEW: PolicyStatus.LEGAL_REVIEW,
                ApprovalStage.MANAGEMENT: PolicyStatus.PENDING_APPROVAL,
            }
            approval.policy.status = stage_status_map.get(next_stage.stage, PolicyStatus.HR_REVIEW)
        else:
            # All stages approved
            approval.policy.status = PolicyStatus.ACTIVE
            active_ver = approval.version
            if active_ver:
                active_ver.status = "approved"
                active_ver.approved_by_id = current_user.id
                active_ver.approved_at = datetime.now(timezone.utc)
            flash("All approvals done — policy is now Active.", "success")
    else:
        approval.status = ApprovalStatus.REJECTED
        approval.rejected_reason = comment
        approval.policy.status = PolicyStatus.REJECTED
        flash("Policy version rejected.", "warning")

    db.session.commit()
    audit("approval.act", "policy", approval.policy_id, {"action": action, "stage": approval.stage})
    return redirect(url_for("admin.policy_detail", policy_id=approval.policy_id))


# ================================================================
# User Management
# ================================================================
@admin_bp.route("/users")
@login_required
@role_required(UserRole.ADMIN)
def user_list():
    page = request.args.get("page", 1, type=int)
    search = request.args.get("q", "").strip()
    role_filter = request.args.get("role", "")
    q = User.query
    if search:
        q = q.filter(User.name.ilike(f"%{search}%") | User.email.ilike(f"%{search}%"))
    if role_filter:
        q = q.filter_by(role=role_filter)
    users = paginate(q.order_by(User.name), page, 20)
    departments = Department.query.order_by(Department.name).all()
    return render_template("admin/user_list.html", users=users,
                           departments=departments, roles=UserRole.ALL,
                           search=search, role_filter=role_filter)


@admin_bp.route("/users/new", methods=["GET", "POST"])
@login_required
@role_required(UserRole.ADMIN)
def user_create():
    departments = Department.query.order_by(Department.name).all()
    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        name = request.form.get("name", "").strip()
        role = request.form.get("role", UserRole.EMPLOYEE)
        password = request.form.get("password", "")
        if User.query.filter_by(email=email).first():
            error = "Email already exists."
        elif not name or not email or not password:
            error = "Name, email and password are required."
        else:
            user = User(
                name=name, email=email, role=role,
                employee_id=request.form.get("employee_id", "").strip() or None,
                department_id=request.form.get("department_id") or None,
                designation=request.form.get("designation", "").strip(),
                email_verified=True, is_active=True,
            )
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            audit("user.create", "user", user.id, {"email": email, "role": role})
            build_onboarding_checklist(user)
            flash(f"User {name} created.", "success")
            return redirect(url_for("admin.user_list"))
    return render_template("admin/user_form.html", user=None, departments=departments,
                           roles=UserRole.ALL, error=error)


@admin_bp.route("/users/<int:user_id>/toggle", methods=["POST"])
@login_required
@role_required(UserRole.ADMIN)
def user_toggle(user_id):
    user = User.query.get_or_404(user_id)
    if user.id == current_user.id:
        flash("Cannot deactivate your own account.", "danger")
    else:
        user.is_active = not user.is_active
        db.session.commit()
        audit("user.toggle", "user", user.id, {"active": user.is_active})
        flash(f"{'Activated' if user.is_active else 'Deactivated'} {user.name}.", "info")
    return redirect(url_for("admin.user_list"))


@admin_bp.route("/users/<int:user_id>/role", methods=["POST"])
@login_required
@role_required(UserRole.ADMIN)
def user_set_role(user_id):
    user = User.query.get_or_404(user_id)
    new_role = request.form.get("role")
    if new_role in UserRole.ALL:
        old_role = user.role
        user.role = new_role
        db.session.commit()
        audit("user.role_change", "user", user.id, {"from": old_role, "to": new_role})
        flash(f"{user.name}'s role changed to {new_role}.", "success")
    return redirect(url_for("admin.user_list"))


# ================================================================
# Categories & Departments
# ================================================================
@admin_bp.route("/categories", methods=["GET", "POST"])
@login_required
@role_required(UserRole.ADMIN)
def category_list():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if name and not PolicyCategory.query.filter_by(name=name).first():
            db.session.add(PolicyCategory(
                name=name,
                icon=request.form.get("icon", ""),
                color=request.form.get("color", "#2a4a38"),
            ))
            db.session.commit()
            flash(f'Category "{name}" added.', "success")
    categories = PolicyCategory.query.order_by(PolicyCategory.name).all()
    return render_template("admin/categories.html", categories=categories)


@admin_bp.route("/departments", methods=["GET", "POST"])
@login_required
@role_required(UserRole.ADMIN)
def department_list():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        code = request.form.get("code", "").strip().upper()
        if name and not Department.query.filter_by(name=name).first():
            db.session.add(Department(name=name, code=code or None))
            db.session.commit()
            flash(f'Department "{name}" added.', "success")
    departments = Department.query.order_by(Department.name).all()
    return render_template("admin/departments.html", departments=departments)


# ================================================================
# Audit Log
# ================================================================
@admin_bp.route("/audit")
@login_required
@role_required(UserRole.ADMIN)
def audit_log():
    page = request.args.get("page", 1, type=int)
    logs = paginate(AuditLog.query.order_by(AuditLog.timestamp.desc()), page, 25)
    return render_template("admin/audit_log.html", logs=logs)


# ================================================================
# API endpoints (JSON) used by admin JS
# ================================================================
@admin_bp.route("/api/stats")
@login_required
@hr_required
def api_stats():
    return jsonify({
        "total": Policy.query.count(),
        "active": Policy.query.filter_by(status=PolicyStatus.ACTIVE).count(),
        "draft": Policy.query.filter_by(status=PolicyStatus.DRAFT).count(),
        "archived": Policy.query.filter_by(status=PolicyStatus.ARCHIVED).count(),
        "users": User.query.filter_by(is_active=True).count(),
    })


@admin_bp.route("/api/documents")
@login_required
@hr_required
def api_documents():
    """
    List policies with their current active version, for the AI Dashboard's
    "Re-index policies" panel. Each entry includes the *version id* (not the
    policy id) so it can be indexed correctly.
    """
    policies = Policy.query.order_by(Policy.title).all()
    docs = []
    for p in policies:
        active_ver = p.versions.filter_by(is_active=True).first()
        docs.append({
            "id": p.id,
            "title": p.title,
            "status": p.status,
            "current_version": active_ver.version_label if active_ver else None,
            "version_id": active_ver.id if active_ver else None,
        })
    return jsonify(docs)


@admin_bp.route("/api/notifications/mark-read", methods=["POST"])
@login_required
def mark_notifications_read():
    from models import Notification
    Notification.query.filter_by(user_id=current_user.id, is_read=False).update({"is_read": True})
    db.session.commit()
    return jsonify({"ok": True})
