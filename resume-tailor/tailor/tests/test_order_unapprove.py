"""The queue is worked best score first (ties: newest), `process --list` shows that order, and `jobapply unapprove` moves
approved jobs back to Found without rejecting them (the sheet's Approve tick is the user's: it is ignored until unticked)."""
import io
import os
import random
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
import tailor  # noqa: E402
from jobbot import commands as C  # noqa: E402
from jobbot import launcher as L  # noqa: E402
from jobbot import sheets as S  # noqa: E402
from jobbot import tracker as T  # noqa: E402
from test_sheets import FakeSheet  # noqa: E402

CFG = {"default_mode": "dry-run", "platforms": {}}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="order test ")
        self.root = Path(self.tmp.name)
        self.out = self.root / "output"
        self.tr = T.Tracker(self.out / "tracker.sqlite3")
        p = mock.patch.object(tailor, "OUTPUT_DIR", self.out)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        self.tr.close()
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    def add(self, n, score, posted=None, status="found", company=None):
        url = f"https://www.linkedin.com/jobs/view/{7000000 + n}"
        self.tr.add_found({"canonical_url": url, "company": company or f"Co{n}", "role": f"Backend Engineer {n}",
                           "route": "manual", "source": "linkedin", "source_url": url, "location": "Bengaluru",
                           "date_posted": posted, "description": "Python. " * 50})
        jid = self.tr.job(url)["id"]
        if score is not None:
            self.tr.set_relevance(jid, score, "fits")
        if status == "approved":
            self.tr.decide([jid], approve=True)
        elif status != "found":
            self.tr.set_state(jid, status, "set by the test")
        return jid

    def row(self, jid):
        return self.tr.by_ids([jid])[0]


class Order(Base):
    def test_best_score_first_ties_by_newest_posting_then_newest_id_and_unscored_last(self):
        low = self.add(1, 6, "2026-10-05", "approved")
        old9 = self.add(2, 9, "2026-10-01", "approved")
        new9 = self.add(3, 9, "2026-10-04", "approved")
        none = self.add(4, None, "2026-10-06", "approved")
        mid = self.add(5, 8, None, "approved")
        mid_same_date_later_id = self.add(6, 8, None, "approved")
        self.assertEqual([r["id"] for r in self.tr.approved()], [new9, old9, mid_same_date_later_id, mid, low, none])

    def test_a_job_with_a_saved_folder_gets_no_special_place(self):
        folder = self.root / "kept"
        folder.mkdir()
        a, b = self.add(1, 6, status="approved"), self.add(2, 9, status="approved")
        self.tr.set_state(a, "approved", "stopped", str(folder))
        self.assertEqual([r["id"] for r in self.tr.approved()], [b, a])

    def test_the_daily_cap_is_spent_on_the_best_jobs(self):
        ids = {score: self.add(n, score, status="approved") for n, score in enumerate([5, 9, 7, 8, 6, 10, 4], 1)}
        done = []

        def process(row):
            done.append(row["relevance"])
            self.tr.set_state(row["id"], "ready_for_you", "ready")
            return "ready_for_you"
        rep = batch.run_batch(self.tr, CFG, batch.Settings((0, 0), 3), process=process, sleep=lambda s: None,
                              rng=random.Random(1), log=lambda *a: None)
        self.assertEqual(done, [10, 9, 8])
        self.assertIn("daily cap of 3", rep.stopped)
        self.assertEqual(sorted(r["relevance"] for r in self.tr.approved()), [4, 5, 6, 7])        # the weaker ones wait
        self.assertEqual(len(ids), 7)

    def test_limit_takes_the_top_n_and_only_keeps_the_score_order(self):
        for n, score in enumerate([5, 9, 7, 8], 1):
            self.add(n, score, status="approved")
        done = []

        def process(row):
            done.append(row["relevance"])
            self.tr.set_state(row["id"], "ready_for_you", "ready")
            return "ready_for_you"
        batch.run_batch(self.tr, CFG, batch.Settings((0, 0), 10), process=process, limit=2, sleep=lambda s: None,
                        log=lambda *a: None)
        self.assertEqual(done, [9, 8])
        done.clear()
        batch.run_batch(self.tr, CFG, batch.Settings((0, 0), 10), process=process, only=[1, 3], sleep=lambda s: None,
                        log=lambda *a: None)
        self.assertEqual(done, [7, 5])

    def test_the_queue_listing_is_in_that_order_with_scores_and_marks_what_fits_today(self):
        for n, score in enumerate([5, 9, 7, 8], 1):
            self.add(n, score, status="approved")
        self.add(5, None, status="approved")
        text = batch.format_queue(self.tr, CFG, batch.Settings((60, 180), 3))
        lines = text.splitlines()
        self.assertIn("best score first", lines[0])
        self.assertIn("3 left", lines[0])
        self.assertIn("SCORE", lines[1])
        body = lines[2:]
        self.assertEqual([ln.split()[1] for ln in body], ["9", "8", "7", "5", "-"])               # the score on each row
        self.assertEqual([ln.split()[-1] for ln in body], ["today", "today", "today", "later", "later"])
        for ln, co in zip(body, ("Co2", "Co4", "Co3", "Co1", "Co5")):
            self.assertIn(co, ln)

    def test_the_listing_counts_what_was_already_started_today(self):
        a, b = self.add(1, 9, status="approved"), self.add(2, 8, status="approved")
        lid = self.tr.batch_start(a)                       # one job already started today
        self.tr.batch_finish(lid, "ready_for_you")
        text = batch.format_queue(self.tr, CFG, batch.Settings((0, 0), 2))
        self.assertIn("1/2 started today (1 left)", text)
        self.assertEqual([ln.split()[-1] for ln in text.splitlines()[2:]], ["today", "later"])

    def test_process_list_shows_the_queue_without_taking_the_lock_or_starting_anything(self):
        self.add(1, 9, status="approved")
        calls = []
        with mock.patch.object(L, "run_module", lambda name, args: calls.append((name, args)) or 0):
            self.assertEqual(L.process_command(L.Plan("process", ["--list"]), worker=lambda a: self.fail("no worker")), 0)
        self.assertEqual(calls, [("batch", ["--list"])])
        self.assertIn("process --list", L.HELP)


class Unapprove(Base):
    def test_approved_jobs_go_back_to_found_not_skipped(self):
        a, b = self.add(1, 9, status="approved"), self.add(2, 5, status="approved")
        out = self.tr.unapprove([b])
        self.assertEqual(out["moved"], [b])
        r = self.row(b)
        self.assertEqual((r["status"], r["relevance"]), ("found", 5))                # still scored, still Found
        self.assertIn("not rejected", r["reason"])
        self.assertEqual(self.row(a)["status"], "approved")
        self.assertIn(b, [x["id"] for x in self.tr.found()])
        again = self.tr.add_found({"canonical_url": r["canonical_url"], "company": "Co2", "role": "Backend Engineer 2",
                                   "route": "manual", "source": "linkedin"})
        self.assertEqual(again, "seen")                                               # a later search refreshes it, as for any Found job
        self.tr.decide([b], approve=True)                                             # and it can be approved again
        self.assertEqual(self.row(b)["status"], "approved")

    def test_only_approved_unprocessed_jobs_move(self):
        folder = self.root / "work"
        folder.mkdir()
        started = self.add(1, 9, status="approved")
        self.tr.set_state(started, "approved", "stopped", str(folder))
        found = self.add(2, 9)
        done = self.add(3, 9, status="ready_for_you")
        out = self.tr.unapprove([started, found, done, 4242])
        self.assertEqual(out["moved"], [])
        self.assertIn(started, out["kept"])
        self.assertEqual(out["ignored"], {found: "found", done: "ready_for_you"})
        self.assertEqual([self.row(i)["status"] for i in (started, found, done)], ["approved", "found", "ready_for_you"])

    def test_all_means_every_approved_job_and_below_n_means_scored_under_n(self):
        hi, lo, edge = self.add(1, 9, status="approved"), self.add(2, 4, status="approved"), self.add(3, 7, status="approved")
        un = self.add(4, None, status="approved")
        found = self.add(5, 2)
        self.assertEqual(sorted(C.resolve_unapproval(self.tr, ["all"])[0]), sorted([hi, lo, edge, un]))
        ids, unscored = C.resolve_unapproval(self.tr, ["below", "7"])
        self.assertEqual((ids, unscored), ([lo], 1))                                  # 7 is not below 7; unscored are left out
        self.assertEqual(C.resolve_unapproval(self.tr, ["below7"])[0], [lo])
        self.assertEqual(C.resolve_unapproval(self.tr, ["<8"])[0], sorted([lo, edge], key=lambda i: -self.row(i)["relevance"]))
        self.assertEqual(C.resolve_unapproval(self.tr, [f"{hi},{lo}"])[0], sorted([hi, lo]))
        self.assertNotIn(found, C.resolve_unapproval(self.tr, ["all"])[0])

    def test_typos_change_nothing(self):
        self.add(1, 9, status="approved")
        for bad in ([], ["below"], ["below", "x"], ["below", "11"], ["below", "0"], ["abc"]):
            with self.assertRaises(C.CommandError, msg=bad):
                C.resolve_unapproval(self.tr, bad)
        self.assertEqual(len(self.tr.approved()), 1)

    def test_the_text_says_what_moved_and_what_stayed(self):
        a = self.add(1, 9, status="approved")
        folder = self.root / "work"
        folder.mkdir()
        b = self.add(2, 6, status="approved")
        self.tr.set_state(b, "approved", "stopped", str(folder))
        text = C.unapprove_text(self.tr, [a, b, 99], unscored=2)
        self.assertIn("Moved back to Found 1", text)
        self.assertIn("[score 9]", text)
        self.assertIn("not rejected", text)
        self.assertIn("work was already started", text)
        self.assertIn("no job with id 99", text)
        self.assertIn("2 approved job(s) have no score yet", text)
        self.assertIn("Nothing matched", C.unapprove_text(self.tr, []))

    def test_the_launcher_command_moves_jobs_syncs_and_reports(self):
        a, b = self.add(1, 9, status="approved"), self.add(2, 3, status="approved")
        said = []
        with mock.patch.object(L, "sync_quietly", lambda tr, say=print: said.append("synced")), \
                mock.patch.object(L, "still_ticked_text", lambda tr, ids, client=None: "ticks: " + ",".join(map(str, ids))):
            code = L.tracker_command(L.Plan("unapprove", ["below", "7"]), say=lambda *x: said.append(" ".join(map(str, x))))
        self.assertEqual(code, 0)
        self.assertEqual([self.row(i)["status"] for i in (a, b)], ["approved", "found"])
        self.assertIn("synced", said)
        self.assertIn(f"ticks: {b}", said)
        self.assertEqual(L.parse(["unapprove", "all"]), L.Plan("unapprove", ["all"]))
        self.assertIn("jobapply unapprove", L.HELP)

    def test_nothing_matched_does_not_touch_the_sheet(self):
        self.add(1, 9, status="approved")
        with mock.patch.object(L, "sync_quietly", lambda tr, say=print: self.fail("nothing changed")):
            self.assertEqual(L.tracker_command(L.Plan("unapprove", ["below", "3"]), say=lambda *x: None), 0)

    def test_the_report_names_the_rows_to_untick(self):
        a = self.add(1, 9, status="approved")
        self.tr.unapprove([a])
        url = self.row(a)["canonical_url"]
        client = SimpleNamespace(read=lambda a1: [S.FULL_HEADER] + [[""] * 6 + [url] + [""] * 13 + ["1", "TRUE"]])
        text = L.still_ticked_text(self.tr, [a], client)
        self.assertIn("Untick these", text)
        self.assertIn("row 2", text)
        self.assertIn(f"#{a}", text)
        clean = SimpleNamespace(read=lambda a1: [S.FULL_HEADER] + [[""] * 6 + [url] + [""] * 13 + ["1", "FALSE"]])
        self.assertIn("None of them is ticked", L.still_ticked_text(self.tr, [a], clean))
        broken = SimpleNamespace(read=mock.Mock(side_effect=RuntimeError("offline")))
        self.assertIn("untick the moved jobs yourself", L.still_ticked_text(self.tr, [a], broken))


class StickyTicks(Base):
    """The bot never writes to the Approve column, so it cannot untick: an unapproved job's tick is ignored until the box
    has been seen unticked, and then a new tick approves it again."""

    def setUp(self):
        super().setUp()
        self.sheet = FakeSheet()
        self.a = self.add(1, 9)
        self.url = self.row(self.a)["canonical_url"]
        S.sync(self.tr, self.sheet)
        self.n = self.sheet.row_for(T.key(self.url))

    def test_the_whole_round_trip(self):
        self.sheet.tick_approve(self.n)
        self.assertEqual(S.sync(self.tr, self.sheet).approved, 1)
        self.assertEqual(self.row(self.a)["status"], "approved")
        self.tr.unapprove([self.a])
        r = S.sync(self.tr, self.sheet)                                                # the tick is still there
        self.assertEqual((r.approved, r.still_ticked), (0, 1))
        self.assertEqual(self.row(self.a)["status"], "found")
        self.assertIn("still ticked", str(r))
        self.assertEqual(S.sync(self.tr, self.sheet).approved, 0)                      # and stays ignored
        self.sheet.cells[(self.n, 22)] = "FALSE"                                       # the user unticks it
        S.sync(self.tr, self.sheet)
        self.assertIsNone(self.row(self.a)["tick_ignored"])                            # seen unticked: the flag is gone
        self.sheet.tick_approve(self.n)                                                # ticking again approves as usual
        self.assertEqual(S.sync(self.tr, self.sheet).approved, 1)
        self.assertEqual(self.row(self.a)["status"], "approved")

    def test_the_bot_never_writes_a_value_into_the_approve_column_for_this(self):
        self.sheet.tick_approve(self.n)
        S.sync(self.tr, self.sheet)
        self.tr.unapprove([self.a])
        before = self.sheet.approve_cells()
        S.sync(self.tr, self.sheet)
        self.assertEqual(self.sheet.approve_cells(), before)                           # the user's tick is exactly as they left it

    def test_an_unapproved_job_that_was_never_ticked_is_approved_by_its_first_tick(self):
        self.tr.decide([self.a], approve=True)
        self.tr.unapprove([self.a])
        S.sync(self.tr, self.sheet)                                                    # sees the box unticked: flag cleared
        self.sheet.tick_approve(self.n)
        self.assertEqual(S.sync(self.tr, self.sheet).approved, 1)

    def test_a_flag_on_a_job_not_in_the_sheet_yet_does_no_harm(self):
        b = self.add(2, 9)
        self.tr.decide([b], approve=True)
        self.tr.unapprove([b])
        self.assertEqual(self.row(b)["tick_ignored"], 1)
        S.sync(self.tr, self.sheet)                                                    # this sync gives it a row ...
        S.sync(self.tr, self.sheet)                                                    # ... the next sees it unticked: cleared
        self.assertIsNone(self.row(b)["tick_ignored"])

    def test_ticked_rows_reads_without_writing(self):
        self.sheet.tick_approve(self.n)
        self.sheet.log.clear()
        self.assertEqual(S.ticked_rows(self.sheet), {T.key(self.url): self.n})
        self.assertEqual(self.sheet.log, [])


if __name__ == "__main__":
    unittest.main()
