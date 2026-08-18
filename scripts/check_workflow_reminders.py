"""
scripts/check_workflow_reminders.py

Module 8: Workflow Automation — SLA reminders + escalation.

Run this periodically (e.g. every hour) via cron or Windows Task Scheduler
to send reminder notifications for approvals nearing their SLA deadline, and
escalate to Admins for approvals that have gone overdue.

Usage:
  python scripts/check_workflow_reminders.py

Windows Task Scheduler:
  Program:   C:\\path\\to\\venv\\Scripts\\python.exe
  Arguments: C:\\path\\to\\policy-ledger-v2\\scripts\\check_workflow_reminders.py
  Start in:  C:\\path\\to\\policy-ledger-v2

cron (Linux/Mac):
  0 * * * * cd /path/to/policy-ledger-v2 && /path/to/venv/bin/python scripts/check_workflow_reminders.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from workflow_engine import check_reminders_and_escalations

if __name__ == "__main__":
    app = create_app(os.environ.get("FLASK_ENV", "production"))
    with app.app_context():
        summary = check_reminders_and_escalations()
        print(f"[workflow reminders] checked={summary['checked']} "
             f"reminders_sent={summary['reminders_sent']} "
             f"escalations_made={summary['escalations_made']}")
