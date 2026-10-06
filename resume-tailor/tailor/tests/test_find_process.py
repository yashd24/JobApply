"""`jobapply find` (run.py --find-only) and `jobapply process` (batch.py): find never approves or processes; both read the
sheet's Approve ticks first; process takes the run lock and can be stopped cleanly. Fakes only."""
import io
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("JOBBOT_NO_SHEET", "1")      # a test must never reach the real Google Sheet
sys.path.insert(0, str(ROOT / "tests"))

import apply  # noqa: E402
import batch  # noqa: E402
import run  # noqa: E402
import tailor  # noqa: E402
from jobbot import sheets  # noqa: E402
from jobbot import stopflag  # noqa: E402
from test_run import CFG, Base  # noqa: E402


class FindOnly(Base):
    def test_find_scores_and_syncs_but_approves_and_processes_nothing(self):
        for i, c in enumerate("ABC", 1):
            self.add(i, company=c)
        s = self.go(find_only=True)
        self.assertEqual(s["scored"], 3)
        self.assertEqual((s["approved"], s["ready_for_you"], s["submitted"]), (0, 0, 0))
        self.assertTrue(s["find_only"])
        self.assertEqual({r["status"] for r in self.tr.db.execute("SELECT status FROM jobs")}, {"found"})
        self.assertNotIn("process A", self.steps)
        self.assertEqual(self.steps[-1], "sync")                                  # the sheet shows the new jobs
        self.assertEqual((s["found_waiting"], s["found_at_threshold"]), (3, 3))
        self.assertTrue(any("Find only" in x for x in self.logs))

    def test_find_does_not_read_the_approve_ticks_or_spend_them(self):
        self.add(1, company="A")
        seen = []
        s = self.go(find_only=True, approvals_fn=lambda: seen.append(1) or [])
        self.assertEqual(seen, [])
        self.assertEqual(s["approved_from_sheet"], 0)

    def test_the_summary_says_find_only(self):
        self.add(1)
        self.assertIn("FIND ONLY", run.format_summary(self.go(find_only=True)))


class Ticks(Base):
    def test_a_ticked_job_is_approved_before_the_run_approves_and_processes(self):
        a = self.add(1, company="Low", description="Python. 1-2 years of experience.")
        self.tr.set_relevance(a, 3, "weak fit")                                   # below the threshold: the run would not pick it
        order = []

        def ticks():
            order.append("ticks")
            self.tr.decide([a], approve=True, why="approved by ticking Approve in the sheet")
            return [a]

        def process(row):
            order.append(f"process {row['company']}")
            self.tr.set_state(row["id"], "ready_for_you", "ready")
            return "ready_for_you"
        s = self.go(approvals_fn=ticks, process=process, llm=self.scorer({"Low": 3}))
        self.assertEqual(order, ["ticks", "process Low"])
        self.assertEqual(s["approved_from_sheet"], 1)
        self.assertEqual(self.row(a)["status"], "ready_for_you")

    def test_a_dry_run_leaves_the_ticks_unread(self):
        self.add(1)
        seen = []
        self.go(dry_run=True, approvals_fn=lambda: seen.append(1) or [])
        self.assertEqual(seen, [])

    def test_a_sheet_problem_is_recorded_not_fatal(self):
        self.add(1)

        def boom():
            raise sheets.SheetsError("offline")
        s = self.go(approvals_fn=boom)
        self.assertTrue(any("sheet approvals" in e for e in s["errors"]))
        self.assertEqual(s["ready_for_you"], 1)                                   # the run still finished its work


class SheetApprovals(Base):
    def test_sheet_approvals_reads_ticks_with_the_given_tracker_and_never_raises(self):
        a = self.add(1)
        client = object()
        logs = []
        with mock.patch.object(apply, "_sheet_client", lambda: client), \
                mock.patch.object(sheets, "consume_approvals", lambda tr, c: [a] if c is client else []):
            self.assertEqual(batch.sheet_approvals(logs.append, self.tr), [a])
        self.assertTrue(any(f"#{a}" in x for x in logs))
        with mock.patch.object(apply, "_sheet_client", lambda: None):
            self.assertEqual(batch.sheet_approvals(logs.append, self.tr), [])    # no sheet: nothing to do
        with mock.patch.object(apply, "_sheet_client", lambda: client), \
                mock.patch.object(sheets, "consume_approvals", mock.Mock(side_effect=RuntimeError("boom"))):
            self.assertEqual(batch.sheet_approvals(logs.append, self.tr), [])
        self.assertTrue(any("could not read the Approve ticks" in x for x in logs))


class ProcessMain(Base):
    """batch.main (what `jobapply process` runs): the run lock, the ticks first, --limit, and a clean stop."""

    def main(self, *argv, process_fn=None):
        cfg = dict(CFG)
        fake_batch = mock.Mock(side_effect=process_fn)
        with mock.patch.object(batch.apply, "CONFIG_FILE", self.root / "config.yaml"), \
                mock.patch.object(batch.cfgmod, "load_config", lambda p: cfg), \
                mock.patch.object(batch.apply, "_tracker_file", lambda: self.out / "tracker.sqlite3"), \
                mock.patch.object(batch, "install_stop_signals", create=True), \
                mock.patch.object(stopflag, "install_stop_signals", lambda: (lambda: None)), \
                mock.patch.object(batch, "sheet_approvals", lambda *a, **k: self.steps.append("ticks") or []), \
                mock.patch.object(batch, "run_batch", fake_batch), \
                mock.patch.object(sys, "argv", ["batch.py", *argv]), redirect_stdout(io.StringIO()) as out:
            code = batch.main()
        return code, fake_batch, out.getvalue()

    def rep(self, **kw):
        return lambda *a, **k: batch.Report(**kw)

    def test_ticks_are_read_first_then_the_queue_runs_with_the_limit_and_the_stop_check(self):
        code, fake, out = self.main("--limit", "2", process_fn=self.rep())
        self.assertEqual(code, 0)
        self.assertEqual(self.steps, ["ticks"])
        kw = fake.call_args.kwargs
        self.assertEqual(kw["limit"], 2)
        self.assertIs(kw["should_stop"], stopflag.requested)

    def test_only_is_passed_on(self):
        _, fake, _ = self.main("--only", "4,9", process_fn=self.rep())
        self.assertEqual(fake.call_args.kwargs["only"], [4, 9])

    def test_a_second_process_cannot_start_while_one_holds_the_lock(self):
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "run.lock").write_text("pid 1 started x\n", encoding="utf-8")
        code, fake, out = self.main(process_fn=self.rep())
        self.assertEqual(code, 3)
        self.assertIn("Not started", out)
        fake.assert_not_called()
        self.assertTrue((self.out / "run.lock").exists())                         # the other run's lock is left alone

    def test_the_lock_is_released_and_a_stale_stop_request_is_cleared(self):
        stopflag.request("left over")
        seen = []
        code, fake, _ = self.main(process_fn=lambda *a, **k: seen.append(stopflag.requested()) or batch.Report())
        self.assertEqual((code, seen), (0, [False]))                             # the old request did not stop this run
        self.assertFalse((self.out / "run.lock").exists())
        self.assertFalse(stopflag.requested())

    def test_a_hard_abort_exits_130_and_frees_the_lock(self):
        code, _, out = self.main(process_fn=KeyboardInterrupt)
        self.assertEqual(code, 130)
        self.assertIn("Aborted at once", out)
        self.assertFalse((self.out / "run.lock").exists())

    def test_list_does_not_take_the_lock_or_read_the_sheet(self):
        self.out.mkdir(parents=True, exist_ok=True)
        (self.out / "run.lock").write_text("pid 1 started x\n", encoding="utf-8")   # a run is going: --list still works
        code, fake, out = self.main("--list", process_fn=self.rep())
        self.assertEqual(code, 0)
        self.assertEqual(self.steps, [])
        fake.assert_not_called()
        self.assertIn("approved job(s) waiting", out)


if __name__ == "__main__":
    unittest.main()
