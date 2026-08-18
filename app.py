"""
app.py  —  Policy Ledger v2
Run: python app.py
"""
import os
import threading
import time

import requests
from flask import Flask, redirect, url_for
from flask_login import LoginManager
from flask_bcrypt import Bcrypt
from config import config
from models import db, bcrypt, User

login_manager = LoginManager()


def _warm_ollama_model():
    try:
        model = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
        keep_alive = os.environ.get("OLLAMA_KEEP_ALIVE", "1h")
        url = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/") + "/api/generate"
        requests.post(
            url,
            json={
                "model": model,
                "keep_alive": keep_alive,
                "stream": False,
                "prompt": "",
            },
            timeout=15,
        )
    except Exception:
        pass


def create_app(env="default"):
    app = Flask(__name__)
    app.config.from_object(config[env])

    # Ensure data dirs exist
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(os.path.dirname(app.config["SQLALCHEMY_DATABASE_URI"].replace("sqlite:///", "")), exist_ok=True)

    # Extensions
    db.init_app(app)
    bcrypt.init_app(app)

    login_manager.init_app(app)
    login_manager.login_view = "auth.login"
    login_manager.login_message = "Please log in to access this page."
    login_manager.login_message_category = "warning"

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    # Blueprints
    from blueprints.auth import auth_bp
    from blueprints.admin import admin_bp
    from blueprints.employee import employee_bp
    from blueprints.meetings import meetings_bp
    from blueprints.search import search_bp
    from blueprints.policy_ai import policy_ai_bp
    from blueprints.workflow import workflow_bp
    from blueprints.bi_dashboard import bi_bp
    from blueprints.compliance import compliance_bp
    from blueprints.audit_center import audit_center_bp
    from blueprints.what_if import what_if_bp
    from blueprints.governance import governance_bp
    from blueprints.confusion_index import confusion_index_bp
    from blueprints.contradiction_radar import contradiction_radar_bp
    from blueprints.knowledge_graph import knowledge_graph_bp
    from rag.api.rag_routes import rag_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(employee_bp)
    app.register_blueprint(meetings_bp)
    app.register_blueprint(search_bp)
    app.register_blueprint(policy_ai_bp)
    app.register_blueprint(workflow_bp)
    app.register_blueprint(bi_bp)
    app.register_blueprint(compliance_bp)
    app.register_blueprint(audit_center_bp)
    app.register_blueprint(what_if_bp)
    app.register_blueprint(governance_bp)
    app.register_blueprint(confusion_index_bp)
    app.register_blueprint(contradiction_radar_bp)
    app.register_blueprint(knowledge_graph_bp)
    app.register_blueprint(rag_bp)

    warm_thread = threading.Thread(target=_warm_ollama_model, daemon=True)
    warm_thread.start()

    # Root redirect
    @app.route("/")
    def index():
        return redirect(url_for("auth.login"))

    # Error pages
    @app.errorhandler(403)
    def forbidden(e):
        return "<h2>403 — You don't have permission to access this page.</h2><a href='/'>Home</a>", 403

    @app.errorhandler(404)
    def not_found(e):
        return "<h2>404 — Page not found.</h2><a href='/'>Home</a>", 404

    # Create tables on first run
    with app.app_context():
        db.create_all()

    return app


if __name__ == "__main__":
    app = create_app("development")
    print("\n" + "="*55)
    print("  Policy Ledger v2 — starting")
    print("  Open: http://127.0.0.1:5000")
    print("  Run seed.py first if this is a fresh install")
    print("="*55 + "\n")
    app.run(debug=True, port=5000)
