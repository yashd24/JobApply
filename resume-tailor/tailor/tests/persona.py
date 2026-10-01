"""A fictional "Test User" (profile + resume) for tests. No real personal data: never paste real values here."""
import copy
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TODAY = date(2030, 7, 15)

_EXAMPLE = yaml.safe_load((ROOT / "profile.example.yaml").read_text(encoding="utf-8"))


def profile() -> dict:
    p = copy.deepcopy(_EXAMPLE)
    p["personal"] = {"first_name": "Test", "last_name": "User", "email": "test.user@example.com",
                     "phone": "+00 000 000 0000",
                     "current_location": {"city": "Testville", "state": "Teststate", "country": "India"},
                     "linkedin": "https://www.linkedin.com/in/test-user",
                     "github": "https://github.com/test-user", "portfolio": ""}
    p["employment"] = {"current_company": "Acme Test Co", "current_title": "Software Engineer",
                       "fulltime_start": "2029-05", "zintlr_internship_start": "2028-11",
                       "zintlr_internship_end": "2029-04", "serving_notice": True, "notice_period_days": 20,
                       "last_working_day": date(2030, 8, 20), "earliest_start_date": date(2030, 8, 26)}
    p["compensation"] = {"current_ctc": {"fixed_lpa": 5.0, "variable_lpa": 1.5, "stated_total_lpa": 9.0,
                                         "stated_total_includes": "allowances", "report_as": "stated_total"},
                         "expected_ctc_inr_lpa": 18}
    p["work_authorization"] = {"India": {"authorized": True, "needs_sponsorship": False},
                               "default_other": {"authorized": False, "needs_sponsorship": True}}
    p["preferences"] = {"willing_to_relocate": True, "work_modes": ["onsite", "hybrid", "remote"]}
    p["education"] = {"degree": "B.Tech in Testing", "institution": "Example Institute of Technology",
                      "institution_location": "Testville", "affiliation": "Test University", "start_year": 2019,
                      "graduation": "2023-05", "cgpa": "7.9 / 10", "percentage": 75,
                      "twelfth": {"board": "TESTBOARD", "year": 2019, "percentage": 84.5},
                      "tenth": {"board": "TESTBOARD", "year": 2017, "percentage": 90.5}}
    p["eeo"] = {"gender": "Female", "ethnicity": "Test Group", "ethnicity_broad": "Test Region",
                "veteran_status": "Not a protected veteran", "disability_status": "No disability",
                "if_no_matching_option": "decline"}
    p["skill_years"] = {"python": 1.5, "django": 1.0, "postgresql": 1.2, "docker": 0.8}
    p["custom_answers"] = {}
    return p


def resume() -> dict:
    return {
        "name": "Test User", "email": "test.user@example.com", "phone": "+00 000 000 0000",
        "linkedin": "test-user", "github": "test-user",
        "education": {"degree": "B.Tech in Testing", "institute": "Example Institute of Technology",
                      "location": "Testville", "date": "May 2023", "cgpa": "7.9 / 10"},
        "skills": [{"label": "Languages:", "items": ["Python", "Java", "SQL"]},
                   {"label": "Databases:", "items": ["PostgreSQL", "MongoDB"]},
                   {"label": "Cloud & Tools:", "items": ["AWS", "Docker", "Git"]}],
        "experience": [{
            "id": "acme", "company": "Acme Test Co", "position": "Software Engineer", "location": "Testville",
            "dates": "Mar 2029 - Present", "required": True,
            "bullets": [
                {"id": "a1", "default": True,
                 "text": "Built a payments API serving 50,000+ requests per week using Python and PostgreSQL."},
                {"id": "a2", "default": True,
                 "text": "Cut the nightly report from 4 hours to 20 minutes with Celery workers."},
                {"id": "a3", "default": False,
                 "text": "Hidden bullet about Kubernetes and Terraform that must never be used."}]}],
        "projects": [{"id": "p1", "name": "Test Project", "stack": "Django, Redis", "default": True,
                      "bullets": [{"id": "p1a", "default": True,
                                   "text": "Built a job board with Django and Redis caching."}]}],
        "cocurricular": [],
    }
