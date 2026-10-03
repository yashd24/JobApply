"""Relevance scoring: one Claude call per 10 postings, validated by code, stored in the tracker. A fake Claude stands in."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
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


if __name__ == "__main__":
    unittest.main()
