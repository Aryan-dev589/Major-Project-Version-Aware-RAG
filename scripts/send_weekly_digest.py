"""
scripts/send_weekly_digest.py

Personalized Policy Digest ("For You This Week") — sends one rule-based,
most-relevant policy nudge per active employee (see digest_engine.py),
instead of the generic broadcasts used elsewhere in the app (e.g. "new
policy published" fanned out identically to everyone).

Run weekly via cron / Windows Task Scheduler, same pattern as
scripts/check_workflow_reminders.py.

Usage:
  python scripts/send_weekly_digest.py

Windows Task Scheduler:
  Program:   C:\\path\\to\\venv\\Scripts\\python.exe
  Arguments: C:\\path\\to\\policy-ledger-v2\\scripts\\send_weekly_digest.py
  Start in:  C:\\path\\to\\policy-ledger-v2

cron (Linux/Mac, every Monday 8am):
  0 8 * * 1 cd /path/to/policy-ledger-v2 && /path/to/venv/bin/python scripts/send_weekly_digest.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from digest_engine import send_weekly_digests

if __name__ == "__main__":
    app = create_app(os.environ.get("FLASK_ENV", "production"))
    with app.app_context():
        summary = send_weekly_digests()
        print(f"[weekly digest] sent={summary['sent']} skipped={summary['skipped']}")
