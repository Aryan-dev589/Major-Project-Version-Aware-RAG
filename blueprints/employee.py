"""
blueprints/employee.py
Employee-facing routes: dashboard, policy browse, view, search, acknowledge, save.
"""
import json
import uuid
from datetime import datetime, timezone
from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify, session, send_file
from flask_login import login_required, current_user
from sqlalchemy import or_
from models import (db, Policy, PolicyVersion, PolicyStatus, PolicyCategory,
                    Department, PolicyAcknowledgement, Notification, User,
                    Tag, saved_policies, PolicyLike, PolicyComment, QuizAttempt,
                    PolicyAIInsight, OnboardingChecklistItem)
from utils import audit, paginate, generate_policy_pdf
from policy_ai import reading_time_minutes

employee_bp = Blueprint("employee", __name__, url_prefix="/")


# ================================================================
# Module: Onboarding Path Generator
# ================================================================
ONBOARDING_PRIORITY_KEYWORDS = ["code of conduct", "conduct", "it security", "security"]


def build_onboarding_checklist(user):
    """
    Auto-sequences a new joiner's mandatory-reading checklist instead of
    dumping them into 'Browse Policies' with no order. Called once, right
    after account creation, from auth.register and admin.user_create.
    Safe to call more than once — a user who already has a checklist is
    left untouched.

    Sequencing (rule-based, no LLM needed):
      1. Code of Conduct / IT Security policies first (by category/title
         keyword match)
      2. The employee's own department-specific mandatory policies next
      3. All other mandatory policies last
    """
    if OnboardingChecklistItem.query.filter_by(user_id=user.id).first():
        return  # already generated for this user

    mandatory = Policy.query.filter_by(status=PolicyStatus.ACTIVE, is_mandatory=True).all()
    if not mandatory:
        return

    def sort_key(p):
        haystack = f"{p.category.name if p.category else ''} {p.title}".lower()
        keyword_rank = next(
            (i for i, kw in enumerate(ONBOARDING_PRIORITY_KEYWORDS) if kw in haystack),
            len(ONBOARDING_PRIORITY_KEYWORDS),
        )
        dept_rank = 0 if (p.department_id and p.department_id == user.department_id) else 1
        return (keyword_rank, dept_rank, p.title)

    for i, p in enumerate(sorted(mandatory, key=sort_key), start=1):
        db.session.add(OnboardingChecklistItem(user_id=user.id, policy_id=p.id, order=i))
    db.session.commit()


@employee_bp.route("/dashboard")
@login_required
def dashboard():
    # Recent active policies
    recent = Policy.query.filter_by(status=PolicyStatus.ACTIVE)\
        .order_by(Policy.updated_at.desc()).limit(6).all()
    # Trending (most viewed)
    trending = Policy.query.filter_by(status=PolicyStatus.ACTIVE)\
        .order_by(Policy.view_count.desc()).limit(6).all()
    # Pending acknowledgements
    acked_ids = [a.policy_id for a in current_user.acknowledgements.all()]
    pending_acks = Policy.query.filter(
        Policy.status == PolicyStatus.ACTIVE,
        Policy.is_mandatory == True,
        ~Policy.id.in_(acked_ids) if acked_ids else True
    ).all()
    # Saved
    saved = current_user.saved.all()
    # Notifications
    notifs = Notification.query.filter_by(user_id=current_user.id, is_read=False)\
        .order_by(Notification.created_at.desc()).limit(5).all()
    # Categories
    categories = PolicyCategory.query.all()
    unread_count = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()

    return render_template("employee/dashboard.html",
        recent=recent, trending=trending, pending_acks=pending_acks,
        saved=saved, notifs=notifs, categories=categories,
        unread_count=unread_count,
    )


@employee_bp.route("/policies")
@login_required
def policy_browse():
    page = request.args.get("page", 1, type=int)
    cat_filter = request.args.get("category", 0, type=int)
    dept_filter = request.args.get("department", 0, type=int)
    search = request.args.get("q", "").strip()
    tag_filter = request.args.get("tag", "").strip()

    q = Policy.query.filter_by(status=PolicyStatus.ACTIVE)
    if cat_filter:
        q = q.filter_by(category_id=cat_filter)
    if dept_filter:
        q = q.filter_by(department_id=dept_filter)
    if search:
        q = q.filter(Policy.title.ilike(f"%{search}%") |
                     Policy.description.ilike(f"%{search}%"))
    if tag_filter:
        tag = Tag.query.filter_by(name=tag_filter).first()
        if tag:
            q = q.filter(Policy.tags.contains(tag))

    policies = paginate(q.order_by(Policy.updated_at.desc()), page, 12)
    categories = PolicyCategory.query.order_by(PolicyCategory.name).all()
    departments = Department.query.order_by(Department.name).all()
    popular_tags = Tag.query.limit(20).all()

    return render_template("employee/policy_browse.html",
        policies=policies, categories=categories, departments=departments,
        popular_tags=popular_tags, search=search,
        cat_filter=cat_filter, dept_filter=dept_filter, tag_filter=tag_filter,
    )


@employee_bp.route("/policies/<int:policy_id>")
@login_required
def policy_view(policy_id):
    policy = Policy.query.get_or_404(policy_id)
    if policy.status != PolicyStatus.ACTIVE and not current_user.can_manage_policies():
        flash("This policy is not currently available.", "warning")
        return redirect(url_for("employee.dashboard"))

    policy.view_count += 1
    db.session.commit()

    version = policy.versions.filter_by(is_active=True).first()
    all_versions = policy.versions.order_by(PolicyVersion.version_num.desc()).all()

    # Acknowledgement status
    ack = PolicyAcknowledgement.query.filter_by(
        policy_id=policy_id, user_id=current_user.id
    ).first()

    # Related policies (same category)
    related = []
    if policy.category_id:
        related = Policy.query.filter(
            Policy.category_id == policy.category_id,
            Policy.id != policy.id,
            Policy.status == PolicyStatus.ACTIVE,
        ).limit(4).all()

    audit("policy.view", "policy", policy.id)
    unread_count = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()

    # Module 12: Employee Portal — likes, comments, quiz
    like_count = policy.likes.count()
    liked_by_me = policy.likes.filter_by(user_id=current_user.id).first() is not None
    comments = policy.comments.order_by(PolicyComment.created_at.desc()).all()
    insight = PolicyAIInsight.query.filter_by(policy_id=policy.id).first()
    my_quiz_attempt = (QuizAttempt.query.filter_by(policy_id=policy.id, user_id=current_user.id)
                       .order_by(QuizAttempt.completed_at.desc()).first())

    return render_template("employee/policy_view.html",
        policy=policy, version=version, all_versions=all_versions,
        ack=ack, related=related, unread_count=unread_count,
        like_count=like_count, liked_by_me=liked_by_me, comments=comments,
        insight=insight, my_quiz_attempt=my_quiz_attempt,
    )


@employee_bp.route("/policies/<int:policy_id>/acknowledge", methods=["POST"])
@login_required
def policy_acknowledge(policy_id):
    policy = Policy.query.get_or_404(policy_id)
    ack = PolicyAcknowledgement.query.filter_by(
        policy_id=policy_id, user_id=current_user.id
    ).first()

    active_ver = policy.versions.filter_by(is_active=True).first()
    now = datetime.now(timezone.utc)

    if not ack:
        ack = PolicyAcknowledgement(
            policy_id=policy_id,
            user_id=current_user.id,
            version_id=active_ver.id if active_ver else None,
            is_mandatory=policy.is_mandatory,
        )
        db.session.add(ack)

    ack.read_at = ack.read_at or now
    ack.acknowledged_at = now
    ack.digital_signature = f"{current_user.name} — {now.strftime('%Y-%m-%d %H:%M')} UTC"
    ack.ip_address = request.remote_addr

    # Module: Onboarding Path Generator — acknowledging a policy also checks
    # it off the guided onboarding checklist, if it's on one.
    onboarding_item = OnboardingChecklistItem.query.filter_by(
        user_id=current_user.id, policy_id=policy_id
    ).first()
    if onboarding_item and not onboarding_item.is_done:
        onboarding_item.is_done = True
        onboarding_item.completed_at = now

    db.session.commit()

    audit("policy.acknowledge", "policy", policy.id)
    flash("Policy acknowledged. Your digital signature has been recorded.", "success")
    return redirect(url_for("employee.policy_view", policy_id=policy_id))


@employee_bp.route("/policies/<int:policy_id>/save", methods=["POST"])
@login_required
def policy_save(policy_id):
    policy = Policy.query.get_or_404(policy_id)
    if policy in current_user.saved:
        current_user.saved.remove(policy)
        saved = False
    else:
        current_user.saved.append(policy)
        saved = True
    db.session.commit()
    return jsonify({"saved": saved})


@employee_bp.route("/search")
@login_required
def search():
    """
    Module 14+ — Search now does two things at once instead of just a
    keyword lookup that dumps documents on the user:

      1. Understands the question — runs it through the same RAG pipeline
         that powers the AI Assistant (embed -> retrieve -> rerank -> LLM)
         and returns a short, plain-language answer with citations
         (policy name, version, section/page) pointing at exactly where
         that answer came from.
      2. Still surfaces the underlying documents — every matching policy
         is listed below with a "View" link (full text) and a
         "Download PDF" button, so the person can always go read/keep the
         whole source document, not just take the AI's word for it.

    The AI step degrades gracefully: if nothing is indexed yet or the LLM
    call fails, we simply skip the explanation panel and fall back to the
    plain keyword results, exactly like before.
    """
    q = request.args.get("q", "").strip()
    results = []
    ai_result = None

    if q:
        # Full-phrase match, plus an OR across individual significant words —
        # a raw whole-question substring match (the old behaviour) almost
        # never hits for a natural-language question like "how many remote
        # work days are allowed", since that exact phrase won't appear
        # verbatim in a title or description.
        words = [w for w in q.split() if len(w) > 2]
        conditions = [Policy.title.ilike(f"%{q}%"), Policy.description.ilike(f"%{q}%")]
        for w in words:
            conditions.append(Policy.title.ilike(f"%{w}%"))
            conditions.append(Policy.description.ilike(f"%{w}%"))
        results = Policy.query.filter(
            Policy.status == PolicyStatus.ACTIVE, or_(*conditions)
        ).order_by(Policy.view_count.desc()).limit(20).all()

        try:
            from rag.chatbot.chat_service import answer as rag_answer
            sess_id = session.get("rag_search_session")
            if not sess_id:
                sess_id = str(uuid.uuid4())
                session["rag_search_session"] = sess_id
            dept = current_user.department.name if current_user.department else ""
            raw = rag_answer(
                query=q,
                session_id=sess_id,
                user_role=current_user.role,
                user_department=dept,
                top_k_rerank=4,
            )
            if not raw.get("fallback") and raw.get("answer"):
                ai_result = raw
        except Exception:
            # No index built yet / LLM unavailable / vector store not configured —
            # fail silently and show the plain document list only.
            ai_result = None

        if not results and ai_result and ai_result.get("citations"):
            # Keyword search still came up empty even though the AI found
            # (and cited) an answer — showing "No results found" right
            # under a successful answer reads as a broken/contradictory
            # page, so fall back to the policies the answer actually cites.
            cited_ids, seen = [], set()
            for c in ai_result["citations"]:
                try:
                    pid = int(c.get("policy_id"))
                except (TypeError, ValueError):
                    continue
                if pid not in seen:
                    seen.add(pid)
                    cited_ids.append(pid)
            if cited_ids:
                cited_policies = {p.id: p for p in Policy.query.filter(Policy.id.in_(cited_ids)).all()}
                results = [cited_policies[pid] for pid in cited_ids if pid in cited_policies]

        audit("search", detail={"query": q, "results": len(results)})

    unread_count = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
    return render_template("employee/search.html",
        query=q, results=results, ai_result=ai_result, unread_count=unread_count)


@employee_bp.route("/policies/<int:policy_id>/download")
@login_required
def policy_download_pdf(policy_id):
    """Generate and stream a formatted PDF of the policy's active version
    on demand — the 'whole document' half of the search result, wired to
    Policy.download_count which already existed in the schema but had
    nothing populating it."""
    policy = Policy.query.get_or_404(policy_id)
    version = policy.active_version or policy.latest_version
    if not version:
        flash("This policy has no content yet.", "warning")
        return redirect(url_for("employee.policy_view", policy_id=policy_id))
    if not current_user.can_manage_policies() and policy.status != PolicyStatus.ACTIVE:
        from flask import abort
        abort(403)

    policy.download_count = (policy.download_count or 0) + 1
    db.session.commit()
    audit("policy.download_pdf", "policy", policy.id, {"version": version.version_label})

    buf = generate_policy_pdf(policy, version)
    filename = f"{policy.policy_id}_{version.version_label}.pdf".replace(" ", "_")
    return send_file(buf, mimetype="application/pdf", as_attachment=True, download_name=filename)


@employee_bp.route("/notifications")
@login_required
def notifications():
    notifs = Notification.query.filter_by(user_id=current_user.id)\
        .order_by(Notification.created_at.desc()).limit(50).all()
    for n in notifs:
        n.is_read = True
    db.session.commit()
    unread_count = 0
    return render_template("employee/notifications.html",
        notifs=notifs, unread_count=unread_count)


@employee_bp.route("/saved")
@login_required
def saved_list():
    saved = current_user.saved.all()
    unread_count = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
    return render_template("employee/saved.html", saved=saved, unread_count=unread_count)


# ================================================================
# Module 12: Employee Portal — likes, comments, quizzes, certificates
# ================================================================
@employee_bp.route("/policies/<int:policy_id>/like", methods=["POST"])
@login_required
def policy_like(policy_id):
    policy = Policy.query.get_or_404(policy_id)
    existing = PolicyLike.query.filter_by(policy_id=policy_id, user_id=current_user.id).first()
    if existing:
        db.session.delete(existing)
        liked = False
    else:
        db.session.add(PolicyLike(policy_id=policy_id, user_id=current_user.id))
        liked = True
    db.session.commit()
    return jsonify({"liked": liked, "count": policy.likes.count()})


@employee_bp.route("/policies/<int:policy_id>/comment", methods=["POST"])
@login_required
def add_comment(policy_id):
    policy = Policy.query.get_or_404(policy_id)
    content = request.form.get("content", "").strip()
    if content:
        db.session.add(PolicyComment(policy_id=policy_id, user_id=current_user.id, content=content))
        db.session.commit()
        audit("policy.comment", "policy", policy.id)
    return redirect(url_for("employee.policy_view", policy_id=policy_id) + "#comments")


@employee_bp.route("/comments/<int:comment_id>/delete", methods=["POST"])
@login_required
def delete_comment(comment_id):
    comment = PolicyComment.query.get_or_404(comment_id)
    if comment.user_id != current_user.id and not current_user.can_manage_policies():
        flash("You can only delete your own comments.", "warning")
        return redirect(url_for("employee.policy_view", policy_id=comment.policy_id))
    policy_id = comment.policy_id
    db.session.delete(comment)
    db.session.commit()
    return redirect(url_for("employee.policy_view", policy_id=policy_id) + "#comments")


@employee_bp.route("/policies/<int:policy_id>/quiz")
@login_required
def take_quiz(policy_id):
    policy = Policy.query.get_or_404(policy_id)
    insight = PolicyAIInsight.query.filter_by(policy_id=policy_id).first()
    if not insight or not insight.quiz:
        flash("No quiz is available for this policy yet — ask HR to generate AI Insights for it first.", "warning")
        return redirect(url_for("employee.policy_view", policy_id=policy_id))
    unread_count = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
    return render_template("employee/quiz.html", policy=policy, quiz=insight.quiz, unread_count=unread_count)


@employee_bp.route("/policies/<int:policy_id>/quiz/submit", methods=["POST"])
@login_required
def submit_quiz(policy_id):
    policy = Policy.query.get_or_404(policy_id)
    insight = PolicyAIInsight.query.filter_by(policy_id=policy_id).first()
    if not insight or not insight.quiz:
        flash("This quiz is no longer available.", "warning")
        return redirect(url_for("employee.policy_view", policy_id=policy_id))

    quiz = insight.quiz
    answers = []
    score = 0
    for i, q in enumerate(quiz):
        selected = request.form.get(f"q{i}", type=int)
        answers.append(selected)
        if selected is not None and selected == q.get("correct_index"):
            score += 1

    total = len(quiz)
    passed = total > 0 and (score / total) >= 0.7

    attempt = QuizAttempt(policy_id=policy_id, user_id=current_user.id, score=score,
                          total=total, passed=passed)
    attempt.answers_json = json.dumps(answers)
    db.session.add(attempt)
    db.session.commit()
    audit("policy.quiz_complete", "policy", policy.id, {"score": score, "total": total, "passed": passed})

    unread_count = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
    return render_template("employee/quiz_result.html",
        policy=policy, quiz=quiz, answers=answers, attempt=attempt, unread_count=unread_count)


@employee_bp.route("/policies/<int:policy_id>/certificate")
@login_required
def certificate(policy_id):
    policy = Policy.query.get_or_404(policy_id)
    ack = PolicyAcknowledgement.query.filter_by(policy_id=policy_id, user_id=current_user.id).first()
    if not ack or not ack.acknowledged_at:
        flash("Acknowledge this policy first to get your completion certificate.", "warning")
        return redirect(url_for("employee.policy_view", policy_id=policy_id))

    best_quiz = (QuizAttempt.query.filter_by(policy_id=policy_id, user_id=current_user.id)
                .order_by(QuizAttempt.score.desc()).first())

    return render_template("employee/certificate.html", policy=policy, ack=ack, quiz=best_quiz)


# ================================================================
# Module: Onboarding Path Generator
# ================================================================
@employee_bp.route("/onboarding")
@login_required
def onboarding():
    items = (OnboardingChecklistItem.query.filter_by(user_id=current_user.id)
            .order_by(OnboardingChecklistItem.order).all())

    total_minutes = 0
    for item in items:
        insight = PolicyAIInsight.query.filter_by(policy_id=item.policy_id).first()
        if insight and insight.reading_time_minutes:
            total_minutes += insight.reading_time_minutes
        else:
            version = item.policy.active_version or item.policy.latest_version
            total_minutes += reading_time_minutes(version.content) if version else 0

    done_count = sum(1 for i in items if i.is_done)
    unread_count = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()
    return render_template("employee/onboarding.html",
        items=items, total_minutes=total_minutes, done_count=done_count,
        unread_count=unread_count)


@employee_bp.route("/onboarding/<int:policy_id>/complete", methods=["POST"])
@login_required
def onboarding_complete(policy_id):
    item = OnboardingChecklistItem.query.filter_by(
        user_id=current_user.id, policy_id=policy_id
    ).first_or_404()
    if not item.is_done:
        item.is_done = True
        item.completed_at = datetime.now(timezone.utc)
        db.session.commit()
    return redirect(url_for("employee.onboarding"))


# ================================================================
# Module: Policy Time Machine
# ================================================================
@employee_bp.route("/policies/<int:policy_id>/as-of")
@login_required
def policy_as_of(policy_id):
    """
    'What did this policy say on the date this incident happened?' —
    search/chat only ever look at the active version today; this walks
    PolicyVersion history to find whichever version was in force on a
    given date, for HR/Legal disciplinary or defense purposes. No LLM
    needed — it's a plain date filter over existing version history.
    """
    policy = Policy.query.get_or_404(policy_id)
    if policy.status != PolicyStatus.ACTIVE and not current_user.can_manage_policies():
        flash("This policy is not currently available.", "warning")
        return redirect(url_for("employee.dashboard"))

    date_str = request.args.get("date", "").strip()
    if not date_str:
        flash("Choose a date to see the policy as it stood then.", "warning")
        return redirect(url_for("employee.policy_view", policy_id=policy_id))

    try:
        as_of_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError:
        flash("Enter a valid date (YYYY-MM-DD).", "warning")
        return redirect(url_for("employee.policy_view", policy_id=policy_id))

    all_versions = policy.versions.order_by(PolicyVersion.version_num.desc()).all()

    def version_date(v):
        return v.effective_date or (v.created_at.date() if v.created_at else None)

    eligible = [v for v in all_versions if version_date(v) and version_date(v) <= as_of_date]
    version = max(eligible, key=lambda v: v.version_num) if eligible else None

    audit("policy.view_as_of", "policy", policy.id, {"as_of": date_str})
    unread_count = Notification.query.filter_by(user_id=current_user.id, is_read=False).count()

    return render_template("employee/policy_view.html",
        policy=policy, version=version, all_versions=all_versions,
        ack=None, related=[], unread_count=unread_count,
        like_count=policy.likes.count(), liked_by_me=False,
        comments=[], insight=None, my_quiz_attempt=None,
        as_of_date=date_str, no_version_found=(version is None),
    )
