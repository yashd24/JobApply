"""A clean stop: `jobapply stop` / Ctrl+C finishes and saves the job in progress, updates the tracker and exits, and the
next run continues from what is saved. Everything is faked: no Claude, no browser, no real run."""
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("JOBBOT_NO_SHEET", "1")      # a test must never reach the real Google Sheet
sys.path.insert(0, str(ROOT / "tests"))

import batch  # noqa: E402
import run  # noqa: E402
import tailor  # noqa: E402
from jobbot import discovery as D  # noqa: E402
from jobbot import launcher as L  # noqa: E402
from jobbot import relevance  # noqa: E402
from jobbot import stopflag  # noqa: E402
from test_run import Base  # noqa: E402


class Flag(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="stop test ")
        self.addCleanup(self.tmp.cleanup)
        p = mock.patch.object(tailor, "OUTPUT_DIR", Path(self.tmp.name))
        p.start()
        self.addCleanup(p.stop)

    def test_request_check_clear(self):
        self.assertFalse(stopflag.requested())
        stopflag.request()
        self.assertTrue(stopflag.requested())
        stopflag.clear()
        self.assertFalse(stopflag.requested())
        stopflag.clear()                                                  # clearing nothing is fine

    def test_the_running_pid_comes_from_the_lock_and_must_be_alive(self):
        self.assertIsNone(stopflag.running_pid(lambda pid: True))        # no lock file
        stopflag.lock_file().write_text("pid 4242 started 2030-01-01T00:00:00\n", encoding="utf-8")
        self.assertEqual(stopflag.running_pid(lambda pid: True), 4242)
        self.assertIsNone(stopflag.running_pid(lambda pid: False))       # a crashed run left its lock behind
        stopflag.lock_file().write_text("garbage", encoding="utf-8")
        self.assertIsNone(stopflag.running_pid(lambda pid: True))


class StopCommand(Flag):
    def stop(self, alive=lambda pid: True):
        with redirect_stdout(io.StringIO()) as out:
            code = L.stop_run(alive)
        return code, out.getvalue()

    def test_with_no_run_in_progress_nothing_is_requested(self):
        code, text = self.stop()
        self.assertEqual(code, 0)
        self.assertIn("No run is in progress", text)
        self.assertFalse(stopflag.requested())                           # a stale request would stop the NEXT run

    def test_a_dead_runs_lock_does_not_count(self):
        stopflag.lock_file().write_text("pid 4242 started x\n", encoding="utf-8")
        self.assertIn("No run is in progress", self.stop(lambda pid: False)[1])
        self.assertFalse(stopflag.requested())

    def test_a_live_run_is_asked_to_stop_once(self):
        stopflag.lock_file().write_text("pid 4242 started x\n", encoding="utf-8")
        code, text = self.stop()
        self.assertEqual(code, 0)
        self.assertTrue(stopflag.requested())
        self.assertIn("4242", text)
        self.assertIn("without repeating paid work", text)
        self.assertIn("already requested", self.stop()[1])


class Supervise(unittest.TestCase):
    """The launcher waits for the run; Ctrl+C asks for a clean stop, the second aborts, the third kills."""

    def proc(self, events):
        calls = iter(events)

        def wait(timeout=None):
            e = next(calls)
            if isinstance(e, BaseException):
                raise e
            return e
        return SimpleNamespace(wait=wait)

    def go(self, events):
        log = []
        code = L.supervise(self.proc(events), lambda: log.append("stop"), lambda: log.append("abort"),
                           lambda: log.append("kill"), say=lambda *a: log.append("say"))
        return code, [x for x in log if x != "say"]

    def test_no_interrupt_just_returns_the_exit_code(self):
        self.assertEqual(self.go([0]), (0, []))
        self.assertEqual(self.go([L.subprocess.TimeoutExpired("x", 1), 3]), (3, []))

    def test_first_ctrl_c_asks_for_a_clean_stop_and_keeps_waiting(self):
        self.assertEqual(self.go([KeyboardInterrupt(), L.subprocess.TimeoutExpired("x", 1), 0]), (0, ["stop"]))

    def test_second_aborts_and_third_kills(self):
        self.assertEqual(self.go([KeyboardInterrupt(), KeyboardInterrupt(), 130]), (130, ["stop", "abort"]))
        self.assertEqual(self.go([KeyboardInterrupt(), KeyboardInterrupt(), KeyboardInterrupt(), 1]),
                         (1, ["stop", "abort", "kill"]))


class Worker(unittest.TestCase):
    def test_the_run_is_a_child_in_its_own_process_group_with_the_given_arguments(self):
        seen = {}

        def popen(cmd, **kw):
            seen.update(cmd=cmd, kw=kw)
            return SimpleNamespace(wait=lambda timeout=None: 0, kill=lambda: None, send_signal=lambda s: None, pid=1)

        self.assertEqual(L.run_worker(["--dry-run"], popen=popen), 0)
        self.assertEqual(seen["cmd"][1:], [str(ROOT / "run.py"), "--dry-run"])
        self.assertEqual(Path(seen["kw"]["cwd"]), ROOT)
        if sys.platform == "win32":                       # so Ctrl+C in this window does not kill claude / LaTeX mid-job
            self.assertEqual(seen["kw"]["creationflags"], L.subprocess.CREATE_NEW_PROCESS_GROUP)


class InTheFlow(Base):
    def test_a_stop_before_scoring_spends_no_claude_and_keeps_everything_for_the_next_run(self):
        self.add(1, company="A")
        s = self.go(should_stop=lambda: True)
        self.assertNotIn("score", self.steps)
        self.assertEqual(s["scored"], 0)
        self.assertEqual(s["unscored_left"], 1)
        self.assertIn(stopflag.STOP_WORDS, s["stopped"])
        self.assertEqual(s["approved"], 0)
        self.assertEqual(self.steps[-1], "sync")                              # the sheet is still brought up to date
        self.assertIn("finished", s)

    def test_the_job_in_progress_is_finished_and_saved_then_the_rest_wait_approved(self):
        ids = [self.add(i, company=c) for i, c in enumerate("ABC", 1)]
        asked = []

        def process(row):
            self.steps.append(f"process {row['company']}")
            asked.append(1)                                                   # the stop request arrives during job A
            self.tr.set_state(row["id"], "ready_for_you", "ready")
            return "ready_for_you"
        s = self.go(process=process, should_stop=lambda: bool(asked), llm=self.scorer({"A": 9, "B": 8, "C": 7}))
        self.assertEqual([x for x in self.steps if x.startswith("process")], ["process A"])
        self.assertEqual(self.row(ids[0])["status"], "ready_for_you")        # finished and saved
        self.assertEqual({self.row(i)["status"] for i in ids[1:]}, {"approved"})   # not started: resume tomorrow
        self.assertEqual(s["ready_for_you"], 1)
        self.assertIn("stopped", s["stopped"])
        self.assertEqual(self.steps[-1], "sync")                              # tracker/sheet updated before exiting
        self.assertEqual(self.tr.batch_count_today(), 1)                      # only the job that ran counts against the cap

    def test_the_next_run_continues_with_the_approved_jobs_and_scores_nothing_twice(self):
        ids = [self.add(i, company=c) for i, c in enumerate("AB", 1)]
        asked = []

        def process(row):
            asked.append(1)
            self.tr.set_state(row["id"], "ready_for_you", "ready")
            return "ready_for_you"
        self.go(process=process, should_stop=lambda: bool(asked), llm=self.scorer({"A": 9, "B": 8}))
        self.steps.clear()
        s = self.go()                                                         # the next run, no stop request
        self.assertNotIn("score", self.steps)                                 # the scores were saved
        self.assertEqual([x for x in self.steps if x.startswith("process")], ["process B"])
        self.assertEqual(s["stopped"], "")
        self.assertEqual(self.row(ids[1])["status"], "ready_for_you")

    def test_a_stop_during_scoring_keeps_the_batches_already_scored(self):
        for i in range(1, 24):
            self.add(i, company=f"C{i}")
        calls = []

        def should_stop():
            return len(calls) >= 1                                            # asked while the first batch of 10 was scoring

        def llm(prompt):
            calls.append(1)
            return self.scorer()(prompt)
        s = self.go(llm=llm, should_stop=should_stop)
        self.assertEqual(len(calls), 1)
        self.assertEqual(s["scored"], 10)
        self.assertEqual(s["unscored_left"], 13)
        self.assertEqual(s["approved"], 0)                                    # nothing is approved or processed after a stop

    def test_the_wait_between_jobs_ends_as_soon_as_a_stop_is_requested(self):
        self.add(1, company="A")
        self.add(2, company="B")
        flag = []
        slept = []

        def sleep(sec):
            slept.append(sec)
            flag.append(1)                                                    # the request arrives during the wait
        settings = batch.Settings((60, 60), 5)
        for jid in (r["id"] for r in self.tr.found()):
            self.tr.decide([jid], approve=True)
        done = []

        def process(row):
            done.append(row["id"])
            self.tr.set_state(row["id"], "ready_for_you", "ready")
            return "ready_for_you"
        rep = batch.run_batch(self.tr, {}, settings, process=process, sleep=sleep, log=lambda *a: None,
                              should_stop=lambda: bool(flag))
        self.assertEqual(len(done), 1)
        self.assertEqual(slept, [3.0])                                        # one short step, not the whole minute
        self.assertIn("stopped at your request", rep.stopped)
        self.assertEqual(len(self.tr.approved()), 1)


class Pieces(unittest.TestCase):
    def test_searches_stop_between_calls_and_keep_what_was_found(self):
        settings = SimpleNamespace(terms=["a", "b", "c"], locations=[SimpleNamespace(name="X", query="X", remote=False)],
                                   sites=["indeed"], distance_miles=25, results_per_search=5, country="india",
                                   max_age_days=7, fetch_description=False, delay_between_searches_s=(0, 0))
        calls, asked = [], []

        def scrape(**kw):
            calls.append(kw["search_term"])
            asked.append(1)
            return []
        logs = []
        with mock.patch.object(D, "records", lambda x: []):
            rows, errors = D.run_searches(settings, scrape, sleep=lambda s: None, log=logs.append,
                                          should_stop=lambda: bool(asked))
        self.assertEqual(calls, ["a"])
        self.assertTrue(any("stop requested" in x for x in logs))

    def test_scoring_stops_between_batches(self):
        tr = mock.Mock()
        tr.unscored.return_value = [{"id": i} for i in range(25)]
        seen = []

        def llm(prompt):
            seen.append(1)
            raise RuntimeError("bad reply")                                   # a failed batch is not a stop; the next is tried
        with mock.patch.object(relevance, "build_prompt", lambda b, s: "p"), \
                mock.patch.object(relevance, "candidate_summary", lambda *a: ""):
            rep = relevance.score_unscored(tr, llm, {}, {}, log=lambda *a: None, should_stop=lambda: len(seen) >= 2)
        self.assertEqual(len(seen), 2)
        self.assertEqual(rep.calls, 2)


class MainFlag(Base):
    """run.main: a stale request is cleared at the start of a run, and another run's request is never cleared."""

    def main(self, *argv):
        with mock.patch.object(run.apply, "CONFIG_FILE", self.root / "config.yaml"), \
                mock.patch.object(run.cfgmod, "load_config", lambda p: CFG_FOR_MAIN), \
                mock.patch.object(run, "run_all", self.fake_run_all), \
                mock.patch.object(run, "install_stop_signals", lambda: (lambda: None)), \
                mock.patch.object(run.apply, "_tracker_file", lambda: self.out / "tracker.sqlite3"), \
                redirect_stdout(io.StringIO()):
            return run.main(list(argv))

    def fake_run_all(self, cfg, tr, **kw):
        self.seen_flag = stopflag.requested()
        self.should_stop = kw["should_stop"]
        return {"date": "2030-01-01", "started": "2030-01-01T00:00:00", "dry_run": False, "scraped": 0, "dropped": {},
                "found_new": 0, "already_tracked": 0, "scored": 0, "unscored_left": 0, "approved": 0, "submitted": 0,
                "ready_for_you": 0, "needs_review": 0, "manual": 0, "failed": 0, "stopped": "", "errors": [], "jobs": [],
                "threshold": 7}

    def test_a_stale_request_does_not_stop_the_next_run_and_the_run_clears_its_own(self):
        stopflag.request("left over")
        self.assertEqual(self.main(), 0)
        self.assertFalse(self.seen_flag)
        self.assertIs(self.should_stop, stopflag.requested)
        self.assertFalse(stopflag.requested())

    def test_a_second_run_that_cannot_start_leaves_the_first_runs_request_alone(self):
        (self.out / "run.lock").write_text("pid 1 started x\n", encoding="utf-8")
        stopflag.request("for the other run")
        self.assertEqual(self.main(), 3)
        self.assertTrue(stopflag.requested())

    def test_a_hard_abort_exits_130_and_releases_the_lock(self):
        def boom(cfg, tr, **kw):
            raise KeyboardInterrupt
        with mock.patch.object(run.apply, "CONFIG_FILE", self.root / "config.yaml"), \
                mock.patch.object(run.cfgmod, "load_config", lambda p: CFG_FOR_MAIN), \
                mock.patch.object(run, "run_all", boom), \
                mock.patch.object(run, "install_stop_signals", lambda: (lambda: None)), \
                mock.patch.object(run.apply, "_tracker_file", lambda: self.out / "tracker.sqlite3"), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(run.main([]), 130)
        self.assertFalse((self.out / "run.lock").exists())


class Signals(unittest.TestCase):
    def test_the_first_ctrl_c_requests_a_stop_and_the_second_raises(self):
        import signal
        with tempfile.TemporaryDirectory() as d, mock.patch.object(tailor, "OUTPUT_DIR", Path(d)):
            old = signal.getsignal(signal.SIGINT)
            restore = run.install_stop_signals()
            try:
                handler = signal.getsignal(signal.SIGINT)
                self.assertNotEqual(handler, old)
                with redirect_stdout(io.StringIO()) as out:
                    handler(signal.SIGINT, None)
                self.assertTrue(stopflag.requested())
                self.assertIn("stopping cleanly", out.getvalue())
                with self.assertRaises(KeyboardInterrupt):
                    handler(signal.SIGINT, None)
            finally:
                restore()
            self.assertEqual(signal.getsignal(signal.SIGINT), old)


CFG_FOR_MAIN = {"default_mode": "dry-run"}


if __name__ == "__main__":
    unittest.main()
