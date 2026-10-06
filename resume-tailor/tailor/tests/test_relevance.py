"""Relevance scoring: one Claude call per 10 postings, validated by code, stored in the tracker. A fake Claude stands in."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("JOBBOT_NO_SHEET", "1")      # a test must never reach the real Google Sheet
sys.path.insert(0, str(ROOT / "tests"))

import persona  # noqa: E402
import tailor  # noqa: E402
from jobbot import relevance as R  # noqa: E402
from jobbot import tracker as T  # noqa: E402

RESUME, PROFILE = persona.resume(), persona.profile()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tr = T.Tracker(Path(self.tmp.name) / "t.sqlite3")

    def tearDown(self):
        self.tr.close()
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    def add(self, n, description="Build Python APIs with Django. 1-2 years."):
        self.tr.add_found({"canonical_url": f"https://job-boards.greenhouse.io/co{n}/jobs/{n}", "company": f"Co{n}",
                           "role": f"Backend Engineer {n}", "route": "greenhouse", "source": "indeed",
                           "source_url": "https://x", "experience_asked": "1-2 yrs", "location": "Bengaluru",
                           "description": description})
        return self.tr.job(f"https://job-boards.greenhouse.io/co{n}/jobs/{n}")["id"]

    def rows(self):
        return self.tr.unscored()


class Prompt(Base):
    def test_it_carries_the_visible_resume_a_few_plain_facts_every_job_and_the_honesty_rubric(self):
        ids = [self.add(i) for i in range(1, 4)]
        prompt = R.build_prompt(self.rows(), R.candidate_summary(RESUME, PROFILE))
        for needle in ("Built a payments API serving 50,000+ requests per week", "Bengaluru", "years of experience",
                       "Roles mainly needing skills the candidate does not have score LOW", "never above 6", "ONE JSON"):
            self.assertIn(needle, prompt)
        for jid in ids:
            self.assertIn(f"### JOB {jid}", prompt)
        self.assertIn("Build Python APIs with Django", prompt)

    def test_hidden_bullets_and_private_details_never_reach_it(self):
        self.add(1)
        prompt = R.build_prompt(self.rows(), R.candidate_summary(RESUME, PROFILE))
        self.assertNotIn("Kubernetes and Terraform", prompt)                           # a hidden bullet
        for secret in (PROFILE["personal"]["email"], RESUME["phone"], str(PROFILE["compensation"]["current_ctc"]["fixed_lpa"]),
                       "notice_period", "last_working_day", "sponsorship"):
            self.assertNotIn(secret, prompt)

    def test_a_long_description_is_cut_and_a_missing_one_is_said_so(self):
        self.add(1, "X" * 9000)
        self.add(2, "")
        prompt = R.build_prompt(self.rows(), "SUMMARY")
        self.assertLess(prompt.count("X"), R.DESCRIPTION_CHARS + 50)
        self.assertIn("(no description available)", prompt)


class Parsing(Base):
    def parse(self, entries, n=3):
        ids = [self.add(i) for i in range(1, n + 1)]
        return R.parse_reply({"scores": [{"id": ids[0], **e} if "id" not in e else e for e in entries]}, self.rows()), ids

    def test_valid_scores_are_accepted_and_the_reason_is_trimmed(self):
        parsed, ids = self.parse([{"score": 8, "reason": "  Django APIs match the resume.\n"}])
        self.assertEqual(parsed.scores, {ids[0]: (8, "Django APIs match the resume.")})
        self.assertEqual(parsed.rejected, [])

    def test_anything_not_a_whole_number_from_1_to_10_is_rejected(self):
        for bad in (0, 11, -3, 7.5, "8", None, True, [8]):
            parsed, ids = self.parse([{"score": bad, "reason": "ok"}])
            self.assertEqual(parsed.scores, {}, repr(bad))
            self.assertEqual(len(parsed.rejected), 1, repr(bad))
        parsed, ids = self.parse([{"score": 7.0, "reason": "a whole number written as 7.0 is fine"}])
        self.assertEqual(parsed.scores[ids[0]][0], 7)

    def test_unknown_ids_duplicates_missing_reasons_and_broken_entries_are_rejected(self):
        ids = [self.add(1), self.add(2)]
        reply = {"scores": [{"id": ids[0], "score": 8, "reason": "good"}, {"id": ids[0], "score": 3, "reason": "again"},
                            {"id": 99999, "score": 9, "reason": "not in the batch"}, {"id": ids[1], "score": 6, "reason": ""},
                            {"score": 5, "reason": "no id"}, "junk"]}
        parsed = R.parse_reply(reply, self.rows())
        self.assertEqual(parsed.scores, {ids[0]: (8, "good")})
        self.assertEqual(len(parsed.rejected), 5)
        self.assertEqual(R.parse_reply({"scores": "no"}, self.rows()).rejected[0][1], "the reply has no 'scores' list")
        self.assertEqual(R.parse_reply([], self.rows()).scores, {})

    def test_a_posting_without_a_description_is_capped(self):
        jid = self.add(1, "")
        parsed = R.parse_reply({"scores": [{"id": jid, "score": 9, "reason": "title looks right"}]}, self.rows())
        score, reason = parsed.scores[jid]
        self.assertEqual(score, R.NO_TEXT_CAP)
        self.assertIn("capped", reason)


class Scoring(Base):
    def llm(self, calls, scores=None):
        def fake(prompt):
            ids = [int(x) for x in __import__("re").findall(r"### JOB (\d+)", prompt)]
            calls.append(ids)
            return {"scores": [{"id": i, "score": (scores or {}).get(i, 8), "reason": f"reason for {i}"} for i in ids]}
        return fake

    def test_twenty_five_postings_take_three_calls_of_10_10_5_and_everything_is_stored(self):
        ids = [self.add(i) for i in range(1, 26)]
        calls = []
        rep = R.score_unscored(self.tr, self.llm(calls), RESUME, PROFILE, log=lambda *a: None)
        self.assertEqual([len(c) for c in calls], [10, 10, 5])
        self.assertEqual((rep.scored, rep.calls, rep.left_unscored, rep.problems), (25, 3, [], []))
        r = self.tr.by_ids([ids[0]])[0]
        self.assertEqual((r["relevance"], r["relevance_reason"]), (8, f"reason for {ids[0]}"))
        self.assertEqual(self.tr.unscored(), [])

    def test_scored_postings_are_never_scored_again(self):
        for i in range(1, 4):
            self.add(i)
        calls = []
        R.score_unscored(self.tr, self.llm(calls), RESUME, PROFILE, log=lambda *a: None)
        self.add(4)
        R.score_unscored(self.tr, self.llm(calls), RESUME, PROFILE, log=lambda *a: None)
        self.assertEqual([len(c) for c in calls], [3, 1])                              # only the new one the second time
        R.score_unscored(self.tr, self.llm(calls), RESUME, PROFILE, log=lambda *a: None)
        self.assertEqual(len(calls), 2)                                                # nothing left: no call at all

    def test_a_usage_limit_keeps_what_was_scored_and_stops(self):
        for i in range(1, 26):
            self.add(i)
        calls = []
        good = self.llm(calls)

        def limited(prompt):
            if len(calls) == 1:
                raise tailor.UsageLimitError("limit")
            return good(prompt)
        with self.assertRaises(tailor.UsageLimitError):
            R.score_unscored(self.tr, limited, RESUME, PROFILE, log=lambda *a: None)
        self.assertEqual(len(self.tr.unscored()), 15)                                  # the first batch of 10 was saved
        calls.clear()
        R.score_unscored(self.tr, good, RESUME, PROFILE, log=lambda *a: None)         # the next run continues
        self.assertEqual([len(c) for c in calls], [10, 5])

    def test_a_bad_batch_is_recorded_and_the_others_still_run(self):
        for i in range(1, 26):
            self.add(i)
        calls = []
        good = self.llm(calls)

        def flaky(prompt):
            if len(calls) == 1:
                calls.append([])
                raise tailor.TailorError("Could not find JSON in Claude's reply")
            return good(prompt)
        rep = R.score_unscored(self.tr, flaky, RESUME, PROFILE, log=lambda *a: None)
        self.assertEqual((rep.scored, len(rep.left_unscored)), (15, 10))
        self.assertIn("TailorError", rep.problems[0])

    def test_ids_the_reply_leaves_out_stay_unscored_for_next_time(self):
        ids = [self.add(i) for i in range(1, 4)]
        rep = R.score_unscored(self.tr, lambda p: {"scores": [{"id": ids[0], "score": 7, "reason": "fits"}]}, RESUME, PROFILE,
                               log=lambda *a: None)
        self.assertEqual((rep.scored, rep.left_unscored), (1, ids[1:]))


class TrackerSupport(Base):
    def test_found_is_ordered_most_relevant_first_and_top_found_respects_threshold_and_limit(self):
        ids = [self.add(i) for i in range(1, 6)]
        for jid, score in zip(ids, (5, 9, 7, 8, 6)):
            self.tr.set_relevance(jid, score, f"r{score}")
        self.assertEqual([r["relevance"] for r in self.tr.found()], [9, 8, 7, 6, 5])
        self.assertEqual([r["relevance"] for r in self.tr.top_found(7, 10)], [9, 8, 7])
        self.assertEqual([r["relevance"] for r in self.tr.top_found(7, 2)], [9, 8])
        self.assertEqual(self.tr.top_found(7, 0), [])
        self.assertEqual(self.tr.top_found(10, 5), [])

    def test_a_rediscovered_posting_keeps_its_score(self):
        jid = self.add(1)
        self.tr.set_relevance(jid, 8, "fits")
        self.add(1)                                                                    # found again by another search
        r = self.tr.by_ids([jid])[0]
        self.assertEqual((r["relevance"], r["relevance_reason"]), (8, "fits"))

    def test_delete_found_forgets_only_undecided_postings(self):
        a, b = self.add(1), self.add(2)
        self.tr.decide([b], approve=True)
        self.assertEqual(self.tr.delete_found(), 1)
        self.assertEqual([r["id"] for r in self.tr.approved()], [b])

    def test_the_score_and_reason_reach_the_sheet_and_the_shortlist(self):
        from jobbot import discovery as D
        from jobbot import sheets as S
        from test_sheets import FakeSheet
        a, b = self.add(1), self.add(2)
        self.tr.set_relevance(a, 9, "Django APIs match")
        self.tr.set_relevance(b, 3, "mostly Java and Spring")
        sheet = FakeSheet()
        S.sync(self.tr, sheet)                                                         # found rows show from relevance 5 up
        shown = [sheet.row(n) for n in range(2, 6) if sheet.row(n)[3]]
        self.assertEqual([(r[3], r[14], r[15]) for r in shown], [("Co1", "9", "Django APIs match")])
        recs = D.shortlist_records(self.tr.found())
        self.assertEqual([(r["relevance_score"], r["reason"]) for r in recs],
                         [(9, "Django APIs match"), (3, "mostly Java and Spring")])
        json.dumps(recs)
        S.sync(self.tr, sheet, found_from=3)
        self.assertEqual(len([n for n in range(2, 6) if sheet.row(n)[3]]), 2)           # the knob lowers the bar


class ScoringRules(Base):
    """2026-10-04 fixes: the score is capped in CODE by experience, missing company, aggregators, its own caveats and Java."""

    RULES = R.Rules()

    def row(self, title="Backend Engineer", company="Acme Co", exp="1-2 yrs", desc="Python and Django APIs. " * 10):
        self._n = getattr(self, "_n", 0) + 1
        return {"id": self._n, "company": company, "role": title, "experience_asked": exp, "description": desc,
                "location": "Bengaluru"}

    def score(self, row, score, reason="Python/Django APIs match the resume.", exceptional=False, rules=None):
        reply = {"scores": [{"id": row["id"], "score": score, "reason": reason, "exceptional": exceptional}]}
        return R.parse_reply(reply, [row], rules or self.RULES).scores[row["id"]]

    # 1. minimum experience of 3+ years
    def test_a_minimum_of_three_or_more_years_caps_the_score_at_six(self):
        for exp in ("3-5 yrs", "3+ yrs", "3 yrs", "5-10 yrs", "8+ yrs"):
            s, why = self.score(self.row(exp=exp), 9)
            self.assertEqual(s, 6, exp)
            self.assertIn("capped at 6", why)
            self.assertIn(exp, why)
        for exp in ("2-4 yrs", "1-3 yrs", "0-2 yrs", "2+ yrs", "not stated", "fresher / entry level", ""):
            self.assertEqual(self.score(self.row(exp=exp), 9)[0], 9, exp)

    def test_an_exceptional_match_is_the_only_way_past_the_experience_cap_and_only_without_a_gap(self):
        self.assertEqual(self.score(self.row(exp="3-5 yrs"), 9, "Django, DRF, Celery, Postgres and AWS match exactly.", True)[0], 9)
        for reason in ("Django matches exactly; Angular is a gap.", "Exact stack, but Kubernetes is missing.",
                       "Matches closely; payments domain not shown."):
            self.assertEqual(self.score(self.row(exp="3-5 yrs"), 9, reason, True)[0], 6, reason)
        self.assertEqual(self.score(self.row(exp="3-5 yrs"), 9, "Exact stack match.", False)[0], 6)     # not claimed
        reply = {"scores": [{"id": 0, "score": 9, "reason": "Exact.", "exceptional": "true"}]}           # a string is not True
        row = self.row(exp="3-5 yrs")
        reply["scores"][0]["id"] = row["id"]
        self.assertEqual(R.parse_reply(reply, [row], self.RULES).scores[row["id"]][0], 6)

    def test_the_experience_threshold_is_configurable(self):
        rules = R.Rules(min_years_from=4)
        self.assertEqual(self.score(self.row(exp="3-5 yrs"), 8, rules=rules)[0], 8)
        self.assertEqual(self.score(self.row(exp="4-6 yrs"), 8, rules=rules)[0], 6)

    # 2. no company name
    def test_no_company_name_caps_at_six(self):
        for company in ("", "   ", None):
            s, why = self.score(self.row(company=company or ""), 9)
            self.assertEqual(s, 6)
            self.assertIn("no company name", why)

    # 3. recruiting platforms and aggregators
    def test_listed_aggregators_are_capped_by_whole_word_match(self):
        for company in ("Uplers", "UPLERS Pvt Ltd", "Ibrowsejobs Technologies", "Jobgether"):
            s, why = self.score(self.row(company=company), 9)
            self.assertEqual(s, 6, company)
            self.assertIn("recruiting platform / aggregator", why)
        self.assertEqual(self.score(self.row(company="Couplers Inc"), 9)[0], 9)                    # "uplers" inside a word
        self.assertEqual(self.score(self.row(company="Acme Co"), 9)[0], 9)

    def test_the_aggregator_list_is_editable_and_drop_mode_leaves_scoring_alone(self):
        mine = R.Rules(aggregators=["Acme"])
        self.assertEqual(self.score(self.row(company="Acme Co"), 9, rules=mine)[0], 6)
        self.assertEqual(self.score(self.row(company="Uplers"), 9, rules=mine)[0], 9)
        dropping = R.Rules(aggregator_action="drop")
        self.assertEqual(self.score(self.row(company="Uplers"), 9, rules=dropping)[0], 9)         # dropped earlier, not capped

    # 4. the score must reflect every caveat in its own reason
    def test_a_serious_caveat_in_the_reason_cannot_sit_next_to_a_high_score(self):
        for reason in ("Python matches, though fresher-level and below candidate's level.", "Django fits but a major gap in Kubernetes.",
                       "Matches the stack; significant gap in cloud depth.", "Overqualified for this junior role.",
                       "Python fits but there is a mismatch in domain.", "Mainly needs Java and Spring Boot.",
                       "Mostly C++ with a little Python.", "A serious stretch on seniority.", "far above the candidate's level"):
            s, why = self.score(self.row(), 8, reason)
            self.assertEqual(s, 6, reason)
            self.assertIn("its own reason says", why)

    def test_small_gaps_do_not_trigger_it(self):
        for reason in ("Django and Postgres match; React is a small gap.", "Python backend fits; FastAPI is a slight stretch.",
                       "Close fit, a modest gap in AWS depth.", "Great fit; Kafka missing."):
            self.assertEqual(self.score(self.row(), 8, reason)[0], 8, reason)

    # 5. Java-centric roles
    def test_java_centric_roles_score_lower(self):
        for title in ("Java Developer", "Full Stack Java Developer", "Spring Boot Engineer", "Kotlin Backend Engineer"):
            s, why = self.score(self.row(title=title), 8)
            self.assertEqual(s, 5, title)
            self.assertIn("Java-centric", why)
        heavy = "Java and Spring Boot with Hibernate and Maven; JPA services. Some python scripting. "
        self.assertEqual(self.score(self.row(title="Software Engineer", desc=heavy * 3), 8)[0], 5)
        for title, desc in (("Python Java Developer", "Python Django. " * 8), ("Backend Engineer", "JavaScript and TypeScript React. " * 8),
                            ("Software Engineer", "Python, Django, Flask and one line of Java."),
                            ("Backend Engineer", "Java and Python services, Django REST. " * 6)):
            self.assertEqual(self.score(self.row(title=title, desc=desc), 8)[0], 8, title)

    def test_java_centric_helper(self):
        self.assertTrue(R.java_centric("Java Developer", ""))
        self.assertFalse(R.java_centric("Python and Java Developer", ""))
        self.assertFalse(R.java_centric("Engineer", "JavaScript everywhere. " * 10))

    # all together
    def test_the_lowest_cap_wins_and_every_reason_is_named(self):
        row = self.row(title="Java Developer", company="Uplers", exp="5-8 yrs")
        s, why = self.score(row, 9, "Looks fine, though below candidate's level.")
        self.assertEqual(s, 5)
        for needle in ("model said 9", "capped at 5", "asks 5-8 yrs", "aggregator", "below candidate", "Java-centric"):
            self.assertIn(needle, why)

    def test_a_score_already_under_the_cap_is_left_alone(self):
        s, why = self.score(self.row(exp="5+ yrs"), 4, "Weak fit.")
        self.assertEqual((s, why), (4, "Weak fit."))

    def test_without_rules_nothing_is_capped(self):
        row = self.row(exp="8+ yrs", company="")
        reply = {"scores": [{"id": row["id"], "score": 9, "reason": "Exact."}]}
        self.assertEqual(R.parse_reply(reply, [row]).scores[row["id"]], (9, "Exact."))


class Recap(Base):
    """A rule change applies to scores already stored, with no Claude usage."""

    def found(self, n, score, reason, exp="1-2 yrs", company="Acme", flag=None, title="Backend Engineer"):
        url = f"https://www.linkedin.com/jobs/view/{7200000 + n}"
        self.tr.add_found({"canonical_url": url, "company": f"{company} {n}", "role": f"{title} {n}", "route": "manual",
                           "source": "linkedin", "source_url": url, "experience_asked": exp, "location": "Bengaluru",
                           "description": "Python Django. " * 20})
        jid = self.tr.job(url)["id"]
        self.tr.set_relevance(jid, score, reason)
        if flag is None:
            self.tr.db.execute("UPDATE jobs SET relevance_exceptional=NULL WHERE id=?", (jid,))
            self.tr.db.commit()
        else:
            self.tr.set_relevance(jid, score, reason, flag)
        return jid

    def test_more_ways_of_saying_below_level_are_caught(self):
        for reason in ("Junior role accepting Python; the level is slightly below their experience.",
                       "Python fits but the work is below the candidate's experience.", "Fresher-level role.",
                       "An entry-level position.", "A junior-level role.", "Too junior for 1.8 years."):
            s_, why = ScoringRules().score.__func__(ScoringRules(), {"id": 1, "company": "A", "role": "Engineer",
                                                                    "experience_asked": "", "description": ""}, 8, reason)
            self.assertEqual(s_, 6, reason)

    def test_a_stored_contradiction_is_lowered_in_place(self):
        a = self.found(1, 8, "Python fits; the level is slightly below their experience.")
        b = self.found(2, 8, "Python and Django match; React is a small gap.")
        changed = R.recap_found(self.tr, R.Rules(), log=lambda *x: None)
        self.assertEqual([(c["id"], c["was"], c["now"]) for c in changed], [(a, 8, 6)])
        self.assertEqual(self.tr.by_ids([b])[0]["relevance"], 8)
        self.assertIn("capped at 6", self.tr.by_ids([a])[0]["relevance_reason"])
        self.assertEqual(R.recap_found(self.tr, R.Rules(), log=lambda *x: None), [])               # idempotent

    def test_a_three_year_posting_keeps_a_pass_it_was_given_but_loses_one_it_never_earned(self):
        unknown_high = self.found(1, 8, "Exact stack match.", exp="3-5 yrs")                       # stored before the flag existed
        earned = self.found(2, 8, "Exact stack match.", exp="3-5 yrs", flag=True)
        not_earned = self.found(3, 8, "Exact stack match.", exp="3-5 yrs", flag=False)
        unknown_low = self.found(4, 6, "Good fit.", exp="3-5 yrs")
        R.recap_found(self.tr, R.Rules(), log=lambda *x: None)
        got = {i: self.tr.by_ids([i])[0]["relevance"] for i in (unknown_high, earned, not_earned, unknown_low)}
        self.assertEqual(got, {unknown_high: 8, earned: 8, not_earned: 6, unknown_low: 6})

    def test_only_found_scored_postings_are_touched_and_nothing_is_called(self):
        a = self.found(1, 8, "Overqualified for this.")
        self.tr.decide([a], approve=True)
        unscored = self.found(2, 5, "x")
        self.tr.db.execute("UPDATE jobs SET relevance=NULL WHERE id=?", (unscored,))
        self.tr.db.commit()
        self.assertEqual(R.recap_found(self.tr, R.Rules(), log=lambda *x: None), [])
        self.assertEqual(self.tr.by_ids([a])[0]["relevance"], 8)                                  # decided: left alone

    def test_the_exceptional_flag_is_stored_when_scoring(self):
        url = "https://www.linkedin.com/jobs/view/7300001"
        self.tr.add_found({"canonical_url": url, "company": "Exact Co", "role": "Django Developer", "route": "manual",
                           "source": "linkedin", "source_url": url, "experience_asked": "3-5 yrs", "location": "Bengaluru",
                           "description": "Django DRF Celery. " * 20})
        jid = self.tr.job(url)["id"]
        R.score_unscored(self.tr, lambda p: {"scores": [{"id": jid, "score": 9, "reason": "Exact stack.", "exceptional": True}]},
                         RESUME, PROFILE, log=lambda *a: None, rules=R.Rules())
        r = self.tr.by_ids([jid])[0]
        self.assertEqual((r["relevance"], r["relevance_exceptional"]), (9, 1))
        self.assertEqual(self.tr.clear_relevance(), 1)
        self.assertIsNone(self.tr.by_ids([jid])[0]["relevance_exceptional"])

    def test_a_run_recaps_before_it_approves(self):
        import run
        from jobbot import tracker as T
        a = self.found(1, 8, "Python fits; the level is below the candidate's experience.")
        s = run.run_all({"discovery": {"search_terms": ["x"], "locations": ["Bengaluru"], "sites": ["indeed"]},
                         "selection": {"relevance_threshold": 7, "fetch_missing_descriptions": False, "board_check": False},
                         "batch": {"delay_between_jobs_s": [0, 0], "daily_cap": 3}}, self.tr, dry_run=True, skip_discovery=True,
                        llm=lambda p: {"scores": []}, profile_loader=lambda: PROFILE, resume_loader=lambda: RESUME,
                        sync_fn=lambda tr: "ok", log=lambda *x: None)
        self.assertEqual(s["recapped"], 1)
        self.assertEqual(s["would_approve"], [])                                                  # 6 is below the threshold


class RulesInTheFlow(Base):
    def test_the_caps_apply_when_postings_are_scored_and_are_stored_with_the_note(self):
        url = "https://www.linkedin.com/jobs/view/7100001"
        self.tr.add_found({"canonical_url": url, "company": "Uplers", "role": "Backend Engineer", "route": "manual",
                           "source": "linkedin", "source_url": url, "experience_asked": "3-5 yrs", "location": "Bengaluru",
                           "description": "Python Django. " * 20})
        jid = self.tr.job(url)["id"]
        R.score_unscored(self.tr, lambda p: {"scores": [{"id": jid, "score": 9, "reason": "Exact match."}]}, RESUME, PROFILE,
                         log=lambda *a: None, rules=R.Rules())
        r = self.tr.by_ids([jid])[0]
        self.assertEqual(r["relevance"], 6)
        self.assertIn("model said 9, capped at 6", r["relevance_reason"])

    def test_the_prompt_states_the_rules_and_asks_for_the_exceptional_flag(self):
        self.add(1)
        prompt = R.build_prompt(self.rows(), "SUMMARY")
        for needle in ("MINIMUM experience is 3 or more years scores at most 6", '"exceptional"', "no company name scores at most 6",
                       "recruiting platform, staffing firm or job aggregator", "must reflect every caveat", "Java-centric",
                       "at most 5"):
            self.assertIn(needle, prompt)

    def test_rules_come_from_the_config_with_the_defaults(self):
        r = R.rules_from_config({})
        self.assertEqual((r.cap, r.min_years_from, r.aggregator_action, r.java_cap), (6, 3, "cap", 5))
        self.assertEqual(r.aggregators, ["Uplers", "Ibrowsejobs", "Jobgether"])
        mine = R.rules_from_config({"selection": {"score_cap": 5, "cap_min_years_from": 4, "java_cap": 4},
                                    "discovery": {"aggregators": ["X"], "aggregator_action": "drop"}})
        self.assertEqual((mine.cap, mine.min_years_from, mine.java_cap, mine.aggregators, mine.aggregator_action),
                         (5, 4, 4, ["X"], "drop"))
        with self.assertRaises(ValueError):
            R.rules_from_config({"discovery": {"aggregator_action": "maybe"}})

    def test_the_example_config_lists_the_aggregators(self):
        from jobbot import config as cfgmod
        r = R.rules_from_config(cfgmod.load_config(ROOT / "config.example.yaml"))
        for name in ("Uplers", "Ibrowsejobs", "Jobgether"):
            self.assertIn(name, r.aggregators)

    def test_drop_mode_removes_aggregators_before_scoring_and_cap_mode_keeps_them(self):
        from jobbot import discovery as D
        base = {"search_terms": ["x"], "locations": ["Bengaluru"], "sites": ["indeed"], "aggregators": ["Uplers"]}
        for action, kept in (("drop", 0), ("cap", 1)):
            self.tr.delete_found()
            self.add(1)
            self.tr.db.execute("UPDATE jobs SET company='Uplers'")
            self.tr.db.commit()
            gone = D.recheck_found(self.tr, D.load_settings({"discovery": {**base, "aggregator_action": action}}), log=lambda *a: None)
            self.assertEqual(len(self.tr.found()), kept, action)
            if action == "drop":
                self.assertEqual(gone[0]["kind"], "aggregator")

    def test_rescoring_forgets_found_scores_only_and_shows_the_top(self):
        a, b = self.add(1), self.add(2)
        self.tr.set_relevance(a, 9, "old")
        self.tr.set_relevance(b, 5, "old")
        self.tr.decide([b], approve=True)
        self.assertEqual(self.tr.clear_relevance(), 1)                                    # b is no longer Found
        self.assertEqual(self.tr.by_ids([b])[0]["relevance"], 5)
        self.tr.set_relevance(a, 8, "new reason")
        text = R.format_top(self.tr.found(), 10)
        self.assertIn("| 1 | " + str(a) + " | 8 |", text)
        self.assertIn("new reason", text)
        self.assertIn("TOP 1 BY RELEVANCE", text)


if __name__ == "__main__":
    unittest.main()
