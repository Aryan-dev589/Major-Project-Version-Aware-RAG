"""
seed.py
Run once after a fresh install to populate the database with:
  - Default admin account
  - Sample departments and categories
  - 5 sample policies with version history

Usage:
    python seed.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))

from app import create_app
from models import (db, User, UserRole, Department, PolicyCategory,
                    Policy, PolicyVersion, PolicyStatus, Tag)
from utils import generate_policy_id
from config import Config
from datetime import date

app = create_app("development")

DEPARTMENTS = [
    ("Human Resources", "HR"), ("Engineering", "ENG"), ("Finance", "FIN"),
    ("Legal", "LEG"), ("Operations", "OPS"), ("Marketing", "MKT"),
    ("Sales", "SAL"), ("IT", "IT"),
]

CATEGORIES = [
    ("Leave", "", "#2a4a38"), ("Attendance", "", "#4a2a38"),
    ("Remote Work", "", "#2a3a4a"), ("Security", "", "#4a3a2a"),
    ("Payroll", "", "#3a4a2a"), ("Travel", "", "#2a4a4a"),
    ("Benefits", "", "#4a2a4a"), ("Recruitment", "", "#3a2a4a"),
    ("Performance", "", "#4a4a2a"), ("POSH", "", "#4a2a2a"),
    ("IT Policy", "", "#2a2a4a"), ("Data Privacy", "", "#3a3a3a"),
]

SAMPLE_POLICIES = [
    {
        "title": "Remote Work Policy",
        "description": "Guidelines for working from home and remote locations.",
        "category": "Remote Work",
        "department": "Human Resources",
        "versions": [
            {
                "num": 1.0, "label": "v1.0",
                "content": """REMOTE WORK POLICY — v1.0
Effective Date: 2022-01-10

1. Eligibility
Employees with 12+ months tenure may apply for remote work, subject to manager approval.

2. Schedule
Employees may work remotely up to 1 day per week. Minimum 4 in-office days required.

3. Equipment
Employees must use personal equipment. Company does not provide home office equipment.

4. VPN
All remote employees must use company VPN when accessing internal systems.

5. Meetings
Employees must be available for all scheduled meetings during core hours (10 AM – 4 PM).""",
                "summary": "Initial remote work policy.",
                "reason": "New policy",
                "eff_date": date(2022, 1, 10),
            },
            {
                "num": 2.0, "label": "v2.0",
                "content": """REMOTE WORK POLICY — v2.0
Effective Date: 2024-03-01

1. Eligibility
All full-time employees may apply from day one, subject to manager approval and role suitability.

2. Schedule
Employees may work remotely up to 3 days per week. Minimum 2 in-office days required for collaboration.

3. Equipment
Company provides a standard laptop and peripherals for approved remote employees.

4. Internet Allowance
Rs. 500/month internet reimbursement claimable via the expense portal.

5. Security
VPN + MFA mandatory for all remote access. Security incidents must be reported within 24 hours.

6. Meetings
Must be available during core hours (10 AM – 4 PM) and attend all mandatory meetings in person once per week.""",
                "summary": "Expanded remote work allowance from 1 to 3 days. Company now provides equipment. Added internet allowance.",
                "reason": "Post-pandemic policy update based on employee feedback",
                "eff_date": date(2024, 3, 1),
            },
        ],
        "tags": ["wfh", "remote", "hybrid"],
        "priority": "high",
        "is_mandatory": True,
    },
    {
        "title": "Leave Policy",
        "description": "Annual, sick, maternity, paternity and bereavement leave entitlements.",
        "category": "Leave",
        "department": "Human Resources",
        "versions": [
            {
                "num": 1.0, "label": "v1.0",
                "content": """LEAVE POLICY — v1.0
Effective Date: 2023-01-01

1. Annual Leave
18 days paid annual leave per calendar year.

2. Sick Leave
8 days paid sick leave per year. Medical certificate needed for 2+ consecutive days.

3. Carry-Forward
Unused leave cannot be carried forward. Forfeited at year end.

4. Application
Submit to manager via email, minimum 5 working days in advance.

5. Maternity / Paternity
Maternity: 12 weeks. Paternity: 5 days.""",
                "summary": "Initial leave policy.",
                "reason": "New policy",
                "eff_date": date(2023, 1, 1),
            },
            {
                "num": 2.0, "label": "v2.0",
                "content": """LEAVE POLICY — v2.0
Effective Date: 2024-07-01

1. Annual Leave
24 days paid annual leave per calendar year, accrued at 2 days/month.

2. Sick Leave
12 days paid sick leave per year. Medical certificate needed for 2+ consecutive days.

3. Carry-Forward
Up to 5 days may be carried to next year. Balance beyond 5 days is forfeited.

4. Application
Submit via HR Portal (hr.company.in) minimum 3 working days in advance.

5. Maternity / Paternity
Maternity: 26 weeks paid (Maternity Benefit Act 2017). Paternity: 10 days paid.

6. Bereavement
3 days paid bereavement leave for immediate family (parents, spouse, children, siblings).

7. Public Holidays
All government-declared public holidays in addition to annual leave.""",
                "summary": "Annual leave increased 18→24 days. Sick leave 8→12 days. Carry-forward introduced. Paternity doubled to 10 days. Bereavement leave added.",
                "reason": "Annual benefits review — aligning with industry standards",
                "eff_date": date(2024, 7, 1),
            },
        ],
        "tags": ["leave", "pto", "vacation", "sick leave"],
        "priority": "high",
        "is_mandatory": True,
    },
    {
        "title": "Code of Conduct",
        "description": "Workplace behaviour standards expected of all employees.",
        "category": "POSH",
        "department": "Human Resources",
        "versions": [
            {
                "num": 1.0, "label": "v1.0",
                "content": """CODE OF CONDUCT — v1.0
Effective Date: 2024-01-01

1. Professional Behaviour
All employees must conduct themselves professionally and treat colleagues with respect.

2. Conflicts of Interest
Disclose any personal interests that may conflict with company interests to your manager.

3. Confidentiality
Do not share company data, client information, or trade secrets with external parties.

4. Anti-Harassment
Zero tolerance for harassment, discrimination, or bullying of any kind.

5. POSH Compliance
Any complaints regarding sexual harassment must be reported to the Internal Complaints Committee (ICC).

6. Disciplinary Action
Violations may result in verbal warning, written warning, suspension, or termination depending on severity.""",
                "summary": "Initial code of conduct.",
                "reason": "New policy",
                "eff_date": date(2024, 1, 1),
            },
        ],
        "tags": ["conduct", "posh", "behaviour", "ethics"],
        "priority": "critical",
        "is_mandatory": True,
    },
    {
        "title": "IT Security Policy",
        "description": "Guidelines for use of company IT assets, passwords, and data security.",
        "category": "IT Policy",
        "department": "IT",
        "versions": [
            {
                "num": 1.0, "label": "v1.0",
                "content": """IT SECURITY POLICY — v1.0
Effective Date: 2024-02-01

1. Password Policy
Minimum 10 characters. Must include uppercase, lowercase, number, and special character. Change every 90 days.

2. Device Usage
Company devices must not be used for personal activities. Personal devices must not access internal systems without MDM enrollment.

3. Software Installation
Only IT-approved software may be installed on company devices.

4. Data Classification
All company data must be classified as Public, Internal, Confidential, or Restricted and handled accordingly.

5. Incident Reporting
All security incidents must be reported to security@company.com within 2 hours of discovery.

6. MFA
Multi-factor authentication is mandatory for all company accounts.""",
                "summary": "Initial IT security policy.",
                "reason": "New policy",
                "eff_date": date(2024, 2, 1),
            },
        ],
        "tags": ["security", "it", "password", "mfa", "data"],
        "priority": "critical",
        "is_mandatory": True,
    },
    {
        "title": "Travel & Expense Policy",
        "description": "Reimbursement guidelines for business travel and expenses.",
        "category": "Travel",
        "department": "Finance",
        "versions": [
            {
                "num": 1.0, "label": "v1.0",
                "content": """TRAVEL & EXPENSE POLICY — v1.0
Effective Date: 2024-04-01

1. Pre-Approval
All business travel must be pre-approved by the reporting manager and Finance.

2. Flight Booking
Economy class for domestic travel. Business class only for international flights over 6 hours.

3. Hotel
Maximum Rs. 5,000/night for domestic travel. Manager approval required for higher amounts.

4. Daily Allowance (DA)
Rs. 1,500/day for metro cities. Rs. 1,000/day for non-metro cities.

5. Reimbursement
Submit all original receipts via the expense portal within 7 days of return. Reimbursement processed within 10 working days.

6. Personal Travel Extension
Personal extension of business travel is permitted but all personal costs must be borne by the employee.""",
                "summary": "Initial travel and expense policy.",
                "reason": "New policy",
                "eff_date": date(2024, 4, 1),
            },
        ],
        "tags": ["travel", "expense", "reimbursement", "flights"],
        "priority": "medium",
        "is_mandatory": False,
    },
]


def seed():
    with app.app_context():
        # Admin user
        if not User.query.filter_by(email=Config.DEFAULT_ADMIN_EMAIL).first():
            admin = User(
                name="System Admin",
                email=Config.DEFAULT_ADMIN_EMAIL,
                role=UserRole.ADMIN,
                email_verified=True,
                is_active=True,
            )
            admin.set_password(Config.DEFAULT_ADMIN_PASSWORD)
            db.session.add(admin)
            db.session.flush()
            print(f"[ok] Admin created: {Config.DEFAULT_ADMIN_EMAIL} / {Config.DEFAULT_ADMIN_PASSWORD}")
        else:
            admin = User.query.filter_by(email=Config.DEFAULT_ADMIN_EMAIL).first()
            print(f"  Admin already exists: {Config.DEFAULT_ADMIN_EMAIL}")

        # HR user
        hr_email = "hr@company.com"
        if not User.query.filter_by(email=hr_email).first():
            hr = User(name="HR Manager", email=hr_email, role=UserRole.HR,
                      email_verified=True, is_active=True)
            hr.set_password("HR@1234")
            db.session.add(hr)
            db.session.flush()
            print(f"[ok] HR user created: {hr_email} / HR@1234")
        else:
            hr = User.query.filter_by(email=hr_email).first()

        # Sample employee
        emp_email = "employee@company.com"
        if not User.query.filter_by(email=emp_email).first():
            emp = User(name="Sample Employee", email=emp_email, role=UserRole.EMPLOYEE,
                       email_verified=True, is_active=True)
            emp.set_password("Emp@1234")
            db.session.add(emp)
            print(f"[ok] Employee created: {emp_email} / Emp@1234")

        db.session.commit()

        # Departments
        dept_map = {}
        for name, code in DEPARTMENTS:
            dept = Department.query.filter_by(name=name).first()
            if not dept:
                dept = Department(name=name, code=code)
                db.session.add(dept)
                db.session.flush()
            dept_map[name] = dept
        db.session.commit()
        print(f"[ok] {len(DEPARTMENTS)} departments seeded")

        # Categories
        cat_map = {}
        for name, icon, color in CATEGORIES:
            cat = PolicyCategory.query.filter_by(name=name).first()
            if not cat:
                cat = PolicyCategory(name=name, icon=icon, color=color)
                db.session.add(cat)
                db.session.flush()
            cat_map[name] = cat
        db.session.commit()
        print(f"[ok] {len(CATEGORIES)} categories seeded")

        # Policies
        for pd in SAMPLE_POLICIES:
            if Policy.query.filter_by(title=pd["title"]).first():
                print(f"  Policy already exists: {pd['title']}")
                continue

            policy = Policy(
                policy_id=generate_policy_id(),
                title=pd["title"],
                description=pd["description"],
                category_id=cat_map.get(pd["category"], PolicyCategory.query.first()).id,
                department_id=dept_map.get(pd["department"]).id if pd["department"] in dept_map else None,
                author_id=hr.id,
                status=PolicyStatus.ACTIVE,
                priority=pd.get("priority", "medium"),
                is_mandatory=pd.get("is_mandatory", False),
                confidentiality="internal",
            )
            for tn in pd.get("tags", []):
                tag = Tag.query.filter_by(name=tn).first() or Tag(name=tn)
                if not tag.id:
                    db.session.add(tag)
                policy.tags.append(tag)

            db.session.add(policy)
            db.session.flush()

            versions = pd["versions"]
            for i, vd in enumerate(versions):
                is_last = (i == len(versions) - 1)
                ver = PolicyVersion(
                    policy_id=policy.id,
                    version_num=vd["num"],
                    version_label=vd["label"],
                    content=vd["content"],
                    summary=vd["summary"],
                    change_reason=vd["reason"],
                    created_by_id=hr.id,
                    approved_by_id=admin.id,
                    is_active=is_last,
                    status="approved" if is_last else "superseded",
                    effective_date=vd["eff_date"],
                )
                db.session.add(ver)

            policy.current_version = versions[-1]["label"]
            policy.effective_date = versions[-1]["eff_date"]
            db.session.commit()
            print(f"[ok] Policy seeded: {policy.policy_id} — {policy.title} ({len(versions)} version(s))")

            # Seeded policies bypass the normal "publish" flow, which is what
            # normally triggers indexing — so without this, seed data exists
            # in the database but is invisible to the AI assistant (it only
            # ever searches the vector store, not the SQL tables directly).
            active_version = next((v for v in policy.versions if v.is_active), None)
            if active_version:
                try:
                    from rag.indexing.index_policy import index_policy_version
                    result = index_policy_version(policy.id, active_version.id)
                    if result.get("success"):
                        print(f"      -> indexed {result['chunks']} chunks for AI search")
                    else:
                        print(f"      -> WARNING: could not index for AI search: {result.get('error')}")
                except Exception as e:
                    print(f"      -> WARNING: could not index for AI search: {e}")

        print("\n[done] Seed complete. Run: python app.py")
        print("   Open: http://127.0.0.1:5000")
        print(f"   Admin: {Config.DEFAULT_ADMIN_EMAIL} / {Config.DEFAULT_ADMIN_PASSWORD}")
        print("   HR:    hr@company.com / HR@1234")
        print("   Employee: employee@company.com / Emp@1234\n")


if __name__ == "__main__":
    seed()
