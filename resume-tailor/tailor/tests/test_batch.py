"""Approving, preparing every approved job (not only Lever), and the batch runner: one job at a time, a delay between jobs,
a daily cap, and the usage-limit stop and resume. No network, no Claude, no browser."""
import json
import random
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import apply  # noqa: E402
import batch  # noqa: E402
import persona  # noqa: E402
import tailor  # noqa: E402
from jobbot import config as cfgmod  # noqa: E402
from jobbot import coverletter as C  # noqa: E402
from jobbot import prepare, intake  # noqa: E402
from jobbot import tracker as T  # noqa: E402
from test_coverletter import GOOD, JD, QUOTES  # noqa: E402

GH = "https://job-boards.greenhouse.io/acme/jobs/111"
LV = "https://jobs.lever.co/acme/0a1b2c3d-1111-2222-3333-444455556666"
LINKEDIN = "https://www.linkedin.com/jobs/view/4474146384"
NAUKRI = "https://www.naukri.com/job-listings-backend-engineer-acme-bengaluru-1-to-2-years-280926008860"
CFG = {"default_mode": "dry-run", "platforms": {"greenhouse": {"mode": "assist"}, "lever": {"mode": "prepare"}}}


def cfg_with(**batch_cfg):
    return {**CFG, "batch": batch_cfg}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="batch test ")
        self.root = Path(self.tmp.name)
        self.tr = T.Tracker(self.root / "output" / "tracker.sqlite3")

    def tearDown(self):
        self.tr.close()
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    def add(self, url, company, route, role="Backend Engineer", description=JD, source="indeed", source_url=None, approve=True):
        self.tr.add_found({"canonical_url": url, "company": company, "role": role, "route": route, "source": source,
                           "source_url": source_url or url, "direct_url": url if route != "manual" else None,
                           "experience_asked": "0-2 yrs", "location": "Bengaluru, India", "platform": route,
                           "description": description})
        jid = self.tr.job(url)["id"]
        if approve:
            self.tr.decide([jid], approve=True)
        return jid


class Approval(Base):
    def test_every_approved_route_waits_for_the_batch_runner_and_an_unscored_found_posting_stays_out_of_the_sheet(self):
        ids = [self.add(GH, "G", "greenhouse"), self.add(LV, "L", "lever"), self.add(LINKEDIN, "M", "manual", source="linkedin")]
        self.assertEqual([self.tr.job(u)["status"] for u in (GH, LV, LINKEDIN)], ["approved"] * 3)
        reasons = [self.tr.job(u)["reason"] for u in (GH, LV, LINKEDIN)]
        self.assertIn("assist", reasons[0])
        self.assertIn("apply by hand", reasons[1])
        self.assertIn("likely answers", reasons[2])
        out = self.tr.decide(ids, approve=True)
        self.assertEqual(out["ignored"], ids)                                    # already decided
        from jobbot import sheets
        from test_sheets import FakeSheet
        self.add("https://job-boards.greenhouse.io/acme/jobs/777", "Unscored", "greenhouse", approve=False)
        sheet = FakeSheet()
        sheets.sync(self.tr, sheet)
        shown = [sheet.row(n)[3] for n in range(2, 8) if sheet.row(n)[3]]
        self.assertEqual(sorted(shown), ["G", "L", "M"])                         # approved jobs are in the sheet ...
        self.assertNotIn("Unscored", shown)                                      # ... a Found one with no score is not

    def test_skip_still_works_and_no_manual_status_comes_from_approval(self):
        jid = self.add(LINKEDIN, "M", "manual", approve=False)
        out = self.tr.decide([jid], approve=True)
        self.assertEqual((out["approved"], "manual" in out), ([jid], False))


class FetchDescription(unittest.TestCase):
    PAGE = ('<html><body><div class="show-more-less-html__markup">' + "We build payment APIs in Python. " * 12 +
            "</div></body></html>")

    def test_linkedin_postings_use_the_public_posting_endpoint(self):
        asked = []
        text = batch.fetch_description(LINKEDIN, get=lambda u: asked.append(u) or self.PAGE)
        self.assertIn("payment APIs", text)
        self.assertEqual(asked, ["https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/4474146384"])

    def test_other_links_use_the_tailors_page_reader_and_short_or_missing_text_is_none(self):
        with mock.patch.object(tailor, "fetch_jd", return_value="Build APIs. " * 40):
            self.assertIn("Build APIs", batch.fetch_description(NAUKRI))
        with mock.patch.object(tailor, "fetch_jd", return_value="too short"):
            self.assertIsNone(batch.fetch_description(NAUKRI))
        with mock.patch.object(tailor, "fetch_jd", return_value=None):
            self.assertIsNone(batch.fetch_description(NAUKRI))
        with mock.patch.object(tailor, "fetch_jd", side_effect=OSError("down")):
            self.assertIsNone(batch.fetch_description(NAUKRI))

    def test_a_linkedin_failure_falls_back_to_the_page_reader(self):
        def boom(u):
            raise OSError("blocked")
        with mock.patch.object(tailor, "fetch_jd", return_value="Plain page text. " * 30):
            self.assertIn("Plain page", batch.fetch_description(LINKEDIN, get=boom))


class PrepareManual(Base):
    def setUp(self):
        super().setUp()
        self.out = self.root / "output"
        self.patches = [mock.patch.object(tailor, "OUTPUT_DIR", self.out),
                        mock.patch.object(apply, "CONFIG_FILE", self.root / "nc.yaml")]
        for p in self.patches:
            p.start()
        self.tailor_calls, self.llm_calls = [], []

    def tearDown(self):
        for p in self.patches:
            p.stop()
        super().tearDown()

    def fake_tailor(self, company, role, jd, url, **kw):
        self.tailor_calls.append((company, role, len(jd), kw.get("job_dir")))
        folder = Path(kw["job_dir"]) if kw.get("job_dir") else self.out / f"2026-10-03_{company}_{role}".replace(" ", "_")
        folder.mkdir(parents=True, exist_ok=True)
        (folder / tailor.PDF_NAME).write_bytes(b"%PDF-1.4 fake")
        (folder / "result.json").write_text(json.dumps({"scores": {"overall": 7}, "gaps": ["Kafka"]}), encoding="utf-8")
        return {"job_dir": str(folder), "resume_pdf": str(folder / tailor.PDF_NAME)}

    def llm(self, prompt):
        self.llm_calls.append(prompt)
        return {"letter": GOOD, "jd_quotes": QUOTES, "answers": {}}

    def prepare(self, row, **kw):
        return batch.prepare_manual(row, profile=persona.profile(), resume=persona.resume(), llm=self.llm,
                                    tailor_job=self.fake_tailor, fetch=kw.pop("fetch", lambda u: None), **kw)

    def row(self, jid):
        return self.tr.db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()

    def test_an_approved_manual_job_gets_a_resume_a_cover_letter_and_a_sheet_and_becomes_ready_for_you(self):
        jid = self.add(LINKEDIN, "Acme", "manual", source="linkedin")
        folder = self.prepare(self.row(jid))
        self.assertEqual(len(self.tailor_calls), 1)
        self.assertTrue((folder / "prepare_sheet.html").exists() and (folder / "prepare_sheet.txt").exists())
        text = (folder / "prepare_sheet.txt").read_text(encoding="utf-8")
        self.assertIn("LIKELY ANSWERS", text)
        self.assertIn(persona.profile()["personal"]["email"], text)
        self.assertIn("Hello,", text)                                              # the cover letter is in the sheet
        self.assertIn(str(folder / tailor.PDF_NAME), text)
        r = self.row(jid)
        self.assertEqual((r["status"], r["route"]), ("ready_for_you", "manual"))
        self.assertEqual(r["job_folder"], str(folder))
        self.assertIn("Mark applied", r["reason"])
        self.assertEqual(r["score"], 7)
        from jobbot import sheets
        link = sheets.action_row(r)[5]
        self.assertEqual(link, LINKEDIN)                                           # Ready for you, with the link

    def test_the_stored_description_is_used_and_a_missing_one_is_fetched_first(self):
        jid = self.add(NAUKRI, "Naukri Co", "manual", description="", source="naukri")
        fetched = []
        self.prepare(self.row(jid), fetch=lambda u: fetched.append(u) or "A real description. " * 20)
        self.assertEqual(fetched, [NAUKRI])
        self.assertEqual(self.tailor_calls[0][2], len(("A real description. " * 20).strip()))
        jid2 = self.add(LINKEDIN, "Has Text", "manual", description=JD)
        fetched.clear()
        self.prepare(self.row(jid2), fetch=lambda u: fetched.append(u) or None)
        self.assertEqual(fetched, [])

    def test_no_readable_description_means_manual_with_a_reason_and_no_claude_usage(self):
        jid = self.add(NAUKRI, "Blank Co", "manual", description="", source="naukri")
        proc = batch.make_processor(self.tr, CFG, manual_prepare=lambda row, **kw: self.prepare(row),
                                    profile_loader=persona.profile, resume_loader=persona.resume)
        self.assertEqual(proc(self.row(jid)), "manual")
        r = self.row(jid)
        self.assertEqual(r["status"], "manual")
        self.assertIn("could not read the job description", r["reason"])
        self.assertEqual((self.tailor_calls, self.llm_calls), ([], []))

    def test_a_saved_folder_is_reused_without_tailoring_again(self):
        jid = self.add(LINKEDIN, "Acme", "manual")
        folder = self.prepare(self.row(jid))
        self.tr.set_state(jid, "approved", "stopped", str(folder))
        self.tailor_calls.clear()
        self.llm_calls.clear()
        self.prepare(self.row(jid))
        self.assertEqual(self.tailor_calls, [])
        self.assertEqual(self.llm_calls, [])                                       # the saved cover letter is reused too
        self.assertEqual(self.row(jid)["status"], "ready_for_you")

    def test_a_usage_limit_during_tailoring_names_the_folder(self):
        jid = self.add(LINKEDIN, "Acme", "manual")

        def limited(*a, **kw):
            raise tailor.UsageLimitError("limit")
        with self.assertRaises(tailor.UsageLimitError):
            batch.prepare_manual(self.row(jid), profile=persona.profile(), resume=persona.resume(), llm=self.llm,
                                 tailor_job=limited, fetch=lambda u: None)

    def test_a_usage_limit_while_writing_the_letter_keeps_the_resume_folder(self):
        jid = self.add(LINKEDIN, "Acme", "manual")

        def limited(prompt):
            raise tailor.UsageLimitError("limit")
        with self.assertRaises(tailor.UsageLimitError) as cm:
            batch.prepare_manual(self.row(jid), profile=persona.profile(), resume=persona.resume(), llm=limited,
                                 tailor_job=self.fake_tailor, fetch=lambda u: None)
        self.assertTrue(Path(cm.exception.job_dir).is_dir())


class Processor(Base):
    def proc(self, **hooks):
        return batch.make_processor(self.tr, CFG, profile_loader=persona.profile, resume_loader=persona.resume, **hooks)

    def row(self, jid):
        return self.tr.db.execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()

    def test_each_route_does_the_right_thing(self):
        calls = []
        run = lambda url, **kw: calls.append(("run", url, kw))                       # noqa: E731
        manual = lambda row, **kw: calls.append(("manual", row["canonical_url"], kw))   # noqa: E731
        g, l, m = (self.add(GH, "G", "greenhouse"), self.add(LV, "L", "lever"), self.add(LINKEDIN, "M", "manual"))
        for jid in (g, l, m):
            self.proc(apply_run=run, manual_prepare=manual)(self.row(jid))
        (k1, u1, kw1), (k2, u2, kw2), (k3, u3, _) = calls
        self.assertEqual((k1, kw1["mode"], kw1["allow_submit"], kw1["resume_from"]), ("run", "assist", GH, None))
        self.assertEqual((k2, kw2["mode"], kw2["prepare_interactive"]), ("run", "prepare", False))
        self.assertNotIn("allow_submit", kw2)                                         # prepare cannot submit
        self.assertEqual((k3, u3), ("manual", LINKEDIN))

    def test_greenhouse_follows_the_configured_mode(self):
        calls = []
        cfg = {**CFG, "platforms": {"greenhouse": {"mode": "auto"}}}
        jid = self.add(GH, "G", "greenhouse")
        batch.make_processor(self.tr, cfg, apply_run=lambda url, **kw: calls.append(kw))(self.row(jid))
        self.assertEqual(calls[0]["mode"], "auto")
        self.assertEqual(calls[0]["allow_submit"], GH)                                # approved for THIS job only

    def test_a_saved_folder_is_passed_on_so_a_stopped_job_resumes(self):
        calls = []
        jid = self.add(GH, "G", "greenhouse")
        folder = self.root / "kept"
        folder.mkdir()
        self.tr.set_state(jid, "approved", "stopped", str(folder))
        self.proc(apply_run=lambda url, **kw: calls.append(kw))(self.row(jid))
        self.assertEqual(calls[0]["resume_from"], folder)

    def test_a_job_that_cannot_be_applied_for_fails_with_its_reason_and_the_queue_goes_on(self):
        jid = self.add(GH, "G", "greenhouse")

        def closed(url, **kw):
            raise SystemExit("https://x answered HTTP 404; the posting may be closed. Nothing was filled.")
        self.assertEqual(self.proc(apply_run=closed)(self.row(jid)), "failed")
        r = self.row(jid)
        self.assertEqual(r["status"], "failed")
        self.assertIn("HTTP 404", r["reason"])
        boom = lambda url, **kw: (_ for _ in ()).throw(RuntimeError("selector changed"))      # noqa: E731
        jid2 = self.add(LV, "L", "lever")
        self.assertEqual(self.proc(apply_run=boom)(self.row(jid2)), "failed")
        self.assertIn("RuntimeError: selector changed", self.row(jid2)["reason"])
        tailor_err = lambda url, **kw: (_ for _ in ()).throw(tailor.TailorError("LaTeX compile failed: x"))   # noqa: E731
        jid3 = self.add("https://job-boards.greenhouse.io/acme/jobs/9", "G3", "greenhouse")
        self.assertEqual(self.proc(apply_run=tailor_err)(self.row(jid3)), "failed")

    def test_a_profile_problem_stops_the_whole_batch_because_every_job_would_fail(self):
        jid = self.add(GH, "G", "greenhouse")

        def no_profile(url, **kw):
            raise SystemExit("profile.yaml not found. Copy profile.example.yaml to profile.yaml")
        with self.assertRaises(batch.StopBatch):
            self.proc(apply_run=no_profile)(self.row(jid))
        self.assertEqual(self.row(jid)["status"], "approved")                        # untouched

    def test_the_status_after_a_run_is_what_the_tracker_says(self):
        jid = self.add(GH, "G", "greenhouse")
        run = lambda url, **kw: self.tr.set_state(jid, "needs_review", "no confirmation was seen")     # noqa: E731
        self.assertEqual(self.proc(apply_run=run)(self.row(jid)), "needs_review")


class Queue(Base):
    def go(self, process, settings=None, **kw):
        slept = []
        rep = batch.run_batch(self.tr, CFG, settings or batch.Settings((60, 180), 10), process=process,
                              sleep=slept.append, rng=random.Random(1), log=lambda *a: None, **kw)
        return rep, slept

    def row_ids(self, n):
        return [self.add(f"https://job-boards.greenhouse.io/acme/jobs/{i}", f"Co{i}", "greenhouse") for i in range(1, n + 1)]

    def ok(self, ids_done):
        def process(row):
            ids_done.append(row["id"])
            self.tr.set_state(row["id"], "ready_for_you", "ready")
            return "ready_for_you"
        return process

    def test_jobs_run_one_at_a_time_in_order_with_a_delay_between_but_not_around_them(self):
        ids = self.row_ids(3)
        done = []
        rep, slept = self.go(self.ok(done))
        self.assertEqual(done, ids)
        self.assertEqual(len(slept), 2)                                              # between the 3 jobs, not before or after
        self.assertTrue(all(60 <= s <= 180 for s in slept))
        self.assertEqual([x[3] for x in rep.done], ["ready_for_you"] * 3)
        self.assertEqual((rep.stopped, rep.left), ("", 0))

    def test_the_delay_comes_from_the_settings(self):
        self.row_ids(3)
        _, slept = self.go(self.ok([]), batch.Settings((5, 5), 10))
        self.assertEqual(slept, [5.0, 5.0])

    def test_the_daily_cap_stops_the_batch_and_the_rest_wait_for_tomorrow(self):
        ids = self.row_ids(5)
        done = []
        rep, slept = self.go(self.ok(done), batch.Settings((1, 1), 3))
        self.assertEqual(done, ids[:3])
        self.assertIn("daily cap of 3", rep.stopped)
        self.assertEqual((rep.left, len(slept)), (2, 2))                            # no pointless wait before stopping
        self.assertEqual([self.tr.by_ids([i])[0]["status"] for i in ids[3:]], ["approved", "approved"])
        again, _ = self.go(self.ok([]), batch.Settings((1, 1), 3))                # still today: nothing more
        self.assertEqual(again.done, [])
        tomorrow = datetime.now() + timedelta(days=1)
        later = batch.run_batch(self.tr, CFG, batch.Settings((1, 1), 3), process=self.ok(done), sleep=lambda s: None,
                                now=lambda: tomorrow, log=lambda *a: None)
        self.assertEqual(len(later.done), 2)                                       # a new day, a new allowance

    def test_limit_and_only(self):
        ids = self.row_ids(4)
        done = []
        rep, _ = self.go(self.ok(done), limit=2)
        self.assertEqual((done, rep.stopped), (ids[:2], "--limit 2 reached"))
        done.clear()
        self.go(self.ok(done), only=[ids[3]])
        self.assertEqual(done, [ids[3]])

    def test_a_usage_limit_stops_at_once_keeps_the_folder_and_does_not_use_up_the_cap(self):
        ids = self.row_ids(3)
        seen = []
        folder = self.root / "kept folder"
        folder.mkdir()

        def process(row):
            seen.append(row["id"])
            if len(seen) == 2:
                err = tailor.UsageLimitError("limit")
                err.job_dir = folder
                raise err
            self.tr.set_state(row["id"], "ready_for_you", "ready")
            return "ready_for_you"
        rep, slept = self.go(process, batch.Settings((1, 1), 2))
        self.assertEqual(seen, ids[:2])                                              # the third was never started
        self.assertIn("usage limit", rep.stopped)
        self.assertIn("resumes this job first", rep.stopped)
        r = self.tr.by_ids([ids[1]])[0]
        self.assertEqual((r["status"], r["job_folder"]), ("approved", str(folder)))
        self.assertIn("usage limit", r["reason"])
        self.assertEqual(self.tr.batch_count_today(), 1)                              # the stopped job did not use the cap
        # the next run resumes that job FIRST, ahead of earlier ids, and passes the saved folder on
        calls = []
        proc = batch.make_processor(self.tr, CFG, apply_run=lambda url, **kw: calls.append((url, kw.get("resume_from"))),
                                    profile_loader=persona.profile, resume_loader=persona.resume)
        self.tr.set_state(ids[0], "approved", "waiting")                              # an earlier id is still queued
        batch.run_batch(self.tr, CFG, batch.Settings((1, 1), 10), process=proc, sleep=lambda s: None, log=lambda *a: None)
        self.assertEqual(calls[0][1], folder)
        self.assertEqual(calls[0][0], T.key("https://job-boards.greenhouse.io/acme/jobs/2"))

    def test_a_usage_limit_without_a_folder_still_stops_cleanly(self):
        ids = self.row_ids(2)

        def process(row):
            raise tailor.UsageLimitError("limit")
        rep, _ = self.go(process)
        self.assertIn("usage limit", rep.stopped)
        r = self.tr.by_ids([ids[0]])[0]
        self.assertEqual((r["status"], r["job_folder"]), ("approved", None))

    def test_a_job_that_fails_does_not_stop_the_queue(self):
        ids = self.row_ids(3)

        def process(row):
            if row["id"] == ids[1]:
                self.tr.set_state(row["id"], "failed", "refused")
                return "failed"
            self.tr.set_state(row["id"], "ready_for_you", "ready")
            return "ready_for_you"
        rep, _ = self.go(process)
        self.assertEqual([x[3] for x in rep.done], ["ready_for_you", "failed", "ready_for_you"])

    def test_a_stop_batch_ends_the_run_and_ctrl_c_is_recorded_then_propagates(self):
        self.row_ids(3)

        def stop(row):
            raise batch.StopBatch("profile.yaml not found")
        rep, _ = self.go(stop)
        self.assertIn("stopped: profile.yaml not found", rep.stopped)

        def interrupt(row):
            raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            self.go(interrupt)
        self.assertIn("interrupted", [r["outcome"] for r in self.tr.db.execute("SELECT outcome FROM batch_log")])

    def test_nothing_approved_means_nothing_happens(self):
        rep, slept = self.go(self.ok([]))
        self.assertEqual((rep.done, slept), ([], []))
        self.assertEqual(rep.lines(), ["  (nothing was processed)"])


class Settings(unittest.TestCase):
    def test_defaults_and_validation(self):
        self.assertEqual(batch.load_settings({}), batch.Settings((60.0, 180.0), 10))
        s = batch.load_settings(cfg_with(delay_between_jobs_s=[30, 90], daily_cap=5))
        self.assertEqual((s.delay_s, s.daily_cap), ((30.0, 90.0), 5))
        for bad in (cfg_with(delay_between_jobs_s=[90, 30]), cfg_with(delay_between_jobs_s=60), cfg_with(daily_cap=0),
                    cfg_with(daily_cap="many")):
            with self.assertRaises(cfgmod.ConfigError):
                batch.load_settings(bad)

    def test_the_example_config_has_a_valid_batch_section(self):
        s = batch.load_settings(cfgmod.load_config(ROOT / "config.example.yaml"))
        self.assertEqual((s.delay_s, s.daily_cap), ((60.0, 180.0), 10))

    def test_batch_must_be_a_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "c.yaml"
            f.write_text("batch: nope\n", encoding="utf-8")
            with self.assertRaises(cfgmod.ConfigError):
                cfgmod.load_config(f)


class Cli(Base):
    def test_list_shows_the_queue_the_count_and_the_cap(self):
        self.add(GH, "G", "greenhouse")
        self.add(LINKEDIN, "M", "manual")
        text = batch.format_queue(self.tr, CFG, batch.Settings((60, 180), 10))
        for needle in ("2 approved job(s) waiting", "0/10 started today", "60-180s", "Greenhouse -> assist", "manual", "#"):
            self.assertIn(needle, text)

    def test_main_runs_the_queue_and_reports(self):
        self.add(LINKEDIN, "M", "manual")
        out = []
        calls = []
        with mock.patch.object(tailor, "OUTPUT_DIR", self.root / "output"), \
                mock.patch.object(apply, "CONFIG_FILE", self.root / "nc.yaml"), \
                mock.patch.object(batch, "make_processor", lambda tr, cfg, **kw: lambda row: (
                    calls.append(row["id"]), tr.set_state(row["id"], "ready_for_you", "ready"), "ready_for_you")[-1]), \
                mock.patch.object(sys, "argv", ["batch.py", "--delay", "0"]), \
                mock.patch("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a)))):
            code = batch.main()
        self.assertEqual((code, len(calls)), (0, 1))
        self.assertIn("Ready for you", "\n".join(out))
        self.assertIn("0 approved job(s) still waiting", "\n".join(out))

    def test_list_does_no_work(self):
        self.add(LINKEDIN, "M", "manual")
        out = []
        with mock.patch.object(tailor, "OUTPUT_DIR", self.root / "output"), mock.patch.object(apply, "CONFIG_FILE", self.root / "nc.yaml"), \
                mock.patch.object(batch, "run_batch", side_effect=AssertionError("must not run")), \
                mock.patch.object(sys, "argv", ["batch.py", "--list"]), \
                mock.patch("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a)))):
            self.assertEqual(batch.main(), 0)
        self.assertIn("1 approved job(s) waiting", "\n".join(out))


if __name__ == "__main__":
    unittest.main()
