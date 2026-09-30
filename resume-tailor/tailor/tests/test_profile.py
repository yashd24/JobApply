"""Profile validation, per-country lookup, and the fill-time answers.

All values below belong to a fictional "Test User" (made-up company, marks, salary, dates, ethnicity...).
Never put real profile values in tests: profile.yaml is gitignored, tests are committed.
"""
import copy
import sys
import unittest
from datetime import date
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jobbot import profile as P  # noqa: E402

EXAMPLE = yaml.safe_load((ROOT / "profile.example.yaml").read_text(encoding="utf-8"))

EMPLOYMENT = {
    "current_company": "Acme Test Co",
    "current_title": "Software Engineer",
    "fulltime_start": "2029-05",
    "zintlr_internship_start": "2028-11",     # key name is part of the schema
    "zintlr_internship_end": "2029-04",
    "serving_notice": True,
    "notice_period_days": 20,
    "last_working_day": date(2030, 8, 20),
    "earliest_start_date": date(2030, 8, 26),
}


def prof(**employment_overrides):
    p = copy.deepcopy(EXAMPLE)
    p["employment"] = {**EMPLOYMENT, **employment_overrides}
    return p


D = date  # shorthand


class Validation(unittest.TestCase):
    def test_example_is_structurally_valid_and_all_todo(self):
        P.validate(copy.deepcopy(EXAMPLE))
        self.assertTrue(P.find_todos(EXAMPLE))
        self.assertEqual(P.find_todos(EXAMPLE)[0], "personal.first_name")

    def test_missing_section_or_key_is_an_error(self):
        bad = copy.deepcopy(EXAMPLE)
        del bad["eeo"]
        with self.assertRaises(P.ProfileError):
            P.validate(bad)
        bad = copy.deepcopy(EXAMPLE)
        del bad["employment"]["last_working_day"]
        with self.assertRaises(P.ProfileError):
            P.validate(bad)

    def test_bad_types_are_errors(self):
        for key, value in [("fulltime_start", "Mar 2029"), ("last_working_day", "20/08/2030"),
                           ("serving_notice", "yes"), ("notice_period_days", "20 days")]:
            with self.assertRaises(P.ProfileError, msg=key):
                P.validate(prof(**{key: value}))

    def test_require_refuses_todo_and_missing(self):
        with self.assertRaises(P.ProfileIncomplete) as cm:
            P.require(EXAMPLE, ["employment.current_company", "nope.nothing"])
        self.assertEqual(cm.exception.paths, ["employment.current_company", "nope.nothing"])
        P.require(prof(), ["employment.current_company"])

    def test_work_auth_unknown_country_is_none_until_default_filled(self):
        p = prof()
        p["work_authorization"] = {"India": {"authorized": True, "needs_sponsorship": False},
                                   "default_other": {"authorized": "TODO", "needs_sponsorship": "TODO"}}
        self.assertEqual(P.work_auth_for(p, "india"), {"authorized": True, "needs_sponsorship": False})
        self.assertIsNone(P.work_auth_for(p, "Germany"))
        p["work_authorization"]["default_other"] = {"authorized": False, "needs_sponsorship": True}
        self.assertEqual(P.work_auth_for(p, "Germany"), {"authorized": False, "needs_sponsorship": True})
        p["work_authorization"]["India"] = {"authorized": "TODO", "needs_sponsorship": "TODO"}
        self.assertEqual(P.work_auth_for(p, "India"), None)   # TODO entry is unknown, not the default


class WorkAuthAndPreferences(unittest.TestCase):
    def setUp(self):
        self.p = prof()
        self.p["work_authorization"] = {"India": {"authorized": True, "needs_sponsorship": False},
                                        "default_other": {"authorized": False, "needs_sponsorship": True}}
        self.p["preferences"] = {"willing_to_relocate": True, "work_modes": ["onsite", "hybrid", "remote"]}

    def test_home_country_and_default_other(self):
        self.assertEqual(P.work_auth_for(self.p, "India"), {"authorized": True, "needs_sponsorship": False})
        for country in ("Germany", "United States", "uk"):
            self.assertEqual(P.work_auth_for(self.p, country), {"authorized": False, "needs_sponsorship": True})

    def test_preferences_validate(self):
        P.validate(self.p)
        self.p["preferences"]["work_modes"] = ["onsite", "office"]
        with self.assertRaises(P.ProfileError):
            P.validate(self.p)
        self.p["preferences"] = {"willing_to_relocate": "yes", "work_modes": ["remote"]}
        with self.assertRaises(P.ProfileError):
            P.validate(self.p)


def edu_prof(**overrides):
    p = prof()
    p["education"] = {
        "degree": "B.Tech in Testing",
        "institution": "Example Institute of Technology", "institution_location": "Testville",
        "affiliation": "Test University", "start_year": 2019, "graduation": "2023-05", "cgpa": "7.9 / 10",
        "percentage": 75,
        "twelfth": {"board": "TESTBOARD", "year": 2019, "percentage": 84.5},
        "tenth": {"board": "TODO", "year": 2017, "percentage": 90.5}, **overrides}
    return p


class Education(unittest.TestCase):
    def test_answers(self):
        p = edu_prof()
        P.validate(p)
        self.assertEqual(P.academic_answer(p, "degree", "cgpa"), "7.9 / 10")
        self.assertEqual(P.academic_answer(p, "degree", "percentage"), "75")
        self.assertEqual(P.academic_answer(p, "degree", "start_year"), "2019")
        self.assertEqual(P.academic_answer(p, "degree", "graduation_year"), "2023")
        self.assertEqual(P.academic_answer(p, "12th", "board"), "TESTBOARD")
        self.assertEqual(P.academic_answer(p, "12th", "percentage"), "84.5")
        self.assertEqual(P.academic_answer(p, "10th", "year"), "2017")

    def test_missing_values_are_flagged_never_guessed(self):
        p = edu_prof(percentage="TODO")
        self.assertIsNone(P.academic_answer(p, "10th", "board"))          # TODO
        self.assertIsNone(P.academic_answer(p, "degree", "percentage"))   # not computed from the CGPA
        self.assertIsNone(P.academic_answer(edu_prof(percentage=""), "degree", "percentage"))

    def test_a_year_in_the_board_field_is_rejected(self):
        with self.assertRaises(P.ProfileError):
            P.validate(edu_prof(tenth={"board": "2017", "year": 2017, "percentage": 90.5}))
        with self.assertRaises(P.ProfileError):
            P.validate(edu_prof(twelfth={"board": "TESTBOARD", "year": "twenty", "percentage": 84.5}))


def eeo_prof(**overrides):
    p = prof()
    p["eeo"] = {"gender": "Female", "ethnicity": "Test Group", "ethnicity_broad": "Test Region",
                "veteran_status": "Not a protected veteran", "disability_status": "No disability",
                "if_no_matching_option": "decline", **overrides}
    return p


class Eeo(unittest.TestCase):
    DECLINE = "Decline to self-identify"

    def test_free_text_returns_the_stored_answer(self):
        self.assertEqual(P.eeo_answer(eeo_prof(), "ethnicity"), "Test Group")
        self.assertEqual(P.eeo_answer(eeo_prof(), "disability_status"), "No disability")

    def test_gender(self):
        opts = ["Female", "Male", "Non-binary", self.DECLINE]
        self.assertEqual(P.eeo_answer(eeo_prof(), "gender", opts), "Female")
        self.assertEqual(P.eeo_answer(eeo_prof(), "gender", ["Woman", "Man", "Other"]), "Woman")
        self.assertEqual(P.eeo_answer(eeo_prof(gender="Male"), "gender", ["Female", "Male"]), "Male")

    def test_ethnicity_falls_back_to_the_broad_category(self):
        opts = ["Group A", "Group B", "Test Region", "American Test Group", "Two or More", self.DECLINE]
        self.assertEqual(P.eeo_answer(eeo_prof(), "ethnicity", opts), "Test Region")

    def test_ethnicity_never_matches_an_american_variant(self):
        # e.g. "Indian" must never select "American Indian or Alaska Native"
        self.assertEqual(P.eeo_answer(eeo_prof(), "ethnicity", ["American Test Group", "Test Group"]),
                         "Test Group")
        self.assertEqual(P.eeo_answer(eeo_prof(ethnicity_broad="TODO"), "ethnicity",
                                      ["American Test Group", self.DECLINE]), self.DECLINE)

    def test_veteran_and_disability(self):
        vet = ["I am a protected veteran", "I am not a protected veteran", "I don't wish to answer"]
        self.assertEqual(P.eeo_answer(eeo_prof(), "veteran_status", vet), "I am not a protected veteran")
        dis = ["Yes, I have a disability (or previously had one)",
               "No, I do not have a disability and have not had one in the past", "I do not want to answer"]
        self.assertEqual(P.eeo_answer(eeo_prof(), "disability_status", dis), dis[1])
        self.assertEqual(P.eeo_answer(eeo_prof(), "disability_status", ["Yes", "No"]), "No")

    def test_a_non_negative_stored_answer_is_never_answered_with_the_no_option(self):
        vet = ["I am a protected veteran", "I am not a protected veteran", "I don't wish to answer"]
        self.assertEqual(P.eeo_answer(eeo_prof(veteran_status="Protected veteran"), "veteran_status", vet),
                         "I don't wish to answer")                 # falls to the decline option, not "not a veteran"
        self.assertIsNone(P.eeo_answer(eeo_prof(veteran_status="Protected veteran", if_no_matching_option="flag"),
                                       "veteran_status", vet))
        self.assertNotEqual(P.eeo_answer(eeo_prof(disability_status="Yes"), "disability_status", ["Yes", "No"]), "No")

    def test_no_matching_option_declines_or_flags(self):
        self.assertEqual(P.eeo_answer(eeo_prof(), "ethnicity", ["Group A", "Group B", self.DECLINE]), self.DECLINE)
        self.assertIsNone(P.eeo_answer(eeo_prof(), "ethnicity", ["Group A", "Group B"]))          # no decline option
        self.assertIsNone(P.eeo_answer(eeo_prof(if_no_matching_option="flag"), "ethnicity",
                                       ["Group A", self.DECLINE]))

    def test_todo_gender_is_never_guessed(self):
        with self.assertRaises(P.ProfileIncomplete):
            P.eeo_answer(eeo_prof(gender="TODO"), "gender", ["Male", "Female"])


class HowDidYouHear(unittest.TestCase):
    def test_platform_to_source(self):
        for platform, expected in [("linkedin", "LinkedIn"), ("Naukri", "Naukri"), ("indeed", "Indeed"),
                                   ("greenhouse", "Company website"), ("lever", "Company website"),
                                   ("company", "Company website")]:
            self.assertEqual(P.how_did_you_hear_answer(platform), expected)
        self.assertIsNone(P.how_did_you_hear_answer("glassdoor"))

    def test_options_are_matched_and_referral_is_never_chosen(self):
        opts = ["Referral", "LinkedIn", "Company Careers Page", "Other"]
        self.assertEqual(P.how_did_you_hear_answer("greenhouse", opts), "Company Careers Page")
        self.assertEqual(P.how_did_you_hear_answer("linkedin", opts), "LinkedIn")
        self.assertIsNone(P.how_did_you_hear_answer("naukri", opts))                  # no Naukri option -> flag
        self.assertIsNone(P.how_did_you_hear_answer("linkedin", ["Referral", "Other"]))

    def test_referrer_questions_are_flagged(self):
        self.assertIsNone(P.how_did_you_hear_answer("linkedin", label="Referrer's name"))
        self.assertIsNone(P.how_did_you_hear_answer("lever", label="Were you referred by an employee?"))

    def test_reason_for_leaving_questions_are_detected(self):
        for q in ["Why are you looking to move?", "Reason for leaving current job",
                  "Why are you leaving Acme Test Co?", "Reason for job change"]:
            self.assertTrue(P.is_reason_for_leaving_question(q), q)
        for q in ["Why do you want to work here?", "Current company", "Notice period"]:
            self.assertFalse(P.is_reason_for_leaving_question(q), q)


class Experience(unittest.TestCase):
    TODAY = D(2030, 7, 15)      # full-time since 2029-05-01 = 14 months; internship 2028-11..2029-04 = 6 months

    def test_full_time_plus_internship_is_default(self):
        self.assertEqual(P.experience_months(prof(), self.TODAY), 14 + 6)
        self.assertEqual(P.experience_years(prof(), self.TODAY), 1.7)
        self.assertEqual(P.experience_years_months(prof(), self.TODAY), (1, 8))

    def test_internship_only_and_fulltime_only(self):
        self.assertEqual(P.internship_months(prof()), 6)
        self.assertEqual(P.experience_months(prof(), self.TODAY, include_internship=False), 14)

    def test_experience_stops_at_last_working_day_while_serving_notice(self):
        after = D(2030, 9, 10)
        self.assertEqual(P.experience_months(prof(), after), 15 + 6)   # 2029-05-01 -> 2030-08-20

    def test_not_serving_notice_keeps_counting(self):
        self.assertEqual(P.experience_months(prof(serving_notice=False), D(2030, 9, 10), include_internship=False), 16)

    def test_dropdown_ranges(self):
        today = self.TODAY   # 1.7 years
        self.assertEqual(P.experience_for_options(prof(), ["0-1 years", "1-3 years", "3-5 years"], today),
                         "1-3 years")
        self.assertEqual(P.experience_for_options(prof(), ["Less than 1 year", "1 year", "2 years", "3+ years"],
                                                  today), "1 year")
        self.assertEqual(P.experience_for_options(prof(), ["0-1", "2-3", "5+"], today), None)   # flag

    def test_dropdown_boundary_prefers_the_half_open_range(self):
        p = prof(serving_notice=False, fulltime_start="2028-07")
        self.assertEqual(P.experience_years(p, self.TODAY, include_internship=False), 2.0)   # exactly 24 months
        self.assertEqual(P.experience_for_options(p, ["1-2 years", "2-3 years"], self.TODAY,
                                                  include_internship=False), "2-3 years")


class Notice(unittest.TestCase):
    TODAY = D(2030, 7, 15)      # 36 days before the last working day (2030-08-20)

    def test_free_text(self):
        self.assertEqual(P.notice_period_answer(prof(), self.TODAY),
                         "Serving notice, last working day 2030-08-20")

    def test_options_prefer_the_stored_notice_period(self):
        self.assertEqual(P.notice_period_answer(prof(), self.TODAY, options=["Immediate", "20 days", "45 days"]),
                         "20 days")

    def test_options_without_it_use_shortest_that_covers_last_working_day(self):
        opts = ["Immediate", "30 days", "60 days", "90 days"]
        self.assertEqual(P.notice_period_answer(prof(), self.TODAY, options=opts), "60 days")
        self.assertEqual(P.notice_period_answer(prof(), self.TODAY, options=["Immediate", "1 week"]), None)

    def test_serving_notice_and_last_working_day(self):
        self.assertEqual(P.serving_notice_answer(prof(), self.TODAY), "Yes")
        self.assertEqual(P.last_working_day_answer(prof(), self.TODAY), "2030-08-20")
        self.assertEqual(P.serving_notice_answer(prof(serving_notice=False), self.TODAY), "No")

    def test_after_last_working_day_notice_questions_are_flagged(self):
        after = D(2030, 8, 21)
        for fn in (P.serving_notice_answer, P.last_working_day_answer, P.notice_period_answer):
            self.assertIsNone(fn(prof(), after))


def comp_prof(report_as="stated_total"):
    p = prof()
    p["compensation"] = {
        "current_ctc": {"fixed_lpa": 5.0, "variable_lpa": 1.5, "stated_total_lpa": 9.0,
                        "stated_total_includes": "allowances", "report_as": report_as},
        "expected_ctc_inr_lpa": 18}
    return p


class Salary(unittest.TestCase):
    def test_current_ctc_uses_the_figure_the_user_chose(self):
        self.assertEqual(P.current_ctc_lpa(comp_prof("fixed")), 5.0)
        self.assertEqual(P.current_ctc_lpa(comp_prof("fixed_plus_variable")), 6.5)
        self.assertEqual(P.current_ctc_lpa(comp_prof("stated_total")), 9.0)

    def test_current_ctc_refuses_until_the_user_picks_a_figure(self):
        with self.assertRaises(P.ProfileIncomplete):
            P.current_ctc_lpa(comp_prof("TODO"))
        with self.assertRaises(P.ProfileError):
            P.validate(comp_prof("everything"))

    def test_stated_total_free_text_shows_the_breakup_numeric_does_not(self):
        p = comp_prof("stated_total")
        self.assertEqual(P.salary_answer(p, "current", "India", free_text=True),
                         ("fill", "9 LPA (5 fixed + 1.5 variable + benefits)"))
        self.assertEqual(P.salary_answer(p, "current", "India"), ("fill", "9"))
        self.assertEqual(P.salary_answer(p, "current", "India", unit="inr"), ("fill", "900000"))

    def test_expected_on_indian_forms(self):
        p = comp_prof()
        self.assertEqual(P.salary_answer(p, "expected", "India", free_text=True), ("fill", "18 LPA (negotiable)"))
        self.assertEqual(P.salary_answer(p, "expected", "India"), ("fill", "18"))
        self.assertEqual(P.salary_answer(p, "expected", "India", unit="inr"), ("fill", "1800000"))

    def test_current_on_indian_forms(self):
        p = comp_prof("fixed_plus_variable")
        self.assertEqual(P.salary_answer(p, "current", "India", free_text=True), ("fill", "6.5 LPA"))
        self.assertEqual(P.salary_answer(p, "current", "india", required=False), ("fill", "6.5"))

    def test_non_indian_forms_flag_expected_always(self):
        p = comp_prof()
        for required in (True, False):
            self.assertEqual(P.salary_answer(p, "expected", "Germany", required=required), ("flag", None))

    def test_non_indian_current_ctc_only_if_required(self):
        p = comp_prof()
        self.assertEqual(P.salary_answer(p, "current", "Germany", required=False), ("blank", None))
        self.assertEqual(P.salary_answer(p, "current", "Germany", required=True, free_text=True),
                         ("fill", "9 LPA INR (5 fixed + 1.5 variable + benefits)"))
        self.assertEqual(P.salary_answer(p, "current", None, required=False), ("blank", None))

    def test_salary_never_guessed_when_profile_value_is_todo(self):
        p = comp_prof()
        p["compensation"]["expected_ctc_inr_lpa"] = "TODO"
        with self.assertRaises(P.ProfileIncomplete):
            P.salary_answer(p, "expected", "India")


class StartDateAndEmployment(unittest.TestCase):
    def test_earliest_start_then_immediately(self):
        self.assertEqual(P.earliest_start_answer(prof(), D(2030, 7, 15)), "2030-08-26")
        self.assertEqual(P.earliest_start_answer(prof(), D(2030, 8, 25)), "2030-08-26")
        self.assertEqual(P.earliest_start_answer(prof(), D(2030, 8, 26)), "Immediately")
        self.assertEqual(P.earliest_start_answer(prof(), D(2031, 1, 1)), "Immediately")

    def test_current_company_stays_the_stored_company(self):
        self.assertEqual(P.current_company_answer(prof()), "Acme Test Co")

    def test_employment_status_questions_are_detected_for_flagging(self):
        for q in ["Are you currently employed?", "Current employment status", "Are you employed",
                  "Are you presently working?"]:
            self.assertTrue(P.is_employment_status_question(q), q)
        for q in ["Current company", "Employer name", "Notice period"]:
            self.assertFalse(P.is_employment_status_question(q), q)


if __name__ == "__main__":
    unittest.main()
