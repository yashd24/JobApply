"""Google Sheets sync against a fake sheet that models cells. The one rule under test: the bot never writes to the
user's own columns (Response, Interview stage, Follow-up date, My notes)."""
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
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
        self.between_reads = None                      # a callable run after each action-tab read (a user ticking mid-sync)

    @property
    def cells(self):
        return self.tabs[self.MAIN]

    @staticmethod
    def _parse(a1):
        m = re.match(r"^([A-Z]+)(\d*)(?::([A-Z]+)(\d*))?$", a1)
        return S._col_number(m.group(1)), int(m.group(2) or 0), S._col_number(m.group(3) or m.group(1)), int(m.group(4) or 0)

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
    def row(self, n, tab=None, width=18):
        cells = self.tabs[tab or self.MAIN]
        return [cells.get((n, c), "") for c in range(1, width + 1)]

    def manual(self):
        return {k: v for k, v in self.cells.items() if k[1] >= 15}

    def set_manual(self, n, values):
        for j, v in enumerate(values):
            self.cells[(n, 15 + j)] = v

    def row_for(self, url):
        return next(r for r in range(2, 50) if self.cells.get((r, 7)) == url)

    def tick(self, tab, n):
        self.tabs[tab][(n, 1)] = "TRUE"


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
        self.assertEqual(self.sheet.row(1), S.HEADER)
        self.assertEqual(S.HEADER[:14], ["Date", "Status", "Mode", "Company", "Role", "Location", "URL", "Score",
                                         "Flagged fields", "Resume file used", "Gaps", "Notes", "Reason", "Job folder"])
        self.assertEqual(S.HEADER[14:], ["Response", "Interview stage", "Follow-up date", "My notes"])
        row = self.sheet.row(self.sheet.row_for(GH))
        self.assertEqual(row[:7], ["2026-10-02", "Submitted", "assist", "project44", "Software Engineer 2", "Bengaluru", GH])
        self.assertEqual((row[7], row[8]), ("7", "Why us?; Salary"))
        self.assertTrue(row[9].startswith("a/") and row[9].endswith("/r.pdf"), row[9])
        self.assertEqual(row[10], "Kafka")
        self.assertEqual(row[14:], ["", "", "", ""])
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
        self.assertEqual(self.sheet.row(n)[14:], ["Rejected", "Phone screen", "2026-10-20", "Recruiter was kind"])

    def test_sorting_the_sheet_cannot_make_the_bot_write_to_the_wrong_row(self):
        S.sync(self.t, self.sheet)
        a, b = self.sheet.row_for(GH), self.sheet.row_for(LV)
        self.sheet.set_manual(a, ["Rejected", "", "", "about project44"])
        self.sheet.set_manual(b, ["Interview", "Round 1", "2026-10-09", "about Hevo"])
        rows = {n: self.sheet.row(n) for n in (a, b)}                           # the user swaps the two rows
        for c in range(1, 19):
            self.sheet.cells[(a, c)], self.sheet.cells[(b, c)] = rows[b][c - 1], rows[a][c - 1]
        self.t.db.execute("UPDATE jobs SET status='failed', notes='x' WHERE canonical_url=?", (T.key(LV),))
        self.t.db.commit()
        S.sync(self.t, self.sheet)
        hevo = self.sheet.row(self.sheet.row_for(LV))
        self.assertEqual((hevo[1], hevo[11], hevo[14:]), ("Failed", "x", ["Interview", "Round 1", "2026-10-09", "about Hevo"]))
        p44 = self.sheet.row(self.sheet.row_for(GH))
        self.assertEqual((p44[1], p44[14], p44[17]), ("Submitted", "Rejected", "about project44"))

    def test_a_new_posting_is_appended_without_touching_existing_rows(self):
        S.sync(self.t, self.sheet)
        self.sheet.set_manual(2, ["Seen", "", "", "note"])
        self.t.record_folder(folder(self.root, "new", "https://job-boards.greenhouse.io/acme/jobs/9", "failed"))
        r = S.sync(self.t, self.sheet)
        self.assertEqual((r.added, r.unchanged), (1, 2))
        self.assertEqual(self.sheet.cells[(2, 15)], "Seen")
        self.assertEqual(self.sheet.cells[(4, 7)], "https://job-boards.greenhouse.io/acme/jobs/9")

    def test_only_the_given_urls_are_synced_after_a_run(self):
        r = S.sync(self.t, self.sheet, urls=[GH + "/"])
        self.assertEqual((r.added, len(self.sheet.read("A1:R"))), (1, 2))        # header + project44 only

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
        for bad in ("O2", "O2:R2", "A2:O2", "A2:R2", "R1", "N2:O2"):
            with self.assertRaises(S.SheetsError, msg=bad):
                guarded.write(bad, [["x"]])
        with self.assertRaises(S.SheetsError):
            guarded.append("A:N", [["x"] * 15])
        with self.assertRaises(S.SheetsError):
            guarded.append("A:R", [["x"]])
        guarded.write("A2:N2", [["x"] * 14])
        guarded.append("A:N", [["y"] * 14])
        self.assertEqual(self.sheet.manual(), {})

    def test_an_existing_sheet_keeps_the_users_renamed_manual_headers(self):
        S.sync(self.t, self.sheet)
        self.sheet.cells[(1, 15)] = "Reply"                                         # the user renamed a header
        self.sheet.cells[(1, 2)] = "State"                                           # and changed one of ours
        S.sync(self.t, self.sheet)
        self.assertEqual((self.sheet.cells[(1, 15)], self.sheet.cells[(1, 2)]), ("Reply", "Status"))

    def test_every_write_of_a_full_scenario_stays_in_the_bots_columns(self):
        S.sync(self.t, self.sheet)
        self.t.db.execute("UPDATE jobs SET notes='n'")
        self.t.db.commit()
        S.sync(self.t, self.sheet)
        for kind, a1, tab in self.sheet.log:
            if tab is None and a1 != "A1:R1":                                        # main tab; R1 = header of an empty sheet
                self.assertLessEqual(S.columns_of(a1)[1], 14, (kind, a1))


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
        with tempfile.TemporaryDirectory() as tmp:
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
        with tempfile.TemporaryDirectory() as tmp:
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
                g.read("A1:R")
        self.assertIn("pip install google-api-python-client google-auth-oauthlib", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
