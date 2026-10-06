"""Google Sheets sync against a fake sheet that models cells. The one rule under test: the bot never writes to the
user's own columns (Response, Interview stage, Follow-up date, My notes)."""
import re
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("JOBBOT_NO_SHEET", "1")      # a test must never reach the real Google Sheet
sys.path.insert(0, str(ROOT / "tests"))

from jobbot import config as cfgmod  # noqa: E402
from jobbot import sheets as S  # noqa: E402
from jobbot import tracker as T  # noqa: E402
from test_tracker import GH, LV, folder  # noqa: E402


class FakeSheet:
    """Cells per tab in dicts; read() trims trailing empty cells and rows like Google does. Records every write."""
    MAIN = "main"

    def __init__(self):
        self.tabs: dict = {self.MAIN: {}}              # tab -> {(row, column): text}
        self.log: list = []
        self.created: list = []
        self.boxes: set = set()                        # rows that were given an Approve checkbox
        self.between_reads = None                      # a callable run after each action-tab read (a user ticking mid-sync)

    @property
    def cells(self):
        return self.tabs[self.MAIN]

    @staticmethod
    def _parse(a1):
        m = re.match(r"^([A-Z]+)(\d*)(?::([A-Z]+)(\d*))?$", a1)
        return S._col_number(m.group(1)), int(m.group(2) or 0), S._col_number(m.group(3) or m.group(1)), int(m.group(4) or 0)

    def show_approve_boxes(self, rows, tab=None):
        self.log.append(("boxes", tuple(rows), tab))
        self.boxes |= set(rows)

    def ensure_action_tab(self, tab, header):
        if tab not in self.tabs:
            self.tabs[tab] = {}
            self.created.append(tab)

    def read(self, a1, tab=None):
        cells = self.tabs[tab or self.MAIN]
        c1, r1, c2, r2 = self._parse(a1)
        last = max((r for (r, c) in cells if c1 <= c <= c2 and cells[(r, c)] != ""), default=0)
        rows = []
        for r in range(max(r1, 1), min(last, r2 or last) + 1):
            row = [cells.get((r, c), "") for c in range(c1, c2 + 1)]
            while row and row[-1] == "":
                row.pop()
            rows.append(row)
        if tab and self.between_reads:
            self.between_reads()
        return rows

    def write(self, a1, values, tab=None):
        self.log.append(("write", a1, tab))
        cells = self.tabs[tab or self.MAIN]
        c1, r1, _, _ = self._parse(a1)
        for i, row in enumerate(values):
            for j, v in enumerate(row):
                cells[(r1 + i, c1 + j)] = v if isinstance(v, bool) else str(v)

    def append(self, a1, values, tab=None):
        self.log.append(("append", a1, tab))
        cells = self.tabs[tab or self.MAIN]
        c1, _, c2, _ = self._parse(a1)
        last = max((r for (r, c) in cells if c1 <= c <= c2), default=0)
        for i, row in enumerate(values):
            for j, v in enumerate(row):
                cells[(last + 1 + i, c1 + j)] = str(v)

    def clear(self, a1, tab=None):
        self.log.append(("clear", a1, tab))
        cells = self.tabs[tab or self.MAIN]
        c1, r1, c2, r2 = self._parse(a1)
        for key in [k for k in cells if r1 <= k[0] <= (r2 or 10**9) and c1 <= k[1] <= c2]:
            del cells[key]

    # helpers for the tests
    def row(self, n, tab=None, width=22):
        cells = self.tabs[tab or self.MAIN]
        return [cells.get((n, c), "") for c in range(1, width + 1)]

    def manual(self):
        return {k: v for k, v in self.cells.items() if 17 <= k[1] <= 20}

    def set_manual(self, n, values):
        for j, v in enumerate(values):
            self.cells[(n, 17 + j)] = v

    def row_for(self, url):
        return next(r for r in range(2, 50) if self.cells.get((r, 7)) == url)

    def tick(self, tab, n):
        self.tabs[tab][(n, 1)] = "TRUE"

    def tick_approve(self, n):
        self.cells[(n, 22)] = "TRUE"                      # the user ticks the Approve box in column V

    def approve_cells(self):
        return {k: v for k, v in self.cells.items() if k[1] == 22 and k[0] > 1}


class Sync(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.t = T.Tracker(self.root / "t.sqlite3")
        self.t.record_folder(folder(self.root, "a", GH, "submitted", company="project44", role="Software Engineer 2",
                                    flagged=["Why us?", "Salary"]))
        self.t.record_folder(folder(self.root, "m", LV, "submitted", mode="assist", submitted_by="manual",
                                    company="Hevo Data", role="SDE I", submitted_on="2026-10-02"))
        self.sheet = FakeSheet()

    def tearDown(self):
        self.t.close()
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    def test_first_sync_writes_the_header_and_one_row_per_posting(self):
        r = S.sync(self.t, self.sheet)
        self.assertEqual((r.added, r.updated, r.unchanged), (2, 0, 0))
        self.assertEqual(self.sheet.row(1), S.FULL_HEADER)
        self.assertEqual(S.FULL_HEADER[20:], ["ID", "Approve"])
        self.assertEqual(S.HEADER[:16], ["Date", "Status", "Mode", "Company", "Role", "Location", "URL", "Score",
                                         "Flagged fields", "Resume file used", "Gaps", "Notes", "Reason", "Job folder",
                                         "Relevance", "Relevance reason"])
        self.assertEqual(S.HEADER[16:], ["Response", "Interview stage", "Follow-up date", "My notes"])
        row = self.sheet.row(self.sheet.row_for(GH))
        self.assertEqual(row[:7], ["2026-10-02", "Submitted", "assist", "project44", "Software Engineer 2", "Bengaluru", GH])
        self.assertEqual((row[7], row[8]), ("7", "Why us?; Salary"))
        self.assertTrue(row[9].startswith("a/") and row[9].endswith("/r.pdf"), row[9])
        self.assertEqual(row[10], "Kafka")
        self.assertEqual(row[16:20], ["", "", "", ""])
        self.assertEqual(self.sheet.row(self.sheet.row_for(LV))[2], "manual")           # a hand submission says so

    def test_a_second_sync_changes_nothing(self):
        S.sync(self.t, self.sheet)
        before = dict(self.sheet.cells)
        main_writes = lambda: [e for e in self.sheet.log if e[2] is None]                 # noqa: E731
        n = len(main_writes())
        r = S.sync(self.t, self.sheet)
        self.assertEqual((r.added, r.updated, r.unchanged), (0, 0, 2))
        self.assertEqual((self.sheet.cells, len(main_writes())), (before, n))             # the main tab is not touched

    def test_the_users_columns_are_never_overwritten_even_when_the_bot_updates_a_row(self):
        S.sync(self.t, self.sheet)
        n = self.sheet.row_for(GH)
        self.sheet.set_manual(n, ["Rejected", "Phone screen", "2026-10-20", "Recruiter was kind"])
        before = self.sheet.manual()
        self.t.db.execute("UPDATE jobs SET notes='checked by hand' WHERE canonical_url=?", (T.key(GH),))
        self.t.db.commit()
        r = S.sync(self.t, self.sheet)
        self.assertEqual(r.updated, 1)
        self.assertEqual(self.sheet.row(n)[11], "checked by hand")
        self.assertEqual(self.sheet.manual(), before)
        self.assertEqual(self.sheet.row(n)[16:20], ["Rejected", "Phone screen", "2026-10-20", "Recruiter was kind"])

    def test_sorting_the_sheet_cannot_make_the_bot_write_to_the_wrong_row(self):
        S.sync(self.t, self.sheet)
        a, b = self.sheet.row_for(GH), self.sheet.row_for(LV)
        self.sheet.set_manual(a, ["Rejected", "", "", "about project44"])
        self.sheet.set_manual(b, ["Interview", "Round 1", "2026-10-09", "about Hevo"])
        rows = {n: self.sheet.row(n) for n in (a, b)}                           # the user swaps the two rows
        for c in range(1, 21):
            self.sheet.cells[(a, c)], self.sheet.cells[(b, c)] = rows[b][c - 1], rows[a][c - 1]
        self.t.db.execute("UPDATE jobs SET status='failed', notes='x' WHERE canonical_url=?", (T.key(LV),))
        self.t.db.commit()
        S.sync(self.t, self.sheet)
        hevo = self.sheet.row(self.sheet.row_for(LV))
        self.assertEqual((hevo[1], hevo[11], hevo[16:20]), ("Failed", "x", ["Interview", "Round 1", "2026-10-09", "about Hevo"]))
        p44 = self.sheet.row(self.sheet.row_for(GH))
        self.assertEqual((p44[1], p44[16], p44[19]), ("Submitted", "Rejected", "about project44"))

    def test_a_new_posting_is_appended_without_touching_existing_rows(self):
        S.sync(self.t, self.sheet)
        self.sheet.set_manual(2, ["Seen", "", "", "note"])
        self.t.record_folder(folder(self.root, "new", "https://job-boards.greenhouse.io/acme/jobs/9", "failed"))
        r = S.sync(self.t, self.sheet)
        self.assertEqual((r.added, r.unchanged), (1, 2))
        self.assertEqual(self.sheet.cells[(2, 17)], "Seen")
        self.assertEqual(self.sheet.cells[(4, 7)], "https://job-boards.greenhouse.io/acme/jobs/9")

    def test_only_the_given_urls_are_synced_after_a_run(self):
        r = S.sync(self.t, self.sheet, urls=[GH + "/"])
        self.assertEqual((r.added, len(self.sheet.read("A1:T"))), (1, 2))        # header + project44 only

    def test_notes_are_written_as_given_so_they_cannot_become_formulas(self):
        self.t.db.execute("UPDATE jobs SET notes=? WHERE canonical_url=?", ('=HYPERLINK("x")', T.key(GH)))
        self.t.db.commit()
        S.sync(self.t, self.sheet)
        self.assertEqual(self.sheet.row(self.sheet.row_for(GH))[11], '=HYPERLINK("x")')
        src = (ROOT / "jobbot" / "sheets.py").read_text(encoding="utf-8")
        self.assertEqual(src.count('valueInputOption="RAW"'), 2)                  # both the update and the append
        self.assertNotIn("USER_ENTERED", src)

    def test_the_guard_refuses_any_write_into_the_users_columns(self):
        guarded = S._AutoOnly(self.sheet)
        for bad in ("Q2", "Q2:T2", "A2:Q2", "A2:T2", "T1", "P2:Q2", "V2", "V2:V9", "U2:V2", "T2:U2", "A1:V1"):
            with self.assertRaises(S.SheetsError, msg=bad):
                guarded.write(bad, [["x"]])
        with self.assertRaises(S.SheetsError):
            guarded.append("A:P", [["x"] * 17])
        with self.assertRaises(S.SheetsError):
            guarded.append("A:T", [["x"]])
        guarded.write("A2:P2", [["x"] * 16])
        guarded.write("U2:U9", [["1"]] * 8)                                   # the ID column is the bot's
        guarded.write("V1", [["Approve"]])                                      # the header label only
        guarded.append("A:P", [["y"] * 16])
        self.assertEqual(self.sheet.manual(), {})
        self.assertEqual(self.sheet.approve_cells(), {})

    def test_an_existing_sheet_keeps_the_users_renamed_manual_headers(self):
        S.sync(self.t, self.sheet)
        self.sheet.cells[(1, 17)] = "Reply"                                         # the user renamed a header
        self.sheet.cells[(1, 2)] = "State"                                           # and changed one of ours
        S.sync(self.t, self.sheet)
        self.assertEqual((self.sheet.cells[(1, 17)], self.sheet.cells[(1, 2)]), ("Reply", "Status"))

    def test_every_write_of_a_full_scenario_stays_in_the_bots_columns(self):
        S.sync(self.t, self.sheet)
        self.t.db.execute("UPDATE jobs SET notes='n'")
        self.t.db.commit()
        S.sync(self.t, self.sheet)
        for kind, a1, tab in self.sheet.log:
            if tab is None and kind in ("write", "append") and a1 != "A1:V1":        # main tab; V1 = header of an empty sheet
                first, last = S.columns_of(a1)
                self.assertTrue(last <= 16 or first == last == 21, (kind, a1))        # A-P, or the ID column U only


class IdsAndApprove(unittest.TestCase):
    """Column U shows the tracker id; column V is the user's Approve checkbox on Found jobs, and the bot never writes to it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.t = T.Tracker(self.root / "t.sqlite3")
        self.sheet = FakeSheet()
        self.urls = []
        for i in range(1, 5):
            url = f"https://www.linkedin.com/jobs/view/{5000000 + i}"
            self.t.add_found({"canonical_url": url, "company": f"Acme {i}", "role": f"Engineer {i}", "route": "manual",
                              "source": "linkedin", "source_url": url, "location": "Bengaluru", "description": "x" * 300})
            jid = self.t.job(url)["id"]
            self.t.set_relevance(jid, 8, "good fit")
            self.urls.append(url)

    def tearDown(self):
        self.t.close()
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    def sync(self):
        return S.sync(self.t, self.sheet)

    def row_of(self, url):
        return self.sheet.row_for(T.key(url))

    def test_every_row_shows_its_tracker_id_and_the_header_names_the_columns(self):
        self.sync()
        for url in self.urls:
            self.assertEqual(self.sheet.cells[(self.row_of(url), 21)], str(self.t.job(url)["id"]))
        self.assertEqual((self.sheet.cells[(1, 21)], self.sheet.cells[(1, 22)]), ("ID", "Approve"))

    def test_ids_follow_the_rows_when_the_user_sorts_the_sheet(self):
        self.sync()
        a, b = self.row_of(self.urls[0]), self.row_of(self.urls[1])
        for c in range(1, 23):                                                   # the user swaps two whole rows
            self.sheet.cells[(a, c)], self.sheet.cells[(b, c)] = self.sheet.cells.get((b, c), ""), self.sheet.cells.get((a, c), "")
        self.sync()
        for url in self.urls:
            self.assertEqual(self.sheet.cells[(self.row_of(url), 21)], str(self.t.job(url)["id"]))

    def test_the_approve_box_is_shown_on_found_rows_only(self):
        self.t.decide([self.t.job(self.urls[0])["id"]], approve=False)           # skipped postings are not in the sheet
        jid = self.t.job(self.urls[1])["id"]
        self.t.set_state(jid, "failed", "x")
        self.sync()
        shown = {self.sheet.cells[(n, 7)] for n in self.sheet.boxes}
        self.assertEqual(shown, {T.key(self.urls[2]), T.key(self.urls[3])})

    def test_a_tick_approves_that_posting_and_only_that_one(self):
        self.sync()
        self.sheet.tick_approve(self.row_of(self.urls[2]))
        r = self.sync()
        self.assertEqual(r.approved, 1)
        self.assertEqual([x["status"] for x in self.t.by_ids([self.t.job(u)["id"] for u in self.urls])],
                         ["found", "found", "approved", "found"])
        self.assertIn("approved from the sheet", str(r))
        self.assertEqual(self.sync().approved, 0)                                # the tick stays; nothing is approved twice

    def test_a_tick_is_found_by_url_not_by_row_position_after_sorting(self):
        self.sync()
        a, b = self.row_of(self.urls[0]), self.row_of(self.urls[1])
        self.sheet.tick_approve(a)                                               # ticked on the first posting ...
        for c in range(1, 23):                                                   # ... then the user sorts: it moves with its row
            self.sheet.cells[(a, c)], self.sheet.cells[(b, c)] = self.sheet.cells.get((b, c), ""), self.sheet.cells.get((a, c), "")
        self.sync()
        states = {u: self.t.job(u)["status"] for u in self.urls}
        self.assertEqual(states[self.urls[0]], "approved")
        self.assertEqual({states[u] for u in self.urls[1:]}, {"found"})

    def test_a_tick_on_a_posting_that_is_not_found_changes_nothing(self):
        self.sync()
        jid = self.t.job(self.urls[0])["id"]
        self.t.decide([jid], approve=False)                                      # skipped after it was ticked
        self.sheet.tick_approve(self.row_of(self.urls[0]))
        self.assertEqual(self.sync().approved, 0)
        self.assertEqual(self.t.job(self.urls[0])["status"], "skipped")

    def test_other_values_in_the_column_are_not_ticks(self):
        self.sync()
        for text in ("", "FALSE", "yes", "x"):
            self.sheet.cells[(self.row_of(self.urls[0]), 22)] = text
            self.assertEqual(self.sync().approved, 0, text)

    def test_a_tick_made_while_the_sync_runs_is_still_caught(self):
        self.sync()
        n = self.row_of(self.urls[1])
        original, calls = self.sheet.read, []

        def read(a1, tab=None):
            rows = original(a1, tab)
            calls.append(a1)
            if len(calls) == 1:
                self.sheet.tick_approve(n)                                       # arrives right after the first read
            return rows
        self.sheet.read = read
        self.assertEqual(self.sync().approved, 1)

    def test_the_bot_never_writes_a_value_into_the_approve_column(self):
        self.sync()
        self.sheet.tick_approve(self.row_of(self.urls[0]))
        self.sync()
        self.t.db.execute("UPDATE jobs SET notes='n'")
        self.t.db.commit()
        self.sync()
        for kind, a1, tab in self.sheet.log:
            if tab is None and kind in ("write", "append"):
                first, last = S.columns_of(a1)
                self.assertFalse(first <= 22 <= last and a1 not in ("A1:V1", "V1"), (kind, a1))
        self.assertEqual(self.sheet.approve_cells(), {(self.row_of(self.urls[0]), 22): "TRUE"})   # only the user's own tick

    def test_a_label_the_user_wrote_in_v1_is_left_alone(self):
        self.sync()
        self.sheet.cells[(1, 22)] = "Go?"
        self.sync()
        self.assertEqual(self.sheet.cells[(1, 22)], "Go?")

    def test_the_users_columns_are_untouched_by_all_of_it(self):
        self.sync()
        n = self.row_of(self.urls[0])
        self.sheet.set_manual(n, ["a", "b", "c", "d"])
        before = self.sheet.manual()
        self.sheet.tick_approve(n)
        self.sync()
        self.assertEqual(self.sheet.manual(), before)

    def test_sync_without_a_tick_reads_nothing_it_should_not(self):
        r = self.sync()
        self.assertEqual(r.approved, 0)


class ConfigAndWiring(unittest.TestCase):
    def test_sheets_config_is_read_and_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "config.yaml"
            f.write_text("sheets:\n  spreadsheet_id: abc\n  sheet_name: Jobs\n", encoding="utf-8")
            cfg = cfgmod.load_config(f)
            self.assertTrue(S.configured(cfg))
            self.assertEqual(S.client_from_config(cfg, Path(tmp)).name, "Jobs")
            f.write_text("sheets: nope\n", encoding="utf-8")
            with self.assertRaises(cfgmod.ConfigError):
                cfgmod.load_config(f)
        self.assertFalse(S.configured(cfgmod.load_config(Path("does-not-exist.yaml"))))
        self.assertFalse(S.configured(cfgmod.load_config(ROOT / "config.example.yaml")))   # the example sets no id

    def test_a_run_updates_the_sheet_only_when_configured_and_authorised(self):
        import apply
        import tailor
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"JOBBOT_NO_SHEET": ""}):
            tmp = Path(tmp)
            d = folder(tmp, "out/a", GH, "submitted")
            fake = FakeSheet()
            cfg = tmp / "config.yaml"
            with mock.patch.object(tailor, "OUTPUT_DIR", tmp / "out"), mock.patch.object(apply, "CONFIG_FILE", cfg), \
                    mock.patch.object(apply, "ROOT", tmp):
                apply._track(d)                                              # no sheets in config: nothing happens
                self.assertEqual(fake.cells, {})
                cfg.write_text("sheets:\n  spreadsheet_id: abc\n", encoding="utf-8")
                apply._track(d)                                              # configured but no token.json: skipped
                self.assertEqual(fake.cells, {})
                (tmp / "token.json").write_text("{}", encoding="utf-8")
                with mock.patch.object(S.GoogleSheet, "read", lambda s, a, tab=None: fake.read(a, tab)), \
                        mock.patch.object(S.GoogleSheet, "write", lambda s, a, v, tab=None: fake.write(a, v, tab)), \
                        mock.patch.object(S.GoogleSheet, "append", lambda s, a, v, tab=None: fake.append(a, v, tab)), \
                        mock.patch.object(S.GoogleSheet, "clear", lambda s, a, tab=None: fake.clear(a, tab)), \
                        mock.patch.object(S.GoogleSheet, "ensure_action_tab", lambda s, t, h: fake.ensure_action_tab(t, h)):
                    apply._track(d)
                self.assertEqual(fake.row(2)[6], GH)

    def test_a_sheet_failure_never_fails_a_run(self):
        import apply
        import tailor
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"JOBBOT_NO_SHEET": ""}):
            tmp = Path(tmp)
            d = folder(tmp, "out/a", GH, "submitted")
            (tmp / "config.yaml").write_text("sheets:\n  spreadsheet_id: abc\n", encoding="utf-8")
            (tmp / "token.json").write_text("{}", encoding="utf-8")
            boom = mock.Mock(side_effect=S.SheetsError("offline"))
            with mock.patch.object(tailor, "OUTPUT_DIR", tmp / "out"), \
                    mock.patch.object(apply, "CONFIG_FILE", tmp / "config.yaml"), \
                    mock.patch.object(apply, "ROOT", tmp), mock.patch.object(S.GoogleSheet, "read", boom):
                apply._track(d)                                              # prints a warning, does not raise
                with T.Tracker(tmp / "out" / "tracker.sqlite3") as t:
                    self.assertTrue(t.is_submitted(GH))                      # and the tracker still got the row

    def test_missing_google_libraries_give_the_install_command(self):
        g = S.GoogleSheet("id", "Applications", Path("nope.json"), Path("nope-token.json"))
        with mock.patch.dict(sys.modules, {"googleapiclient": None, "googleapiclient.discovery": None}):
            with self.assertRaises(S.SheetsError) as cm:
                g.read("A1:T")
        self.assertIn("pip install google-api-python-client google-auth-oauthlib", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
