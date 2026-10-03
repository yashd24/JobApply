"""run.py: the whole unattended flow, with fakes for Claude, the job sites, the browser and the sheet."""
import json
import os
import random
import sys
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import apply  # noqa: E402
import batch  # noqa: E402
import persona  # noqa: E402
import run  # noqa: E402
import tailor  # noqa: E402
from jobbot import config as cfgmod  # noqa: E402
from jobbot import tracker as T  # noqa: E402

CFG = {"default_mode": "dry-run", "platforms": {"greenhouse": {"mode": "assist"}},
       "selection": {"relevance_threshold": 7}, "batch": {"delay_between_jobs_s": [0, 0], "daily_cap": 3},
       "run": {"greenhouse_mode": "auto"}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="run test ")
        self.root = Path(self.tmp.name)
        self.out = self.root / "output"
        self.tr = T.Tracker(self.out / "tracker.sqlite3")
        self.patch = mock.patch.object(tailor, "OUTPUT_DIR", self.out)
        self.patch.start()
        self.steps, self.logs = [], []

    def tearDown(self):
        self.patch.stop()
        self.tr.close()
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    def add(self, n, route="manual", company=None, role=None, description="Build Python APIs. 1-2 years of experience."):
        url = {"greenhouse": f"https://job-boards.greenhouse.io/co{n}/jobs/{n}", "lever": f"https://jobs.lever.co/co{n}/{n:08d}-1111-2222-3333-444455556666"}.get(route, f"https://www.linkedin.com/jobs/view/{4000000 + n}")
        self.tr.add_found({"canonical_url": url, "company": company or f"Co{n}", "role": role or f"Backend Engineer {n}",
                           "route": route, "source": "linkedin", "source_url": url, "experience_asked": "1-2 yrs",
                           "location": "Bengaluru", "platform": route, "description": description,
                           "date_posted": f"2026-10-{n:02d}"})
        return self.tr.job(url)["id"]

    def scorer(self, by_company=None, default=8):
        def llm(prompt):
            self.steps.append("score")
            ids = [int(x) for x in __import__("re").findall(r"### JOB (\d+)", prompt)]
            out = []
            for i in ids:
                row = self.tr.by_ids([i])[0]
                out.append({"id": i, "score": (by_company or {}).get(row["company"], default), "reason": f"why {row['company']}"})
            return {"scores": out}
        return llm

    def go(self, *, discover_fn=None, llm=None, process=None, sync_fn=None, cfg=None, fetch_text=None, board_http=None, **kw):
        def default_process(row):
            self.steps.append(f"process {row['company']}")
            self.tr.set_state(row["id"], "ready_for_you", "ready")
            return "ready_for_you"
        return run.run_all(cfg or CFG, self.tr, discover_fn=discover_fn, llm=llm or self.scorer(),
                           profile_loader=persona.profile, resume_loader=persona.resume,
                           process=process or default_process, sync_fn=sync_fn or (lambda tr: self.steps.append("sync") or "synced"),
                           sleep=lambda s: self.steps.append("wait") if s == 0 else None, rng=random.Random(1),
                           fetch_text=fetch_text or (lambda url: None), board_http=board_http or (lambda url: (404, "")),
                           log=lambda *a: self.logs.append(" ".join(map(str, a))), **kw)

    def row(self, jid):
        return self.tr.by_ids([jid])[0]


class Flow(Base):
    def test_the_steps_run_in_order_and_the_best_are_approved_up_to_the_daily_cap(self):
        ids = {c: self.add(i, company=c) for i, c in enumerate(["A", "B", "C", "D", "E", "F"], 1)}
        scores = {"A": 9, "B": 8, "C": 7, "D": 7, "E": 6, "F": 5}

        def discover():
            self.steps.append("discover")
            return SimpleNamespace(scraped=40, dropped={"experience": [1, 2, 3], "location": [1]}, saved={"new": 6}, errors=[])
        s = self.go(discover_fn=discover, llm=self.scorer(scores))
        self.assertEqual(s["approved"], 3)                                             # the daily cap is 3
        self.assertEqual([r["company"] for r in self.tr.db.execute("SELECT company FROM jobs WHERE status='ready_for_you' ORDER BY relevance DESC, id")],
                         ["A", "B", "D"])                                       # C and D both score 7: the newer one wins
        self.assertEqual(self.row(ids["C"])["status"], "found")                        # at the threshold but over the cap
        self.assertEqual(self.row(ids["F"])["status"], "found")                        # below the threshold
        order = [x for x in self.steps if x != "wait"]
        self.assertEqual(order[0], "discover")
        self.assertLess(order.index("score"), order.index("process A"))
        self.assertEqual(order[-1], "sync")
        self.assertEqual([x for x in order if x.startswith("process")], ["process A", "process B", "process D"])
        self.assertEqual(self.steps.count("wait"), 2)                                  # between 3 jobs
        self.assertEqual((s["scraped"], s["found_new"], s["dropped"], s["scored"]), (40, 6, {"experience": 3, "location": 1}, 6))
        self.assertEqual((s["ready_for_you"], s["submitted"], s["needs_review"], s["failed"]), (3, 0, 0, 0))
        self.assertEqual(self.row(ids["A"])["reason"], "ready")

    def test_the_approval_reason_records_the_score_and_why(self):
        jid = self.add(1, company="A")
        approvals = []

        def process(row):
            approvals.append(row["reason"])
            self.tr.set_state(row["id"], "ready_for_you", "ready")
            return "ready_for_you"
        self.go(llm=self.scorer({"A": 9}), process=process)
        self.assertIn("auto-approved: relevance 9/10 (threshold 7): why A", approvals[0])

    def test_the_threshold_comes_from_the_config(self):
        for i, c in enumerate("ABC", 1):
            self.add(i, company=c)
        cfg = {**CFG, "selection": {"relevance_threshold": 9}}
        s = self.go(llm=self.scorer({"A": 9, "B": 8, "C": 7}), cfg=cfg)
        self.assertEqual(s["approved"], 1)

    def test_todays_earlier_jobs_and_jobs_already_waiting_use_up_the_cap(self):
        for i, c in enumerate("ABCDE", 1):
            self.add(i, company=c)
        old = self.add(9, company="Waiting")
        self.tr.decide([old], approve=True)                                            # already approved, waiting
        self.tr.batch_finish(self.tr.batch_start(old + 100), "ready_for_you")        # one started earlier today
        s = self.go(llm=self.scorer(default=9))
        self.assertEqual(s["approved"], 1)                                             # cap 3 - 1 started - 1 waiting

    def test_the_outcomes_are_counted_by_kind(self):
        for i, c in enumerate("ABCDE", 1):
            self.add(i, company=c)
        outcomes = {"A": "submitted", "B": "ready_for_you", "C": "needs_review", "D": "failed", "E": "manual"}
        cfg = {**CFG, "batch": {"delay_between_jobs_s": [0, 0], "daily_cap": 5}}

        def process(row):
            self.tr.set_state(row["id"], outcomes[row["company"]], "x")
            return outcomes[row["company"]]
        s = self.go(process=process, cfg=cfg)
        self.assertEqual([s[k] for k in ("submitted", "ready_for_you", "needs_review", "failed", "manual")], [1, 1, 1, 1, 1])
        self.assertEqual(len(s["jobs"]), 5)
        self.assertEqual(s["jobs"][0]["relevance"], 8)

    def test_a_posting_that_is_already_in_the_tracker_is_never_approved(self):
        twin = self.add(2, company="Done Co", role="Backend Engineer")                 # found, scored well ...
        self.tr.db.execute("INSERT INTO jobs (canonical_url, status, company, role, fingerprint, created_at) VALUES "
                           "('https://x.example/applied', 'submitted', 'Done Co', 'Backend Engineer', ?, 'x')",
                           (T.fingerprint("Done Co", "Backend Engineer"),))            # ... but applied for elsewhere meanwhile
        self.tr.db.commit()
        seen = []
        s = self.go(llm=self.scorer(default=9), process=lambda row: seen.append(row["id"]) or "ready_for_you")
        self.assertEqual((s["approved"], seen), (0, []))
        self.assertEqual(self.row(twin)["status"], "skipped")
        self.assertIn("already in the tracker as Submitted", self.row(twin)["reason"])


class DryRun(Base):
    def test_a_dry_run_scores_for_real_but_approves_processes_and_submits_nothing(self):
        for i, c in enumerate("ABC", 1):
            self.add(i, company=c)
        called = []
        s = self.go(dry_run=True, llm=self.scorer({"A": 9, "B": 8, "C": 4}), process=lambda row: called.append(row) or "x")
        self.assertEqual(called, [])
        self.assertEqual((s["dry_run"], s["approved"], s["scored"]), (True, 0, 3))
        self.assertEqual([r["status"] for r in self.tr.by_ids([1, 2, 3])], ["found"] * 3)
        self.assertEqual([(w["company"], w["relevance"]) for w in s["would_approve"]], [("A", 9), ("B", 8)])
        self.assertEqual(self.tr.batch_count_today(), 0)
        text = "\n".join(self.logs)
        self.assertIn("DRY RUN: would auto-approve 2", text)
        self.assertIn("why A", text)                                                    # the scored list shows the reasons

    def test_the_scores_a_dry_run_stored_are_not_paid_for_again(self):
        self.add(1, company="A")
        self.go(dry_run=True, llm=self.scorer({"A": 9}))
        self.steps.clear()
        self.go(llm=self.scorer({"A": 1}))                                             # a real run: the stored 9 stands
        self.assertNotIn("score", self.steps)
        self.assertEqual(self.tr.by_ids([1])[0]["relevance"], 9)


class Stops(Base):
    def test_a_usage_limit_while_scoring_stops_the_claude_steps_but_still_syncs_and_reports(self):
        self.add(1, company="A")

        def limited(prompt):
            raise tailor.UsageLimitError("limit")
        called = []
        s = self.go(llm=limited, process=lambda row: called.append(row) or "x")
        self.assertIn("usage limit", s["stopped"])
        self.assertEqual((called, s["approved"]), ([], 0))
        self.assertIn("sync", self.steps)
        self.assertEqual(s["unscored_left"], 1)

    def test_a_usage_limit_while_processing_stops_and_the_job_resumes_next_run(self):
        ids = [self.add(i, company=c) for i, c in enumerate("AB", 1)]
        folder = self.root / "kept"
        folder.mkdir()

        def process(row):
            err = tailor.UsageLimitError("limit")
            err.job_dir = folder
            raise err
        s = self.go(process=process)
        self.assertIn("usage limit", s["stopped"])
        a = self.row(ids[0])
        self.assertEqual((a["status"], a["job_folder"]), ("approved", str(folder)))
        self.steps.clear()
        self.go(llm=self.scorer())                                                      # next run: A first, then B
        self.assertEqual([x for x in self.steps if x.startswith("process")], ["process A", "process B"])

    def test_discovery_failing_does_not_stop_scoring_or_processing(self):
        self.add(1, company="A")

        def broken():
            from jobbot import discovery
            raise discovery.DiscoveryError("python-jobspy is not installed. Run: pip install -U python-jobspy")
        s = self.go(discover_fn=broken)
        self.assertIn("python-jobspy is not installed", s["errors"][0])
        self.assertEqual(s["ready_for_you"], 1)

    def test_a_scoring_problem_or_a_sheet_problem_is_recorded_not_fatal(self):
        self.add(1, company="A")

        def boom(prompt):
            raise ValueError("garbled")

        def bad_sync(tr):
            raise RuntimeError("offline")
        s = self.go(llm=boom, sync_fn=bad_sync)
        joined = " ".join(s["errors"])
        self.assertIn("sheet: RuntimeError: offline", joined)
        self.assertIsInstance(s["finished"], str)


class Summary(Base):
    def test_the_summary_file_is_named_by_date_and_a_second_run_keeps_the_first(self):
        s = self.go()
        first = run.write_summary(s)
        self.assertEqual(first, self.out / "runs" / f"{s['date']}.json")
        again = run.write_summary({**s, "started": s["date"] + "T13:45:09"})
        self.assertNotEqual(again, first)
        self.assertTrue(again.name.startswith(s["date"] + "_134509"))
        data = json.loads(first.read_text(encoding="utf-8"))
        for key in ("scraped", "dropped", "found_new", "scored", "approved", "submitted", "ready_for_you", "needs_review",
                    "stopped", "errors", "jobs", "threshold", "dry_run"):
            self.assertIn(key, data)

    def test_the_printed_summary_names_every_number_asked_for(self):
        for i, c in enumerate("AB", 1):
            self.add(i, company=c)
        s = self.go(discover_fn=lambda: SimpleNamespace(scraped=50, dropped={"title": [1] * 7, "experience": [1] * 20},
                                                        saved={"new": 2}, errors=[]))
        text = run.format_summary(s)
        for needle in ("found 2 new", "scraped 50", "title 7", "experience 20", "scored 2", "approved 2", "submitted 0",
                       "ready for you 2", "needs review 0"):
            self.assertIn(needle, text)

    def test_the_shortlist_json_is_refreshed_by_a_run(self):
        self.add(1, company="Low")
        self.go(llm=self.scorer({"Low": 3}))
        data = json.loads((self.out / "shortlist.json").read_text(encoding="utf-8"))
        self.assertEqual([(r["company"], r["relevance_score"]) for r in data], [("Low", 3)])
        self.assertFalse((self.out / "shortlist.html").exists())

    def test_the_scored_list_marks_what_would_be_approved(self):
        for i, c in enumerate("AB", 1):
            self.add(i, company=c)
        self.go(dry_run=True, llm=self.scorer({"A": 9, "B": 4}))
        text = run.format_scored(self.tr.found(), 7)
        self.assertLess(text.index("Co" if False else "why A"), text.index("why B"))
        self.assertIn("9* ", text)
        self.assertIn("4  ", text)


class UnattendedWiring(Base):
    def test_greenhouse_is_run_in_auto_mode_unattended_with_the_approval_for_that_url_and_the_rest_are_prepared(self):
        calls = []
        g, l, m = (self.add(1, "greenhouse", "G"), self.add(2, "lever", "L"), self.add(3, "manual", "M"))
        cfg = {**CFG, "batch": {"delay_between_jobs_s": [0, 0], "daily_cap": 5}}
        proc = batch.make_processor(self.tr, cfg, apply_run=lambda url, **kw: calls.append((url, kw)),
                                    manual_prepare=lambda row, **kw: calls.append(("manual", row["canonical_url"])),
                                    profile_loader=persona.profile, resume_loader=persona.resume,
                                    unattended=True, greenhouse_mode=run.greenhouse_mode_of(cfg))
        for jid in (g, l, m):
            self.tr.decide([jid], approve=True)
            proc(self.row(jid))
        (u1, kw1), (u2, kw2), (k3, u3) = calls
        self.assertEqual(k3, "manual")
        self.assertEqual((kw1["mode"], kw1["unattended"], kw1["allow_submit"]), ("auto", True, u1))
        self.assertEqual((kw2["mode"], kw2["prepare_interactive"]), ("prepare", False))
        self.assertNotIn("allow_submit", kw2)
        self.assertEqual(u3, "https://www.linkedin.com/jobs/view/4000003")

    def test_the_greenhouse_mode_is_validated(self):
        self.assertEqual(run.greenhouse_mode_of({}), "auto")
        self.assertEqual(run.greenhouse_mode_of({"run": {"greenhouse_mode": "assist"}}), "assist")
        with self.assertRaises(cfgmod.ConfigError):
            run.greenhouse_mode_of({"run": {"greenhouse_mode": "yolo"}})
        with self.assertRaises(cfgmod.ConfigError):
            run.threshold_of({"selection": {"relevance_threshold": "high"}})
        self.assertEqual(run.threshold_of({}), 7)


class Locking(Base):
    def test_two_runs_cannot_overlap_and_a_stale_lock_is_ignored(self):
        path = self.out / "run.lock"
        with run.Lock(path):
            self.assertTrue(path.exists())
            with self.assertRaises(run.AlreadyRunning):
                with run.Lock(path):
                    pass
        self.assertFalse(path.exists())                                               # released
        path.write_text("pid 1", encoding="utf-8")
        old = time.time() - (run.LOCK_MAX_AGE_S + 60)
        os.utime(path, (old, old))
        with run.Lock(path):                                                          # a crashed run's lock expires
            pass


class Main(Base):
    def main(self, argv, summary=None):
        import contextlib
        import io
        out = io.StringIO()
        summary = summary or {"date": "2026-10-04", "started": "2026-10-04T07:00:00", "dry_run": False, "scraped": 0,
                              "dropped": {}, "found_new": 0, "already_tracked": 0, "scored": 0, "unscored_left": 0,
                              "approved": 0, "submitted": 0, "ready_for_you": 0, "needs_review": 0, "manual": 0, "failed": 0,
                              "stopped": "", "errors": [], "jobs": [], "threshold": 7}
        seen = {}
        with mock.patch.object(apply, "CONFIG_FILE", self.root / "config.yaml"), \
                mock.patch.object(run, "run_all", lambda cfg, tr, **kw: seen.update(kw) or summary), \
                contextlib.redirect_stdout(out):
            code = run.main(argv)
        return code, out.getvalue(), seen

    def test_a_run_writes_the_summary_and_the_log_and_releases_the_lock(self):
        (self.root / "config.yaml").write_text("selection: {relevance_threshold: 7}\n", encoding="utf-8")
        code, out, seen = self.main([])
        self.assertEqual(code, 0)
        self.assertIn("RUN SUMMARY 2026-10-04", out)
        self.assertTrue((self.out / "runs" / "2026-10-04.json").exists())
        self.assertFalse((self.out / "run.lock").exists())
        logs = list((self.out / "runs").glob("*.log"))
        self.assertEqual(len(logs), 1)
        self.assertIn("run.py", logs[0].read_text(encoding="utf-8"))
        self.assertEqual((seen["dry_run"], seen["skip_discovery"]), (False, False))

    def test_the_flags_reach_the_run(self):
        (self.root / "config.yaml").write_text("selection: {relevance_threshold: 7}\n", encoding="utf-8")
        code, out, seen = self.main(["--dry-run", "--skip-discovery"])
        self.assertEqual((seen["dry_run"], seen["skip_discovery"]), (True, True))

    def test_a_run_already_in_progress_exits_with_3_without_touching_anything(self):
        (self.root / "config.yaml").write_text("selection: {relevance_threshold: 7}\n", encoding="utf-8")
        (self.out / "run.lock").write_text("pid 1", encoding="utf-8")
        code, out, seen = self.main([])
        self.assertEqual(code, 3)
        self.assertIn("Not started", out)
        self.assertEqual(seen, {})
        self.assertTrue((self.out / "run.lock").exists())                              # someone else's: left alone

    def test_a_bad_config_exits_with_2_and_a_message(self):
        (self.root / "config.yaml").write_text("selection: nope\n", encoding="utf-8")
        code, out, seen = self.main([])
        self.assertEqual(code, 2)
        self.assertIn("ConfigError: selection must be a mapping", out)
        self.assertEqual(seen, {})
        self.assertFalse((self.out / "run.lock").exists())


if __name__ == "__main__":
    unittest.main()
