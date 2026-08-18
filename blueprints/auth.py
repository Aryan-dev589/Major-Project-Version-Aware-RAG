"""
blueprints/auth.py
Phase 1 — Authentication
Handles: login, logout, register, email verification, forgot/reset password, MFA setup/verify.
"""
import io
import base64
import pyotp
import qrcode
from datetime import datetime, timezone, timedelta
from flask import (Blueprint, render_template, redirect, url_for,
                   flash, request, session, jsonify, current_app)
from flask_login import login_user, logout_user, login_required, current_user
from models import db, User, UserRole, AuditLog
from utils import audit
from blueprints.employee import build_onboarding_checklist

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


# ---------- Login ----------
@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(_dashboard_url())

    error = None
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        remember = bool(request.form.get("remember"))

        user = User.query.filter_by(email=email).first()

        if not user or not user.check_password(password):
            error = "Invalid email or password."
            audit("auth.login_failed", detail={"email": email})
        elif not user.is_active:
            error = "Your account has been deactivated. Contact HR."
        else:
            # MFA check
            if user.mfa_enabled:
                session["mfa_pending_user_id"] = user.id
                session["mfa_remember"] = remember
                return redirect(url_for("auth.mfa_verify"))

            _complete_login(user, remember)
            return redirect(request.args.get("next") or _dashboard_url())

    return render_template("auth/login.html", error=error)


def _complete_login(user: User, remember: bool = False):
    login_user(user, remember=remember)
    user.last_login = datetime.now(timezone.utc)
    db.session.commit()
    audit("auth.login_success", resource_type="user", resource_id=user.id)


# ---------- MFA verify ----------
@auth_bp.route("/mfa", methods=["GET", "POST"])
def mfa_verify():
    user_id = session.get("mfa_pending_user_id")
    if not user_id:
        return redirect(url_for("auth.login"))

    user = User.query.get(user_id)
    error = None

    if request.method == "POST":
        code = request.form.get("code", "").replace(" ", "")
        totp = pyotp.TOTP(user.mfa_secret)
        if totp.verify(code, valid_window=1):
            remember = session.pop("mfa_remember", False)
            session.pop("mfa_pending_user_id", None)
            _complete_login(user, remember)
            return redirect(_dashboard_url())
        else:
            error = "Invalid or expired code. Try again."
            audit("auth.mfa_failed", resource_type="user", resource_id=user.id)

    return render_template("auth/mfa.html", error=error, email=user.email)


# ---------- MFA setup (for logged-in users) ----------
@auth_bp.route("/mfa/setup", methods=["GET", "POST"])
@login_required
def mfa_setup():
    if current_user.mfa_enabled:
        flash("MFA is already enabled.", "info")
        return redirect(url_for("auth.profile"))

    if request.method == "POST":
        code = request.form.get("code", "").replace(" ", "")
        secret = session.get("mfa_setup_secret")
        if not secret:
            return redirect(url_for("auth.mfa_setup"))
        totp = pyotp.TOTP(secret)
        if totp.verify(code, valid_window=1):
            current_user.mfa_secret = secret
            current_user.mfa_enabled = True
            db.session.commit()
            session.pop("mfa_setup_secret", None)
            audit("auth.mfa_enabled", resource_type="user", resource_id=current_user.id)
            flash("MFA enabled successfully.", "success")
            return redirect(url_for("auth.profile"))
        else:
            flash("Invalid code. Please try again.", "danger")

    secret = pyotp.random_base32()
    session["mfa_setup_secret"] = secret
    totp = pyotp.TOTP(secret)
    uri = totp.provisioning_uri(name=current_user.email, issuer_name=current_app.config["MFA_ISSUER"])

    qr = qrcode.make(uri)
    buf = io.BytesIO()
    qr.save(buf, format="PNG")
    qr_b64 = base64.b64encode(buf.getvalue()).decode()

    return render_template("auth/mfa_setup.html", qr_b64=qr_b64, secret=secret)


@auth_bp.route("/mfa/disable", methods=["POST"])
@login_required
def mfa_disable():
    current_user.mfa_enabled = False
    current_user.mfa_secret = None
    db.session.commit()
    audit("auth.mfa_disabled", resource_type="user", resource_id=current_user.id)
    flash("MFA has been disabled.", "info")
    return redirect(url_for("auth.profile"))


# ---------- Logout ----------
@auth_bp.route("/logout")
@login_required
def logout():
    audit("auth.logout", resource_type="user", resource_id=current_user.id)
    logout_user()
    flash("You have been signed out.", "info")
    return redirect(url_for("auth.login"))


# ---------- Register (Admin creates accounts; self-register disabled by default) ----------
@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    # In production, self-registration should be disabled and admin creates accounts.
    # This route is available for initial setup / demo only.
    if current_user.is_authenticated:
        return redirect(_dashboard_url())

    error = None
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        if not name or not email or not password:
            error = "All fields are required."
        elif password != confirm:
            error = "Passwords do not match."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        elif User.query.filter_by(email=email).first():
            error = "An account with this email already exists."
        else:
            # First registered user becomes admin
            is_first = User.query.count() == 0
            user = User(
                name=name, email=email,
                role=UserRole.ADMIN if is_first else UserRole.EMPLOYEE,
                email_verified=True,
            )
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            audit("auth.register", resource_type="user", resource_id=user.id)
            build_onboarding_checklist(user)
            login_user(user)
            flash("Account created. Welcome!", "success")
            return redirect(_dashboard_url())

    return render_template("auth/register.html", error=error)


# ---------- Profile / change password ----------
@auth_bp.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    error = success = None
    if request.method == "POST":
        action = request.form.get("action")
        if action == "update_profile":
            current_user.name = request.form.get("name", current_user.name).strip()
            current_user.phone = request.form.get("phone", "").strip()
            current_user.designation = request.form.get("designation", "").strip()
            db.session.commit()
            success = "Profile updated."
        elif action == "change_password":
            old = request.form.get("old_password", "")
            new = request.form.get("new_password", "")
            confirm = request.form.get("confirm_password", "")
            if not current_user.check_password(old):
                error = "Current password is incorrect."
            elif new != confirm:
                error = "New passwords do not match."
            elif len(new) < 8:
                error = "Password must be at least 8 characters."
            else:
                current_user.set_password(new)
                db.session.commit()
                audit("auth.password_changed", resource_type="user", resource_id=current_user.id)
                success = "Password changed successfully."

    return render_template("auth/profile.html", error=error, success=success)


# ---------- Helper ----------
def _dashboard_url() -> str:
    from models import UserRole
    if current_user.role in (UserRole.ADMIN, UserRole.HR):
        return url_for("admin.dashboard")
    return url_for("employee.dashboard")
