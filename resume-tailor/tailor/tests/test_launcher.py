"""The `jobapply` command: argument handling, the action list, the log, the scheduled-task switch and the closing summary.
Nothing here runs the real flow, touches the real tracker or the real scheduled task."""
import io
import json
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tailor  # noqa: E402
from jobbot import launcher as L  # noqa: E402
from jobbot import tracker as T  # noqa: E402


class Parse(unittest.TestCase):
    def test_no_arguments_is_the_full_run(self):
        self.assertEqual(L.parse([]), L.Plan("run", []))

    def test_dry_is_a_dry_run_and_keeps_extra_arguments(self):
        self.assertEqual(L.parse(["dry"]), L.Plan("run", ["--dry-run"]))
        self.assertEqual(L.parse(["dry", "--skip-discovery"]), L.Plan("run", ["--dry-run", "--skip-discovery"]))

    def test_status_and_sync_go_to_apply(self):
        self.assertEqual(L.parse(["status"]), L.Plan("apply", ["--status"]))
        self.assertEqual(L.parse(["status", "5"]), L.Plan("apply", ["--status", "5"]))
        self.assertEqual(L.parse(["sync"]), L.Plan("apply", ["--sync-sheet"]))

    def test_the_other_words(self):
        self.assertEqual(L.parse(["action"]).kind, "action")
        self.assertEqual(L.parse(["log"]).kind, "log")
        self.assertEqual((L.parse(["stop"]).kind, L.parse(["stop"]).enable), ("task", False))
        self.assertEqual((L.parse(["start"]).kind, L.parse(["start"]).enable), ("task", True))
        for word in ("help", "-h", "--help", "/?", "?"):
            self.assertEqual(L.parse([word]).kind, "help", word)

    def test_words_are_not_case_sensitive(self):
        self.assertEqual(L.parse(["DRY"]), L.Plan("run", ["--dry-run"]))
        self.assertEqual(L.parse(["Status"]).kind, "apply")

    def test_anything_else_goes_to_run_py_untouched(self):
        self.assertEqual(L.parse(["--skip-discovery"]), L.Plan("run", ["--skip-discovery"]))
        self.assertEqual(L.parse(["--dry-run", "--skip-discovery"]), L.Plan("run", ["--dry-run", "--skip-discovery"]))
        self.assertEqual(L.parse(["something", "else"]), L.Plan("run", ["something", "else"]))     # run.py will complain

    def test_help_lists_every_command(self):
        for word in ("dry", "status", "sync", "action", "log", "stop", "start", "help"):
            self.assertIn(f"jobapply {word}", L.HELP)


class Main(unittest.TestCase):
    def go(self, argv, code=0, summary=None):
        calls = []
        with mock.patch.object(L, "run_module", lambda name, args: calls.append((name, args)) or code), \
                mock.patch.object(L, "newest_summary", lambda since: summary), \
                mock.patch.object(L, "set_task", lambda enable: calls.append(("task", enable)) or 0), \
                redirect_stdout(io.StringIO()) as out:
            rc = L.main(argv)
        return rc, calls, out.getvalue()

    def test_the_right_module_gets_the_right_arguments(self):
        self.assertEqual(self.go([])[1], [("run", [])])
        self.assertEqual(self.go(["dry"])[1], [("run", ["--dry-run"])])
        self.assertEqual(self.go(["status", "3"])[1], [("apply", ["--status", "3"])])
        self.assertEqual(self.go(["sync"])[1], [("apply", ["--sync-sheet"])])
        self.assertEqual(self.go(["stop"])[1], [("task", False)])
        self.assertEqual(self.go(["start"])[1], [("task", True)])

    def test_a_run_ends_with_the_summary_and_keeps_its_exit_code(self):
        s = {"date": "2030-01-02", "dry_run": False, "found_new": 12, "scored": 40, "submitted": 1, "ready_for_you": 2,
             "needs_review": 3}
        rc, _, out = self.go([], 0, s)
        self.assertEqual(rc, 0)
        tail = out.strip().splitlines()[-2]
        self.assertIn("found 12", out)
        for text in ("scored 40", "submitted 1", "ready for you 2", "needs review 3"):
            self.assertIn(text, out)
        self.assertTrue(out.strip().endswith("=" * 78), tail)                 # the block is the last thing printed
        self.assertEqual(self.go([], 3, s)[0], 3)

    def test_a_run_that_did_not_finish_has_no_summary_but_says_so(self):
        out = self.go([], 3, {"found_new": 99})[2]
        self.assertNotIn("found 99", out)
        self.assertIn("did not finish", out)

    def test_help_for_run_py_itself_is_not_followed_by_a_summary(self):
        self.assertEqual(self.go(["--help"])[1], [])                          # "--help" is jobapply's own help
        out = self.go(["--skip-discovery", "-h"])[2]
        self.assertNotIn("summary", out.lower())

    def test_a_dry_run_says_so(self):
        out = self.go(["dry"], 0, {"date": "d", "dry_run": True, "would_approve": [1, 2]})[2]
        self.assertIn("DRY RUN", out)
        self.assertIn("would approve: 2", out)


class Summary(unittest.TestCase):
    def test_the_newest_summary_since_the_start_is_read(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(tailor, "OUTPUT_DIR", Path(d)):
            runs = Path(d) / "runs"
            runs.mkdir()
            old = runs / "2030-01-01.json"
            old.write_text(json.dumps({"date": "old"}), encoding="utf-8")
            since = time.time() + 5
            self.assertIsNone(L.newest_summary(since))                        # nothing written during this run
            new = runs / "2030-01-02.json"
            new.write_text(json.dumps({"date": "new"}), encoding="utf-8")
            self.assertEqual(L.newest_summary(time.time() - 1)["date"], "new")

    def test_no_runs_folder_is_fine(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(tailor, "OUTPUT_DIR", Path(d)):
            self.assertIsNone(L.newest_summary(0))


class Action(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="launcher test ")
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)

    def add(self, t, n, status, reason="", folder=None):
        url = f"https://boards.greenhouse.io/acme{n}/jobs/{n}"
        t.add_found({"canonical_url": url, "company": f"Acme {n}", "role": f"Engineer {n}", "route": "apply",
                     "source": "greenhouse", "source_url": url, "location": "Bengaluru"})
        jid = t.job(url)["id"]
        t.set_state(jid, status, reason, folder)
        return jid

    def test_only_the_jobs_waiting_on_you_are_listed_with_link_folder_and_reason(self):
        with mock.patch.object(tailor, "OUTPUT_DIR", self.out):
            with T.Tracker(self.out / "tracker.sqlite3") as t:
                self.add(t, 1, "ready_for_you", "Prepared; submit it yourself.", r"C:\jobs\one")
                self.add(t, 2, "needs_review", "A captcha was shown.")
                self.add(t, 3, "manual")
                self.add(t, 4, "failed", "Form changed.")
                self.add(t, 5, "submitted")
                self.add(t, 6, "approved")
                self.add(t, 7, "found")
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(L.show_action(), 0)
        text = out.getvalue()
        self.assertIn("4 job(s) need you", text)
        for company in ("Acme 1", "Acme 2", "Acme 3", "Acme 4"):
            self.assertIn(company, text)
        for company in ("Acme 5", "Acme 6", "Acme 7"):
            self.assertNotIn(company, text)
        self.assertIn("https://job-boards.greenhouse.io/acme1/jobs/1", text)
        self.assertIn(r"C:\jobs\one", text)
        self.assertIn("A captcha was shown.", text)
        self.assertLess(text.index("Ready for you"), text.index("Needs review"))     # the tracker's own order

    def test_nothing_waiting_and_no_tracker_are_both_plain_messages(self):
        with mock.patch.object(tailor, "OUTPUT_DIR", self.out):
            with redirect_stdout(io.StringIO()) as out:
                L.show_action()
            self.assertIn("No tracker yet", out.getvalue())
            with T.Tracker(self.out / "tracker.sqlite3"):
                pass
            with redirect_stdout(io.StringIO()) as out:
                L.show_action()
            self.assertIn("Nothing needs you", out.getvalue())


class LogAndTask(unittest.TestCase):
    def test_the_log_opens_only_when_today_has_one(self):
        with tempfile.TemporaryDirectory() as d, mock.patch.object(tailor, "OUTPUT_DIR", Path(d)):
            opened = []
            with redirect_stdout(io.StringIO()) as out:
                self.assertEqual(L.open_log(opened.append), 1)
            self.assertEqual(opened, [])
            self.assertIn("No run log for today", out.getvalue())
            path = L.todays_log()
            path.parent.mkdir(parents=True)
            path.write_text("x", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                self.assertEqual(L.open_log(opened.append), 0)
            self.assertEqual(opened, [str(path)])

    def test_stop_and_start_change_the_named_task(self):
        seen = []

        def fake(cmd, **kw):
            seen.append(cmd)
            return SimpleNamespace(returncode=0, stdout="SUCCESS", stderr="")

        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(L.set_task(False, fake), 0)
            self.assertEqual(L.set_task(True, fake), 0)
        self.assertEqual(seen[0], ["schtasks", "/Change", "/TN", "JobApply daily run", "/DISABLE"])
        self.assertEqual(seen[1], ["schtasks", "/Change", "/TN", "JobApply daily run", "/ENABLE"])
        self.assertIn("disabled", out.getvalue())
        self.assertIn("enabled", out.getvalue())

    def test_a_missing_task_is_explained_not_a_traceback(self):
        def fake(cmd, **kw):
            return SimpleNamespace(returncode=1, stdout="", stderr="ERROR: The specified task name does not exist")

        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(L.set_task(False, fake), 1)
        self.assertIn("Could not disable", out.getvalue())
        self.assertIn("does not exist", out.getvalue())


class Launcher(unittest.TestCase):
    """bin/jobapply.cmd works from any folder: absolute paths, every argument passed on."""

    def setUp(self):
        self.text = (ROOT / "bin" / "jobapply.cmd").read_text(encoding="utf-8")

    def test_it_uses_absolute_paths_that_exist(self):
        home = next(line for line in self.text.splitlines() if "JOBAPPLY_HOME=" in line).split("=", 1)[1].strip('"')
        self.assertRegex(home, r"^[A-Za-z]:\\")
        self.assertEqual(Path(home).resolve(), ROOT.resolve())
        self.assertIn(r'"%JOBAPPLY_HOME%\.venv\Scripts\python.exe"', self.text)
        self.assertTrue((ROOT / "jobapply.py").exists())

    def test_it_changes_to_the_project_folder_and_passes_every_argument_on(self):
        self.assertIn("pushd", self.text)
        self.assertIn("jobapply.py", self.text)
        self.assertIn("%*", self.text)
        self.assertIn("exit /b %JOBAPPLY_RC%", self.text)

    def test_it_never_touches_the_path(self):
        self.assertNotIn("setx", self.text.lower())
        self.assertNotRegex(self.text, r"(?i)set\s+\"?path=")

    def test_the_entry_script_calls_the_launcher(self):
        self.assertIn("from jobbot.launcher import main", (ROOT / "jobapply.py").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
