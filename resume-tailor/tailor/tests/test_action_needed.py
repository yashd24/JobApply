"""Tracker statuses (Found, Approved, Ready for you, Needs review, Manual, Failed, Submitted, Skipped), the Reason and
job-folder columns, the "Action needed" tab and its "Mark applied" checkbox."""
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from jobbot import sheets as S  # noqa: E402
from jobbot import tracker as T  # noqa: E402
from test_sheets import FakeSheet  # noqa: E402
from test_tracker import GH, LV, folder  # noqa: E402

TAB = S.ACTION_TAB
MANUAL_URL = "https://webco.example/jobs/1"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.t = T.Tracker(self.root / "t.sqlite3")
        self.sheet = FakeSheet()

    def tearDown(self):
        self.t.close()
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    def run_folder(self, name, url, status, **kw):
        return self.t.record_folder(folder(self.root, name, url, status, **kw))

    def found(self, url, company, route="manual", role="Backend Engineer"):
        self.t.add_found({"canonical_url": url, "company": company, "role": role, "route": route, "source": "naukri",
                          "source_url": "https://www.naukri.com/job-1", "direct_url": url, "experience_asked": "0-2 yrs",
                          "location": "Bengaluru", "platform": route})
        return self.t.job(url)["id"]


class Statuses(Base):
    def test_the_eight_statuses_have_labels(self):
        self.assertEqual([T.label(s) for s in ("found", "approved", "ready_for_you", "needs_review", "manual", "failed",
                                               "submitted", "skipped")],
                         ["Found", "Approved", "Ready for you", "Needs review", "Manual", "Failed", "Submitted", "Skipped"])

    def test_a_prepared_run_is_ready_for_you_and_old_rows_are_converted(self):
        self.run_folder("p", LV, "prepared", mode="prepare")
        self.assertEqual(self.t.job(LV)["status"], "ready_for_you")
        db = self.root / "old.sqlite3"
        con = sqlite3.connect(str(db))
        con.executescript("CREATE TABLE runs (job_dir TEXT PRIMARY KEY, canonical_url TEXT NOT NULL, status TEXT NOT NULL, "
                          "mode TEXT, submitted_by TEXT, finished_at TEXT, platform TEXT, company TEXT, role TEXT, "
                          "location TEXT, score INTEGER, gaps TEXT, resume_pdf TEXT, answers_path TEXT, screenshots TEXT, "
                          "flagged_fields TEXT, notes TEXT);"
                          "CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, canonical_url TEXT UNIQUE NOT NULL, "
                          "platform TEXT, company TEXT, role TEXT, location TEXT, status TEXT NOT NULL, mode TEXT, "
                          "submitted_by TEXT, score INTEGER, gaps TEXT, resume_pdf TEXT, answers_path TEXT, screenshots TEXT, "
                          "flagged_fields TEXT, created_at TEXT, submitted_at TEXT, notes TEXT, runs INTEGER DEFAULT 0);"
                          "INSERT INTO jobs (canonical_url, status, company, role) VALUES ('https://a.example/1', 'prepared', "
                          "'Old', 'Role');")
        con.commit()
        con.close()
        with T.Tracker(db) as t:
            self.assertEqual(t.job("https://a.example/1")["status"], "ready_for_you")
            self.assertIn("reason", {r["name"] for r in t.db.execute("PRAGMA table_info(runs)")})
            self.assertIn("job_folder", {r["name"] for r in t.db.execute("PRAGMA table_info(jobs)")})


class Reasons(Base):
    def test_every_run_status_gets_a_plain_reason_and_the_job_folder(self):
        cases = {
            "submitted": (GH, {}, "confirmation seen"),
            "prepared": (LV, {"mode": "prepare"}, "tick 'Mark applied'"),
            "needs_review": ("https://job-boards.greenhouse.io/x/jobs/3", {}, "no confirmation was seen"),
            "failed": ("https://job-boards.greenhouse.io/x/jobs/4", {"validation_errors": ["Email is required"]},
                       "Email is required")}
        for i, (status, (url, extra, expect)) in enumerate(cases.items()):
            d = folder(self.root, f"r{i}", url, status, **extra)
            self.t.record_folder(d)
            r = self.t.job(url)
            self.assertIn(expect, r["reason"], status)
            self.assertEqual(r["job_folder"], str(d))

    def test_a_manual_hand_submission_says_so(self):
        self.run_folder("m", LV, "submitted", submitted_by="manual", submitted_on="2026-10-02")
        self.assertIn("submitted by you, by hand", self.t.job(LV)["reason"])

    def test_a_failed_refusal_without_field_errors_uses_the_verification_reason(self):
        self.run_folder("f", GH, "failed", verification={"reason": "the employer refused the submission"})
        self.assertEqual(self.t.job(GH)["reason"], "the employer refused the submission")

    def test_discovery_decisions_carry_a_reason(self):
        a, m = self.found(GH, "Gh Co", "greenhouse"), self.found(MANUAL_URL, "Web Co")
        self.assertIn("found", self.t.job(GH)["reason"])
        self.t.decide([a, m], approve=True)
        self.assertIn("batch runner", self.t.job(GH)["reason"])
        self.assertIn("likely answers", self.t.job(MANUAL_URL)["reason"])
        self.assertIn("you apply by hand", self.t.job(MANUAL_URL)["reason"])


class ActionNeeded(Base):
    def test_only_what_waits_on_you_is_listed_in_a_useful_order(self):
        self.run_folder("sub", GH, "submitted")
        self.run_folder("fail", "https://job-boards.greenhouse.io/x/jobs/4", "failed")
        self.run_folder("prep", LV, "prepared", mode="prepare")
        self.run_folder("rev", "https://job-boards.greenhouse.io/x/jobs/3", "needs_review")
        self.run_folder("dry", "https://job-boards.greenhouse.io/x/jobs/5", "dry_run", mode="dry-run")
        m = self.found(MANUAL_URL, "Web Co")
        self.found("https://webco.example/jobs/2", "Not Yet Co")                   # still only found
        self.t.set_state(m, "manual", "could not be prepared: apply by hand from the link")
        got = [(r["status"], r["company"]) for r in self.t.action_needed()]
        self.assertEqual([s for s, _ in got], ["ready_for_you", "needs_review", "failed", "manual"])

    def test_mark_applied_makes_it_submitted_by_hand_and_it_sticks(self):
        self.run_folder("prep", LV, "prepared", mode="prepare")
        jid = self.t.job(LV)["id"]
        self.assertEqual(self.t.mark_applied(jid), "marked")
        r = self.t.job(LV)
        self.assertEqual((r["status"], r["submitted_by"], r["reason"]), ("submitted", "manual", "marked as applied by you"))
        self.assertTrue(r["submitted_at"])
        self.assertTrue(r["job_folder"].endswith("prep"))                             # the job folder is kept
        self.assertEqual(self.t.mark_applied(jid), "already")
        self.run_folder("later", LV, "failed")                                        # a later failed run cannot undo it
        self.assertEqual(self.t.job(LV)["status"], "submitted")
        self.assertTrue(self.t.is_submitted(LV))
        self.assertEqual(self.t.action_needed(), [])

    def test_mark_applied_by_url_for_a_manual_posting_and_for_unknown_ones(self):
        m = self.found(MANUAL_URL, "Web Co")
        self.t.set_state(m, "manual", "could not be prepared: apply by hand from the link")
        self.assertEqual(self.t.mark_applied(MANUAL_URL), "marked")
        self.assertEqual(self.t.job(MANUAL_URL)["status"], "submitted")
        self.assertEqual(self.t.mark_applied(99999), "unknown")
        self.assertEqual(self.t.mark_applied("https://nowhere.example/x"), "unknown")


class ActionTab(Base):
    def setUp(self):
        super().setUp()
        self.run_folder("prep", LV, "prepared", mode="prepare", company="Hevo Data", role="SDE I")
        self.run_folder("rev", "https://job-boards.greenhouse.io/x/jobs/3", "needs_review", company="Rev Co")
        self.manual = self.found(MANUAL_URL, "Web Co", role="Python Developer")
        self.t.set_state(self.manual, "manual", "could not be prepared: apply by hand, then tick 'Mark applied'")

    def rows(self):
        return [r for r in (self.sheet.row(n, TAB, 9) for n in range(2, 12)) if any(c != "" for c in r)]

    def test_the_tab_is_created_with_the_header_a_checkbox_column_and_one_row_per_waiting_posting(self):
        r = S.sync(self.t, self.sheet)
        self.assertEqual(self.sheet.created, [TAB])
        self.assertEqual(self.sheet.row(1, TAB, 9), S.ACTION_HEADER)
        self.assertEqual(S.ACTION_HEADER[0], "Mark applied")
        self.assertEqual(r.action_rows, 3)
        rows = self.rows()
        self.assertEqual([x[2] for x in rows], ["Ready for you", "Needs review", "Manual"])
        self.assertEqual([x[0] for x in rows], [False, False, False])                  # every box starts unticked
        manual = rows[2]
        self.assertEqual((manual[3], manual[4], manual[5]), ("Web Co", "Python Developer", MANUAL_URL))
        self.assertIn("Mark applied", manual[6])
        self.assertEqual(rows[0][5], T.key(LV))                                         # an ATS job links to its apply URL
        self.assertTrue(rows[0][7].endswith("prep"))                                    # and shows its job folder

    def test_the_main_tab_gets_reason_and_job_folder_but_not_found_or_skipped_postings(self):
        self.found("https://webco.example/jobs/77", "Only Found Co")
        S.sync(self.t, self.sheet)
        main = [self.sheet.row(n) for n in range(2, 8) if self.sheet.row(n)[3]]
        self.assertEqual(sorted(r[3] for r in main), ["Hevo Data", "Rev Co", "Web Co"])
        hevo = next(r for r in main if r[3] == "Hevo Data")
        self.assertEqual(hevo[1], "Ready for you")
        self.assertIn("Mark applied", hevo[12])
        self.assertTrue(hevo[13].endswith("prep"))

    def test_a_tick_marks_the_posting_applied_and_it_leaves_the_tab(self):
        S.sync(self.t, self.sheet)
        n = next(i for i in range(2, 8) if self.sheet.row(i, TAB, 9)[3] == "Hevo Data")
        self.sheet.tick(TAB, n)
        r = S.sync(self.t, self.sheet)
        self.assertEqual(r.marked_applied, 1)
        self.assertEqual(self.t.job(LV)["status"], "submitted")
        self.assertEqual(self.t.job(LV)["submitted_by"], "manual")
        self.assertEqual(sorted(x[3] for x in self.rows()), ["Rev Co", "Web Co"])
        hevo_main = next(self.sheet.row(i) for i in range(2, 8) if self.sheet.row(i)[3] == "Hevo Data")
        self.assertEqual((hevo_main[1], hevo_main[2]), ("Submitted", "manual"))        # updated in the same sync

    def test_after_a_tick_no_other_row_ends_up_ticked(self):
        S.sync(self.t, self.sheet)
        self.sheet.tick(TAB, 2)
        S.sync(self.t, self.sheet)
        self.assertEqual([x[0] for x in self.rows()], [False] * 2)
        self.assertEqual(len(self.rows()), 2)
        again = S.sync(self.t, self.sheet)                                              # nothing is applied twice
        self.assertEqual((again.marked_applied, again.action_rows), (0, 2))

    def test_a_tick_made_during_the_sync_is_caught_not_lost(self):
        S.sync(self.t, self.sheet)
        state = {"reads": 0}

        def user_ticks_after_first_read():
            state["reads"] += 1
            if state["reads"] == 1:
                n = next(i for i in range(2, 8) if self.sheet.row(i, TAB, 9)[3] == "Rev Co")
                self.sheet.tick(TAB, n)
        self.sheet.between_reads = user_ticks_after_first_read
        r = S.sync(self.t, self.sheet)
        self.assertEqual(self.t.job("https://job-boards.greenhouse.io/x/jobs/3")["status"], "submitted")
        self.assertEqual(r.marked_applied, 1)

    def test_a_tick_identifies_the_posting_by_id_and_falls_back_to_the_link(self):
        S.sync(self.t, self.sheet)
        n = next(i for i in range(2, 8) if self.sheet.row(i, TAB, 9)[3] == "Web Co")
        self.sheet.tabs[TAB][(n, 2)] = ""                                               # the user cleared the ID cell
        self.sheet.tick(TAB, n)
        S.sync(self.t, self.sheet)
        self.assertEqual(self.t.job(MANUAL_URL)["status"], "submitted")

    def test_a_tick_on_something_unknown_is_ignored(self):
        S.sync(self.t, self.sheet)
        self.sheet.tabs[TAB][(9, 1)] = "TRUE"
        self.sheet.tabs[TAB][(9, 2)] = "424242"
        r = S.sync(self.t, self.sheet)
        self.assertEqual(r.marked_applied, 0)
        self.assertEqual(len(self.rows()), 3)

    def test_the_sync_never_touches_your_own_columns_on_the_main_tab(self):
        S.sync(self.t, self.sheet)
        n = self.sheet.row_for(T.key(LV))
        self.sheet.set_manual(n, ["Replied", "Phone screen", "2026-10-20", "my note"])
        before = self.sheet.manual()
        self.sheet.tick(TAB, next(i for i in range(2, 8) if self.sheet.row(i, TAB, 9)[3] == "Hevo Data"))
        S.sync(self.t, self.sheet)
        self.assertEqual(self.sheet.manual(), before)

    def test_the_tab_shrinks_when_postings_leave_it(self):
        S.sync(self.t, self.sheet)
        for r in self.t.action_needed():
            self.t.mark_applied(r["id"])
        r = S.sync(self.t, self.sheet)
        self.assertEqual((r.action_rows, self.rows()), (0, []))
        self.assertTrue(any(e[0] == "clear" and e[2] == TAB for e in self.sheet.log))

    def test_the_summary_mentions_what_waits_on_you(self):
        text = str(S.sync(self.t, self.sheet))
        self.assertIn("3 waiting on you", text)


class RealSheetRequests(unittest.TestCase):
    """The Google calls for the checkbox tab, against a mock service (the live call is the user's first sync)."""

    def service(self, existing_titles):
        svc = mock.MagicMock()
        sheets_api = svc.spreadsheets.return_value
        sheets_api.get.return_value.execute.return_value = {"sheets": [{"properties": {"sheetId": 7, "title": t}}
                                                                       for t in existing_titles]}
        sheets_api.batchUpdate.return_value.execute.return_value = {"replies": [{"addSheet": {"properties": {
            "sheetId": 42, "title": TAB}}}]}
        return svc, sheets_api

    def test_a_missing_tab_is_added_and_column_a_becomes_checkboxes(self):
        g = S.GoogleSheet("id", "Applications", Path("c.json"), Path("t.json"))
        svc, api = self.service(["Applications"])
        g._svc = svc
        g.ensure_action_tab(TAB, S.ACTION_HEADER)
        bodies = [c.kwargs["body"]["requests"][0] for c in api.batchUpdate.call_args_list]
        self.assertEqual(bodies[0], {"addSheet": {"properties": {"title": TAB}}})
        v = bodies[1]["setDataValidation"]
        self.assertEqual(v["range"], {"sheetId": 42, "startRowIndex": 1, "endRowIndex": S.ACTION_MAX_ROW,
                                      "startColumnIndex": 0, "endColumnIndex": 1})
        self.assertEqual((v["rule"]["condition"]["type"], v["rule"]["showCustomUi"]), ("BOOLEAN", True))
        g.ensure_action_tab(TAB, S.ACTION_HEADER)                                      # once per run
        self.assertEqual(api.batchUpdate.call_count, 2)

    def test_an_existing_tab_is_not_added_again(self):
        g = S.GoogleSheet("id", "Applications", Path("c.json"), Path("t.json"))
        svc, api = self.service(["Applications", TAB])
        g._svc = svc
        g.ensure_action_tab(TAB, S.ACTION_HEADER)
        requests = [c.kwargs["body"]["requests"][0] for c in api.batchUpdate.call_args_list]
        self.assertEqual([next(iter(r)) for r in requests], ["setDataValidation"])
        self.assertEqual(requests[0]["setDataValidation"]["range"]["sheetId"], 7)

    def test_reads_and_writes_name_the_right_tab_and_stay_raw(self):
        g = S.GoogleSheet("id", "Applications", Path("c.json"), Path("t.json"))
        svc = mock.MagicMock()
        g._svc = svc
        values = svc.spreadsheets.return_value.values.return_value
        g.write("A1:I2", [[False, 1]], tab=TAB)
        kw = values.update.call_args.kwargs
        self.assertEqual((kw["range"], kw["valueInputOption"]), (f"'{TAB}'!A1:I2", "RAW"))
        g.read("A1:I1000", tab=TAB)
        self.assertEqual(values.get.call_args.kwargs["range"], f"'{TAB}'!A1:I1000")
        g.clear("A5:I1000", tab=TAB)
        self.assertEqual(values.clear.call_args.kwargs["range"], f"'{TAB}'!A5:I1000")
        g.read("A1:T")
        self.assertEqual(values.get.call_args.kwargs["range"], "'Applications'!A1:T")


class Cli(Base):
    def test_mark_applied_from_the_command_line(self):
        import apply
        import tailor
        self.run_folder("prep", LV, "prepared", mode="prepare")
        jid = self.t.job(LV)["id"]
        self.t.close()
        out = []
        with mock.patch.object(tailor, "OUTPUT_DIR", self.root), mock.patch.object(apply, "CONFIG_FILE", self.root / "nc.yaml"), \
                mock.patch.object(sys, "argv", ["apply.py", "--mark-applied", f"{jid}, 9999"]), \
                mock.patch("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a)))):
            (self.root / "t.sqlite3").rename(self.root / "tracker.sqlite3")
            apply.main()
        text = "\n".join(out)
        self.assertIn(f"{jid}: marked", text)
        self.assertIn("9999: unknown", text)
        self.t = T.Tracker(self.root / "tracker.sqlite3")
        self.assertEqual(self.t.job(LV)["status"], "submitted")

    def test_status_shows_the_labels(self):
        self.run_folder("prep", LV, "prepared", mode="prepare")
        text = T.format_status(self.t.recent(), self.t.counts())
        self.assertIn("Ready for you", text)
        self.assertNotIn("ready_for_you", text)


if __name__ == "__main__":
    unittest.main()
