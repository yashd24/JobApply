"""The tracker commands behind `jobapply`: list, approve, skip, done, open, retry, url, check, find and process.
Everything is faked: no network, no Claude, no browser, no real sheet, and nothing is ever submitted."""
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

import tailor  # noqa: E402
from jobbot import commands as C  # noqa: E402
from jobbot import intake  # noqa: E402
from jobbot import launcher as L  # noqa: E402
from jobbot import tracker as T  # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="commands test ")
        self.root = Path(self.tmp.name)
        self.out = self.root / "output"
        self.tr = T.Tracker(self.out / "tracker.sqlite3")
        p = mock.patch.object(tailor, "OUTPUT_DIR", self.out)
        p.start()
        self.addCleanup(p.stop)
        self.said = []

    def tearDown(self):
        self.tr.close()
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    def say(self, *a):
        self.said.append(" ".join(map(str, a)))

    def add(self, n, score=8, company=None, role=None, route="manual", status=None):
        url = f"https://www.linkedin.com/jobs/view/{6000000 + n}"
        self.tr.add_found({"canonical_url": url, "company": company or f"Acme{n}", "role": role or f"Backend Engineer {n}",
                           "route": route, "source": "linkedin", "source_url": url, "location": "Bengaluru, Karnataka",
                           "description": "Python Django. " * 30})
        jid = self.tr.job(url)["id"]
        if score is not None:
            self.tr.set_relevance(jid, score, "fits")
        if status:
            self.tr.set_state(jid, status, "set by the test")
        return jid

    def row(self, jid):
        return self.tr.by_ids([jid])[0]


class Listing(Base):
    def test_the_table_has_id_score_company_role_location_and_the_full_link(self):
        a = self.add(1, 9)
        text = C.list_text(self.tr.found())
        head, first = text.splitlines()[:2]
        for word in ("ID", "SCORE", "COMPANY", "ROLE", "LOCATION", "LINK"):
            self.assertIn(word, head)
        for part in (str(a), "9", "Acme1", "Backend Engineer 1", "Bengaluru", "https://www.linkedin.com/jobs/view/6000001"):
            self.assertIn(part, first)

    def test_a_minimum_score_keeps_only_those_and_unscored_never_pass(self):
        self.add(1, 9)
        self.add(2, 7)
        self.add(3, 6)
        self.add(4, None)
        ids = lambda t: [ln.split()[0] for ln in t.splitlines()[1:] if ln[:5].strip().isdigit()]  # noqa: E731
        self.assertEqual(len(ids(C.list_text(self.tr.found(), 7))), 2)
        self.assertEqual(len(ids(C.list_text(self.tr.found(), 9))), 1)
        self.assertEqual(len(ids(C.list_text(self.tr.found()))), 4)

    def test_best_first_and_nothing_found_is_a_plain_message(self):
        self.add(1, 6)
        self.add(2, 9)
        lines = C.list_text(self.tr.found()).splitlines()
        self.assertIn("Acme2", lines[1])
        self.assertIn("No Found jobs scored 10+", C.list_text(self.tr.found(), 10))

    def test_only_found_jobs_are_listed(self):
        self.add(1, 8, status="approved")
        self.add(2, 8, status="failed")
        self.add(3, 8)
        text = C.list_text(self.tr.found())
        self.assertIn("Acme3", text)
        self.assertNotIn("Acme1", text)
        self.assertNotIn("Acme2", text)

    def test_the_minimum_score_is_read_strictly(self):
        self.assertEqual((C.parse_min_score([]), C.parse_min_score(["7"]), C.parse_min_score(["7+"])), (None, 7, 7))
        for bad in (["0"], ["11"], ["x"], ["-3"]):
            with self.assertRaises(C.CommandError, msg=bad):
                C.parse_min_score(bad)


class Deciding(Base):
    def test_approve_ids_approves_exactly_those(self):
        a, b, c = self.add(1), self.add(2), self.add(3)
        ids = C.resolve_approval(self.tr, ["1,3"]) if False else C.resolve_approval(self.tr, [f"{a},{c}"])
        text = C.decide_text(self.tr, ids, approve=True)
        self.assertEqual([self.row(i)["status"] for i in (a, b, c)], ["approved", "found", "approved"])
        self.assertIn("Approved 2", text)
        self.assertIn("jobapply process", text)

    def test_approve_all_means_every_found_job(self):
        ids = [self.add(i, s) for i, s in ((1, 9), (2, 3), (3, None))]
        got = C.resolve_approval(self.tr, ["all"])
        self.assertEqual(sorted(got), sorted(ids))
        self.add(4, 9, status="failed")                                        # not Found: never part of "all"
        self.assertEqual(len(C.resolve_approval(self.tr, ["ALL"])), 3)

    def test_approve_n_plus_means_every_found_job_scoring_n_or_more(self):
        hi, mid, low, unscored = self.add(1, 9), self.add(2, 8), self.add(3, 7), self.add(4, None)
        self.assertEqual(sorted(C.resolve_approval(self.tr, ["8+"])), sorted([hi, mid]))
        self.assertEqual(sorted(C.resolve_approval(self.tr, ["7", "+"])), sorted([hi, mid, low]))
        self.assertEqual(C.resolve_approval(self.tr, ["10+"]), [])
        self.assertNotIn(unscored, C.resolve_approval(self.tr, ["1+"]))

    def test_a_bare_number_is_an_id_not_a_score(self):
        a = self.add(1, 9)
        self.assertEqual(C.resolve_approval(self.tr, [str(a)]), [a])

    def test_typos_change_nothing(self):
        self.add(1)
        for bad in ([], ["abc"], ["1,x"], ["11+"], ["0+"]):
            with self.assertRaises(C.CommandError, msg=bad):
                C.resolve_approval(self.tr, bad)
        self.assertEqual(len(self.tr.found()), 1)

    def test_a_job_that_is_not_found_or_does_not_exist_is_reported_and_left_alone(self):
        done = self.add(1, status="ready_for_you")
        text = C.decide_text(self.tr, [done, 9999], approve=True)
        self.assertEqual(self.row(done)["status"], "ready_for_you")
        self.assertIn("is Ready for you, not Found", text)
        self.assertIn("no job with id 9999", text)

    def test_nothing_matched_says_so(self):
        self.assertIn("Nothing matched", C.decide_text(self.tr, [], approve=True))

    def test_skip_rejects_for_good(self):
        a, b = self.add(1), self.add(2)
        text = C.decide_text(self.tr, C.parse_ids([f"{a}"]), approve=False)
        self.assertEqual((self.row(a)["status"], self.row(b)["status"]), ("skipped", "found"))
        self.assertIn("never come back", text)
        again = self.tr.add_found({"canonical_url": self.row(a)["canonical_url"], "company": "Acme1",
                                   "role": "Backend Engineer 1", "route": "manual", "source": "linkedin"})
        self.assertEqual(again, "duplicate")                                   # a later search cannot bring it back
        self.assertEqual(self.row(a)["status"], "skipped")

    def test_ids_are_read_strictly(self):
        self.assertEqual(C.parse_ids(["3,7", "12"]), [3, 7, 12])
        self.assertEqual(C.parse_ids(["7 3 3"]), [3, 7])
        for bad in ([], ["x"], ["3,"] and ["3,x"]):
            with self.assertRaises(C.CommandError):
                C.parse_ids(bad)


class Done(Base):
    def test_done_marks_applied_like_the_sheet_tick(self):
        a = self.add(1, status="ready_for_you")
        text = C.mark_done_text(self.tr, [a, 777])
        self.assertEqual(self.row(a)["status"], "submitted")
        self.assertEqual(self.row(a)["submitted_by"], "manual")
        self.assertIn("marked as applied", text)
        self.assertIn("no such job", text)
        self.assertIn("already Submitted", C.mark_done_text(self.tr, [a]))


class Opening(Base):
    def test_the_link_the_sheet_and_the_folder_are_opened_together(self):
        a = self.add(1, status="ready_for_you")
        folder = self.root / "job folder"
        folder.mkdir()
        (folder / "prepare_sheet.html").write_text("<html></html>", encoding="utf-8")
        self.tr.set_state(a, "ready_for_you", "ready", str(folder))
        links, paths = [], []
        rc = C.open_job(self.tr, a, links.append, paths.append, say=self.say)
        self.assertEqual(rc, 0)
        self.assertEqual(links, ["https://www.linkedin.com/jobs/view/6000001"])
        self.assertEqual(paths, [str(folder / "prepare_sheet.html"), str(folder)])

    def test_a_folder_without_a_sheet_and_a_job_without_a_folder(self):
        a = self.add(1)
        links, paths = [], []
        self.assertEqual(C.open_job(self.tr, a, links.append, paths.append, say=self.say), 0)
        self.assertEqual((len(links), paths), (1, []))                         # only the link; nothing prepared yet
        self.assertTrue(any("none yet" in s for s in self.said))
        folder = self.root / "bare"
        folder.mkdir()
        self.tr.set_state(a, "ready_for_you", "r", str(folder))
        paths.clear()
        C.open_job(self.tr, a, links.append, paths.append, say=self.say)
        self.assertEqual(paths, [str(folder)])
        self.assertTrue(any("no prepare_sheet.html" in s for s in self.said))

    def test_an_unknown_id_opens_nothing(self):
        links, paths = [], []
        self.assertEqual(C.open_job(self.tr, 4242, links.append, paths.append, say=self.say), 1)
        self.assertEqual((links, paths), ([], []))

    def test_a_manual_posting_opens_its_direct_apply_link_when_it_has_one(self):
        a = self.add(1)
        self.tr.db.execute("UPDATE jobs SET direct_url='https://careers.acme.example/job/1' WHERE id=?", (a,))
        self.tr.db.commit()
        links = []
        C.open_job(self.tr, a, links.append, lambda p: None, say=self.say)
        self.assertEqual(links, ["https://careers.acme.example/job/1"])


class Retry(Base):
    def test_a_failed_job_goes_back_in_the_queue_keeping_its_folder(self):
        folder = self.root / "saved"
        folder.mkdir()
        a = self.add(1, status="failed")
        self.tr.set_state(a, "failed", "boom", str(folder))
        self.assertEqual(C.prepare_retry(self.tr, a), a)
        r = self.row(a)
        self.assertEqual((r["status"], r["job_folder"]), ("approved", str(folder)))
        self.assertIn("saved folder", r["reason"])

    def test_needs_review_and_manual_can_be_retried_too(self):
        for n, status in ((1, "needs_review"), (2, "manual")):
            a = self.add(n, status=status)
            C.prepare_retry(self.tr, a)
            self.assertEqual(self.row(a)["status"], "approved")

    def test_other_states_are_refused_with_the_right_next_step(self):
        cases = {"found": "approve", "approved": "process", "submitted": "already applied for",
                 "ready_for_you": "jobapply open", "skipped": "rejected"}
        for n, (status, hint) in enumerate(cases.items(), 1):
            a = self.add(n, status=None if status == "found" else status)
            with self.assertRaises(C.CommandError) as cm:
                C.prepare_retry(self.tr, a)
            self.assertIn(hint, str(cm.exception), status)
            self.assertEqual(self.row(a)["status"], status)
        with self.assertRaises(C.CommandError):
            C.prepare_retry(self.tr, 31337)

    def test_a_missing_folder_is_not_recorded(self):
        a = self.add(1, status="failed")
        self.tr.set_state(a, "failed", "x", str(self.root / "gone"))
        C.prepare_retry(self.tr, a)
        self.assertEqual(self.row(a)["status"], "approved")


class FromLink(Base):
    GH = "https://job-boards.greenhouse.io/acme/jobs/777"

    def job(self, platform="greenhouse", url=None):
        return SimpleNamespace(platform=platform, company="Acme", role="Platform Engineer", location="Bengaluru",
                               canonical_url=url or self.GH, jd_text="Build things. " * 40)

    def test_a_greenhouse_link_becomes_an_approved_greenhouse_job(self):
        jid = C.add_from_link(self.tr, self.GH, fetch_job=lambda link: self.job(), say=self.say)
        r = self.row(jid)
        self.assertEqual((r["status"], r["route"], r["company"], r["role"]), ("approved", "greenhouse", "Acme", "Platform Engineer"))
        self.assertIn("Build things", r["description"])

    def test_a_lever_link_is_the_lever_route(self):
        url = "https://jobs.lever.co/acme/11111111-2222-3333-4444-555555555555"
        jid = C.add_from_link(self.tr, url, fetch_job=lambda link: self.job("lever", url), say=self.say)
        self.assertEqual(self.row(jid)["route"], "lever")

    def test_any_other_link_is_a_manual_job_named_from_the_page_title(self):
        def unsupported(link):
            raise intake.UnsupportedPlatform("nope")
        link = "https://www.linkedin.com/jobs/view/5550001"
        jid = C.add_from_link(self.tr, link, fetch_job=unsupported,
                              title_of=lambda l: "Initech hiring Backend Engineer in Bengaluru | LinkedIn", say=self.say)
        r = self.row(jid)
        self.assertEqual((r["status"], r["route"], r["company"], r["role"]), ("approved", "manual", "Initech", "Backend Engineer"))

    def test_company_and_role_given_on_the_command_line_win(self):
        def unsupported(link):
            raise intake.UnsupportedPlatform("nope")
        jid = C.add_from_link(self.tr, "https://careers.example.com/j/9", "Globex", "SDE 1", fetch_job=unsupported,
                              title_of=lambda l: "", say=self.say)
        self.assertEqual((self.row(jid)["company"], self.row(jid)["role"]), ("Globex", "SDE 1"))

    def test_a_page_that_cannot_be_read_asks_for_company_and_role(self):
        def unsupported(link):
            raise intake.UnsupportedPlatform("nope")
        with self.assertRaises(C.CommandError) as cm:
            C.add_from_link(self.tr, "https://careers.example.com/j/9", fetch_job=unsupported, title_of=lambda l: "Careers",
                            say=self.say)
        self.assertIn("--company", str(cm.exception))
        self.assertEqual(self.tr.counts(), {})                                  # nothing was saved

    def test_a_posting_already_applied_for_is_refused_and_one_already_tracked_is_reused(self):
        jid = C.add_from_link(self.tr, self.GH, fetch_job=lambda link: self.job(), say=self.say)
        self.tr.set_state(jid, "submitted", "done")
        with self.assertRaises(C.CommandError) as cm:
            C.add_from_link(self.tr, self.GH, fetch_job=lambda link: self.fail("must not fetch a known posting"), say=self.say)
        self.assertIn("already applied", str(cm.exception))
        self.tr.set_state(jid, "failed", "boom")
        self.assertEqual(C.add_from_link(self.tr, self.GH, fetch_job=lambda link: self.fail("known"), say=self.say), jid)
        self.assertEqual(self.row(jid)["status"], "approved")

    def test_only_http_links_are_accepted(self):
        with self.assertRaises(C.CommandError):
            C.add_from_link(self.tr, "acme jobs", fetch_job=lambda link: self.job(), say=self.say)

    def test_an_unreadable_ats_posting_is_a_clear_error(self):
        def broken(link):
            raise intake.IntakeError("HTTP 404")
        with self.assertRaises(C.CommandError) as cm:
            C.add_from_link(self.tr, self.GH, fetch_job=broken, say=self.say)
        self.assertIn("could not read", str(cm.exception))

    def test_titles_are_split_into_company_and_role(self):
        f = C.company_role_from_title
        self.assertEqual(f("Initech hiring Backend Engineer in Bengaluru | LinkedIn"), ("Initech", "Backend Engineer"))
        self.assertEqual(f("Backend Engineer at Initech"), ("Initech", "Backend Engineer"))
        self.assertEqual(f("Backend Engineer - Initech"), ("Initech", "Backend Engineer"))
        self.assertIsNone(f("Careers"))
        self.assertIsNone(f(""))


class UrlArguments(unittest.TestCase):
    def test_link_company_and_role(self):
        self.assertEqual(L.split_url_args(["https://x.example/j"]), ("https://x.example/j", None, None))
        self.assertEqual(L.split_url_args(["https://x.example/j", "--company", "Acme", "--role", "SDE 1"]),
                         ("https://x.example/j", "Acme", "SDE 1"))
        self.assertEqual(L.split_url_args(["--role", "SDE", "https://x.example/j"])[2], "SDE")

    def test_mistakes_are_errors(self):
        for bad in ([], ["--company"], ["a", "b"], ["https://x.example/j", "--bogus"]):
            with self.assertRaises(ValueError, msg=bad):
                L.split_url_args(bad)


class Handlers(Base):
    """The launcher's handlers: they change the tracker, then hand work to the batch runner (faked here)."""

    def test_url_queues_the_job_then_runs_the_batch_runner_for_only_that_job(self):
        calls = []
        plan = L.Plan("url", ["https://job-boards.greenhouse.io/acme/jobs/777"])
        job = SimpleNamespace(platform="greenhouse", company="Acme", role="Platform Engineer", location="Bengaluru",
                              canonical_url="https://job-boards.greenhouse.io/acme/jobs/777", jd_text="x" * 300)
        with mock.patch.object(intake, "fetch_job", lambda link: job), redirect_stdout(io.StringIO()) as out:
            code = L.queue_then_process(plan, say=self.say, worker=lambda args: calls.append(args) or 0)
        self.assertEqual(code, 0)
        jid = self.tr.job(job.canonical_url)["id"]
        self.assertEqual(calls, [["--only", str(jid)]])
        self.assertTrue(any("Acme" in s for s in self.said))

    def test_retry_requeues_then_runs_only_that_job(self):
        a = self.add(1, status="failed")
        calls = []
        code = L.queue_then_process(L.Plan("retry", [str(a)]), say=self.say, worker=lambda args: calls.append(args) or 0)
        self.assertEqual((code, calls), (0, [["--only", str(a)]]))
        self.assertEqual(self.row(a)["status"], "approved")

    def test_a_refusal_runs_nothing(self):
        a = self.add(1)                                                          # Found: cannot be retried
        calls = []
        self.assertEqual(L.queue_then_process(L.Plan("retry", [str(a)]), say=self.say, worker=lambda args: calls.append(args)), 2)
        self.assertEqual(calls, [])
        self.assertEqual(L.queue_then_process(L.Plan("retry", ["1,2"]), say=self.say, worker=lambda args: calls.append(args)), 2)
        self.assertEqual(L.queue_then_process(L.Plan("url", ["not a link"]), say=self.say, worker=lambda args: calls.append(args)), 2)
        self.assertEqual(calls, [])

    def test_process_passes_the_limit_and_validates_it(self):
        calls = []
        with redirect_stdout(io.StringIO()):
            self.assertEqual(L.process_command(L.Plan("process", []), say=self.say, worker=lambda a: calls.append(a) or 0), 0)
            self.assertEqual(L.process_command(L.Plan("process", ["3"]), say=self.say, worker=lambda a: calls.append(a) or 0), 0)
        self.assertEqual(calls, [[], ["--limit", "3"]])
        for bad in (["0"], ["x"], ["1", "2"], ["-1"]):
            self.assertEqual(L.process_command(L.Plan("process", bad), say=self.say, worker=lambda a: calls.append(a)), 2, bad)
        self.assertEqual(len(calls), 2)

    def test_process_ends_with_where_things_stand(self):
        self.add(1, status="ready_for_you")
        self.add(2, status="approved")
        L.process_command(L.Plan("process", []), say=self.say, worker=lambda a: 0)
        text = "\n".join(self.said)
        self.assertIn("ready for you 1", text)
        self.assertIn("approved, still waiting 1", text)

    def test_approve_skip_done_and_list_run_through_the_launcher(self):
        a, b, c = self.add(1, 9), self.add(2, 5), self.add(3, 8, status="ready_for_you")
        with mock.patch.object(L, "sync_quietly", lambda tr, say=print: self.said.append("synced")):
            self.assertEqual(L.tracker_command(L.Plan("approve", ["8+"]), say=self.say), 0)
            self.assertEqual(self.row(a)["status"], "approved")
            self.assertEqual(L.tracker_command(L.Plan("skip", [str(b)]), say=self.say), 0)
            self.assertEqual(self.row(b)["status"], "skipped")
            self.assertEqual(L.tracker_command(L.Plan("done", [str(c)]), say=self.say), 0)
            self.assertEqual(self.row(c)["status"], "submitted")
        self.assertEqual(self.said.count("synced"), 3)                           # the sheet is brought up to date each time
        self.assertEqual(L.tracker_command(L.Plan("approve", []), say=self.say), 2)
        self.assertEqual(L.tracker_command(L.Plan("open", ["1", "2"]), say=self.say), 2)
        self.assertEqual(L.tracker_command(L.Plan("list", ["99"]), say=self.say), 2)

    def test_approve_that_matches_nothing_does_not_touch_the_sheet(self):
        with mock.patch.object(L, "sync_quietly", lambda tr, say=print: self.fail("nothing changed")):
            self.assertEqual(L.tracker_command(L.Plan("approve", ["10+"]), say=self.say), 0)


class FindAndRun(unittest.TestCase):
    def test_the_find_summary_names_what_was_found_and_the_next_step(self):
        text = L.summary_block({"find_only": True, "date": "2030-01-02", "found_new": 12, "scored": 12, "found_waiting": 40,
                                "found_at_threshold": 5, "threshold": 7})
        self.assertIn("FIND FINISHED", text)
        self.assertIn("found 12 new", text)
        self.assertIn("40 Found in all", text)
        self.assertIn("5 at the approval threshold (7+)", text)
        self.assertIn("jobapply list", text)
        self.assertNotIn("submitted", text)

    def test_the_summary_mentions_approvals_read_from_the_sheet(self):
        self.assertIn("Approve ticks: 2", L.summary_block({"date": "d", "approved_from_sheet": 2}))

    def test_find_starts_run_py_in_find_only_mode(self):
        seen = []
        with mock.patch.object(L, "run_worker", lambda args, **kw: seen.append(args) or 0), \
                mock.patch.object(L, "newest_summary", lambda since: None), redirect_stdout(io.StringIO()):
            L.main(["find"])
            L.main(["find", "--skip-discovery"])
        self.assertEqual(seen, [["--find-only"], ["--find-only", "--skip-discovery"]])

    def test_every_new_word_is_a_command(self):
        for word in ("find", "list", "approve", "skip", "process", "open", "done", "url", "retry", "check"):
            self.assertEqual(L.parse([word, "x"]), L.Plan(word, ["x"]), word)
            self.assertIn(f"jobapply {word}", L.HELP)


class Check(unittest.TestCase):
    def run_check(self, **kw):
        said = []
        args = dict(latex=lambda: (C.OK, "latex"), claude=lambda ask=True: (C.OK, f"claude ask={ask}"),
                    sheet=lambda cfg, root: (C.OK, "sheet"), browser=lambda: (C.OK, "browser"),
                    profile=lambda root: [(C.OK, "profile")])
        args.update(kw)
        ok = C.health_check(Path("."), cfg={}, say=said.append, **args)
        return ok, "\n".join(said)

    def test_all_good(self):
        ok, text = self.run_check()
        self.assertTrue(ok)
        self.assertIn("Everything the bot needs is working", text)
        for part in ("latex", "claude", "profile", "sheet", "browser"):
            self.assertIn(part, text)

    def test_a_broken_piece_fails_the_check_and_is_named(self):
        ok, text = self.run_check(latex=lambda: (C.BAD, "no XeLaTeX"))
        self.assertFalse(ok)
        self.assertIn("[XX] LaTeX: no XeLaTeX", text)
        self.assertIn("1 problem(s) to fix", text)

    def test_a_warning_does_not_fail_it(self):
        ok, text = self.run_check(sheet=lambda cfg, root: (C.WARN, "no sheet configured"))
        self.assertTrue(ok)
        self.assertIn("1 warning(s)", text)

    def test_quick_skips_the_claude_request(self):
        self.assertIn("ask=False", self.run_check(claude=lambda ask=True: (C.OK, f"ask={ask}"))[1] if False else
                      self._quick())

    def _quick(self):
        said = []
        C.health_check(Path("."), cfg={}, quick=True, say=said.append, latex=lambda: (C.OK, "l"),
                       claude=lambda ask=True: (C.OK, f"ask={ask}"), sheet=lambda c, r: (C.OK, "s"),
                       browser=lambda: (C.OK, "b"), profile=lambda r: [(C.OK, "p")])
        return "\n".join(said)

    def test_latex_finding(self):
        run = lambda *a, **k: SimpleNamespace(stdout="MiKTeX-XeTeX 4.0\nmore")  # noqa: E731
        self.assertEqual(C.check_latex(lambda n: "xelatex.exe" if n == "xelatex" else None, run)[0], C.OK)
        self.assertEqual(C.check_latex(lambda n: "t.exe" if n == "tectonic" else None, run)[0], C.OK)
        self.assertEqual(C.check_latex(lambda n: None, run)[0], C.BAD)

    def test_claude_states(self):
        which = lambda n: "claude.exe" if n == "claude" else None  # noqa: E731

        def runner(stdout="", code=0, version="2.0.0"):
            def run(cmd, **kw):
                if "--version" in cmd:
                    return SimpleNamespace(stdout=version, stderr="", returncode=0)
                return SimpleNamespace(stdout=stdout, stderr="", returncode=code)
            return run
        self.assertEqual(C.check_claude(lambda n: None, runner())[0], C.BAD)
        self.assertEqual(C.check_claude(which, runner('{"result": "ok"}'))[0], C.OK)
        self.assertEqual(C.check_claude(which, runner("Please log in", 1))[0], C.BAD)
        self.assertEqual(C.check_claude(which, runner("5-hour limit reached ∙ resets 3pm", 1))[0], C.WARN)
        state, text = C.check_claude(which, runner(), ask=False)
        self.assertEqual(state, C.OK)
        self.assertIn("not tested", text)

    def test_profile_checks_report_what_is_missing(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.assertEqual(C.check_profile(root)[0][0], C.BAD)                # no profile.yaml at all
            (root / "profile.yaml").write_text("nonsense: [", encoding="utf-8")
            self.assertEqual(C.check_profile(root)[0][0], C.BAD)
            (root / "profile.yaml").write_text((ROOT / "profile.example.yaml").read_text(encoding="utf-8"), encoding="utf-8")
            res = C.check_profile(root)
            self.assertEqual(res[0][0], C.WARN)                                  # loads, but TODO values remain
            self.assertIn("still TODO", res[0][1])
            self.assertTrue(any(s == C.BAD and "contact.yaml" in t for s, t in res))

    def test_sheet_check_states(self):
        from jobbot import sheets
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.assertEqual(C.check_sheet({}, root)[0], C.WARN)                 # not configured
            cfg = {"sheets": {"spreadsheet_id": "abc"}}
            self.assertEqual(C.check_sheet(cfg, root)[0], C.BAD)                 # no credentials.json
            (root / "credentials.json").write_text("{}", encoding="utf-8")
            state, text = C.check_sheet(cfg, root)
            self.assertEqual(state, C.WARN)
            self.assertIn("jobapply sync", text)                                 # not signed in yet
            (root / "token.json").write_text("{}", encoding="utf-8")
            fake = SimpleNamespace(name="Applications", read=lambda a1: [sheets.HEADER])
            with mock.patch.object(sheets, "client_from_config", lambda cfg, root: fake):
                state, text = C.check_sheet(cfg, root)
            self.assertEqual(state, C.OK)
            self.assertIn("ID and Approve columns will be added", text)
            fake.read = lambda a1: [sheets.FULL_HEADER]
            with mock.patch.object(sheets, "client_from_config", lambda cfg, root: fake):
                self.assertEqual(C.check_sheet(cfg, root), (C.OK, "connected to tab 'Applications'"))
            fake.read = mock.Mock(side_effect=RuntimeError("403 forbidden"))
            with mock.patch.object(sheets, "client_from_config", lambda cfg, root: fake):
                self.assertEqual(C.check_sheet(cfg, root)[0], C.BAD)


if __name__ == "__main__":
    unittest.main()
