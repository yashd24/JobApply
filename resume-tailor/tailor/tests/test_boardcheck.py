"""The strict company-board check, and fetching missing descriptions before scoring. Fake Greenhouse/Lever APIs: no network."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("JOBBOT_NO_SHEET", "1")      # a test must never reach the real Google Sheet
sys.path.insert(0, str(ROOT / "tests"))

import persona  # noqa: E402
import tailor  # noqa: E402
from unittest import mock  # noqa: E402
from jobbot import boardcheck as B  # noqa: E402
from jobbot import discovery as D  # noqa: E402
from jobbot import tracker as T  # noqa: E402

S = D.load_settings({"discovery": {"search_terms": ["x"], "locations": ["Bengaluru"], "sites": ["indeed"]}})
NOW = datetime(2026, 10, 4, 8, 0)


class FakeApis:
    """Greenhouse boards-api and Lever api/jobs pages, keyed by URL. Records every request."""

    def __init__(self):
        self.pages: dict = {}
        self.requests: list = []

    def gh(self, token, name, jobs):
        """jobs: [(id, title, location, live=True)]"""
        self.pages[f"https://boards-api.greenhouse.io/v1/boards/{token}"] = (200, json.dumps({"name": name}))
        self.pages[f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"] = (200, json.dumps({"jobs": [
            {"id": j[0], "title": j[1], "location": {"name": j[2]}} for j in jobs]}))
        for j in jobs:
            live = j[3] if len(j) > 3 else True
            self.pages[f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{j[0]}"] = \
                (200, json.dumps({"id": j[0]})) if live else (404, "")

    def lever(self, site, company_in_page, postings):
        """postings: [(id, title, location, live=True)]"""
        self.pages[f"https://api.lever.co/v0/postings/{site}?mode=json"] = (200, json.dumps([
            {"id": p[0], "text": p[1], "categories": {"location": p[2]}, "hostedUrl": f"https://jobs.lever.co/{site}/{p[0]}"}
            for p in postings]))
        for p in postings:
            live = p[3] if len(p) > 3 else True
            self.pages[f"https://jobs.lever.co/{site}/{p[0]}"] = (200, f"<html><head><title>{company_in_page} - {p[1]}</title></head></html>")
            self.pages[f"https://api.lever.co/v0/postings/{site}/{p[0]}"] = (200, json.dumps({"id": p[0]})) if live else (404, "")

    def __call__(self, url):
        self.requests.append(url)
        return self.pages.get(url, (404, ""))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tr = T.Tracker(Path(self.tmp.name) / "t.sqlite3")
        self.api = FakeApis()
        self.logs = []

    def tearDown(self):
        self.tr.close()
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    def add(self, company="Acme Robotics", title="Backend Engineer", location="Bengaluru, Karnataka, India", remote=False,
            relevance=8, n=1, route="manual"):
        """Postings of one company must differ in title (the tracker drops a same-company, same-title twin)."""
        url = f"https://www.linkedin.com/jobs/view/{5000000 + n}"
        self.tr.add_found({"canonical_url": url, "company": company, "role": title, "route": route, "source": "linkedin",
                           "source_url": url, "experience_asked": "1-2 yrs", "location": location, "remote": remote,
                           "description": "x" * 300})
        jid = self.tr.job(url)["id"]
        if relevance is not None:
            self.tr.set_relevance(jid, relevance, "fits")
        return jid

    def check(self, **kw):
        kw.setdefault("sleep", lambda s: None)
        return B.check_found(self.tr, S, http=self.api, now=lambda: NOW, log=lambda *a: self.logs.append(" ".join(map(str, a))), **kw)

    def row(self, jid):
        return self.tr.by_ids([jid])[0]

    def evidence(self, jid):
        return json.loads(self.row(jid)["route_evidence"])


class Matches(Base):
    def test_greenhouse_same_company_title_location_live_is_rerouted_with_evidence(self):
        self.api.gh("acmerobotics", "Acme Robotics, Inc.", [(111, "Backend Engineer", "Bengaluru, India"),
                                                            (112, "Data Engineer", "Bengaluru, India")])
        jid = self.add()
        rep = self.check()
        r = self.row(jid)
        self.assertEqual((r["route"], r["platform"], r["status"]), ("greenhouse", "greenhouse", "found"))
        self.assertEqual(r["canonical_url"], "https://job-boards.greenhouse.io/acmerobotics/jobs/111")
        self.assertEqual(r["direct_url"], r["canonical_url"])
        self.assertEqual(r["source_url"], "https://www.linkedin.com/jobs/view/5000001")            # where it was found is kept
        self.assertIn("board check", r["reason"])
        e = self.evidence(jid)
        self.assertEqual((e["matched"], e["platform"], e["board"], e["job_id"]), (True, "greenhouse", "acmerobotics", "111"))
        self.assertEqual((e["company"]["ours"], e["company"]["theirs"], e["company"]["equal"]),
                         ("Acme Robotics", "Acme Robotics, Inc.", True))
        self.assertEqual((e["title"]["ours"], e["title"]["theirs"]), ("Backend Engineer", "Backend Engineer"))
        self.assertIn("Bengaluru", e["location"]["rule"])
        self.assertEqual(e["live"]["status"], 200)
        self.assertEqual((rep.checked, len(rep.rerouted)), (1, 1))
        text = "\n".join(self.logs)
        for needle in ("BOARD MATCH", "company  'Acme Robotics' = 'Acme Robotics, Inc.'", "title    'Backend Engineer'",
                       "live     boards-api single job -> HTTP 200"):
            self.assertIn(needle, text)

    def test_lever_needs_the_company_confirmed_from_the_posting_page(self):
        self.api.lever("acmerobotics", "Acme Robotics", [("abc-123", "Backend Engineer", "Bangalore, India")])
        jid = self.add()
        self.check()
        r = self.row(jid)
        self.assertEqual((r["route"], r["canonical_url"]), ("lever", "https://jobs.lever.co/acmerobotics/abc-123"))
        e = self.evidence(jid)
        self.assertEqual(e["company"]["how"], "Lever page title")
        self.assertEqual(e["company"]["theirs"], "Acme Robotics")

    def test_remote_in_india_matches_remote_in_india(self):
        self.api.gh("acmerobotics", "Acme Robotics", [(7, "Backend Engineer", "Remote - India")])
        jid = self.add(location="India", remote=True)
        self.check()
        self.assertEqual(self.row(jid)["route"], "greenhouse")
        self.assertIn("remote in India", self.evidence(jid)["location"]["rule"])

    def test_a_rerouted_posting_then_follows_the_greenhouse_path(self):
        from jobbot import discovery
        self.api.gh("acmerobotics", "Acme Robotics", [(111, "Backend Engineer", "Bengaluru, India")])
        jid = self.add()
        self.check()
        self.tr.decide([jid], approve=True)
        self.assertEqual(discovery.route_label(self.row(jid), {"platforms": {"greenhouse": {"mode": "assist"}}}),
                         "Greenhouse -> assist")


class Refusals(Base):
    """Anything weaker than a full match stays on its original route."""

    def stays(self, jid, fragment):
        r = self.row(jid)
        self.assertEqual((r["route"], r["canonical_url"]), ("manual", "https://www.linkedin.com/jobs/view/5000001"))
        e = self.evidence(jid)
        self.assertFalse(e["matched"])
        self.assertIn(fragment, " | ".join(e["tried"]))

    def test_a_title_with_a_suffix_is_not_the_same_title(self):
        self.api.gh("acmerobotics", "Acme Robotics", [(111, "Backend Engineer", "Bengaluru, India")])
        jid = self.add(title="Backend Engineer, India")
        self.check()
        self.stays(jid, "none titled 'Backend Engineer, India'")

    def test_every_word_of_the_title_must_match_not_just_the_part_the_fingerprint_keeps(self):
        self.api.gh("acmerobotics", "Acme Robotics", [(111, "Backend Engineer", "Bengaluru, India"),
                                                      (112, "Backend Engineer (Python)", "Bengaluru, India")])
        for n, title in enumerate(["Backend Engineer, India", "Backend Engineer - Python Platform", "Backend Engineer (Python) II"], 1):
            jid = self.add(title=title, n=n)
            self.check()
            self.assertEqual(self.row(jid)["route"], "manual", title)

    def test_case_and_spacing_do_not_matter(self):
        self.api.gh("acmerobotics", "Acme Robotics", [(111, "Backend Engineer", "Bengaluru, India")])
        exact = self.add(title="backend  ENGINEER")
        self.check()
        self.assertEqual(self.row(exact)["route"], "greenhouse")

    def test_a_different_city_is_not_the_same_location(self):
        self.api.gh("acmerobotics", "Acme Robotics", [(111, "Backend Engineer", "Pune, India")])
        jid = self.add()
        self.check()
        self.stays(jid, "location differs")

    def test_a_state_only_location_is_never_enough(self):
        self.api.gh("acmerobotics", "Acme Robotics", [(111, "Backend Engineer", "Bengaluru, India")])
        jid = self.add(location="KA, IN")
        self.check()
        self.stays(jid, "location differs")

    def test_remote_must_be_remote_in_india(self):
        self.api.gh("acmerobotics", "Acme Robotics", [(7, "Backend Engineer", "Remote - US")])
        jid = self.add(location="India", remote=True)
        self.check()
        self.stays(jid, "location differs")

    def test_a_board_of_a_different_company_with_a_similar_token_is_not_used(self):
        self.api.gh("acmerobotics", "Acme Robotics Holdings Group", [(111, "Backend Engineer", "Bengaluru, India")])
        jid = self.add()
        self.check()
        self.stays(jid, "board is 'Acme Robotics Holdings Group', not 'Acme Robotics'")

    def test_a_lever_page_that_names_another_company_is_not_used(self):
        self.api.lever("acmerobotics", "Acme Robotics Staffing", [("abc-123", "Backend Engineer", "Bangalore, India")])
        jid = self.add()
        self.check()
        self.stays(jid, "its page names 'Acme Robotics Staffing'")

    def test_a_job_the_api_no_longer_serves_is_not_live(self):
        self.api.gh("acmerobotics", "Acme Robotics", [(111, "Backend Engineer", "Bengaluru, India", False)])
        jid = self.add()
        self.check()
        self.stays(jid, "not live")
        self.api.lever("acmerobotics", "Acme Robotics", [("abc", "Platform Engineer", "Bangalore, India", False)])
        jid2 = self.add(title="Platform Engineer", n=2)
        self.check()
        self.assertEqual(self.row(jid2)["route"], "manual")
        self.assertIn("not live", " | ".join(self.evidence(jid2)["tried"]))

    def test_two_equally_good_jobs_on_one_board_is_ambiguous(self):
        self.api.gh("acmerobotics", "Acme Robotics", [(111, "Backend Engineer", "Bengaluru, India"),
                                                      (112, "Backend Engineer", "Bangalore")])
        jid = self.add()
        self.check()
        self.stays(jid, "ambiguous: 2 postings fit")

    def test_a_fit_on_both_greenhouse_and_lever_is_ambiguous(self):
        self.api.gh("acmerobotics", "Acme Robotics", [(111, "Backend Engineer", "Bengaluru, India")])
        self.api.lever("acmerobotics", "Acme Robotics", [("abc", "Backend Engineer", "Bangalore, India")])
        jid = self.add()
        self.check()
        self.stays(jid, "ambiguous")

    def test_no_company_name_no_check(self):
        jid = self.add(company="")
        self.check()
        self.assertIn("no company name", self.evidence(jid)["tried"][0])
        self.assertEqual(self.api.requests, [])

    def test_an_unknown_company_or_failing_apis_leave_it_alone(self):
        jid = self.add(company="Nowhere Ltd")
        rep = self.check()
        self.assertEqual((rep.checked, len(rep.rerouted)), (1, 0))
        self.assertEqual(self.row(jid)["route"], "manual")

        def boom(url):
            raise OSError("network down")
        jid2 = self.add(company="Other Co", n=2)
        B.check_found(self.tr, S, http=boom, sleep=lambda s: None, now=lambda: NOW, log=lambda *a: None)
        self.assertEqual(self.row(jid2)["route"], "manual")

    def test_the_same_posting_already_in_the_tracker_is_skipped_never_applied_twice(self):
        self.api.gh("acmerobotics", "Acme Robotics", [(111, "Backend Engineer", "Bengaluru, India")])
        self.tr.db.execute("INSERT INTO jobs (canonical_url, status, company, role, fingerprint, created_at) VALUES "
                           "(?, 'submitted', 'Acme Robotics', 'Backend Engineer Old', 'x|y', 'x')",
                           (T.key("https://job-boards.greenhouse.io/acmerobotics/jobs/111"),))
        self.tr.db.commit()
        jid = self.add()
        rep = self.check()
        r = self.row(jid)
        self.assertEqual((r["status"], rep.rerouted[0]["outcome"]), ("skipped", "duplicate"))
        self.assertIn("already in the tracker as Submitted", r["reason"])


class WhichPostingsAreChecked(Base):
    def setUp(self):
        super().setUp()
        self.api.gh("acmerobotics", "Acme Robotics", [(111, "Backend Engineer", "Bengaluru, India")])

    def test_only_scored_found_manual_postings_at_or_above_the_minimum(self):
        low = self.add(relevance=3, n=1)
        unscored = self.add(title="Backend Engineer U", relevance=None, n=2)
        lever_route = self.add(title="Backend Engineer L", route="lever", n=3)
        approved = self.add(title="Backend Engineer A", n=4)
        self.tr.decide([approved], approve=True)
        rep = self.check(min_relevance=5)
        self.assertEqual(rep.checked, 0)
        self.assertEqual([self.row(i)["route"] for i in (low, unscored)], ["manual", "manual"])
        self.assertEqual(self.api.requests, [])

    def test_a_check_is_remembered_for_a_week(self):
        jid = self.add(company="Nowhere Ltd")
        self.check()
        n = len(self.api.requests)
        self.assertGreater(n, 0)
        self.assertEqual(self.check().checked, 0)                                       # same day: not repeated
        self.assertEqual(len(self.api.requests), n)
        later = B.check_found(self.tr, S, http=self.api, sleep=lambda s: None, now=lambda: NOW + timedelta(days=8),
                              log=lambda *a: None)
        self.assertEqual(later.checked, 1)                                              # a week on: looked at again

    def test_a_dry_run_changes_nothing_and_does_not_use_up_the_weekly_check(self):
        jid = self.add()
        rep = self.check(dry_run=True)
        self.assertEqual((len(rep.rerouted), rep.rerouted[0]["dry_run"]), (1, True))
        r = self.row(jid)
        self.assertEqual((r["route"], r["route_evidence"], r["board_checked"]), ("manual", None, None))
        self.assertEqual(self.check().checked, 1)                                       # the real run still checks it

    def test_requests_are_cached_per_company_within_a_run(self):
        self.add(n=1)
        self.add(title="Data Engineer", n=2)
        self.add(title="Platform Engineer", n=3)
        self.check()
        board_calls = [u for u in self.api.requests if u.endswith("/boards/acmerobotics") or u.endswith("/boards/acmerobotics/jobs")]
        self.assertEqual(len(board_calls), 2)                                           # not 6

    def test_the_company_cap_limits_a_run(self):
        for i, c in enumerate(["A Co", "B Co", "C Co"], 1):
            self.add(company=c, n=i)
        self.assertEqual(self.check(max_companies=2).checked, 2)


class MissingDescriptions(Base):
    def add_bare(self, n, desc=""):
        url = f"https://www.linkedin.com/jobs/view/{6000000 + n}"
        self.tr.add_found({"canonical_url": url, "company": f"Co{n}", "role": f"Backend Engineer {n}", "route": "manual",
                           "source": "linkedin", "source_url": url, "experience_asked": "not stated", "location": "Bengaluru",
                           "description": desc})
        return self.tr.job(url)["id"]

    def test_missing_and_short_descriptions_are_fetched_stored_and_counted(self):
        a, b, c = self.add_bare(1), self.add_bare(2, "short"), self.add_bare(3, "y" * 400)
        asked = []

        def fetch(url):
            asked.append(url)
            return "Django APIs and REST. " * 20 if url.endswith("6000001") else None
        out = D.fill_descriptions(self.tr, fetch, log=lambda *a: None)
        self.assertEqual(out, {"tried": 2, "filled": 1, "still_missing": 1})
        self.assertIn("Django APIs", self.row(a)["description"])
        self.assertEqual(len(asked), 2)                                                  # one request per posting
        self.assertEqual(self.row(c)["description"], "y" * 400)                          # untouched

    def test_the_limit_and_a_failing_fetch(self):
        for i in range(1, 6):
            self.add_bare(i)
        out = D.fill_descriptions(self.tr, lambda u: (_ for _ in ()).throw(OSError("blocked")), limit=3, log=lambda *a: None)
        self.assertEqual((out["tried"], out["filled"], out["still_missing"]), (3, 0, 5))

    def test_a_dry_run_counts_without_storing(self):
        a = self.add_bare(1)
        out = D.fill_descriptions(self.tr, lambda u: "text " * 80, save=False, log=lambda *a: None)
        self.assertEqual((out["filled"], out["still_missing"]), (1, 0))
        self.assertEqual((self.row(a)["description"] or ""), "")


class InTheRun(Base):
    """run.py: descriptions are fetched BEFORE scoring, and the board check runs AFTER it, before approval."""

    CFG = {"discovery": {"search_terms": ["x"], "locations": ["Bengaluru"], "sites": ["indeed"], "max_min_experience_years": 3},
           "selection": {"relevance_threshold": 7}, "batch": {"delay_between_jobs_s": [0, 0], "daily_cap": 5},
           "run": {"greenhouse_mode": "auto"}, "platforms": {"greenhouse": {"mode": "assist"}}}

    def go(self, llm, fetch_text, board_http, cfg=None, **kw):
        import random
        import run
        seen = []

        def process(row):
            seen.append((row["id"], row["route"], row["canonical_url"]))
            self.tr.set_state(row["id"], "ready_for_you", "ready")
            return "ready_for_you"
        s = run.run_all(cfg or self.CFG, self.tr, skip_discovery=True, llm=llm, profile_loader=persona.profile,
                        resume_loader=persona.resume, process=process, sync_fn=lambda tr: "ok", sleep=lambda x: None,
                        rng=random.Random(1), fetch_text=fetch_text, board_http=board_http, log=lambda *a: self.logs.append(" ".join(map(str, a))),
                        now=lambda: NOW, **kw)
        return s, seen

    def scorer(self, prompts, score=8):
        def llm(prompt):
            import re
            prompts.append(prompt)
            ids = [int(x) for x in re.findall(r"### JOB (\d+)", prompt)]
            return {"scores": [{"id": i, "score": score, "reason": "fits"} for i in ids]}
        return llm

    def test_a_fetched_description_reaches_the_scorer_and_new_text_can_drop_the_posting(self):
        good = self.add(company="Good Co", n=1, relevance=None)
        self.tr.set_description(good, "")
        bad = self.add(company="Bad Co", n=2, relevance=None)
        self.tr.set_description(bad, "")
        prompts = []
        texts = {"https://www.linkedin.com/jobs/view/5000001": "Build Django REST APIs. 1-2 years of experience. " * 8,
                 "https://www.linkedin.com/jobs/view/5000002": "Requirements:\n8+ years;\nJava Spring. " * 8}
        s, seen = self.go(self.scorer(prompts), lambda url: texts.get(url), lambda u: (404, ""))
        self.assertEqual(s["descriptions"]["filled"], 2)
        self.assertEqual(s["dropped"], {"rechecked: experience": 1})
        self.assertIn("Build Django REST APIs", prompts[0])                              # the scorer saw the fetched text
        self.assertNotIn("Java Spring", prompts[0])                                      # the dropped one was never scored
        self.assertEqual([x[0] for x in seen], [good])                                   # the dropped one is gone for good
        self.assertEqual([r["id"] for r in self.tr.found()], [])                         # (the approved one is no longer Found)
        self.assertIn("descriptions fetched 2 of 2", __import__("run").format_summary(s))

    def test_the_fetch_can_be_turned_off(self):
        jid = self.add(n=1)
        self.tr.set_description(jid, "")
        calls = []
        cfg = {**self.CFG, "selection": {"relevance_threshold": 7, "fetch_missing_descriptions": False}}
        s, _ = self.go(self.scorer([]), lambda u: calls.append(u), lambda u: (404, ""), cfg=cfg)
        self.assertEqual((calls, s["descriptions"]["tried"]), ([], 0))

    def test_a_board_match_moves_the_posting_onto_greenhouse_before_approval_and_is_in_the_summary(self):
        api = FakeApis()
        api.gh("acmerobotics", "Acme Robotics", [(111, "Backend Engineer", "Bengaluru, India")])
        jid = self.add()
        import run
        s, seen = self.go(self.scorer([]), lambda u: None, api)
        self.assertEqual(seen, [(jid, "greenhouse", "https://job-boards.greenhouse.io/acmerobotics/jobs/111")])
        self.assertEqual((s["board_check"]["checked"], s["board_check"]["matched"]), (1, 1))
        ev = s["board_check"]["rerouted"][0]
        self.assertEqual((ev["platform"], ev["company"]["theirs"], ev["title"]["theirs"]), ("greenhouse", "Acme Robotics", "Backend Engineer"))
        text = run.format_summary(s)
        self.assertIn("board check: 1 checked, 1 re-routed onto Greenhouse/Lever", text)
        self.assertIn("https://job-boards.greenhouse.io/acmerobotics/jobs/111", text)
        self.assertIn("BOARD MATCH", "\n".join(self.logs))
        json.dumps(s)

    def test_a_posting_that_does_not_match_strictly_is_still_approved_on_its_own_route(self):
        api = FakeApis()
        api.gh("acmerobotics", "Acme Robotics", [(111, "Backend Engineer", "Pune, India")])
        self.add()
        s, seen = self.go(self.scorer([]), lambda u: None, api)
        self.assertEqual((seen[0][1], s["board_check"]["matched"]), ("manual", 0))

    def test_the_check_can_be_switched_off_and_a_failing_one_does_not_stop_the_run(self):
        self.add()
        cfg = {**self.CFG, "selection": {"relevance_threshold": 7, "board_check": False}}
        s, seen = self.go(self.scorer([]), lambda u: None, lambda u: (_ for _ in ()).throw(AssertionError("no requests")), cfg=cfg)
        self.assertEqual((s["board_check"]["checked"], len(seen)), (0, 1))


if __name__ == "__main__":
    unittest.main()
