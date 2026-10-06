"""Date-driven change: from the day AFTER profile.employment.last_working_day the current job is in the past.
Resume dates close, cover letters use the past tense, and the bullets are checked for present-tense wording.
Every test uses made-up dates (read from a profile), never the real one, and checks both sides of the boundary."""
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("JOBBOT_NO_SHEET", "1")      # a test must never reach the real Google Sheet
sys.path.insert(0, str(ROOT / "tests"))

import persona  # noqa: E402
import render  # noqa: E402
import tailor  # noqa: E402
from jobbot import coverletter as C  # noqa: E402
from jobbot import profile as P  # noqa: E402
from jobbot import tense  # noqa: E402
from jobbot.fields import Field  # noqa: E402
from test_coverletter import CFG, GOOD, JD, QUOTES, RESUME  # noqa: E402

LWD = date(2031, 3, 5)                     # made up: the date comes from the profile, never from the code
DAY_AFTER = LWD + timedelta(days=1)
COMPANY = "Acme Test Co"                   # persona's current employer
ENDED = (COMPANY, LWD)


def profile(lwd=LWD, serving=True, company=COMPANY):
    return {"employment": {"current_company": company, "serving_notice": serving, "last_working_day": lwd}}


class Boundary(unittest.TestCase):
    def test_the_job_ends_the_day_after_the_last_working_day(self):
        self.assertIsNone(P.employment_end(profile(), LWD - timedelta(days=30)))
        self.assertIsNone(P.employment_end(profile(), LWD - timedelta(days=1)))
        self.assertIsNone(P.employment_end(profile(), LWD))                      # the last day itself is still employed
        self.assertEqual(P.employment_end(profile(), DAY_AFTER), LWD)
        self.assertEqual(P.employment_end(profile(), LWD + timedelta(days=400)), LWD)

    def test_the_date_comes_from_the_profile_not_the_code(self):
        other = date(2040, 1, 31)
        self.assertIsNone(P.employment_end(profile(other), other))
        self.assertEqual(P.employment_end(profile(other), other + timedelta(days=1)), other)
        self.assertEqual(P.employment_end(profile("2040-01-31"), date(2040, 2, 1)), other)       # a string date works

    def test_nothing_ends_without_a_usable_date_or_when_not_serving_notice(self):
        after = DAY_AFTER + timedelta(days=100)
        for p in (profile(serving=False), profile("TODO"), profile(None), {}, None, {"employment": {}}):
            self.assertIsNone(P.employment_end(p, after), p)

    def test_the_apply_helper_returns_company_and_date(self):
        import apply
        self.assertIsNone(apply.ended_job(profile(), LWD))
        self.assertEqual(apply.ended_job(profile(), DAY_AFTER), ENDED)
        self.assertEqual(apply.ended_job(persona.profile(), date(2030, 8, 21))[1], date(2030, 8, 20))
        self.assertIsNone(apply.ended_job(persona.profile(), date(2030, 8, 20)))


class ResumeDates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="employment end ")
        self.dir = Path(self.tmp.name)
        (self.dir / "contact.yaml").write_text('email: test.user@example.com\nphone: "+00 000 000 0000"\n', encoding="utf-8")
        self.profile_file = self.dir / "profile.yaml"

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, today, prof=None):
        if prof is not None:
            self.profile_file.write_text(yaml.safe_dump(prof), encoding="utf-8")
        with mock.patch.object(tailor, "CONTACT_FILE", self.dir / "contact.yaml"), \
                mock.patch.object(tailor, "PROFILE_FILE", self.profile_file):
            return tailor.load_resume_data(today)

    def current_entry(self, data):
        return next(e for e in data["experience"] if e["company"] == "Zintlr")

    def test_present_until_the_last_working_day_then_the_end_month(self):
        prof = profile(company="Zintlr")
        for today in (LWD - timedelta(days=60), LWD):
            self.assertEqual(self.current_entry(self.load(today, prof))["dates"], "Aug 2025 - Present", today)
        data = self.load(DAY_AFTER)
        self.assertEqual(self.current_entry(data)["dates"], "Aug 2025 - Mar 2031")
        self.assertEqual(self.current_entry(self.load(LWD + timedelta(days=500)))["dates"], "Aug 2025 - Mar 2031")

    def test_a_different_last_working_day_gives_a_different_month(self):
        prof = profile(date(2032, 11, 30), company="Zintlr")
        self.assertEqual(self.current_entry(self.load(date(2032, 12, 1), prof))["dates"], "Aug 2025 - Nov 2032")

    def test_the_rendered_resume_shows_the_end_month_and_no_present(self):
        data = self.load(DAY_AFTER, profile(company="Zintlr"))
        tex = render.render_tex(data, render.default_plan(data))
        self.assertIn("Aug 2025", tex)
        self.assertIn("Mar 2031", tex)
        self.assertNotIn("Present", tex)
        before = self.load(LWD, profile(company="Zintlr"))
        self.assertIn("Present", render.render_tex(before, render.default_plan(before)))

    def test_other_jobs_are_untouched(self):
        base = self.load(LWD, profile(company="Zintlr"))
        after = self.load(DAY_AFTER)
        changed = [(a["company"], a["dates"]) for a, b in zip(after["experience"], base["experience"]) if a["dates"] != b["dates"]]
        self.assertEqual(changed, [("Zintlr", "Aug 2025 - Mar 2031")])

    def test_no_change_without_a_matching_company_a_profile_or_a_valid_date(self):
        self.assertEqual(self.current_entry(self.load(DAY_AFTER, profile(company="Somewhere Else")))["dates"],
                         "Aug 2025 - Present")
        self.assertEqual(self.current_entry(self.load(DAY_AFTER, profile("TODO", company="Zintlr")))["dates"],
                         "Aug 2025 - Present")
        self.profile_file.unlink()
        self.assertEqual(self.current_entry(self.load(DAY_AFTER))["dates"], "Aug 2025 - Present")
        self.profile_file.write_text(": not: valid: yaml: [", encoding="utf-8")
        self.assertEqual(self.current_entry(self.load(DAY_AFTER))["dates"], "Aug 2025 - Present")


PRESENT_LETTER = GOOD.replace("At Acme Test Co I built a payments API", "At Acme Test Co I currently build a payments API")


class CoverLetterTense(unittest.TestCase):
    def problems(self, text, ended=None):
        return [p for p in C.check_letter(text, RESUME, CFG, JD, QUOTES, ended=ended) if "tense" in p or "current" in p]

    def test_before_the_end_the_present_tense_is_fine(self):
        self.assertEqual(self.problems(PRESENT_LETTER, None), [])
        self.assertEqual(self.problems(GOOD, None), [])

    def test_after_the_end_present_tense_and_current_position_wording_are_rejected(self):
        for text in (PRESENT_LETTER,
                     GOOD.replace("At Acme Test Co I built", "In my current position at Acme Test Co I built"),
                     GOOD.replace("At Acme Test Co I built", "I work at Acme Test Co, where I built"),
                     GOOD.replace("I also cut a nightly", "I own a nightly report and I also cut a nightly"),
                     GOOD.replace("I also cut a nightly", "I am currently cutting a nightly"),
                     GOOD.replace("At Acme Test Co I built", "At Acme Test Co I lead a team that built"),
                     GOOD.replace("At Acme Test Co I built", "At Acme Test Co, I'm responsible for a payments API that I built")):
            found = self.problems(text, ENDED)
            self.assertTrue(found, text[:200])
            self.assertIn("ended on 5 Mar 2031", found[0])

    def test_after_the_end_a_past_tense_letter_passes(self):
        self.assertEqual(self.problems(GOOD, ENDED), [])
        past = GOOD.replace("At Acme Test Co I built", "At Acme Test Co I owned the release plan and built")
        self.assertEqual(self.problems(past, ENDED), [])
        self.assertEqual(C.check_letter(GOOD, RESUME, CFG, JD, QUOTES, ended=ENDED), [])

    def test_skills_and_intent_in_the_present_are_not_flagged(self):
        text = GOOD.replace("I can share more detail", "I work with Python daily and can share more detail")
        self.assertEqual(self.problems(text, ENDED), [])

    def test_the_prompt_demands_the_past_tense_only_after_the_end(self):
        after = C.build_prompt("Acme", "Backend", JD, RESUME, CFG, ["Hi,\n\nHello."], ended=ENDED)
        before = C.build_prompt("Acme", "Backend", JD, RESUME, CFG, ["Hi,\n\nHello."])
        for needle in ("ENDED on 5 Mar 2031", "PAST tense", f"At {COMPANY}, I owned", "Never call it the current or present"):
            self.assertIn(needle, after)
        self.assertNotIn("TENSE:", before)
        self.assertNotIn("what you do now", after + before)

    def test_the_provider_rejects_a_present_tense_letter_after_the_end_and_accepts_it_before(self):
        def run(ended, letter):
            with tempfile.TemporaryDirectory() as tmp:
                prov = C.make_provider(Path(tmp) / "job", "Acme", "Backend", JD, RESUME,
                                       lambda prompt: {"letter": letter, "jd_quotes": QUOTES}, cfg=CFG,
                                       samples=["Hi,\n\nHello."], history_path=Path(tmp) / "h.json", model="", ended=ended)
                return prov(Field("cl", "Cover Letter", "textarea", False))
        value, note = run(ENDED, PRESENT_LETTER)
        self.assertIsNone(value)
        self.assertIn("past tense", note)
        self.assertIsNotNone(run(None, PRESENT_LETTER)[0])
        self.assertIsNotNone(run(ENDED, GOOD)[0])


class BulletTense(unittest.TestCase):
    def test_present_tense_wording_is_found(self):
        for text, expect in ((r"Owns the \textbf{release} process end to end.", "Owns"),
                             ("Manage a team of 2 developers.", "Manage"),
                             ("Built a service which handles 80,000 calls per week.", "which handles"),
                             ("Built a service that serves clients, currently in use.", "currently"),
                             ("Is responsible for the deployments.", "responsible")):
            problems = tense.bullet_problems(text)
            self.assertTrue(problems, text)
            self.assertIn(expect, " ".join(problems))

    def test_past_tense_bullets_pass(self):
        for text in (r"Led the \textbf{B2B2B partner API platform} (\textbf{80,000+ API calls/week}).",
                     "Built an AI-powered pipeline.", "Acted as the point of contact.", "Owned release planning.",
                     "Designed and implemented rate limiting middleware."):
            self.assertEqual(tense.bullet_problems(text), [], text)

    def test_only_the_ended_roles_bullets_are_checked_and_edits_count(self):
        resume = {"experience": [
            {"company": "Old Co", "bullets": [{"id": "o1", "text": "Manages the old thing."}]},
            {"company": COMPANY, "bullets": [{"id": "a1", "text": "Built the API."},
                                             {"id": "a2", "text": "Owns the report.", "default": False}]}]}
        found = tense.resume_problems(resume, COMPANY)
        self.assertEqual(len(found), 1)
        self.assertIn("a2", found[0])
        self.assertEqual(tense.resume_problems(resume, COMPANY, {"a2": "Owned the report."}), [])
        edited = tense.resume_problems(resume, COMPANY, {"a1": "Builds the API."})          # a keyword edit went present
        self.assertEqual(len(edited), 2)
        self.assertIn("a1", " ".join(edited))

    def test_the_real_base_resumes_ended_role_reads_as_past_tense(self):
        data = tailor.yaml.safe_load(tailor.DATA_FILE.read_text(encoding="utf-8"))
        self.assertEqual(tense.resume_problems(data, "Zintlr"), [])


if __name__ == "__main__":
    unittest.main()
