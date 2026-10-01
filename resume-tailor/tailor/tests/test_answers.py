"""M4 answer pipeline on the saved real fixtures + a fictional persona. No network, no browser, no LLM."""
import json
import sys
import unittest
import unittest.mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import persona  # noqa: E402
from jobbot import answers as A  # noqa: E402
from jobbot import intake as I  # noqa: E402
from jobbot.fields import Field, fields_from_questions  # noqa: E402

REAL = ROOT / "tests" / "fixtures" / "real"


def load_job(name):
    d = json.loads((REAL / name / "urls.json").read_text(encoding="utf-8"))
    rs = {u: t for u, t in d["responses"].items() if t}
    return I.fetch_job(d["input"], http=lambda u: (200, rs[u]) if u in rs else (404, ""))


def ctx(job=None, llm=None, **kw):
    base = dict(profile=persona.profile(), resume=persona.resume(), today=persona.TODAY,
                company=job.company if job else "Acme", role=job.role if job else "Engineer",
                job_location=job.location if job else "Testville, India", platform=job.platform if job else "lever",
                jd_text=job.jd_text if job else "Build Python APIs.", resume_pdf=Path("resume.pdf"), llm=llm)
    base.update(kw)
    return A.Context(**base)


def by_label(answers, start):
    hits = [a for a in answers if a.label.lower().startswith(start.lower())]
    assert len(hits) == 1, (start, [a.label for a in answers])
    return hits[0]


def f(label, type="text", required=True, options=None, group="standard", key=None, multiple=False):
    return Field(key=key or label, label=label, type=type, required=required, options=options or [],
                 group=group, multiple=multiple)


class Classify(unittest.TestCase):
    def test_labels(self):
        cases = {
            "Full name": "name_full", "First Name": "name_first", "Last Name": "name_last", "Email": "email",
            "Phone": "phone", "LinkedIn URL": "linkedin", "Github URL": "github", "Other Website URL": "website",
            "Current company": "company", "Current Job Title": "title", "Current location": "location",
            "Location (City)": "city", "Country": "country",
            "What is your Current CTC?": "salary_current", "What is your Expected CTC?": "salary_expected",
            "Expected Fixed/Base Salary in your local currency": "salary_expected",
            "Notice Period": "notice_period", "Are you serving notice?": "serving_notice",
            "Last working day?": "last_working_day", "How soon are you able to join us?": "start_date",
            "Willingness to relocate": "relocation",
            "Will you require Commvault to sponsor a work visa?": "sponsorship",
            "Do you have the legal right to work in the country where you are applying?": "work_auth",
            "How did you hear about us?": "how_heard", "Were you referred by someone currently working at X?": "referral",
            "What gender do you identify as?": "eeo_gender", "I identify my ethnicity as...": "eeo_ethnicity",
            "Are you a protected veteran?": "eeo_veteran", "Do you have a disability or chronic condition?": "eeo_disability",
            "What is your age range?": "eeo_other", "Pronouns": "pronouns",
            "What is your total years of experience (excluding internship)?": "experience_total",
            "# of years with Python or Python-based backend frameworks": "tool_years",
            "How many years of experience do you have with Django?": "tool_years",
            "Do you have at least 4 yrs of production system experience in a B2B SaaS startup?": "tool_years",
            "Please tick mark all the tech stacks you are hands on with.": "tech_stack",
            "From which college did you graduate?": "institution", "Cover Letter": "cover_letter",
            "Why are you looking to move?": "reason_leaving", "Are you currently employed?": "employment_status",
            "Do you currently work for, or have you previously worked for Zscaler?": "history_flag",
            "Home Address": "address", "Why do you want to work here?": "llm",
            "Describe a project where you used Redis.": "llm",
            "Have you worked closely with Product, Data Science or Sales teams?": "llm",
        }
        for label, expected in cases.items():
            ftype = "checkbox" if expected == "tech_stack" else "file" if label == "Cover Letter" else "text"
            opts = ["a", "b", "c"] if ftype == "checkbox" else []
            self.assertEqual(A.classify(f(label, ftype, options=opts)), expected, label)

    def test_regressions_found_by_the_fixtures(self):
        # "Preferred ..." contains the letters "referr"; it is not a referral question
        self.assertEqual(A.classify(f("Preferred stack?")), "tech_stack")
        self.assertEqual(A.classify(f("Preferred name")), "name_first")
        self.assertEqual(A.classify(f("Were you referred by an employee?")), "referral")
        # "currently working" inside a referral question must not make it an employment-status question
        self.assertEqual(A.classify(f("Were you referred by someone currently working at Acme?")), "referral")
        # "work visa" inside an eligibility question is about eligibility, not sponsorship
        self.assertEqual(A.classify(f("Are you eligible to work in the country for which this role is located "
                                      "(for example, as a citizen or holder of a valid work visa)?")), "work_auth")
        self.assertEqual(A.classify(f("Do you require a work permit, visa or additional right to work support?")),
                         "sponsorship")
        # legal questions are never generated
        for label in ("Do you have any criminal convictions?", "Are you subject to a non-compete agreement?",
                      "Do you hold a security clearance?", "Do you have a conflict of interest?"):
            self.assertEqual(A.classify(f(label)), "legal_flag", label)

    def test_files_and_consents(self):
        self.assertEqual(A.classify(f("ATTACH RESUME/CV", "file")), "resume")
        self.assertEqual(A.classify(f("Upload your transcript", "file")), "file_other")
        self.assertEqual(A.classify(f("Zscaler Privacy Policy", "multiselect", options=["Acknowledge"])), "consent")
        self.assertEqual(A.classify(f("x", "checkbox", options=["Yes, X can contact me"], group="consent")), "consent")

    def test_known_technology_makes_an_experience_question_tool_specific(self):
        self.assertEqual(A.classify(f("Total years of experience in Python")), "tool_years")


class GushworkForm(unittest.TestCase):
    """The form from the M4 brief, run through the pipeline with the fictional persona."""

    @classmethod
    def setUpClass(cls):
        cls.job = load_job("lv_gushwork")
        cls.fields = fields_from_questions(cls.job.questions)
        cls.calls = []
        cls.answers = A.answer_fields(cls.fields, ctx(cls.job, llm=lambda p: cls.calls.append(p) or {"answers": {}}))

    def test_every_field_has_an_answer_and_nothing_goes_to_the_llm(self):
        self.assertEqual([a.key for a in self.answers], [x.key for x in self.fields])
        self.assertEqual(self.calls, [])

    def test_standard_fields_come_from_the_profile(self):
        expect = {"Full name": "Test User", "Email": "test.user@example.com", "Phone": "+00 000 000 0000",
                  "Current location": "Testville, Teststate, India", "Current company": "Acme Test Co",
                  "LinkedIn URL": "https://www.linkedin.com/in/test-user",
                  "From which college did you graduate?": "Example Institute of Technology",
                  "What is your current location?": "Testville, Teststate, India"}
        for label, value in expect.items():
            a = by_label(self.answers, label)
            self.assertEqual((a.value, a.status), (value, A.FILLED), label)
            self.assertTrue(a.source.startswith("profile:"), (label, a.source))

    def test_resume_is_the_tailored_pdf(self):
        a = by_label(self.answers, "Resume/CV")
        self.assertEqual((a.value, a.source, a.category), ("resume.pdf", "tailor:resume pdf", "resume"))

    def test_sensitive_answers_come_from_the_profile_with_sources(self):
        self.assertEqual(by_label(self.answers, "What is your Current CTC").value,
                         "9 LPA (5 fixed + 1.5 variable + benefits)")
        self.assertEqual(by_label(self.answers, "What is your Expected CTC").value, "18 LPA (negotiable)")
        notice = by_label(self.answers, "Notice Period")
        self.assertEqual(notice.value, "Serving notice, last working day 20 Aug 2030")
        self.assertIn("profile:", notice.source)

    def test_experience_question_that_excludes_internships_overrides_the_default(self):
        a = by_label(self.answers, "What is your total years of experience")
        self.assertEqual(a.value, "1.2 years")                 # 14 full-time months
        self.assertIn("full-time only", a.source)
        both = A.answer_fields([f("Total years of experience?")], ctx(self.job))[0]
        self.assertEqual(both.value, "1.7 years")              # 14 + 6 months, the default
        self.assertIn("full-time + internship", both.source)

    def test_tech_stack_ticks_only_what_the_visible_resume_supports(self):
        a = by_label(self.answers, "Please tick mark all the tech stacks")
        self.assertEqual(sorted(a.value), ["AWS", "Java", "MongoDB", "Postgresql", "Python"])
        for unsupported in ("ReactJS", "NextJS", "NodeJS", "DynamoDB"):
            self.assertNotIn(unsupported, a.value)

    def test_referral_is_flagged_and_left_empty(self):
        a = by_label(self.answers, "Were you referred")
        self.assertEqual((a.status, a.value, a.required), (A.FLAGGED, None, False))

    def test_hidden_bullets_never_back_a_tech_stack_answer(self):
        r = persona.resume()                                   # a3 (hidden) mentions Kubernetes and Terraform
        a = A.answer_fields([f("Tick the technologies you know", "checkbox", options=["Kubernetes", "Terraform", "Python"])],
                            ctx(resume=r))[0]
        self.assertEqual(a.value, ["Python"])


class EveryFieldOfEveryFixtureIsAnswered(unittest.TestCase):
    """Plan M4 done-when: every field is filled or flagged (or deliberately blank), never silently skipped."""

    def test_all_real_fixtures(self):
        names = sorted(p.name for p in REAL.iterdir() if (p / "urls.json").exists())
        self.assertGreaterEqual(len(names), 9)
        for name in names:
            with self.subTest(fixture=name):
                job = load_job(name)
                fields = fields_from_questions(job.questions)
                answers = A.answer_fields(fields, ctx(job, llm=lambda p: {"answers": {}}))
                self.assertEqual([a.key for a in answers], [x.key for x in fields])
                for a in answers:
                    self.assertIn(a.status, (A.FILLED, A.FLAGGED, A.BLANK))
                    self.assertTrue(a.source)
                    if a.status == A.FILLED:
                        self.assertNotIn(a.value, (None, "", []), a.label)
                    if a.required and a.status == A.BLANK:
                        self.fail(f"required field left blank: {a.label}")
                    if a.category in A.SENSITIVE or a.category == "consent":
                        self.assertNotEqual(a.source, "llm", a.label)
                    if a.category == "consent" and a.status == A.FILLED:          # Option A: only these may be ticked
                        self.assertTrue(a.required, a.label)
                        self.assertRegex(a.label + " " + " ".join(a.value), r"(?i)privacy|data|gdpr")
                        self.assertNotRegex(a.label, r"(?i)market|future|contact me|confidential")

    def test_non_indian_form_salary_and_currency(self):
        job = load_job("lv_kwalee_eu")                        # England, salaries in GBP
        answers = A.answer_fields(fields_from_questions(job.questions), ctx(job, llm=lambda p: {"answers": {}}))
        for label in ("Current gross salary", "Expected annual gross salary"):
            a = by_label(answers, label)
            self.assertEqual((a.status, a.value), (A.FLAGGED, None), label)
        sponsor = by_label(answers, "Do you currently have the right to work in the UK")
        self.assertEqual(sponsor.status, A.FLAGGED)           # 'without requiring sponsorship': ambiguous polarity

    def test_greenhouse_options_are_matched(self):
        job = load_job("lv_portcast") if False else load_job("gh_commvault")
        answers = A.answer_fields(fields_from_questions(job.questions), ctx(job, llm=lambda p: {"answers": {}}))
        self.assertEqual(by_label(answers, "Willingness to relocate").value, "Yes")
        self.assertEqual(by_label(answers, "Will you require Commvault").value, "No")
        self.assertEqual(by_label(answers, "Are you eligible to work in the country").value, "Yes")
        heard = by_label(answers, "How did you hear about us")
        self.assertIn(heard.status, (A.FILLED, A.FLAGGED))
        self.assertNotIn("referral", str(heard.value).lower())

    def test_demographic_section_uses_the_eeo_profile_or_declines(self):
        job = load_job("gh_backbase_eeo")
        answers = A.answer_fields(fields_from_questions(job.questions), ctx(job, llm=lambda p: {"answers": {}}))
        demo = [a for a in answers if a.category.startswith("eeo")]
        self.assertGreaterEqual(len(demo), 3)
        for a in demo:
            self.assertTrue(a.source.startswith("profile:eeo"), a.label)
            fld = next(x for x in fields_from_questions(job.questions) if x.key == a.key)
            if a.status == A.FILLED and fld.type == "multiselect":
                self.assertIsInstance(a.value, list)          # multi-select questions take a list of options

    def test_cover_letter_field_uses_the_generator_or_is_left_empty(self):
        job = load_job("gh_zscaler_coverletter")
        fields = fields_from_questions(job.questions)
        off = A.answer_fields(fields, ctx(job, llm=lambda p: {"answers": {}}))
        self.assertEqual(by_label(off, "Cover Letter").status, A.FLAGGED)          # no generator configured
        ok = A.answer_fields(fields, ctx(job, llm=lambda p: {"answers": {}},
                                         cover_letter=lambda fld: ("letter.pdf", "ok")))
        self.assertEqual((by_label(ok, "Cover Letter").value, by_label(ok, "Cover Letter").status),
                         ("letter.pdf", A.FILLED))
        bad = A.answer_fields(fields, ctx(job, llm=lambda p: {"answers": {}},
                                          cover_letter=lambda fld: (None, "banned phrase")))
        self.assertEqual(by_label(bad, "Cover Letter").status, A.BLANK)            # optional: stays empty
        req = A.answer_fields([f("Cover Letter", "file", required=True)], ctx(job, cover_letter=lambda fld: (None, "x")))[0]
        self.assertEqual(req.status, A.FLAGGED)
        self.assertIn("assist", req.note)


class FoundByTheLiveGreenhouseRun(unittest.TestCase):
    def test_an_upload_whose_label_is_just_attach_is_recognised_by_its_key(self):
        self.assertEqual(A.classify(Field("resume", "Attach", "file", True)), "resume")
        self.assertEqual(A.classify(Field("cover_letter", "Attach", "file", False)), "cover_letter")
        self.assertEqual(A.classify(Field("transcript", "Attach", "file", False)), "file_other")
        a = A.answer_fields([Field("resume", "Attach", "file", True)], ctx())[0]
        self.assertEqual((a.status, a.value, a.category), (A.FILLED, "resume.pdf", "resume"))

    def test_if_yes_follow_ups_after_a_no_are_blank_not_flagged_and_never_sent_to_the_llm(self):
        sent = []
        c = ctx(llm=lambda p: sent.append(p) or {"answers": {}})
        fields = [f("Will you require visa sponsorship?", "select", options=["Yes", "No"]),
                  f("If yes, please confirm which type of visa would be required", required=False)]
        out = A.answer_fields(fields, c)
        self.assertEqual(out[0].value, "No")                                   # India: no sponsorship needed
        self.assertEqual((out[1].status, out[1].category, out[1].value), (A.BLANK, "followup", None))
        self.assertEqual(sent, [])

    def test_if_yes_follow_ups_after_a_yes_or_an_unanswered_question_are_flagged(self):
        c = ctx(llm=lambda p: {"answers": {}})
        c.job_location = "Berlin, Germany"                                       # default_other: needs sponsorship = Yes
        out = A.answer_fields([f("Will you require visa sponsorship?", "select", options=["Yes", "No"]),
                               f("If yes, please confirm which type of visa would be required")], c)
        self.assertEqual((out[0].value, out[1].status), ("Yes", A.FLAGGED))
        first = A.answer_fields([f("If yes, please explain")], ctx())[0]          # nothing before it
        self.assertEqual(first.status, A.FLAGGED)
        flagged_before = A.answer_fields([f("Do you have a family member who works here?", "select", options=["Yes", "No"]),
                                          f("If yes, please provide the name")], ctx())
        self.assertEqual([a.status for a in flagged_before], [A.FLAGGED, A.FLAGGED])

    def test_a_question_that_mentions_github_is_not_the_github_profile_field(self):
        q = f("Do you have at least 6 months of experience using GitHub Copilot?", "select", options=["Yes", "No"])
        self.assertEqual(A.classify(q), "llm")
        self.assertEqual(A.classify(f("Do you have a LinkedIn profile?", "radio", options=["Yes", "No"])), "llm")
        self.assertEqual(A.classify(f("GitHub URL")), "github")
        self.assertEqual(A.classify(f("Link to your GitHub profile")), "github")
        self.assertEqual(A.classify(f("What is your name?")), "name_full")
        out = A.answer_fields([q], ctx(llm=lambda p: {"answers": {}}))[0]
        self.assertNotIn("github.com", str(out.value))

    def test_nationality_and_work_status_questions_are_legal_and_never_reach_the_llm(self):
        sent = []
        c = ctx(llm=lambda p: sent.append(p) or {"answers": {}})
        labels = ["Are you an India national?", "What is your working status in India?", "Nationality",
                  "Do you hold a valid passport?", "Are you a citizen of this country?"]
        out = A.answer_fields([f(l, "select", options=["Yes", "No"]) for l in labels], c)
        self.assertEqual([a.category for a in out], ["legal_flag"] * len(labels))
        self.assertEqual([a.status for a in out], [A.FLAGGED] * len(labels))
        self.assertEqual(sent, [])

    def test_previously_applied_is_employment_history(self):
        for label in ("Have you previously applied to a role at EarnIn?", "Have you ever applied to Acme before?"):
            self.assertEqual(A.classify(f(label, "select", options=["Yes", "No"])), "history_flag", label)

    def test_a_choice_field_is_never_filled_with_a_value_that_is_not_an_option(self):
        c = ctx()
        # an identity answer landing on a dropdown (the GitHub URL on a yes/no select) must be flagged, not filled
        weird = Field("x", "Please share your LinkedIn", "select", True, ["Yes", "No"])
        with unittest.mock.patch.object(A, "classify", lambda fld: "linkedin"):
            a = A.answer_fields([weird], c)[0]
        self.assertEqual((a.status, a.value), (A.FLAGGED, None))
        self.assertIn("not one of this question's options", a.note)

    def test_a_follow_up_that_names_an_option_only_applies_to_that_option(self):
        heard = f("How did you learn about this job?", "select", options=["Referral", "Careers Page", "Other"])
        follow = f("If you selected Other please tell us how you learned about this job.", required=False)
        c = ctx(platform="greenhouse", llm=lambda p: {"answers": {}})
        out = A.answer_fields([heard, follow], c)
        self.assertEqual(out[0].value, "Careers Page")
        self.assertEqual((out[1].status, out[1].category), (A.BLANK, "followup"))
        self.assertIn("not 'Other'", out[1].note)
        # but if the previous answer is unknown (here a free-text question), the user answers
        unknown = A.answer_fields([f("Tell us something", "textarea", required=False), follow], ctx(llm=lambda p: {"answers": {}}))
        self.assertEqual(unknown[1].status, A.FLAGGED)

    def test_follow_up_detection(self):
        for label in ("If yes, please provide the name", "If so, which one?", "When applicable, list them",
                      "If you answered yes, explain"):
            self.assertEqual(A.classify(f(label)), "followup", label)
        for label in ("What is your name?", "Notice period, if any", "Why do you want to work here?"):
            self.assertNotEqual(A.classify(f(label)), "followup", label)


class FactualYesNoNeedsHighConfidence(unittest.TestCase):
    """Decided by the user: a factual yes/no answer below high confidence is flagged, not filled."""

    def run_llm(self, field, reply):
        return A.answer_fields([field], ctx(llm=lambda p: {"answers": {field.key: reply}}))[0]

    def yes_no(self, label="Have you worked with high-volume data?"):
        return f(label, "radio", options=["No", "Yes"])

    def test_medium_and_low_confidence_yes_no_are_flagged(self):
        for conf in ("medium", "low", "MEDIUM", "unsure", ""):
            a = self.run_llm(self.yes_no(), {"answer": "Yes", "confidence": conf, "based_on": ["a1"]})
            self.assertEqual((a.status, a.value), (A.FLAGGED, None), conf)
        a = self.run_llm(self.yes_no(), {"answer": "Yes", "confidence": "medium", "based_on": ["a1"]})
        self.assertIn("medium confidence", a.note)

    def test_high_confidence_with_evidence_is_filled(self):
        a = self.run_llm(self.yes_no(), {"answer": "Yes", "confidence": "high", "based_on": ["Built a payments API"]})
        self.assertEqual((a.status, a.value, a.confidence), (A.FILLED, "Yes", "high"))

    def test_high_confidence_without_cited_evidence_is_flagged(self):
        a = self.run_llm(self.yes_no(), {"answer": "Yes", "confidence": "high", "based_on": []})
        self.assertEqual(a.status, A.FLAGGED)
        self.assertIn("no resume evidence", a.note)

    def test_other_answers_keep_the_old_rule(self):
        multi = self.run_llm(f("Preferred stack?", "select", options=["Python", "Java", "Go"]),
                             {"answer": "Python", "confidence": "medium", "based_on": ["skills"]})
        self.assertEqual((multi.status, multi.value), (A.FILLED, "Python"))      # not a yes/no question
        text = self.run_llm(f("Describe a project", "textarea"),
                            {"answer": "I built a payments API.", "confidence": "medium"})
        self.assertEqual(text.status, A.FILLED)


class FixedSalaryAndSkillYears(unittest.TestCase):
    def with_profile(self, **comp):
        c = ctx(job_location="Delhi")
        c.profile["compensation"].update(comp)
        return c

    def test_expected_fixed_uses_the_users_own_figure(self):
        c = self.with_profile(expected_fixed_ctc_lpa=12)
        a = A.answer_fields([f("Expected Fixed/Base Salary in your local currency")], c)[0]
        self.assertEqual((a.status, a.value), (A.FILLED, "12 LPA (negotiable)"))
        self.assertEqual(a.source, "profile:compensation.expected_fixed_ctc_lpa")
        n = A.answer_fields([f("Expected fixed CTC in LPA", "number")], c)[0]
        self.assertEqual(n.value, "12")

    def test_expected_fixed_missing_or_todo_is_flagged(self):
        for value in (None, "TODO", 0):
            c = self.with_profile(expected_fixed_ctc_lpa=value) if value is not None else ctx(job_location="Delhi")
            a = A.answer_fields([f("Expected Fixed/Base Salary")], c)[0]
            self.assertEqual((a.status, a.value), (A.FLAGGED, None), value)
            self.assertIn("expected_fixed_ctc_lpa", a.note)

    def test_current_fixed_uses_the_fixed_part_of_the_current_ctc(self):
        a = A.answer_fields([f("What is your current fixed CTC?")], ctx(job_location="Delhi"))[0]
        self.assertEqual((a.status, a.value), (A.FILLED, "5 LPA"))               # not the 9 LPA total, no breakup text

    def test_expected_fixed_on_a_non_indian_job_is_still_flagged(self):
        self.assertEqual(A.answer_fields([f("Expected base salary")], self.with_profile(expected_fixed_ctc_lpa=12))[0].status,
                         A.FILLED)                                              # the same question on an Indian job
        c = ctx(job_location="Berlin, Germany")
        c.profile["compensation"]["expected_fixed_ctc_lpa"] = 12
        self.assertEqual(A.answer_fields([f("Expected base salary")], c)[0].status, A.FLAGGED)

    def test_years_with_a_listed_tool(self):
        c = ctx()
        a = A.answer_fields([f("# of years with Python or Python-based backend frameworks", required=False)], c)[0]
        self.assertEqual((a.status, a.value, a.source), (A.FILLED, "1.5 years", "profile:skill_years"))
        self.assertEqual(A.answer_fields([f("Years of experience with Postgres?")], c)[0].value, "1.2 years")   # alias
        self.assertEqual(A.answer_fields([f("Years with Docker", "number")], c)[0].value, "0.8")
        r = A.answer_fields([f("Years of experience with Django", "select", options=["0-1 years", "1-3 years", "3+"])], c)[0]
        self.assertEqual(r.value, "1-3 years")                                  # exactly 1.0: the half-open range wins

    def test_multi_word_names_resolve_to_the_right_entry(self):
        c = ctx()
        c.profile["skill_years"] = {"python": 2, "django": 3, "drf": 1, "elasticsearch": 1, "openai": 1}
        for label, expected in [("Years of experience with DRF?", "1 year"),
                                ("Years with Django REST Framework", "1 year"),          # DRF, not Django (3)
                                ("Years of experience with Django", "3 years"),
                                ("Years with Elasticsearch / ELK", "1 year"),            # one entry, not two tools
                                ("Years with the ELK stack", "1 year"),
                                ("Years of experience with the OpenAI API", "1 year")]:
            a = A.answer_fields([f(label)], c)[0]
            self.assertEqual((a.status, a.value), (A.FILLED, expected), label)
        self.assertEqual(A._tools_in("Python, Django REST Framework and Redis"), ["drf", "python", "redis"])

    def test_an_unlisted_tool_is_flagged_never_estimated(self):
        a = A.answer_fields([f("Years of experience with Kafka?")], ctx())[0]
        self.assertEqual((a.status, a.value), (A.FLAGGED, None))
        self.assertIn("not in profile.yaml skill_years", a.note)
        c = ctx()
        c.profile["skill_years"]["python"] = "TODO"
        self.assertEqual(A.answer_fields([f("Years of experience with Python?")], c)[0].status, A.FLAGGED)
        c.profile.pop("skill_years")
        self.assertEqual(A.answer_fields([f("Years of experience with Django?")], c)[0].status, A.FLAGGED)

    def test_several_tools_or_a_yes_no_threshold_are_flagged(self):
        c = ctx()
        a = A.answer_fields([f("Years of experience with Python and Django?")], c)[0]
        self.assertEqual(a.status, A.FLAGGED)
        self.assertIn("several tools", a.note)
        t = A.answer_fields([f("Do you have at least 3 years of Python experience?", "radio", options=["Yes", "No"])], c)[0]
        self.assertEqual(t.status, A.FLAGGED)
        self.assertIn("threshold", t.note)
        p = A.answer_fields([f("Do you have at least 4 yrs of production system experience in a B2B SaaS startup?",
                               "radio", options=["Yes", "No"])], c)[0]
        self.assertEqual(p.status, A.FLAGGED)

    def test_total_experience_still_uses_dates_not_skill_years(self):
        a = A.answer_fields([f("What is your total years of experience?")], ctx())[0]
        self.assertEqual(a.category, "experience_total")
        self.assertIn("computed:profile.employment dates", a.source)


class CityOptions(unittest.TestCase):
    def bangalore(self):
        c = ctx()
        c.profile["personal"]["current_location"] = {"city": "Bangalore", "state": "Karnataka", "country": "India"}
        return c

    def test_city_options_match_bangalore_or_bengaluru(self):
        c = self.bangalore()
        for options, expected in [(["Delhi", "Bengaluru", "Mumbai", "Other"], "Bengaluru"),
                                  (["Bangalore", "Chennai"], "Bangalore"),
                                  (["Bangalore, India", "Delhi, India"], "Bangalore, India"),
                                  (["Bengaluru Urban, Karnataka", "Mysuru"], "Bengaluru Urban, Karnataka")]:
            a = A.answer_fields([f("Where are you currently based?", "select", options=options)], c)[0]
            self.assertEqual((a.status, a.value), (A.FILLED, expected), options)
            self.assertEqual(a.source, "profile:personal.current_location")

    def test_no_matching_city_is_flagged(self):
        a = A.answer_fields([f("Where are you based?", "select", options=["Delhi", "Mumbai", "Singapore"])], self.bangalore())[0]
        self.assertEqual((a.status, a.value), (A.FLAGGED, None))

    def test_location_combined_with_the_right_to_work_stays_flagged(self):
        label = "Where are you currently based, and do you have the legal right to work in that location?"
        a = A.answer_fields([f(label, "select", options=["Delhi", "Bangalore", "Singapore", "Manila"])], self.bangalore())[0]
        self.assertEqual((a.category, a.status, a.value), ("work_auth", A.FLAGGED, None))

    def test_the_real_portcast_question_stays_flagged_even_though_bangalore_is_an_option(self):
        job = load_job("lv_portcast")
        answers = A.answer_fields(fields_from_questions(job.questions), ctx(job, llm=lambda p: {"answers": {}}))
        q = by_label(answers, "Where are you currently based, and do you have")
        self.assertIn("Bangalore", " ".join(next(x for x in fields_from_questions(job.questions)
                                                 if x.label.startswith("Where are you currently")).options))
        self.assertEqual((q.status, q.value), (A.FLAGGED, None))

    def test_free_text_location_is_unchanged(self):
        a = A.answer_fields([f("Current location")], self.bangalore())[0]
        self.assertEqual(a.value, "Bangalore, Karnataka, India")


class SalaryFlags(unittest.TestCase):
    def test_fixed_base_salary_is_flagged_for_the_right_reason_on_an_indian_job(self):
        a = A.answer_fields([f("Expected Fixed/Base Salary in your local currency")], ctx(job_location="Delhi"))[0]
        self.assertEqual((a.status, a.value), (A.FLAGGED, None))
        self.assertIn("fixed/base", a.note)                     # not "another currency": local currency is INR in India
        self.assertNotIn("currency", a.note.replace("fixed/base", ""))

    def test_local_currency_abroad_and_named_foreign_currencies_are_flagged_as_currency(self):
        a = A.answer_fields([f("Expected salary in your local currency")], ctx(job_location="Berlin, Germany"))[0]
        self.assertEqual(a.status, A.FLAGGED)
        b = A.answer_fields([f("Current gross salary (GBP)?")], ctx(job_location="Delhi"))[0]
        self.assertEqual(b.status, A.FLAGGED)
        self.assertIn("another currency", b.note)

    def test_a_plain_indian_salary_question_is_still_answered(self):
        a = A.answer_fields([f("What is your Expected CTC in your local currency?")], ctx(job_location="Delhi"))[0]
        self.assertEqual((a.status, a.value), (A.FILLED, "18 LPA (negotiable)"))


class ConsentOptionA(unittest.TestCase):
    """Decided by the user: tick REQUIRED privacy / data-processing acknowledgements only; flag everything else."""

    def answer(self, label, options, required=True, type="checkbox", group="standard"):
        return A.answer_fields([f(label, type, required=required, options=options, group=group)], ctx())[0]

    def test_required_privacy_acknowledgements_are_ticked(self):
        a = self.answer("Acme Privacy Policy", ["Acknowledge"], type="multiselect")
        self.assertEqual((a.status, a.value, a.category), (A.FILLED, ["Acknowledge"], "consent"))
        self.assertIn("Option A", a.source)
        b = self.answer("I consent to the processing of my personal data for this application", ["Yes", "No"], type="radio")
        self.assertEqual((b.status, b.value), (A.FILLED, "Yes"))
        c = self.answer("", ["I agree to the data processing and privacy notice"], group="consent")
        self.assertEqual((c.status, c.value), (A.FILLED, ["I agree to the data processing and privacy notice"]))   # caption-only box

    def test_marketing_optional_and_non_privacy_consents_are_never_ticked(self):
        cases = [
            ("Yes, Acme can contact me about future job opportunities for up to 1 year", ["Yes, ..."], False, "consent"),
            ("Subscribe to our newsletter and job alerts", ["Yes"], True, "standard"),
            ("Privacy policy (optional)", ["Acknowledge"], False, "standard"),                     # optional
            ("Acme Confidential Information", ["Acknowledge"], True, "standard"),
            ("Interview Recording Policy", ["Acknowledge"], True, "standard"),
            ("I agree to the terms of employment", ["I agree"], True, "standard"),
            ("Privacy policy and permission to share my details with third parties", ["Yes"], True, "standard"),
        ]
        for label, options, required, group in cases:
            a = self.answer(label, options, required=required, group=group)
            self.assertEqual((a.status, a.value), (A.FLAGGED, None), label)

    def test_every_real_fixture_only_ticks_required_privacy_boxes(self):
        ticked = []
        for name in sorted(p.name for p in REAL.iterdir() if (p / "urls.json").exists()):
            job = load_job(name)
            for a in A.answer_fields(fields_from_questions(job.questions), ctx(job, llm=lambda p: {"answers": {}})):
                if a.category == "consent" and a.status == A.FILLED:
                    ticked.append((name, a.label))
        self.assertTrue(ticked, "expected at least one required privacy acknowledgement in the fixtures")
        for name, label in ticked:
            self.assertRegex(label, r"(?i)privacy|data protection|gdpr", (name, label))


class Referrals(unittest.TestCase):
    """Decided by the user: keep flagging referral questions."""

    def test_they_are_flagged_and_never_answered(self):
        for label, type_, opts in [("Were you referred by someone currently working at Acme?", "textarea", []),
                                   ("Referrer's name", "text", []),
                                   ("Is this a referral?", "radio", ["Yes", "No"]),
                                   ("Employee referral name", "text", [])]:
            a = A.answer_fields([f(label, type_, required=False, options=opts)], ctx(llm=lambda p: {"answers": {}}))[0]
            self.assertEqual((a.status, a.value, a.category), (A.FLAGGED, None, "referral"), label)


class DateStyle(unittest.TestCase):
    def test_free_text_dates_are_written_as_13_oct_2026_and_date_inputs_keep_iso(self):
        c = ctx()
        text = A.answer_fields([f("Last working day?"), f("Earliest joining date?")], c)
        self.assertEqual([a.value for a in text], ["20 Aug 2030", "26 Aug 2030"])
        dates = A.answer_fields([f("Last working day?", "date"), f("Earliest joining date?", "date")], c)
        self.assertEqual([a.value for a in dates], ["2030-08-20", "2030-08-26"])
        notice = A.answer_fields([f("Notice period?", "textarea")], c)[0]
        self.assertEqual(notice.value, "Serving notice, last working day 20 Aug 2030")

    def test_the_llm_is_told_the_date_style(self):
        prompts = []
        A.answer_fields([f("Describe a project", "textarea")], ctx(llm=lambda p: prompts.append(p) or {"answers": {}}))
        self.assertIn('"13 Oct 2026"', prompts[0])


class SensitiveNeverReachesTheLlm(unittest.TestCase):
    SENSITIVE = [
        f("What is your Current CTC?"), f("What is your Expected CTC?"), f("Notice Period"),
        f("Are you serving notice?", "radio", options=["Yes", "No"]), f("Last working day?"),
        f("When can you join?"), f("Are you willing to relocate?", "radio", options=["Yes", "No"]),
        f("Do you need visa sponsorship?", "radio", options=["Yes", "No"]),
        f("Are you legally authorized to work in this country?", "radio", options=["Yes", "No"]),
        f("What gender do you identify as?", "select", options=["Male", "Female"]),
        f("Race / ethnicity", "select", options=["Group A", "Group B"]),
        f("Protected veteran status?", "select", options=["Yes", "No"]),
        f("Do you have a disability?", "select", options=["Yes", "No"]),
        f("What is your age range?", "radio", options=["18-24", "25-34"]),
        f("Do you have any criminal convictions?", "radio", options=["Yes", "No"]),
        f("Do you have a family member who works here?", "radio", options=["Yes", "No"]),
    ]

    def test_only_the_generic_question_is_sent_and_no_sensitive_value_is_in_the_prompt(self):
        prompts = []

        def llm(prompt):
            prompts.append(prompt)
            return {"answers": {"Why this role?": {"answer": "I build Python APIs.", "confidence": "high", "based_on": ["a1"]}}}

        fields = self.SENSITIVE + [f("Why this role?", "textarea", required=False)]
        out = A.answer_fields(fields, ctx(llm=llm))
        self.assertEqual(len(prompts), 1)
        prompt = prompts[0]
        self.assertIn("Why this role?", prompt)
        for fld in self.SENSITIVE:
            self.assertNotIn(fld.label, prompt, fld.label)
        p = persona.profile()
        for secret in ("9 LPA", "18 LPA", "5.0", "1.5", "2030-08-20", "2030-08-26", "Test Group", "Female",
                       "test.user@example.com", "+00 000 000 0000", "84.5", "90.5", "TESTBOARD",   # (CGPA is on the resume)
                       "allowances", "Test Region"):
            self.assertNotIn(secret, prompt, secret)
        self.assertNotIn(p["personal"]["linkedin"], prompt)
        self.assertEqual(by_label(out, "Why this role?").value, "I build Python APIs.")
        for a in out:
            if a.label != "Why this role?":
                self.assertNotEqual(a.source, "llm", a.label)
        self.assertEqual(A.to_json(out)["summary"]["sent_to_llm"], ["Why this role?"])

    def test_unanswerable_sensitive_questions_are_flagged_not_guessed(self):
        c = ctx(job_location="Berlin, Germany")
        c.profile["work_authorization"]["default_other"] = {"authorized": "TODO", "needs_sponsorship": "TODO"}
        out = A.answer_fields([f("Are you legally authorized to work here?", "radio", options=["Yes", "No"]),
                               f("What is your Expected CTC?"), f("What is your age range?", "radio", options=["a", "b"])],
                              c)
        self.assertEqual([a.status for a in out], [A.FLAGGED] * 3)
        self.assertEqual([a.value for a in out], [None] * 3)


class LlmAnswerValidation(unittest.TestCase):
    def run_llm(self, field, reply):
        return A.answer_fields([field], ctx(llm=lambda p: {"answers": {field.key: reply}}))[0]

    def test_valid_free_text_and_select(self):
        a = self.run_llm(f("Describe a project", "textarea"),
                         {"answer": "I built a payments API serving 50,000+ requests per week.", "confidence": "high"})
        self.assertEqual(a.status, A.FILLED)
        s = self.run_llm(f("Preferred stack?", "select", options=["Python", "Java"]), {"answer": "python", "confidence": "high"})
        self.assertEqual((s.status, s.value), (A.FILLED, "Python"))

    def test_bad_answers_are_flagged(self):
        cases = [
            (f("Preferred stack?", "select", options=["Python", "Java"]), {"answer": "Rust", "confidence": "high"}),
            (f("Describe a project", "textarea"), {"answer": "It served 9,999,999 users.", "confidence": "high"}),
            (f("Describe a project", "textarea"), {"answer": "Something", "confidence": "low"}),
            (f("Describe a project", "textarea"), {"answer": None, "confidence": "high"}),
            (f("Describe a project", "textarea"), {}),
        ]
        for fld, reply in cases:
            a = self.run_llm(fld, reply)
            self.assertEqual((a.status, a.value), (A.FLAGGED, None), reply)

    def test_llm_failure_flags_everything(self):
        def boom(_):
            raise RuntimeError("usage limit")
        a = A.answer_fields([f("Describe a project", "textarea")], ctx(llm=boom))[0]
        self.assertEqual(a.status, A.FLAGGED)
        self.assertIn("LLM call failed", a.note)

    def test_prompt_contains_only_visible_resume_content(self):
        prompts = []
        A.answer_fields([f("Describe a project", "textarea")],
                        ctx(llm=lambda p: prompts.append(p) or {"answers": {}}))
        self.assertIn("payments API", prompts[0])
        self.assertNotIn("Kubernetes", prompts[0])            # hidden bullet a3


class Json(unittest.TestCase):
    def test_answers_json_has_source_and_status_for_every_answer(self):
        out = A.answer_fields([f("Full name"), f("Why this role?", "textarea", required=False)],
                              ctx(llm=lambda p: {"answers": {}}))
        data = A.to_json(out)
        self.assertEqual(data["summary"]["total"], 2)
        for a in data["answers"]:
            for key in ("label", "category", "value", "source", "confidence", "status"):
                self.assertIn(key, a)


if __name__ == "__main__":
    unittest.main()
