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
    """Cells in a dict; read() trims trailing empty cells and rows like Google does. Records every write."""

    def __init__(self):
        self.cells: dict = {}                  # (row, column) -> text
        self.log: list = []

    @staticmethod
    def _parse(a1):
        m = re.match(r"^([A-Z]+)(\d*)(?::([A-Z]+)(\d*))?$", a1)
        return S._col_number(m.group(1)), int(m.group(2) or 0), S._col_number(m.group(3) or m.group(1)), int(m.group(4) or 0)

    def read(self, a1):
        c1, r1, c2, r2 = self._parse(a1)
        last = max((r for (r, c) in self.cells if c1 <= c <= c2 and self.cells[(r, c)] != ""), default=0)
        rows = []
        for r in range(max(r1, 1), min(last, r2 or last) + 1):
            row = [self.cells.get((r, c), "") for c in range(c1, c2 + 1)]
            while row and row[-1] == "":
                row.pop()
            rows.append(row)
        return rows

    def write(self, a1, values):
        self.log.append(("write", a1))
        c1, r1, _, _ = self._parse(a1)
        for i, row in enumerate(values):
            for j, v in enumerate(row):
                self.cells[(r1 + i, c1 + j)] = str(v)

    def append(self, a1, values):
        self.log.append(("append", a1))
        c1, _, c2, _ = self._parse(a1)
        last = max((r for (r, c) in self.cells if c1 <= c <= c2), default=0)
        for i, row in enumerate(values):
            for j, v in enumerate(row):
                self.cells[(last + 1 + i, c1 + j)] = str(v)

    # helpers for the tests
    def row(self, n):
        return [self.cells.get((n, c), "") for c in range(1, 17)]

    def manual(self):
        return {k: v for k, v in self.cells.items() if k[1] >= 13}

    def set_manual(self, n, values):
        for j, v in enumerate(values):
            self.cells[(n, 13 + j)] = v

    def row_for(self, url):
        return next(r for r in range(2, 50) if self.cells.get((r, 7)) == url)


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
        self.assertEqual(S.HEADER[:12], ["Date", "Status", "Mode", "Company", "Role", "Location", "URL", "Score",
                                         "Flagged fields", "Resume file used", "Gaps", "Notes"])
        self.assertEqual(S.HEADER[12:], ["Response", "Interview stage", "Follow-up date", "My notes"])
        row = self.sheet.row(self.sheet.row_for(GH))
        self.assertEqual(row[:7], ["2026-10-02", "submitted", "assist", "project44", "Software Engineer 2", "Bengaluru", GH])
        self.assertEqual((row[7], row[8]), ("7", "Why us?; Salary"))
        self.assertTrue(row[9].startswith("a/") and row[9].endswith("/r.pdf"), row[9])
        self.assertEqual(row[10], "Kafka")
        self.assertEqual(row[12:], ["", "", "", ""])
        self.assertEqual(self.sheet.row(self.sheet.row_for(LV))[2], "manual")           # a hand submission says so

    def test_a_second_sync_changes_nothing(self):
        S.sync(self.t, self.sheet)
        before, n = dict(self.sheet.cells), len(self.sheet.log)
        r = S.sync(self.t, self.sheet)
        self.assertEqual((r.added, r.updated, r.unchanged), (0, 0, 2))
        self.assertEqual((self.sheet.cells, len(self.sheet.log)), (before, n))

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
        self.assertEqual(self.sheet.row(n)[12:], ["Rejected", "Phone screen", "2026-10-20", "Recruiter was kind"])

    def test_sorting_the_sheet_cannot_make_the_bot_write_to_the_wrong_row(self):
        S.sync(self.t, self.sheet)
        a, b = self.sheet.row_for(GH), self.sheet.row_for(LV)
        self.sheet.set_manual(a, ["Rejected", "", "", "about project44"])
        self.sheet.set_manual(b, ["Interview", "Round 1", "2026-10-09", "about Hevo"])
        rows = {n: self.sheet.row(n) for n in (a, b)}                           # the user swaps the two rows
        for c in range(1, 17):
            self.sheet.cells[(a, c)], self.sheet.cells[(b, c)] = rows[b][c - 1], rows[a][c - 1]
        self.t.db.execute("UPDATE jobs SET status='failed', notes='x' WHERE canonical_url=?", (T.key(LV),))
        self.t.db.commit()
        S.sync(self.t, self.sheet)
        hevo = self.sheet.row(self.sheet.row_for(LV))
        self.assertEqual((hevo[1], hevo[11], hevo[12:]), ("failed", "x", ["Interview", "Round 1", "2026-10-09", "about Hevo"]))
        p44 = self.sheet.row(self.sheet.row_for(GH))
        self.assertEqual((p44[1], p44[12], p44[15]), ("submitted", "Rejected", "about project44"))

    def test_a_new_posting_is_appended_without_touching_existing_rows(self):
        S.sync(self.t, self.sheet)
        self.sheet.set_manual(2, ["Seen", "", "", "note"])
        self.t.record_folder(folder(self.root, "new", "https://job-boards.greenhouse.io/acme/jobs/9", "dry_run", mode="dry-run"))
        r = S.sync(self.t, self.sheet)
        self.assertEqual((r.added, r.unchanged), (1, 2))
        self.assertEqual(self.sheet.cells[(2, 13)], "Seen")
        self.assertEqual(self.sheet.cells[(4, 7)], "https://job-boards.greenhouse.io/acme/jobs/9")

    def test_only_the_given_urls_are_synced_after_a_run(self):
        r = S.sync(self.t, self.sheet, urls=[GH + "/"])
        self.assertEqual((r.added, len(self.sheet.read("A1:P"))), (1, 2))        # header + project44 only

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
        for bad in ("M2", "M2:P2", "A2:M2", "A2:P2", "P1", "L2:M2"):
            with self.assertRaises(S.SheetsError, msg=bad):
                guarded.write(bad, [["x"]])
        with self.assertRaises(S.SheetsError):
            guarded.append("A:L", [["x"] * 13])
        with self.assertRaises(S.SheetsError):
            guarded.append("A:P", [["x"]])
        guarded.write("A2:L2", [["x"] * 12])
        guarded.append("A:L", [["y"] * 12])
        self.assertEqual(self.sheet.manual(), {})

    def test_an_existing_sheet_keeps_the_users_renamed_manual_headers(self):
        S.sync(self.t, self.sheet)
        self.sheet.cells[(1, 13)] = "Reply"                                         # the user renamed a header
        self.sheet.cells[(1, 2)] = "State"                                           # and changed one of ours
        S.sync(self.t, self.sheet)
        self.assertEqual((self.sheet.cells[(1, 13)], self.sheet.cells[(1, 2)]), ("Reply", "Status"))

    def test_every_write_of_a_full_scenario_stays_in_the_bots_columns(self):
        S.sync(self.t, self.sheet)
        self.t.db.execute("UPDATE jobs SET notes='n'")
        self.t.db.commit()
        S.sync(self.t, self.sheet)
        for kind, a1 in self.sheet.log:
            if a1 != "A1:P1":                                                        # the header of an empty sheet
                self.assertLessEqual(S.columns_of(a1)[1], 12, (kind, a1))


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
                with mock.patch.object(S.GoogleSheet, "read", lambda s, a: fake.read(a)), \
                        mock.patch.object(S.GoogleSheet, "write", lambda s, a, v: fake.write(a, v)), \
                        mock.patch.object(S.GoogleSheet, "append", lambda s, a, v: fake.append(a, v)):
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
                g.read("A1:P")
        self.assertIn("pip install google-api-python-client google-auth-oauthlib", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
