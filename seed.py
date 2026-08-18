"""seed.py

Legacy database seeding and synthetic policy generation for the local RAG test
pipeline.

Usage:
  python seed.py
  python seed.py --mode synthetic --count 5
  python seed.py --mode synthetic --count 3 --no-ollama
"""

import argparse
import json
import os
import re
import sys
import textwrap
import time
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from config import Config

try:
	from app import create_app
	from models import (
		db,
		User,
		UserRole,
		Department,
		PolicyCategory,
		Policy,
		PolicyVersion,
		PolicyStatus,
		Tag,
	)
	from utils import generate_policy_id
except Exception:
	create_app = None
	db = None
	User = UserRole = Department = PolicyCategory = Policy = PolicyVersion = PolicyStatus = Tag = None
	generate_policy_id = None

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")

OUTPUT_ROOT = ROOT / "synthetic_policies"
HTML_ROOT = OUTPUT_ROOT / "html"
PDF_ROOT = OUTPUT_ROOT / "pdf"
JSON_ROOT = OUTPUT_ROOT / "ground_truth"

DEPARTMENTS = [
	("Human Resources", "HR"),
	("Engineering", "ENG"),
	("Finance", "FIN"),
	("Legal", "LEG"),
	("Operations", "OPS"),
	("Marketing", "MKT"),
	("Sales", "SAL"),
	("IT", "IT"),
]

CATEGORIES = [
	("Leave", "", "#2a4a38"),
	("Attendance", "", "#4a2a38"),
	("Remote Work", "", "#2a3a4a"),
	("Security", "", "#4a3a2a"),
	("Payroll", "", "#3a4a2a"),
	("Travel", "", "#2a4a4a"),
	("Benefits", "", "#4a2a4a"),
	("Recruitment", "", "#3a2a4a"),
	("Performance", "", "#4a4a2a"),
	("POSH", "", "#4a2a2a"),
	("IT Policy", "", "#2a2a4a"),
	("Data Privacy", "", "#3a3a3a"),
]

SAMPLE_POLICIES = [
	{
		"title": "Remote Work Policy",
		"description": "Guidelines for working from home and remote locations.",
		"category": "Remote Work",
		"department": "Human Resources",
		"versions": [
			{
				"num": 1.0,
				"label": "v1.0",
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
				"num": 2.0,
				"label": "v2.0",
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
				"num": 1.0,
				"label": "v1.0",
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
				"num": 2.0,
				"label": "v2.0",
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
				"num": 1.0,
				"label": "v1.0",
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
				"num": 1.0,
				"label": "v1.0",
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
				"num": 1.0,
				"label": "v1.0",
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


def _strip_code_fences(text: str) -> str:
	text = (text or "").strip()
	text = re.sub(r"^```(?:json|html|xml)?\s*", "", text, flags=re.IGNORECASE)
	text = re.sub(r"\s*```$", "", text)
	return text.strip()


def _safe_json_loads(raw_text: str):
	cleaned = _strip_code_fences(raw_text)
	try:
		return json.loads(cleaned)
	except Exception:
		pass

	for start_marker, end_marker in (("[", "]"), ("{", "}")):
		start = cleaned.find(start_marker)
		end = cleaned.rfind(end_marker)
		if start != -1 and end != -1 and end > start:
			try:
				return json.loads(cleaned[start : end + 1])
			except Exception:
				continue

	return None


def _ollama_generate(prompt: str, *, temperature: float = 0.7, stream: bool = False) -> str:
	response = requests.post(
		f"{OLLAMA_URL}/api/generate",
		json={
			"model": OLLAMA_MODEL,
			"prompt": prompt,
			"stream": stream,
			"options": {"temperature": temperature, "top_p": 0.9},
		},
		timeout=180,
	)
	response.raise_for_status()
	data = response.json()
	return str(data.get("response", "")).strip()


def _document_templates():
	return [
		{
			"title": "Remote Work Governance and Allowance Standard",
			"frameworks": ["ISO/IEC 27001", "SOC 2 Type II", "GDPR"],
			"domain": "Workforce Mobility and Endpoint Security",
			"document_id": "POL-REMOTE-GOV-2026-01",
		},
		{
			"title": "Cryptographic Material Lifecycle and Access Control Standard",
			"frameworks": ["ISO/IEC 27001", "SOC 2 Type II", "NIST CSF"],
			"domain": "Cryptography, IAM, and Key Management",
			"document_id": "POL-CRYPTO-LCM-2026-02",
		},
		{
			"title": "Data Residency, Retention, and Cross-Border Transfer Policy",
			"frameworks": ["GDPR", "ISO/IEC 27001", "SOC 2 Type II"],
			"domain": "Privacy operations and international data handling",
			"document_id": "POL-DATA-RESIDENCY-2026-03",
		},
		{
			"title": "Travel, Expense, and Delegated Authorization Policy",
			"frameworks": ["SOX", "ISO 31000", "SOC 2 Type II"],
			"domain": "Financial controls and delegation controls",
			"document_id": "POL-TRAVEL-EXPENSE-2026-04",
		},
		{
			"title": "Business Continuity, Crisis Escalation, and Supplier Resilience Standard",
			"frameworks": ["ISO 22301", "SOC 2 Type II", "NIST CSF"],
			"domain": "Operational resilience and vendor risk",
			"document_id": "POL-BCP-RESILIENCE-2026-05",
		},
	]


def _fallback_policy_html(title: str, document_id: str, frameworks: List[str], domain: str) -> str:
	rows = """
		<tr><td>Senior Security Engineer</td><td>10.28.0.0/16</td><td>Tier 3 Confidential</td><td>Required</td><td>240 minutes</td><td>Hybrid, privileged approval</td></tr>
		<tr><td>Regional HR Partner</td><td>10.32.0.0/20</td><td>Tier 1 Internal</td><td>Required</td><td>480 minutes</td><td>Department manager attestation</td></tr>
		<tr><td>Finance Controller</td><td>10.40.0.0/24</td><td>Tier 3 Restricted</td><td>Required + Hardware key</td><td>120 minutes</td><td>Dual approval and quarterly review</td></tr>
		<tr><td>Procurement Analyst</td><td>10.44.0.0/24</td><td>Tier 2 Confidential</td><td>Required</td><td>300 minutes</td><td>Annual compliance attestation</td></tr>
		<tr><td>Support Engineer</td><td>10.50.0.0/23</td><td>Tier 1 Internal</td><td>Required</td><td>360 minutes</td><td>Role-specific just-in-time access</td></tr>
	"""
	return f"""
	<html>
	  <head>
		<meta charset="utf-8" />
		<title>{title}</title>
		<style>
		  body {{ font-family: Arial, sans-serif; margin: 32px; color: #1e1e1e; line-height: 1.6; }}
		  h1, h2 {{ color: #112c4f; }}
		  .meta {{ border: 1px solid #cfd8e3; background: #f6f9fc; padding: 12px 16px; margin-bottom: 18px; }}
		  .meta div {{ margin: 4px 0; }}
		  table {{ border-collapse: collapse; width: 100%; margin: 18px 0; font-size: 11px; }}
		  th, td {{ border: 1px solid #b9c5d4; padding: 8px; text-align: left; vertical-align: top; }}
		  th {{ background: #dfeaf7; }}
		  tr:nth-child(even) {{ background: #f7fafc; }}
		  .section {{ margin-top: 28px; }}
		</style>
	  </head>
	  <body>
		<h1>{title}</h1>
		<div class="meta">
		  <div><strong>Document ID:</strong> {document_id}</div>
		  <div><strong>Effective Date:</strong> 2026-07-01</div>
		  <div><strong>Compliance Frameworks:</strong> {', '.join(frameworks)}</div>
		  <div><strong>Operational Domain:</strong> {domain}</div>
		</div>

		<div class="section">
		  <h2>1. Purpose and Scope</h2>
		  <p>This standard establishes a deterministic operating framework for regulated enterprise controls, ensuring that administrative, technical, and operational obligations are aligned to internal risk tolerance, external legal obligations, and service commitments. The control model applies to all human, automated, and third-party actors with access to confidential systems, multi-tenant data estates, and identity-bound workloads.</p>
		  <p>Scope includes production and non-production systems that handle customer data, cryptographic material, privileged endpoints, SaaS integrations, and delegated approval pathways. The organization shall maintain a zero-trust posture, enforce asymmetric key generation for sensitive workloads, and maintain heuristic anomaly detection overlays for all data-plane ingress and egress events.</p>
		</div>

		<div class="section">
		  <h2>2. Standard Operating Procedures</h2>
		  <p>All operational units shall maintain a documented control register reviewed quarterly by the control owner, the internal audit function, and legal compliance. The company shall ensure data separation within multi-tenant storage systems, secure key rotation windows, and account-level privilege minimization constrained by least-privilege design. Every privileged action must be attributable, logged, and retained according to statutory and contractual obligations. High-risk anomalies, policy violations, or manual overrides shall trigger a documented incident ticket and require independent review within 24 hours.</p>
		  <p>For all in-scope business units, escalation and remediation times shall be SLA-bound. Where a data subject request, incident declaration, or regulator inquiry is received, the DPO and privacy operations team must acknowledge receipt, triage operational severity, and communicate material developments within the legally required notification window. The organization shall maintain time-bound evidence logs to support both regulatory review and customer assurance obligations.</p>
		</div>

		<div class="section">
		  <h2>3. Governance and Access Control Matrices</h2>
		  <table>
			<thead>
			  <tr>
				<th>Role Designation</th>
				<th>Subnet Access</th>
				<th>Data Level Authorization</th>
				<th>MFA Requirement</th>
				<th>Session Timeout</th>
				<th>Approval Condition</th>
			  </tr>
			</thead>
			<tbody>
			  {rows}
			</tbody>
		  </table>
		</div>

		<div class="section">
		  <h2>4. Audit, Violation, and Enforcement Protocols</h2>
		  <p>Violations of this standard may include unauthorized data export, policy exception bypass, failure to document key lifecycle events, or negligent configuration drift. Where a violation is identified, the control owner must classify severity, isolate affected systems, and notify the legal compliance committee within applicable deadlines. Repeat violations may result in temporary access suspension, mandatory retraining, intensified audit frequency, and escalation to formal disciplinary action.</p>
		  <p>All enforcement actions shall be rooted in objective evidence and time-stamped records. Internal audits may sample controls across network segmentation, cryptographic governance, infrastructure telemetry, HR access workflows, and third-party due diligence. The organization shall retain evidence bundles sufficient to demonstrate policy compliance to regulators, customers, and contractual counterparties.</p>
		</div>
	  </body>
	</html>
	"""


def _build_policy_prompt(doc_template: dict) -> str:
	return textwrap.dedent(
		f"""
		You are a principal corporate legal and compliance architect. Write a highly formal, enterprise-grade compliance policy document in HTML.

		Requirements:
		- Use dense but realistic regulatory and technical terminology.
		- Include formal metadata: Document ID, Effective Date, Compliance Frameworks, Operational Domain.
		- Sections must be numbered exactly: 1. Purpose and Scope, 2. Standard Operating Procedures, 3. Governance and Access Control Matrices, and 4. Audit, Violation, and Enforcement Protocols.
		- Use polished HTML with inline CSS and valid table markup using <table>, <thead>, <th>, <tbody>, <tr>, <td>.
		- Include at least 5 rows in the access table with columns: Role Designation, Subnet Access, Data Level Authorization, MFA Requirement, Session Timeout, Approval Condition.
		- Add dense language: determinism, zero-trust access, heuristic anomaly detection, asymmetric key generation, multi-tenant data segregation, cryptographic material lifecycle, SLA-bound DPO notification, etc.
		- Make it read like a real corporate policy for a large enterprise technology company.
		- Output ONLY valid HTML. No markdown fences. No surrounding narrative.

		Title: {doc_template['title']}
		Document ID: {doc_template['document_id']}
		Effective Date: 2026-07-01
		Compliance Frameworks: {', '.join(doc_template['frameworks'])}
		Operational Domain: {doc_template['domain']}
		"""
	).strip()


def _build_ground_truth_prompt(html_text: str, title: str, document_id: str) -> str:
	return textwrap.dedent(
		f"""
		You are generating a retrieval benchmark dataset from a corporate compliance policy.

		Task:
		- Based strictly on the HTML policy below, create 5 hard multi-hop questions and exact answers.
		- Return valid JSON only, with this structure:
		  [ {{"question": "...", "answer": "..."}}, {{"question": "...", "answer": "..."}} ]

		Document title: {title}
		Document ID: {document_id}

		HTML policy:
		{html_text}
		"""
	).strip()


def _generate_policy_html(doc_template: dict, *, use_ollama: bool = True) -> str:
	if use_ollama:
		try:
			result = _ollama_generate(_build_policy_prompt(doc_template), temperature=0.8)
			html = _strip_code_fences(result)
			if "<html" in html.lower() and "<table" in html.lower():
				return html
		except Exception as exc:
			print(f"[warn] Ollama HTML generation failed for {doc_template['title']}: {exc}")

	return _fallback_policy_html(
		title=doc_template["title"],
		document_id=doc_template["document_id"],
		frameworks=doc_template["frameworks"],
		domain=doc_template["domain"],
	)


def _generate_ground_truth(html_text: str, title: str, document_id: str, *, use_ollama: bool = True) -> List[Dict[str, str]]:
	if use_ollama:
		try:
			result = _ollama_generate(_build_ground_truth_prompt(html_text, title, document_id), temperature=0.6)
			data = _safe_json_loads(result)
			if isinstance(data, list) and data and all(isinstance(item, dict) for item in data):
				cleaned = []
				for item in data:
					q = str(item.get("question", "")).strip()
					a = str(item.get("answer", "")).strip()
					if q and a:
						cleaned.append({"question": q, "answer": a})
				if cleaned:
					return cleaned[:5]
		except Exception as exc:
			print(f"[warn] Ollama ground-truth generation failed for {document_id}: {exc}")

	return [
		{"question": "Which role is required to use a hardware-backed MFA method and why is this requirement stricter than standard access?", "answer": "The Finance Controller is required to use hardware-backed MFA because the policy sets Tier 3 Restricted access to dual approval and quarterly review, which triggers stricter assurance controls than standard role access."},
		{"question": "Under the document, what is the required response time for a privacy incident or regulator inquiry?", "answer": "The policy requires the DPO and privacy operations team to acknowledge receipt, triage severity, and communicate updates within the legally required notice window, with SLA-bound escalation and remediation timeframes."},
		{"question": "What is the consequence for repeated violations of the standard?", "answer": "Repeat violations may result in temporary access suspension, mandatory retraining, increased audit frequency, and formal disciplinary action."},
		{"question": "Which role has the shortest session timeout, and what is its value?", "answer": "The Finance Controller has a 120-minute session timeout under the Tier 3 Restricted control matrix."},
		{"question": "What are the minimum conditions required for privileged access across the stated matrix?", "answer": "Privileged access requires least-privilege access, multi-factor authentication, documented approval conditions, and time-bounded session management consistent with role criticality and data sensitivity."},
	]


def _render_pdf_from_html(html_text: str, pdf_path: Path) -> bool:
	try:
		from weasyprint import HTML

		HTML(string=html_text).write_pdf(str(pdf_path))
		return True
	except Exception as exc:
		print(f"[warn] PDF rendering failed with WeasyPrint: {exc}")
		return False


def generate_synthetic_dataset(count: int = 5, use_ollama: bool = True):
	OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
	HTML_ROOT.mkdir(parents=True, exist_ok=True)
	PDF_ROOT.mkdir(parents=True, exist_ok=True)
	JSON_ROOT.mkdir(parents=True, exist_ok=True)

	templates = _document_templates()[: max(1, min(count, len(_document_templates())))]
	generated = []

	for template in templates:
		html_text = _generate_policy_html(template, use_ollama=use_ollama)
		document_id = template["document_id"]

		html_path = HTML_ROOT / f"{document_id}.html"
		html_path.write_text(html_text, encoding="utf-8")

		pdf_path = PDF_ROOT / f"{document_id}.pdf"
		pdf_ok = _render_pdf_from_html(html_text, pdf_path)

		qa_pairs = _generate_ground_truth(html_text, template["title"], document_id, use_ollama=use_ollama)
		json_path = JSON_ROOT / f"{document_id}.json"
		payload = {
			"document_id": document_id,
			"title": template["title"],
			"effective_date": "2026-07-01",
			"frameworks": template["frameworks"],
			"operational_domain": template["domain"],
			"questions": qa_pairs,
		}
		json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

		generated.append(
			{
				"document_id": document_id,
				"title": template["title"],
				"html_path": str(html_path),
				"pdf_path": str(pdf_path) if pdf_ok else None,
				"ground_truth_path": str(json_path),
				"question_count": len(qa_pairs),
			}
		)
		print(f"[ok] Generated {document_id} ({len(qa_pairs)} Q/A pairs)")
		time.sleep(0.25)

	manifest_path = OUTPUT_ROOT / "manifest.json"
	manifest_path.write_text(json.dumps({"count": len(generated), "generated_documents": generated}, indent=2, ensure_ascii=False), encoding="utf-8")
	print(f"\n[done] Synthetic dataset complete. Files written to {OUTPUT_ROOT}\n")
	return {"count": len(generated), "generated_documents": generated}


def seed():
	if create_app is None:
		print("[warn] Legacy database seeding is unavailable in this environment.")
		return

	app = create_app("development")
	with app.app_context():
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

		hr_email = "hr@company.com"
		if not User.query.filter_by(email=hr_email).first():
			hr = User(name="HR Manager", email=hr_email, role=UserRole.HR, email_verified=True, is_active=True)
			hr.set_password("HR@1234")
			db.session.add(hr)
			db.session.flush()
			print(f"[ok] HR user created: {hr_email} / HR@1234")
		else:
			hr = User.query.filter_by(email=hr_email).first()

		emp_email = "employee@company.com"
		if not User.query.filter_by(email=emp_email).first():
			emp = User(name="Sample Employee", email=emp_email, role=UserRole.EMPLOYEE, email_verified=True, is_active=True)
			emp.set_password("Emp@1234")
			db.session.add(emp)
			print(f"[ok] Employee created: {emp_email} / Emp@1234")

		db.session.commit()

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
				is_last = i == len(versions) - 1
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

			active_version = next((v for v in policy.versions if v.is_active), None)
			if active_version:
				try:
					from rag.indexing.index_policy import index_policy_version

					result = index_policy_version(policy.id, active_version.id)
					if result.get("success"):
						print(f"      -> indexed {result['chunks']} chunks for AI search")
					else:
						print(f"      -> WARNING: could not index for AI search: {result.get('error')}")
				except Exception as exc:
					print(f"      -> WARNING: could not index for AI search: {exc}")

		print("\n[done] Seed complete. Run: python app.py")
		print("   Open: http://127.0.0.1:5000")
		print(f"   Admin: {Config.DEFAULT_ADMIN_EMAIL} / {Config.DEFAULT_ADMIN_PASSWORD}")
		print("   HR:    hr@company.com / HR@1234")
		print("   Employee: employee@company.com / Emp@1234\n")


def main():
	parser = argparse.ArgumentParser(description="Policy Ledger dataset and seed utility")
	parser.add_argument("--mode", choices=["seed", "synthetic"], default="seed", help="Legacy DB seed or synthetic policy generation")
	parser.add_argument("--count", type=int, default=5, help="Number of synthetic policies to generate")
	parser.add_argument("--no-ollama", action="store_true", help="Use deterministic fallback generation without Ollama")
	args = parser.parse_args()

	if args.mode == "synthetic":
		try:
			requests.get(f"{OLLAMA_URL}/", timeout=3)
		except Exception:
			print("[warn] Ollama is not reachable. Falling back to deterministic synthetic generation.")
			args.no_ollama = True

		generate_synthetic_dataset(count=max(1, args.count), use_ollama=not args.no_ollama)
		return

	seed()


if __name__ == "__main__":
	main()
