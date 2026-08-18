"""
scripts/scan_contradictions.py

Contradiction Radar — continuous version of the existing on-demand
find_conflicts() check (policy_ai.py, normally only run manually per-policy
from the AI Review tab in blueprints/policy_ai.py). This script walks every
pair of *active* policies that share a category, runs the same conflict
check on each pair, and persists results as ContradictionFlag rows so the
/admin/contradictions dashboard reflects live, ongoing drift instead of a
one-off snapshot from whenever someone last happened to click "AI Review".

Degrades gracefully without an LLM configured: find_conflicts() already
returns an empty list rather than raising when no LLM provider is set up,
so with nothing configured this scan simply (and correctly) reports
"0 found" every run — same pattern as everything else in the app.

Run periodically (e.g. nightly) via cron or Windows Task Scheduler.

Usage:
  python scripts/scan_contradictions.py

Windows Task Scheduler:
  Program:   C:\\path\\to\\venv\\Scripts\\python.exe
  Arguments: C:\\path\\to\\policy-ledger-v2\\scripts\\scan_contradictions.py
  Start in:  C:\\path\\to\\policy-ledger-v2

cron (Linux/Mac, nightly at 2am):
  0 2 * * * cd /path/to/policy-ledger-v2 && /path/to/venv/bin/python scripts/scan_contradictions.py
"""
import os
import sys
from datetime import datetime, timezone
from itertools import combinations

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from models import db, Policy, PolicyStatus, ContradictionFlag, ContradictionScanStatus
from policy_ai import find_conflicts


def _pair_key(a_id: int, b_id: int):
    """Canonical (lower_id, higher_id) ordering so each unordered pair maps
    to exactly one ContradictionFlag row regardless of scan order."""
    return (a_id, b_id) if a_id < b_id else (b_id, a_id)


def scan_contradictions() -> dict:
    policies = Policy.query.filter_by(status=PolicyStatus.ACTIVE).all()
    by_category = {}
    for p in policies:
        by_category.setdefault(p.category_id, []).append(p)

    checked = 0
    flagged = 0
    now = datetime.now(timezone.utc)

    for cat_id, group in by_category.items():
        if cat_id is None or len(group) < 2:
            continue  # need a shared, real category to compare within
        for a, b in combinations(group, 2):
            ver_a = a.versions.filter_by(is_active=True).first()
            ver_b = b.versions.filter_by(is_active=True).first()
            if not ver_a or not ver_b:
                continue
            checked += 1

            a_id, b_id = _pair_key(a.id, b.id)
            first, second = (a, b) if a_id == a.id else (b, a)
            first_ver, second_ver = (ver_a, ver_b) if a_id == a.id else (ver_b, ver_a)

            existing = ContradictionFlag.query.filter_by(policy_a_id=a_id, policy_b_id=b_id).first()
            result = find_conflicts(first.title, first_ver.content, [(second.id, second.title, second_ver.content)])
            has_conflict = bool(result)

            if has_conflict:
                description = result[0]["conflict_description"]
                flagged += 1
                if existing:
                    existing.status = ContradictionScanStatus.OPEN
                    existing.description = description
                    existing.last_confirmed_at = now
                else:
                    db.session.add(ContradictionFlag(
                        policy_a_id=a_id, policy_b_id=b_id, description=description,
                        detected_at=now, last_confirmed_at=now,
                    ))
            elif existing and existing.status == ContradictionScanStatus.OPEN:
                # No longer detected on this pass — treat as resolved rather
                # than deleting, so there's a visible history of what was
                # once flagged and no longer is.
                existing.status = ContradictionScanStatus.RESOLVED
                existing.resolved_at = now

    db.session.commit()
    return {"checked": checked, "flagged": flagged}


if __name__ == "__main__":
    app = create_app(os.environ.get("FLASK_ENV", "production"))
    with app.app_context():
        summary = scan_contradictions()
        print(f"[contradiction radar] pairs_checked={summary['checked']} "
             f"flagged={summary['flagged']}")
