"""
config.py
All configuration in one place. Copy .env.example to .env and fill in values.
"""
import os
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))


class Config:
    # --- Core ---
    SECRET_KEY = os.environ.get("SECRET_KEY", "change-this-in-production-please")
    DEBUG = False
    TESTING = False

    # --- Database ---
   # --- Database ---
   # --- Database ---
    # 1. Get the absolute path to the instance folder
    db_path = os.path.join(BASE_DIR, "instance", "ledger.db")
    
    # 2. Convert Windows backslashes (\) to forward slashes (/) for SQLite
    db_path = db_path.replace("\\", "/")
    
    # 3. Force SQLAlchemy to use this absolute path (requires 3 slashes + absolute path)
    SQLALCHEMY_DATABASE_URI = f"sqlite:///{db_path}"
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # --- JWT ---
    JWT_SECRET_KEY = os.environ.get("JWT_SECRET_KEY", SECRET_KEY)
    JWT_ACCESS_TOKEN_EXPIRES = timedelta(hours=8)

    # --- File uploads ---
    UPLOAD_FOLDER = os.path.join(BASE_DIR, "data", "uploads")
    MAX_CONTENT_LENGTH = 32 * 1024 * 1024  # 32 MB
    ALLOWED_EXTENSIONS = {"pdf", "docx", "txt", "md"}

    # --- Email (optional — notifications) ---
    MAIL_SERVER = os.environ.get("MAIL_SERVER", "smtp.gmail.com")
    MAIL_PORT = int(os.environ.get("MAIL_PORT", 587))
    MAIL_USE_TLS = True
    MAIL_USERNAME = os.environ.get("MAIL_USERNAME", "")
    MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD", "")
    MAIL_DEFAULT_SENDER = os.environ.get("MAIL_DEFAULT_SENDER", "noreply@company.com")

    # --- App-level settings ---
    APP_NAME = os.environ.get("APP_NAME", "Policy Ledger")
    COMPANY_NAME = os.environ.get("COMPANY_NAME", "Your Company")
    DEFAULT_ADMIN_EMAIL = os.environ.get("DEFAULT_ADMIN_EMAIL", "admin@company.com")
    DEFAULT_ADMIN_PASSWORD = os.environ.get("DEFAULT_ADMIN_PASSWORD", "Admin@1234")

    # --- Ollama keep-alive ---
    OLLAMA_KEEP_ALIVE = os.environ.get("OLLAMA_KEEP_ALIVE", "1h")
    OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
    OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")

    # --- MFA ---
    MFA_ISSUER = os.environ.get("MFA_ISSUER", "PolicyLedger")


class DevelopmentConfig(Config):
    DEBUG = True


class ProductionConfig(Config):
    DEBUG = False


config = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "default": DevelopmentConfig,
}
