"""
blueprints/policy_ai.py
Module 3: AI Policy Assistant

Routes:
  POST /admin/policies/generate-draft            AI Writer: generate a brand-new policy draft (used from create form)
  GET  /admin/policies/<id>/ai                    AI Assistant page (Writer / Review / Insights tabs)
  POST /admin/policies/<id>/ai/writer             AJAX: rewrite/simplify/grammar/tone/translate/explain
  POST /admin/policies/<id>/ai/apply              Apply AI writer output as a new policy version
  POST /admin/policies/<id>/ai/review             Run AI Review (missing sections, compliance, risk, duplicates, conflicts)
  POST /admin/policies/<id>/ai/insights           Run AI Insights (summary, FAQ, quiz, key points, impact)
"""
import json
from datetime import datetime, timezone

from flask import Blueprint, render_template, redirect, url_for, flash, request, jsonify, abort
from flask_login import login_required, current_user

from models import (db, Policy, PolicyVersion, PolicyStatus, PolicyAIReview,
                    PolicyAIInsight, PolicyCategory)
from utils import audit, next_version, compute_diff

# NOTE: If you ever get a "circular import" error here, rename your root 
# policy_ai.py file to ai_services.py and update this import line!
from policy_ai import (generate_policy_draft, rewrite_text, explain_clause, review_policy,
                       find_duplicates, find_conflicts, generate_insights, summarize_changes,
                       reading_time_minutes)

# --- NEW: RAG & Vector Store imports ---
from rag.vectordb.chroma import get_store
from rag.embeddings.embedder import get_embedder

policy_ai_bp = Blueprint("policy_ai", __name__, url_prefix="/admin/policies")


def _hr_or_admin():
    if not current_user.can_manage_policies():
        abort(403)


# ================================================================
# Writer: generate a brand-new policy draft
# ================================================================
@policy_ai_bp.route("/generate-draft", methods=["POST"])
@login_required
def generate_draft():
    _hr_or_admin()
    title = request.form.get("title", "").strip()
    description = request.form.get("description", "").strip()
    category_name = request.form.get("category_name", "").strip()
    key_points = request.form.get("key_points", "").strip()

    if not title:
        return jsonify({"success": False, "error": "Enter a policy title first."}), 400

    content = generate_policy_draft(title, description, category_name, key_points)
    audit("policy_ai.generate_draft", "policy", None, {"title": title})
    return jsonify({"success": True, "content": content})


# ================================================================
# AI Assistant page
# ================================================================
@policy_ai_bp.route("/<int:policy_id>/ai")
@login_required
def ai_assistant(policy_id):
    _hr_or_admin()
    policy = Policy.query.get_or_404(policy_id)
    current_ver = policy.versions.filter_by(is_active=True).first()
    review = PolicyAIReview.query.filter_by(policy_id=policy.id).first()
    insight = PolicyAIInsight.query.filter_by(policy_id=policy.id).first()

    return render_template("admin/policy_ai.html",
        policy=policy, current_ver=current_ver, review=review, insight=insight,
    )


# ================================================================
# AI Writer actions (AJAX)
# ================================================================
@policy_ai_bp.route("/<int:policy_id>/ai/writer", methods=["POST"])
@login_required
def writer_action(policy_id):
    _hr_or_admin()
    policy = Policy.query.get_or_404(policy_id)
    mode = request.form.get("mode", "rewrite")
    content = request.form.get("content", "").strip()
    target_language = request.form.get("target_language", "").strip()
    clause = request.form.get("clause", "").strip()

    if not content:
        return jsonify({"success": False, "error": "No content to work with."}), 400

    if mode == "explain":
        if not clause:
            return jsonify({"success": False, "error": "Select or paste a clause to explain."}), 400
        result = explain_clause(content, clause)
    else:
        result = rewrite_text(content, mode, target_language)

    audit("policy_ai.writer", "policy", policy.id, {"mode": mode})
    return jsonify({"success": True, "result": result})


@policy_ai_bp.route("/<int:policy_id>/ai/apply", methods=["POST"])
@login_required
def apply_writer_output(policy_id):
    _hr_or_admin()
    policy = Policy.query.get_or_404(policy_id)
    content = request.form.get("content", "").strip()
    change_summary = request.form.get("change_summary", "AI-assisted edit").strip()
    bump_type = request.form.get("bump_type", "minor")

    if not content:
        flash("No content to save.", "warning")
        return redirect(url_for("policy_ai.ai_assistant", policy_id=policy.id))

    current_ver = policy.versions.filter_by(is_active=True).first()
    ver_num, ver_label = next_version(policy.current_version, major=(bump_type == "major"))
    diff = compute_diff(current_ver.content, content) if current_ver else {}

    # --- NEW: Capture old version label before deactivating it in SQL ---
    old_version_label = current_ver.version_label if current_ver else None

    if current_ver:
        current_ver.is_active = False
        current_ver.status = "superseded"

    new_ver = PolicyVersion(
        policy_id=policy.id, version_num=ver_num, version_label=ver_label,
        content=content, summary=change_summary or "AI-assisted edit",
        diff_json=json.dumps(diff), change_reason="Generated/edited via AI Policy Assistant",
        created_by_id=current_user.id, is_active=True, status="draft",
    )
    policy.current_version = ver_label
    policy.status = PolicyStatus.DRAFT
    db.session.add(new_ver)
    db.session.commit()

    # --- NEW: Sync Changes with Vector DB ---
    try:
        store = get_store()
        
        # 1. Flip active status of old version chunks in ChromaDB
        if old_version_label:
            store.deactivate_policy_version(policy.id, old_version_label)

        # 2. Embed & Index the new AI-generated version into ChromaDB
        embedder = get_embedder()
        
        # Simple paragraph/section chunking for the new content
        paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
        chunks = []
        for idx, paragraph in enumerate(paragraphs):
            chunks.append({
                "text": paragraph,
                "policy_id": policy.id,
                "policy_name": policy.title,
                "version": ver_label,
                "department": policy.department.name if getattr(policy, "department", None) else "",
                "section": f"Section {idx + 1}",
                "page": "1",
                "chunk_index": idx,
                "is_active": True,
            })

        if chunks:
            embeddings = [embedder.embed_query(c["text"]) for c in chunks]
            store.upsert_chunks(chunks, embeddings)

    except Exception as e:
        print(f"[Warning] ChromaDB vector sync failed during AI policy application: {e}")

    audit("policy_ai.apply", "policy", policy.id, {"version": ver_label})
    flash(f"AI-generated content saved as version {ver_label}.", "success")
    return redirect(url_for("admin.policy_detail", policy_id=policy.id))


# ================================================================
# AI Review
# ================================================================
@policy_ai_bp.route("/<int:policy_id>/ai/review", methods=["POST"])
@login_required
def run_review(policy_id):
    _hr_or_admin()
    policy = Policy.query.get_or_404(policy_id)
    current_ver = policy.versions.filter_by(is_active=True).first()
    if not current_ver:
        return jsonify({"success": False, "error": "This policy has no content yet."}), 400

    result = review_policy(current_ver.content)

    # Duplicate detection against other active policies (heuristic, cheap)
    other_policies = Policy.query.filter(Policy.id != policy.id, Policy.status == PolicyStatus.ACTIVE).all()
    candidates = []
    for p in other_policies:
        v = p.versions.filter_by(is_active=True).first()
        if v:
            candidates.append((p.id, p.title, v.content))
    duplicates = find_duplicates(current_ver.content, candidates)

    # Conflict detection only against the duplicate shortlist (bounded LLM cost)
    conflict_candidates = [(d["policy_id"], d["title"],
                            next((c for pid, t, c in candidates if pid == d["policy_id"]), ""))
                           for d in duplicates]
    conflicts = find_conflicts(policy.title, current_ver.content, conflict_candidates) if conflict_candidates else []

    review = PolicyAIReview.query.filter_by(policy_id=policy.id).first() or PolicyAIReview(policy_id=policy.id)
    review.missing_sections = result["missing_sections"]
    review.compliance_issues = result["compliance_issues"]
    review.legal_issues = result["legal_language_issues"]
    review.suggestions = result["suggestions"]
    review.duplicates = duplicates
    review.conflicts = conflicts
    review.risk_score = result["risk_score"]
    review.generated_at = datetime.now(timezone.utc)
    review.generated_by_id = current_user.id
    if not review.id:
        db.session.add(review)
    db.session.commit()

    audit("policy_ai.review", "policy", policy.id, {"risk_score": result["risk_score"]})
    return jsonify({
        "success": True, **result, "duplicates": duplicates, "conflicts": conflicts,
    })


# ================================================================
# AI Insights
# ================================================================
@policy_ai_bp.route("/<int:policy_id>/ai/insights", methods=["POST"])
@login_required
def run_insights(policy_id):
    _hr_or_admin()
    policy = Policy.query.get_or_404(policy_id)
    current_ver = policy.versions.filter_by(is_active=True).first()
    if not current_ver:
        return jsonify({"success": False, "error": "This policy has no content yet."}), 400

    result = generate_insights(current_ver.content, policy.title)

    # "Important changes" vs the previous version, if any
    prev_ver = (policy.versions.filter(PolicyVersion.version_num < current_ver.version_num)
               .order_by(PolicyVersion.version_num.desc()).first())
    important_changes = summarize_changes(prev_ver.content if prev_ver else "", current_ver.content)

    insight = PolicyAIInsight.query.filter_by(policy_id=policy.id).first() or PolicyAIInsight(policy_id=policy.id)
    insight.summary = result["summary"]
    insight.executive_summary = result["executive_summary"]
    insight.key_points = result["key_points"]
    insight.faq = result["faq"]
    insight.quiz = result["quiz"]
    insight.impact_analysis = result["impact_analysis"]
    insight.reading_time_minutes = result["reading_time_minutes"]
    insight.generated_at = datetime.now(timezone.utc)
    if not insight.id:
        db.session.add(insight)
    db.session.commit()

    audit("policy_ai.insights", "policy", policy.id)
    return jsonify({"success": True, **result, "important_changes": important_changes})