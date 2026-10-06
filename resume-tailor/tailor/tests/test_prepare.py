"""Prepare mode: answers + a copy-ready sheet, the job opened in the DEFAULT browser, a manual-submission record.
No automation browser is started in any of these tests; nothing leaves the machine."""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("JOBBOT_NO_SHEET", "1")      # a test must never reach the real Google Sheet
sys.path.insert(0, str(ROOT / "tests"))

import persona  # noqa: E402
from jobbot import config as cfgmod  # noqa: E402
from jobbot import intake, prepare  # noqa: E402
from jobbot.intake import FieldSpec, Question  # noqa: E402

CANONICAL = "https://jobs.lever.co/acme/1"


def questions():
    q = lambda label, name, typ="text", req=True: Question(label, req, [FieldSpec(name, typ)])  # noqa: E731
    return [q("Resume/CV", "resume", "file", False), q("Full name", "name"), q("Email", "email", "email"),
            q("Phone", "phone"), q("Current company", "org"), q("Why do you want to work here?", "cards[1][f1]", "textarea")]


class Config(unittest.TestCase):
    def test_lever_defaults_to_prepare_and_nothing_else_does(self):
        cfg = cfgmod.load_config(Path("does-not-exist.yaml"))
        self.assertEqual(cfgmod.requested_mode(cfg, "lever"), "prepare")
        for platform in ("greenhouse", "linkedin", "anything"):
            self.assertEqual(cfgmod.requested_mode(cfg, platform), "dry-run", platform)
        self.assertEqual(cfgmod.requested_mode(cfg, "lever", cli_mode="dry-run"), "dry-run")
        self.assertEqual(cfgmod.requested_mode(cfg, "lever", dry_run_flag=True), "dry-run")

    def test_prepare_is_a_valid_config_mode_and_the_example_uses_it_for_lever(self):
        cfg = cfgmod.load_config(ROOT / "config.example.yaml")
        self.assertEqual(cfg["platforms"]["lever"]["mode"], "prepare")

    def test_prepare_needs_no_approval_because_it_never_submits(self):
        import apply
        self.assertEqual(apply.decide_mode("prepare", CANONICAL, None), ("prepare", ""))


class Prepare(unittest.TestCase):
    def go(self, replies=("yes",), qs=None, opener=None, pre=None, post=None, reapply=False, interactive=True):
        import apply
        import tailor
        job = intake.Job(platform="lever", company="Acme", role="Backend Engineer", location="Bengaluru",
                         canonical_url=CANONICAL, apply_url=CANONICAL + "/apply", jd_text="Build Python APIs. " * 20,
                         questions=questions() if qs is None else qs, job_id="1", board="acme", source="api",
                         captcha_possible=True)
        opened, asked = [], []
        replies = list(replies)

        def input_fn(prompt):
            asked.append(prompt)
            if not replies:
                raise EOFError
            return replies.pop(0)

        with tempfile.TemporaryDirectory(prefix="prepare test ") as tmp:
            tmp = Path(tmp)
            prof = tmp / "profile.yaml"
            prof.write_text(yaml.safe_dump(persona.profile(), allow_unicode=True), encoding="utf-8")
            pdf = tmp / "resume.pdf"
            pdf.write_bytes(b"%PDF-1.4\n%fake\n")
            with mock.patch.object(apply, "PROFILE_FILE", prof), \
                    mock.patch.object(apply, "CONFIG_FILE", tmp / "no-config.yaml"), \
                    mock.patch.object(tailor, "load_resume_data", lambda: persona.resume()), \
                    mock.patch.object(tailor, "OUTPUT_DIR", tmp / "output"), \
                    mock.patch.object(tailor, "call_claude", lambda p: {"answers": {}}), \
                    mock.patch.object(intake, "fetch_job", lambda url: job), \
                    mock.patch("jobbot.browser.BrowserSession", side_effect=AssertionError("automation browser used")):
                if pre:
                    pre(tmp / "output")
                try:
                    out = apply.run(CANONICAL, resume_pdf=pdf, input_fn=input_fn, reapply=reapply,
                                    prepare_interactive=interactive,
                                    opener=opener or (lambda u: opened.append(u) or True))
                finally:
                    if post:
                        post(tmp / "output")
            job_dir = tmp / "output" / "dryrun_Acme_Backend_Engineer"
            files = {p.name: p.read_text(encoding="utf-8") for p in job_dir.iterdir() if p.is_file() and p.suffix != ".pdf"}
            result = json.loads(files["apply_result.json"])
            return out, result, files, opened, asked, str(pdf)

    def test_lever_runs_prepare_by_default_and_opens_the_job_and_the_sheet_in_the_default_browser(self):
        out, result, files, opened, asked, pdf = self.go()
        self.assertEqual(result["mode"], "prepare")
        self.assertEqual(opened[0], CANONICAL + "/apply")
        self.assertTrue(opened[1].startswith("file:") and opened[1].endswith("prepare_sheet.html"))
        self.assertEqual(len(asked), 1)

    def test_confirming_records_a_manual_submission(self):
        out, result, files, *_ = self.go(replies=["yes"])
        self.assertEqual((result["status"], result["submitted_by"], result["confirmation"]), ("submitted", "manual", True))
        self.assertEqual(json.loads(files["answers.json"])["outcome"]["status"], "submitted")

    def test_saying_no_or_garbage_then_no_records_nothing_as_submitted(self):
        out, result, files, opened, asked, pdf = self.go(replies=["maybe", "no"])
        self.assertEqual(result["status"], "prepared")
        self.assertNotEqual(result.get("confirmation"), True)
        self.assertEqual(len(asked), 2)

    def test_without_a_terminal_it_stays_prepared_and_says_how_to_record_it_later(self):
        import apply
        out, result, files, *_ = self.go(replies=[])
        self.assertEqual(result["status"], "prepared")
        self.assertIn("--mark-submitted", result["note"])
        import tailor
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(tailor, "OUTPUT_DIR", Path(tmp) / "output"):
            (Path(tmp) / "apply_result.json").write_text(json.dumps(result), encoding="utf-8")
            marked = apply.mark_submitted(Path(tmp))
            self.assertEqual((marked["status"], marked["submitted_by"]), ("submitted", "manual"))
            (Path(tmp) / "apply_result.json").write_text(json.dumps({"mode": "dry-run"}), encoding="utf-8")
            with self.assertRaises(SystemExit):
                apply.mark_submitted(Path(tmp))

    def test_the_sheet_lists_every_answer_in_form_order_with_the_pdf_path(self):
        out, result, files, opened, asked, pdf = self.go()
        text = files["prepare_sheet.txt"]
        labels = [q.label for q in questions()]
        positions = [text.index(l) for l in labels]
        self.assertEqual(positions, sorted(positions))
        self.assertIn(pdf, text)
        self.assertIn(persona.profile()["personal"]["email"], text)
        page = files["prepare_sheet.html"]
        self.assertIn(pdf.replace("&", "&amp;"), page)
        self.assertIn("data-copy", page)

    def test_what_the_bot_cannot_answer_is_marked_as_yours(self):
        out, result, files, *_ = self.go()
        flagged = [a for a in out["answers"] if a["status"] == "flagged"]
        self.assertTrue(flagged)
        self.assertIn(prepare.FLAGGED_TEXT, files["prepare_sheet.txt"])

    def test_html_in_an_answer_is_escaped_in_the_sheet(self):
        from jobbot import answers as A
        job = mock.Mock(company="A<b>", role="R", location="L", apply_url="u")
        a = A.Answer("k", "Q", "text", True, "x", "<script>alert(1)</script>", "t", "high", A.FILLED)
        page = prepare.sheet_html([a], Path("x.pdf"), job)
        self.assertNotIn("<script>alert(1)", page)
        self.assertIn("&lt;script&gt;", page)

    def test_a_posting_with_no_readable_questions_is_refused_clearly(self):
        with self.assertRaises(SystemExit) as cm:
            self.go(qs=[Question("x", False, [FieldSpec("h", "hidden")])])
        self.assertIn("nothing to prepare", str(cm.exception))

    def test_the_run_is_tracked_and_a_second_real_run_of_a_submitted_posting_is_refused(self):
        from jobbot import tracker
        seen = {}

        def post(output):
            with tracker.Tracker(output / "tracker.sqlite3") as t:
                seen["row"] = dict(t.job(CANONICAL))

        self.go(post=post)
        self.assertEqual((seen["row"]["status"], seen["row"]["submitted_by"], seen["row"]["company"]),
                         ("submitted", "manual", "Acme"))

        def pre(output):                                    # the posting is already submitted in the tracker
            import json
            d = output / "earlier"
            d.mkdir(parents=True)
            (d / "apply_result.json").write_text(json.dumps({"status": "submitted", "mode": "prepare", "url": CANONICAL,
                                                             "company": "Acme", "role": "Backend Engineer"}), encoding="utf-8")
            with tracker.Tracker(output / "tracker.sqlite3") as t:
                t.record_folder(d)
        with self.assertRaises(SystemExit) as cm:
            self.go(pre=pre)
        self.assertIn("Already submitted", str(cm.exception))
        self.assertIn("--reapply", str(cm.exception))
        out, result, *_ = self.go(pre=pre, reapply=True)           # explicit override
        self.assertEqual(result["status"], "submitted")

    def test_the_batch_variant_opens_nothing_asks_nothing_and_leaves_the_job_ready_for_you(self):
        from jobbot import tracker
        seen = {}

        def post(output):
            with tracker.Tracker(output / "tracker.sqlite3") as t:
                seen["row"] = dict(t.job(CANONICAL))
        out, result, files, opened, asked, pdf = self.go(interactive=False, post=post)
        self.assertEqual((opened, asked), ([], []))
        self.assertEqual(result["status"], "prepared")
        self.assertNotEqual(result.get("confirmation"), True)
        self.assertIn("prepare_sheet.html", files)
        self.assertEqual(seen["row"]["status"], "ready_for_you")
        self.assertIn("Mark applied", seen["row"]["reason"])

    def test_when_the_real_form_cannot_be_read_the_batch_variant_prepares_the_usual_questions(self):
        out, result, files, opened, asked, pdf = self.go(interactive=False, qs=[])
        self.assertEqual(result["status"], "prepared")
        self.assertIn("LIKELY ANSWERS", files["prepare_sheet.txt"])
        self.assertIn("Current CTC", files["prepare_sheet.txt"])
        with self.assertRaises(SystemExit):                                       # the interactive run still refuses
            self.go(qs=[])

    def test_if_no_browser_can_be_opened_the_run_still_finishes(self):
        out, result, *_ = self.go(opener=lambda u: False)
        self.assertEqual(result["status"], "submitted")


if __name__ == "__main__":
    unittest.main()
