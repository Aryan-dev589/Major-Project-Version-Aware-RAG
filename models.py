"""
models.py
Complete database schema for Phases 1-3.

Tables:
  User                 - all roles (employee, hr, manager, admin)
  Department           - org departments
  PolicyCategory       - policy categories (Leave, Attendance, etc.)
  Policy               - master policy record
  PolicyVersion        - every version of every policy, with full content + diff
  ApprovalWorkflow     - approval chain per policy version
  PolicyAcknowledgement - read receipts + digital sign-off per employee
  AuditLog             - immutable log of every action (Phase 20)
  Notification         - in-app notifications per user
  PolicyTag            - many-to-many tags on policies
  SavedPolicy          - bookmarks per user
"""
import json
from datetime import datetime, timezone
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from flask_bcrypt import Bcrypt

db = SQLAlchemy()
bcrypt = Bcrypt()


# ---------- Helpers ----------
def now_utc():
    return datetime.now(timezone.utc)


# ---------- Association tables ----------
policy_tags = db.Table(
    "policy_tags",
    db.Column("policy_id", db.Integer, db.ForeignKey("policy.id"), primary_key=True),
    db.Column("tag_id", db.Integer, db.ForeignKey("tag.id"), primary_key=True),
)

saved_policies = db.Table(
    "saved_policies",
    db.Column("user_id", db.Integer, db.ForeignKey("user.id"), primary_key=True),
    db.Column("policy_id", db.Integer, db.ForeignKey("policy.id"), primary_key=True),
    db.Column("saved_at", db.DateTime, default=now_utc),
)


# ---------- Department ----------
class Department(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    code = db.Column(db.String(20), unique=True)
    created_at = db.Column(db.DateTime, default=now_utc)

    users = db.relationship("User", backref="department", lazy="dynamic")
    policies = db.relationship("Policy", backref="department", lazy="dynamic")

    def __repr__(self):
        return f"<Department {self.name}>"


# ---------- User ----------
class UserRole:
    EMPLOYEE = "employee"
    HR = "hr"
    MANAGER = "manager"
    ADMIN = "admin"
    ALL = [EMPLOYEE, HR, MANAGER, ADMIN]


class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(db.String(20), unique=True, nullable=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(200), nullable=False, unique=True, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(20), nullable=False, default=UserRole.EMPLOYEE)
    department_id = db.Column(db.Integer, db.ForeignKey("department.id"), nullable=True)
    designation = db.Column(db.String(100))
    phone = db.Column(db.String(20))
    avatar_url = db.Column(db.String(300))

    # Auth state
    is_active = db.Column(db.Boolean, default=True)
    email_verified = db.Column(db.Boolean, default=False)
    mfa_enabled = db.Column(db.Boolean, default=False)
    mfa_secret = db.Column(db.String(64))
    last_login = db.Column(db.DateTime)
    password_changed_at = db.Column(db.DateTime, default=now_utc)

    created_at = db.Column(db.DateTime, default=now_utc)
    updated_at = db.Column(db.DateTime, default=now_utc, onupdate=now_utc)

    # Relationships
    notifications = db.relationship("Notification", backref="user", lazy="dynamic", cascade="all, delete-orphan")
    acknowledgements = db.relationship("PolicyAcknowledgement", backref="user", lazy="dynamic")
    audit_logs = db.relationship("AuditLog", backref="user", lazy="dynamic")
    saved = db.relationship("Policy", secondary=saved_policies, lazy="dynamic")

    def set_password(self, password: str):
        self.password_hash = bcrypt.generate_password_hash(password).decode("utf-8")
        self.password_changed_at = now_utc()

    def check_password(self, password: str) -> bool:
        return bcrypt.check_password_hash(self.password_hash, password)

    def has_role(self, *roles) -> bool:
        return self.role in roles

    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN

    def is_hr(self) -> bool:
        return self.role in (UserRole.HR, UserRole.ADMIN)

    def can_manage_policies(self) -> bool:
        return self.role in (UserRole.HR, UserRole.ADMIN, UserRole.MANAGER)

    def __repr__(self):
        return f"<User {self.email} [{self.role}]>"


# ---------- Tag ----------
class Tag(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), nullable=False, unique=True)

    def __repr__(self):
        return f"<Tag {self.name}>"


# ---------- PolicyCategory ----------
class PolicyCategory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    description = db.Column(db.Text)
    icon = db.Column(db.String(10), default="")
    color = db.Column(db.String(20), default="#2a4a38")

    policies = db.relationship("Policy", backref="category", lazy="dynamic")

    def __repr__(self):
        return f"<Category {self.name}>"


# ---------- Policy ----------
class PolicyStatus:
    DRAFT = "draft"
    HR_REVIEW = "hr_review"
    LEGAL_REVIEW = "legal_review"
    PENDING_APPROVAL = "pending_approval"
    ACTIVE = "active"
    ARCHIVED = "archived"
    REJECTED = "rejected"
    ALL = [DRAFT, HR_REVIEW, LEGAL_REVIEW, PENDING_APPROVAL, ACTIVE, ARCHIVED, REJECTED]
    PUBLISHED = [ACTIVE]
    IN_REVIEW = [HR_REVIEW, LEGAL_REVIEW, PENDING_APPROVAL]


class ConfidentialityLevel:
    PUBLIC = "public"
    INTERNAL = "internal"
    CONFIDENTIAL = "confidential"
    RESTRICTED = "restricted"


class Priority:
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Policy(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    policy_id = db.Column(db.String(30), unique=True, nullable=False)  # e.g. POL-2024-001
    title = db.Column(db.String(300), nullable=False, index=True)
    description = db.Column(db.Text)
    category_id = db.Column(db.Integer, db.ForeignKey("policy_category.id"), nullable=True)
    department_id = db.Column(db.Integer, db.ForeignKey("department.id"), nullable=True)

    # People
    author_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    reviewer_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    approver_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)

    # Dates
    created_at = db.Column(db.DateTime, default=now_utc)
    updated_at = db.Column(db.DateTime, default=now_utc, onupdate=now_utc)
    effective_date = db.Column(db.Date, nullable=True)
    review_date = db.Column(db.Date, nullable=True)
    expiry_date = db.Column(db.Date, nullable=True)

    # State
    status = db.Column(db.String(30), default=PolicyStatus.DRAFT, index=True)
    current_version = db.Column(db.String(10), default="1.0")
    priority = db.Column(db.String(20), default=Priority.MEDIUM)
    confidentiality = db.Column(db.String(20), default=ConfidentialityLevel.INTERNAL)
    is_mandatory = db.Column(db.Boolean, default=False)
    view_count = db.Column(db.Integer, default=0)
    download_count = db.Column(db.Integer, default=0)

    # Relationships
    tags = db.relationship("Tag", secondary=policy_tags, lazy="subquery", backref=db.backref("policies", lazy=True))
    versions = db.relationship("PolicyVersion", backref="policy", lazy="dynamic", cascade="all, delete-orphan", order_by="PolicyVersion.version_num.desc()")
    approvals = db.relationship("ApprovalWorkflow", backref="policy", lazy="dynamic", cascade="all, delete-orphan")
    acknowledgements = db.relationship("PolicyAcknowledgement", backref="policy", lazy="dynamic", cascade="all, delete-orphan")
    author = db.relationship("User", foreign_keys=[author_id])
    reviewer = db.relationship("User", foreign_keys=[reviewer_id])
    approver = db.relationship("User", foreign_keys=[approver_id])

    @property
    def latest_version(self):
        return self.versions.order_by(PolicyVersion.version_num.desc()).first()

    @property
    def active_version(self):
        return self.versions.filter_by(is_active=True).first()

    @property
    def tag_names(self):
        return [t.name for t in self.tags]

    def increment_views(self):
        self.view_count += 1
        db.session.commit()

    def __repr__(self):
        return f"<Policy {self.policy_id}: {self.title}>"


# ---------- PolicyVersion (the USP — Phase 3) ----------
class PolicyVersion(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    policy_id = db.Column(db.Integer, db.ForeignKey("policy.id"), nullable=False, index=True)
    version_num = db.Column(db.Float, nullable=False)        # 1.0, 1.1, 2.0
    version_label = db.Column(db.String(10), nullable=False) # "v1.0", "v1.1"
    content = db.Column(db.Text, nullable=False)             # full policy text
    summary = db.Column(db.Text)                             # what changed (human written)
    diff_json = db.Column(db.Text)                           # structured diff vs previous version
    change_reason = db.Column(db.Text)

    # People trail
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    approved_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)

    # Dates
    created_at = db.Column(db.DateTime, default=now_utc)
    approved_at = db.Column(db.DateTime)
    effective_date = db.Column(db.Date)

    # State
    is_active = db.Column(db.Boolean, default=False)   # only one version active at a time
    status = db.Column(db.String(30), default="draft")  # draft / approved / superseded

    # Relationships
    created_by = db.relationship("User", foreign_keys=[created_by_id])
    approved_by = db.relationship("User", foreign_keys=[approved_by_id])

    def __repr__(self):
        return f"<PolicyVersion {self.version_label} of policy {self.policy_id}>"


# ---------- ApprovalWorkflow (Phase 12) ----------
class ApprovalStage:
    HR_REVIEW = "hr_review"
    LEGAL_REVIEW = "legal_review"
    MANAGEMENT = "management"
    ALL = [HR_REVIEW, LEGAL_REVIEW, MANAGEMENT]


class ApprovalStatus:
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SKIPPED = "skipped"


class ApprovalWorkflow(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    policy_id = db.Column(db.Integer, db.ForeignKey("policy.id"), nullable=False)
    version_id = db.Column(db.Integer, db.ForeignKey("policy_version.id"), nullable=False)
    stage = db.Column(db.String(30), nullable=False)
    status = db.Column(db.String(20), default=ApprovalStatus.PENDING)
    actor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    comment = db.Column(db.Text)
    rejected_reason = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=now_utc)
    acted_at = db.Column(db.DateTime)
    order = db.Column(db.Integer, default=1)

    actor = db.relationship("User")
    version = db.relationship("PolicyVersion")

    def __repr__(self):
        return f"<Approval {self.stage} [{self.status}]>"


# ---------- PolicyAcknowledgement (Phase 13) ----------
class PolicyAcknowledgement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    policy_id = db.Column(db.Integer, db.ForeignKey("policy.id"), nullable=False)
    version_id = db.Column(db.Integer, db.ForeignKey("policy_version.id"), nullable=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    read_at = db.Column(db.DateTime)
    acknowledged_at = db.Column(db.DateTime)
    digital_signature = db.Column(db.String(300))  # "Full Name — YYYY-MM-DD HH:MM"
    ip_address = db.Column(db.String(45))
    is_mandatory = db.Column(db.Boolean, default=False)

    version = db.relationship("PolicyVersion")

    __table_args__ = (
        db.UniqueConstraint("policy_id", "user_id", name="uq_ack_policy_user"),
    )


# ---------- Notification (Phase 11) ----------
class NotificationType:
    NEW_POLICY = "new_policy"
    POLICY_UPDATED = "policy_updated"
    REVIEW_DUE = "review_due"
    POLICY_EXPIRED = "policy_expired"
    MANDATORY_READ = "mandatory_read"
    APPROVAL_NEEDED = "approval_needed"
    APPROVAL_DONE = "approval_done"
    MEETING_INVITE = "meeting_invite"
    ACTION_ITEM_ASSIGNED = "action_item_assigned"


class Notification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    type = db.Column(db.String(40), nullable=False)
    title = db.Column(db.String(200), nullable=False)
    message = db.Column(db.Text)
    link = db.Column(db.String(300))
    is_read = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=now_utc)

    def __repr__(self):
        return f"<Notification {self.type} for user {self.user_id}>"


# ---------- AuditLog (Phase 20) ----------
class PolicyChunk(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    policy_id = db.Column(db.Integer, db.ForeignKey("policy.id"), nullable=False, index=True)
    version_id = db.Column(db.Integer, db.ForeignKey("policy_version.id"), nullable=True)
    section = db.Column(db.String(200))
    page = db.Column(db.Integer, nullable=True)
    chunk_index = db.Column(db.Integer, default=0)
    text_preview = db.Column(db.Text)
    char_count = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=now_utc)


class ChatSession(db.Model):
    id = db.Column(db.String(36), primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=now_utc)
    messages = db.relationship("ChatMessage", backref="session", lazy="dynamic", cascade="all, delete-orphan")


class ChatMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(36), db.ForeignKey("chat_session.id"), nullable=False, index=True)
    role = db.Column(db.String(10), nullable=False)  # "user" | "assistant"
    content = db.Column(db.Text, nullable=False)
    citations_json = db.Column(db.Text)
    chunks_used = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=now_utc)


class SearchHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    query_text = db.Column(db.String(500), nullable=False)
    answered = db.Column(db.Boolean, default=True)
    chunks_found = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=now_utc)


# ================================================================
# Module 14: Search Engine — Saved Searches
# ================================================================
class SavedSearch(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    name = db.Column(db.String(150), nullable=False)
    scope = db.Column(db.String(20), default="policies")  # "policies" | "meetings" | "all"
    filters_json = db.Column(db.Text, nullable=False)  # querystring-style filters, JSON-encoded
    created_at = db.Column(db.DateTime, default=now_utc)
    last_used_at = db.Column(db.DateTime, nullable=True)
    use_count = db.Column(db.Integer, default=0)

    user = db.relationship("User")

    @property
    def filters(self):
        try:
            return json.loads(self.filters_json) if self.filters_json else {}
        except (ValueError, TypeError):
            return {}

    @filters.setter
    def filters(self, value):
        self.filters_json = json.dumps(value or {})

    def __repr__(self):
        return f"<SavedSearch {self.name!r} for user {self.user_id}>"


class Feedback(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    message_id = db.Column(db.Integer, db.ForeignKey("chat_message.id"), nullable=True)
    vote = db.Column(db.String(4), nullable=False)  # "up" | "down"
    comment = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=now_utc)


class IndexingJob(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    policy_id = db.Column(db.Integer, db.ForeignKey("policy.id"), nullable=True)
    version_id = db.Column(db.Integer, db.ForeignKey("policy_version.id"), nullable=True)
    chunks_indexed = db.Column(db.Integer, default=0)
    status = db.Column(db.String(20), default="pending")  # pending/success/failed
    error_message = db.Column(db.Text)
    completed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=now_utc)


class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    action = db.Column(db.String(100), nullable=False)  # e.g. "policy.create", "user.login"
    resource_type = db.Column(db.String(50))             # "policy", "user", "version"
    resource_id = db.Column(db.Integer)
    detail = db.Column(db.Text)                          # JSON string of extra context
    ip_address = db.Column(db.String(45))
    user_agent = db.Column(db.String(300))
    timestamp = db.Column(db.DateTime, default=now_utc, index=True)

    def __repr__(self):
        return f"<AuditLog {self.action} at {self.timestamp}>"


# ================================================================
# Module 5: Meeting Management (MOM)
# ================================================================
meeting_policies = db.Table(
    "meeting_policies",
    db.Column("meeting_id", db.Integer, db.ForeignKey("meeting.id"), primary_key=True),
    db.Column("policy_id", db.Integer, db.ForeignKey("policy.id"), primary_key=True),
)


class MeetingType:
    STANDUP = "standup"
    REVIEW = "review"
    PLANNING = "planning"
    BOARD = "board"
    CLIENT = "client"
    HR = "hr"
    OTHER = "other"
    ALL = [STANDUP, REVIEW, PLANNING, BOARD, CLIENT, HR, OTHER]


class MeetingStatus:
    SCHEDULED = "scheduled"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    ALL = [SCHEDULED, COMPLETED, CANCELLED]


class ActionItemStatus:
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    ALL = [PENDING, IN_PROGRESS, DONE]


class Meeting(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    meeting_code = db.Column(db.String(30), unique=True, nullable=False)  # e.g. MTG-2026-001
    title = db.Column(db.String(300), nullable=False, index=True)
    meeting_type = db.Column(db.String(20), default=MeetingType.OTHER)
    department_id = db.Column(db.Integer, db.ForeignKey("department.id"), nullable=True)
    organizer_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)

    scheduled_at = db.Column(db.DateTime, nullable=False)
    location = db.Column(db.String(300))  # room name or meeting link
    agenda = db.Column(db.Text)
    raw_notes = db.Column(db.Text)  # pasted transcript / rough notes, source for AI generation

    status = db.Column(db.String(20), default=MeetingStatus.SCHEDULED, index=True)
    created_at = db.Column(db.DateTime, default=now_utc)
    updated_at = db.Column(db.DateTime, default=now_utc, onupdate=now_utc)

    department = db.relationship("Department")
    organizer = db.relationship("User", foreign_keys=[organizer_id])
    participants = db.relationship("MeetingParticipant", backref="meeting", lazy="dynamic", cascade="all, delete-orphan")
    action_items = db.relationship("MeetingActionItem", backref="meeting", lazy="dynamic", cascade="all, delete-orphan")
    decisions = db.relationship("MeetingDecision", backref="meeting", lazy="dynamic", cascade="all, delete-orphan")
    minutes = db.relationship("MeetingMinutes", backref="meeting", uselist=False, cascade="all, delete-orphan")
    related_policies = db.relationship("Policy", secondary=meeting_policies, lazy="subquery",
                                       backref=db.backref("related_meetings", lazy="dynamic"))

    @property
    def attendee_count(self):
        return self.participants.count()

    @property
    def open_action_count(self):
        return self.action_items.filter(MeetingActionItem.status != ActionItemStatus.DONE).count()

    def __repr__(self):
        return f"<Meeting {self.meeting_code}: {self.title}>"


class MeetingParticipant(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meeting.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    is_organizer = db.Column(db.Boolean, default=False)
    attended = db.Column(db.Boolean, nullable=True)  # null = unknown/not yet marked

    user = db.relationship("User")

    __table_args__ = (
        db.UniqueConstraint("meeting_id", "user_id", name="uq_participant_meeting_user"),
    )


class MeetingMinutes(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meeting.id"), nullable=False, unique=True)
    summary = db.Column(db.Text)          # short executive summary
    full_minutes = db.Column(db.Text)     # detailed MOM body
    key_points_json = db.Column(db.Text)  # JSON list of strings
    followup_email = db.Column(db.Text)   # AI-generated follow-up email draft
    generated_by_ai = db.Column(db.Boolean, default=False)
    generated_at = db.Column(db.DateTime)
    edited_at = db.Column(db.DateTime)
    edited_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)

    edited_by = db.relationship("User")

    @property
    def key_points(self):
        try:
            return json.loads(self.key_points_json) if self.key_points_json else []
        except (ValueError, TypeError):
            return []

    @key_points.setter
    def key_points(self, value):
        self.key_points_json = json.dumps(value or [])


class MeetingDecision(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meeting.id"), nullable=False, index=True)
    description = db.Column(db.Text, nullable=False)
    source = db.Column(db.String(10), default="manual")  # "manual" | "ai"
    created_at = db.Column(db.DateTime, default=now_utc)


class MeetingActionItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    meeting_id = db.Column(db.Integer, db.ForeignKey("meeting.id"), nullable=False, index=True)
    description = db.Column(db.Text, nullable=False)
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    owner_name_raw = db.Column(db.String(150))  # fallback display name if no user match
    due_date = db.Column(db.Date, nullable=True)
    priority = db.Column(db.String(20), default=Priority.MEDIUM)
    status = db.Column(db.String(20), default=ActionItemStatus.PENDING, index=True)
    source = db.Column(db.String(10), default="manual")  # "manual" | "ai"
    created_at = db.Column(db.DateTime, default=now_utc)
    completed_at = db.Column(db.DateTime, nullable=True)

    owner = db.relationship("User")

    @property
    def owner_display(self):
        return self.owner.name if self.owner else (self.owner_name_raw or "Unassigned")

    def __repr__(self):
        return f"<ActionItem {self.description[:30]!r} [{self.status}]>"


# ================================================================
# Module 3: AI Policy Assistant
# ================================================================
class PolicyAIReview(db.Model):
    """AI Review results for a policy: missing sections, compliance, risk, duplicates, conflicts."""
    id = db.Column(db.Integer, primary_key=True)
    policy_id = db.Column(db.Integer, db.ForeignKey("policy.id"), nullable=False, unique=True)
    missing_sections_json = db.Column(db.Text)
    compliance_issues_json = db.Column(db.Text)
    legal_issues_json = db.Column(db.Text)
    suggestions_json = db.Column(db.Text)
    duplicates_json = db.Column(db.Text)   # [{policy_id, title, similarity}]
    conflicts_json = db.Column(db.Text)    # [{policy_id, title, conflict_description}]
    risk_score = db.Column(db.Integer, default=0)
    generated_at = db.Column(db.DateTime, default=now_utc)
    generated_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)

    policy = db.relationship("Policy", backref=db.backref("ai_review", uselist=False, cascade="all, delete-orphan"))
    generated_by = db.relationship("User")

    def _get(self, field):
        try:
            return json.loads(getattr(self, field)) if getattr(self, field) else []
        except (ValueError, TypeError):
            return []

    def _set(self, field, value):
        setattr(self, field, json.dumps(value or []))

    @property
    def missing_sections(self): return self._get("missing_sections_json")
    @missing_sections.setter
    def missing_sections(self, v): self._set("missing_sections_json", v)

    @property
    def compliance_issues(self): return self._get("compliance_issues_json")
    @compliance_issues.setter
    def compliance_issues(self, v): self._set("compliance_issues_json", v)

    @property
    def legal_issues(self): return self._get("legal_issues_json")
    @legal_issues.setter
    def legal_issues(self, v): self._set("legal_issues_json", v)

    @property
    def suggestions(self): return self._get("suggestions_json")
    @suggestions.setter
    def suggestions(self, v): self._set("suggestions_json", v)

    @property
    def duplicates(self): return self._get("duplicates_json")
    @duplicates.setter
    def duplicates(self, v): self._set("duplicates_json", v)

    @property
    def conflicts(self): return self._get("conflicts_json")
    @conflicts.setter
    def conflicts(self, v): self._set("conflicts_json", v)


class PolicyAIInsight(db.Model):
    """AI Insights for a policy: summary, executive summary, FAQ, quiz, key points, impact analysis."""
    id = db.Column(db.Integer, primary_key=True)
    policy_id = db.Column(db.Integer, db.ForeignKey("policy.id"), nullable=False, unique=True)
    summary = db.Column(db.Text)
    executive_summary = db.Column(db.Text)
    key_points_json = db.Column(db.Text)
    faq_json = db.Column(db.Text)
    quiz_json = db.Column(db.Text)
    impact_analysis = db.Column(db.Text)
    reading_time_minutes = db.Column(db.Integer, default=0)
    generated_at = db.Column(db.DateTime, default=now_utc)

    policy = db.relationship("Policy", backref=db.backref("ai_insight", uselist=False, cascade="all, delete-orphan"))

    def _get(self, field):
        try:
            return json.loads(getattr(self, field)) if getattr(self, field) else []
        except (ValueError, TypeError):
            return []

    def _set(self, field, value):
        setattr(self, field, json.dumps(value or []))

    @property
    def key_points(self): return self._get("key_points_json")
    @key_points.setter
    def key_points(self, v): self._set("key_points_json", v)

    @property
    def faq(self): return self._get("faq_json")
    @faq.setter
    def faq(self, v): self._set("faq_json", v)

    @property
    def quiz(self): return self._get("quiz_json")
    @quiz.setter
    def quiz(self, v): self._set("quiz_json", v)


# ================================================================
# Module 8: Workflow Automation
# ================================================================
class WorkflowApprovalMode:
    ANY = "any"   # parallel approval: any one approver clears the stage
    ALL = "all"   # parallel approval: every approver must approve
    ALL_VALUES = [ANY, ALL]


class WorkflowStageStatus:
    WAITING = "waiting"      # a later stage, not yet active
    PENDING = "pending"      # currently active, awaiting action
    APPROVED = "approved"
    REJECTED = "rejected"
    SKIPPED = "skipped"      # e.g. sibling in an "any" parallel group once one approves
    ALL = [WAITING, PENDING, APPROVED, REJECTED, SKIPPED]


class WorkflowInstanceStatus:
    IN_PROGRESS = "in_progress"
    APPROVED = "approved"
    REJECTED = "rejected"
    ALL = [IN_PROGRESS, APPROVED, REJECTED]


class WorkflowTemplate(db.Model):
    """A reusable, configurable approval chain (Module 8 'visual workflow builder')."""
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(150), nullable=False)
    description = db.Column(db.Text)
    category_id = db.Column(db.Integer, db.ForeignKey("policy_category.id"), nullable=True)
    applies_priorities_json = db.Column(db.Text)  # JSON list, e.g. ["high","critical"]; empty/None = all
    is_default = db.Column(db.Boolean, default=False)  # fallback template when nothing else matches
    is_active = db.Column(db.Boolean, default=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)
    created_at = db.Column(db.DateTime, default=now_utc)

    category = db.relationship("PolicyCategory")
    created_by = db.relationship("User")
    stages = db.relationship("WorkflowStage", backref="template", order_by="WorkflowStage.order",
                             cascade="all, delete-orphan")

    @property
    def applies_priorities(self):
        try:
            return json.loads(self.applies_priorities_json) if self.applies_priorities_json else []
        except (ValueError, TypeError):
            return []

    @applies_priorities.setter
    def applies_priorities(self, value):
        self.applies_priorities_json = json.dumps(value or [])

    def matches(self, policy) -> bool:
        if not self.is_active:
            return False
        if self.category_id and policy.category_id != self.category_id:
            return False
        priorities = self.applies_priorities
        if priorities and policy.priority not in priorities:
            return False
        return True

    def __repr__(self):
        return f"<WorkflowTemplate {self.name!r}>"


class WorkflowStage(db.Model):
    """One stage definition within a WorkflowTemplate."""
    id = db.Column(db.Integer, primary_key=True)
    template_id = db.Column(db.Integer, db.ForeignKey("workflow_template.id"), nullable=False)
    name = db.Column(db.String(100), nullable=False)  # e.g. "Legal Review", "CEO Sign-off"
    order = db.Column(db.Integer, default=1)
    approval_mode = db.Column(db.String(10), default=WorkflowApprovalMode.ALL)  # any/all across its approvers
    sla_hours = db.Column(db.Integer, nullable=True)  # deadline from activation; null = no SLA

    approvers = db.relationship("WorkflowStageApprover", backref="stage", cascade="all, delete-orphan")

    def __repr__(self):
        return f"<WorkflowStage {self.name!r} (order {self.order})>"


class WorkflowStageApprover(db.Model):
    """Who can act at a WorkflowStage: either anyone with a given role, or one specific user."""
    id = db.Column(db.Integer, primary_key=True)
    stage_id = db.Column(db.Integer, db.ForeignKey("workflow_stage.id"), nullable=False)
    approver_type = db.Column(db.String(10), nullable=False)  # "role" | "user"
    role = db.Column(db.String(20), nullable=True)             # UserRole value, if approver_type == "role"
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)  # if approver_type == "user"

    user = db.relationship("User")

    @property
    def display_name(self):
        if self.approver_type == "user" and self.user:
            return self.user.name
        return (self.role or "").title()


class WorkflowStageInstance(db.Model):
    """A running/completed stage for one specific policy's workflow submission."""
    id = db.Column(db.Integer, primary_key=True)
    policy_id = db.Column(db.Integer, db.ForeignKey("policy.id"), nullable=False, index=True)
    version_id = db.Column(db.Integer, db.ForeignKey("policy_version.id"), nullable=False)
    template_id = db.Column(db.Integer, db.ForeignKey("workflow_template.id"), nullable=True)
    name = db.Column(db.String(100), nullable=False)
    order = db.Column(db.Integer, default=1)
    approval_mode = db.Column(db.String(10), default=WorkflowApprovalMode.ALL)
    sla_hours = db.Column(db.Integer, nullable=True)
    sla_due_at = db.Column(db.DateTime, nullable=True)   # set when stage becomes PENDING
    status = db.Column(db.String(20), default=WorkflowStageStatus.WAITING, index=True)
    escalated = db.Column(db.Boolean, default=False)
    escalated_at = db.Column(db.DateTime, nullable=True)
    reminder_sent_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=now_utc)
    completed_at = db.Column(db.DateTime, nullable=True)

    policy = db.relationship("Policy", backref=db.backref("workflow_stages", lazy="dynamic", cascade="all, delete-orphan"))
    version = db.relationship("PolicyVersion")
    template = db.relationship("WorkflowTemplate")
    actions = db.relationship("WorkflowApprovalAction", backref="stage_instance", cascade="all, delete-orphan")

    @property
    def is_overdue(self):
        if not self.sla_due_at or self.status != WorkflowStageStatus.PENDING:
            return False
        return datetime.now(timezone.utc).replace(tzinfo=None) > self.sla_due_at

    def can_user_act(self, user) -> bool:
        return any(a.user_can_act(user) for a in self.actions)

    def __repr__(self):
        return f"<WorkflowStageInstance {self.name!r} [{self.status}]>"


class WorkflowApprovalAction(db.Model):
    """One approver's action (or pending assignment) within a WorkflowStageInstance."""
    id = db.Column(db.Integer, primary_key=True)
    stage_instance_id = db.Column(db.Integer, db.ForeignKey("workflow_stage_instance.id"), nullable=False)
    approver_type = db.Column(db.String(10), nullable=False)  # "role" | "user" — who's eligible
    role = db.Column(db.String(20), nullable=True)
    assigned_user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)  # if approver_type == "user"
    actor_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)  # who actually acted
    status = db.Column(db.String(20), default=WorkflowStageStatus.PENDING)  # pending/approved/rejected/skipped
    comment = db.Column(db.Text)
    acted_at = db.Column(db.DateTime, nullable=True)

    assigned_user = db.relationship("User", foreign_keys=[assigned_user_id])
    actor = db.relationship("User", foreign_keys=[actor_id])

    @property
    def eligible_display(self):
        if self.approver_type == "user" and self.assigned_user:
            return self.assigned_user.name
        return (self.role or "").title()

    def user_can_act(self, user) -> bool:
        if self.status != WorkflowStageStatus.PENDING:
            return False
        if self.approver_type == "user":
            return self.assigned_user_id == user.id or user.is_admin()
        return user.role == self.role or user.is_admin()


# ================================================================
# Module 12: Employee Portal — likes, comments, quiz attempts
# ================================================================
class PolicyLike(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    policy_id = db.Column(db.Integer, db.ForeignKey("policy.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=now_utc)

    policy = db.relationship("Policy", backref=db.backref("likes", lazy="dynamic", cascade="all, delete-orphan"))
    user = db.relationship("User")

    __table_args__ = (db.UniqueConstraint("policy_id", "user_id", name="uq_like_policy_user"),)


class PolicyComment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    policy_id = db.Column(db.Integer, db.ForeignKey("policy.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=now_utc)

    policy = db.relationship("Policy", backref=db.backref("comments", lazy="dynamic", cascade="all, delete-orphan"))
    user = db.relationship("User")


class QuizAttempt(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    policy_id = db.Column(db.Integer, db.ForeignKey("policy.id"), nullable=False, index=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    score = db.Column(db.Integer, default=0)
    total = db.Column(db.Integer, default=0)
    passed = db.Column(db.Boolean, default=False)
    answers_json = db.Column(db.Text)  # list of selected option indices
    completed_at = db.Column(db.DateTime, default=now_utc)

    policy = db.relationship("Policy", backref=db.backref("quiz_attempts", lazy="dynamic", cascade="all, delete-orphan"))
    user = db.relationship("User")

    @property
    def percentage(self):
        return round(100 * self.score / self.total) if self.total else 0


# ================================================================
# Module 9: Compliance Center
# ================================================================
policy_frameworks = db.Table(
    "policy_frameworks",
    db.Column("policy_id", db.Integer, db.ForeignKey("policy.id"), primary_key=True),
    db.Column("framework_id", db.Integer, db.ForeignKey("compliance_framework.id"), primary_key=True),
)

DEFAULT_FRAMEWORKS = [
    ("ISO 27001", "Information security management"),
    ("ISO 9001", "Quality management systems"),
    ("GDPR", "EU data protection & privacy"),
    ("SOC 2", "Service organization security/availability/confidentiality controls"),
    ("HIPAA", "US health information privacy & security"),
    ("Indian Labour Laws", "Statutory employment & workplace compliance in India"),
    ("Company Compliance", "Internal company-specific compliance requirements"),
]


class ComplianceFramework(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    description = db.Column(db.Text)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=now_utc)

    policies = db.relationship("Policy", secondary=policy_frameworks, lazy="subquery",
                               backref=db.backref("frameworks", lazy="subquery"))

    def __repr__(self):
        return f"<ComplianceFramework {self.name!r}>"


def ensure_default_frameworks():
    """Lazily seeds the standard framework list on first use — same pattern
    as the default WorkflowTemplate; zero setup required."""
    if ComplianceFramework.query.count() > 0:
        return
    for name, desc in DEFAULT_FRAMEWORKS:
        db.session.add(ComplianceFramework(name=name, description=desc, is_active=True))
    db.session.commit()


# ================================================================
# Module 25: Policy Impact Simulator ("What-If" Compliance Checker)
# ================================================================
class WhatIfVerdict:
    COMPLIANT = "compliant"
    NOT_COMPLIANT = "not_compliant"
    DEPENDS = "depends"
    UNCLEAR = "unclear"
    ALL = [COMPLIANT, NOT_COMPLIANT, DEPENDS, UNCLEAR]


class WhatIfQuery(db.Model):
    """
    A logged run of the What-If Simulator: an employee describes a
    real-world scenario/decision in plain language ("I want to work from
    Goa for 6 weeks and expense my flights"), and the system checks it
    against the active policy set, returning a structured verdict instead
    of a free-form chat answer. Logging these gives HR/Compliance a feed
    of the actual grey-area situations employees are running into —
    something no other module captures today.
    """
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    scenario_text = db.Column(db.Text, nullable=False)
    verdict = db.Column(db.String(20), default=WhatIfVerdict.UNCLEAR)
    confidence = db.Column(db.Integer, default=0)  # 0-100
    explanation = db.Column(db.Text)
    required_actions_json = db.Column(db.Text)      # ["Get manager approval", ...]
    applicable_policies_json = db.Column(db.Text)    # [{policy_id, title, section, relevance}]
    flagged_for_hr = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=now_utc)

    user = db.relationship("User")

    def _get(self, field):
        try:
            return json.loads(getattr(self, field)) if getattr(self, field) else []
        except (ValueError, TypeError):
            return []

    def _set(self, field, value):
        setattr(self, field, json.dumps(value or []))

    @property
    def required_actions(self): return self._get("required_actions_json")
    @required_actions.setter
    def required_actions(self, v): self._set("required_actions_json", v)

    @property
    def applicable_policies(self): return self._get("applicable_policies_json")
    @applicable_policies.setter
    def applicable_policies(self, v): self._set("applicable_policies_json", v)

    def __repr__(self):
        return f"<WhatIfQuery {self.id} [{self.verdict}]>"


# ================================================================
# Module: Contradiction Radar (continuous)
# ================================================================
class ContradictionScanStatus:
    OPEN = "open"
    RESOLVED = "resolved"
    DISMISSED = "dismissed"
    ALL = [OPEN, RESOLVED, DISMISSED]


class ContradictionFlag(db.Model):
    """
    Persisted result of a pairwise contradiction check between two active
    policies in the same category. Populated by scripts/scan_contradictions.py
    (cron-able), which reuses the existing find_conflicts() LLM check from
    policy_ai.py but runs it continuously across the whole portfolio instead
    of only on demand during a single policy's AI Review. This turns
    conflict detection from a one-off check into ongoing monitoring that
    catches drift as policies are edited independently over time.
    """
    id = db.Column(db.Integer, primary_key=True)
    policy_a_id = db.Column(db.Integer, db.ForeignKey("policy.id"), nullable=False, index=True)
    policy_b_id = db.Column(db.Integer, db.ForeignKey("policy.id"), nullable=False, index=True)
    description = db.Column(db.Text)
    status = db.Column(db.String(20), default=ContradictionScanStatus.OPEN, index=True)
    detected_at = db.Column(db.DateTime, default=now_utc)
    last_confirmed_at = db.Column(db.DateTime, default=now_utc)
    resolved_at = db.Column(db.DateTime, nullable=True)
    resolved_by_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True)

    policy_a = db.relationship("Policy", foreign_keys=[policy_a_id])
    policy_b = db.relationship("Policy", foreign_keys=[policy_b_id])
    resolved_by = db.relationship("User")

    __table_args__ = (
        db.UniqueConstraint("policy_a_id", "policy_b_id", name="uq_contradiction_pair"),
    )

    def __repr__(self):
        return f"<ContradictionFlag {self.policy_a_id}<->{self.policy_b_id} [{self.status}]>"


# ================================================================
# Module: Onboarding Path Generator
# ================================================================
class OnboardingChecklistItem(db.Model):
    """
    One row per policy in a new joiner's auto-sequenced reading checklist,
    generated once at account creation (see blueprints.employee.
    build_onboarding_checklist, called from auth.register and
    admin.user_create) so new joiners land on a guided path instead of
    'Browse Policies' with no order.
    """
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    policy_id = db.Column(db.Integer, db.ForeignKey("policy.id"), nullable=False)
    order = db.Column(db.Integer, default=1)
    is_done = db.Column(db.Boolean, default=False)
    completed_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=now_utc)

    user = db.relationship("User")
    policy = db.relationship("Policy")

    __table_args__ = (
        db.UniqueConstraint("user_id", "policy_id", name="uq_onboarding_user_policy"),
    )

    def __repr__(self):
        return f"<OnboardingChecklistItem user={self.user_id} policy={self.policy_id} done={self.is_done}>"
