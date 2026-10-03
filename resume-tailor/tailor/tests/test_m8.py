"""M8: clear errors (usage limit, LaTeX, missing profile, unsupported site) and --resume-from."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import persona  # noqa: E402
import tailor  # noqa: E402
from jobbot import answers as A  # noqa: E402
from jobbot import coverletter as C  # noqa: E402
from jobbot import intake  # noqa: E402
from jobbot.fields import Field  # noqa: E402
from jobbot.intake import FieldSpec, Question  # noqa: E402
from test_coverletter import CFG, GOOD, JD, QUOTES, RESUME  # noqa: E402

CANONICAL = "https://jobs.lever.co/acme/1"
LIMIT_TEXT = "Claude AI usage limit reached|1760000000"


def completed(stdout="", stderr="", code=0):
    return subprocess.CompletedProcess(["claude"], code, stdout, stderr)


class UsageLimit(unittest.TestCase):
    def call(self, proc):
        with mock.patch.object(tailor.shutil, "which", return_value="claude"), \
                mock.patch.object(tailor.subprocess, "run", return_value=proc):
            return tailor.call_claude("prompt")

    def test_a_json_error_that_mentions_the_limit_is_a_usage_limit_error(self):
        with self.assertRaises(tailor.UsageLimitError) as cm:
            self.call(completed(json.dumps({"is_error": True, "result": LIMIT_TEXT})))
        self.assertIn("usage limit has been reached", str(cm.exception))
        self.assertIn("wait for your Pro limit to reset", str(cm.exception))

    def test_non_json_output_that_mentions_the_limit_is_one_too(self):
        with self.assertRaises(tailor.UsageLimitError):
            self.call(completed("", "Error: you have hit your usage limit. It resets at 5pm.", 1))

    def test_other_errors_stay_ordinary_tailor_errors(self):
        with self.assertRaises(tailor.TailorError) as cm:
            self.call(completed(json.dumps({"is_error": True, "result": "something else broke"})))
        self.assertNotIsInstance(cm.exception, tailor.UsageLimitError)

    def test_it_is_a_tailor_error_so_the_old_handlers_still_catch_it(self):
        self.assertTrue(issubclass(tailor.UsageLimitError, tailor.TailorError))

    def test_tailor_job_names_the_folder_and_keeps_the_jd_so_nothing_is_lost(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            with mock.patch.object(tailor, "load_resume_data", lambda today=None: persona.resume()), \
                    mock.patch.object(tailor, "OUTPUT_DIR", tmp / "output"), \
                    mock.patch.object(tailor, "call_claude", side_effect=tailor.UsageLimitError("limit")):
                with self.assertRaises(tailor.UsageLimitError) as cm:
                    tailor.tailor_job("Acme", "Backend Engineer", "Build APIs. " * 40, CANONICAL)
            folder = cm.exception.job_dir
            self.assertEqual(folder.parent, tmp / "output")
            self.assertTrue((folder / "jd.txt").exists())
            self.assertEqual(json.loads((folder / "job_info.json").read_text(encoding="utf-8")),
                             {"company": "Acme", "role": "Backend Engineer", "url": CANONICAL})

    def test_a_usage_limit_stops_a_cover_letter_and_llm_answers_instead_of_becoming_empty_fields(self):
        def limit(prompt):
            raise tailor.UsageLimitError("limit")
        with self.assertRaises(tailor.UsageLimitError):
            C.generate("Acme", "Backend", JD, RESUME, CFG, ["Hi,\n\nHello."], limit)
        ctx = A.Context(profile=persona.profile(), resume=RESUME, llm=limit)
        with self.assertRaises(tailor.UsageLimitError):
            A.apply_llm([Field("q1", "Why us?", "textarea", True)], ctx)
        broken = lambda prompt: (_ for _ in ()).throw(RuntimeError("boom"))           # noqa: E731
        res = C.generate("Acme", "Backend", JD, RESUME, CFG, ["Hi,\n\nHello."], broken)
        self.assertFalse(res.ok)                                                      # other failures still just fail

    def test_the_command_line_prints_the_wait_message_and_the_resume_command(self):
        import apply
        err = tailor.UsageLimitError("Claude Code's usage limit has been reached.")
        err.job_dir = Path("output/2026-10-03_Acme_Backend")
        with mock.patch.object(apply, "run", side_effect=err), \
                mock.patch.object(sys, "argv", ["apply.py", "--url", CANONICAL]):
            with self.assertRaises(SystemExit) as cm:
                apply.main()
        msg = str(cm.exception)
        self.assertIn("usage limit", msg)
        self.assertIn("--resume-from", msg)
        self.assertIn("2026-10-03_Acme_Backend", msg)


class LatexErrors(unittest.TestCase):
    LOG = ("This is XeTeX\n(./resume.tex\n! Undefined control sequence.\nl.45 \\foo\n"
           "                bar baz\n? \n")

    def test_the_first_error_and_its_line_are_summarised(self):
        s = tailor.latex_error_summary(self.LOG)
        self.assertIn("! Undefined control sequence.", s)
        self.assertIn("line 45", s)
        self.assertIn("\\foo", s)
        self.assertEqual(tailor.latex_error_summary("no bang here at all"), "no bang here at all")
        self.assertEqual(tailor.latex_error_summary(""), "no error text was written")

    def test_a_failed_compile_gives_the_summary_the_log_path_and_a_hint_not_the_whole_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            build = Path(tmp) / "build"

            def fake_run(cmd, cwd=None, **kw):
                (Path(cwd) / "resume.log").write_text(self.LOG + "x" * 5000, encoding="utf-8")
                return completed("", "", 1)
            with mock.patch.object(tailor.shutil, "which", return_value="xelatex"), \
                    mock.patch.object(tailor.subprocess, "run", side_effect=fake_run):
                with self.assertRaises(tailor.TailorError) as cm:
                    tailor.compile_pdf("\\documentclass{article}", build)
        msg = str(cm.exception)
        self.assertIn("! Undefined control sequence.", msg)
        self.assertIn("resume.log", msg)
        self.assertLess(len(msg), 700)

    def test_a_missing_latex_engine_says_what_to_install(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(tailor.shutil, "which", return_value=None):
            with self.assertRaises(tailor.TailorError) as cm:
                tailor.compile_pdf("x", Path(tmp) / "b")
        self.assertIn("MiKTeX", str(cm.exception))


class FriendlyStops(unittest.TestCase):
    def run_with(self, profile_text=None, job=None):
        import apply
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            prof = tmp / "profile.yaml"
            if profile_text is not None:
                prof.write_text(profile_text, encoding="utf-8")
            with mock.patch.object(apply, "PROFILE_FILE", prof), \
                    mock.patch.object(apply, "CONFIG_FILE", tmp / "no-config.yaml"), \
                    mock.patch.object(tailor, "OUTPUT_DIR", tmp / "output"), \
                    mock.patch.object(tailor, "load_resume_data", lambda today=None: persona.resume()), \
                    mock.patch.object(intake, "fetch_job", lambda url: job):
                with self.assertRaises(SystemExit) as cm:
                    apply.run(CANONICAL)
        return str(cm.exception)

    def test_a_missing_profile_says_where_to_start(self):
        msg = self.run_with(None)
        self.assertIn("profile.yaml not found", msg)
        self.assertIn("profile.example.yaml", msg)

    def test_a_broken_profile_lists_what_to_fix(self):
        msg = self.run_with("hello: world\n")
        self.assertIn("profile.yaml has problems to fix first", msg)

    def test_todo_values_are_named(self):
        profile = persona.profile()
        profile["personal"]["first_name"] = "TODO"
        msg = self.run_with(yaml.safe_dump(profile, allow_unicode=True))
        self.assertIn("still has TODO in", msg)
        self.assertIn("first_name", msg)

    def test_an_unsupported_site_names_what_is_supported_and_what_is_not(self):
        with self.assertRaises(intake.UnsupportedPlatform) as cm:
            intake.fetch_job("https://www.linkedin.com/jobs/view/123", http=lambda u: (200, "<html></html>"))
        msg = str(cm.exception)
        self.assertIn("Greenhouse or Lever", msg)
        self.assertIn("LinkedIn, Naukri and Indeed are not supported yet", msg)

    def test_a_platform_without_an_adapter_is_a_plain_message_not_a_traceback(self):
        job = intake.Job(platform="workday", company="Acme", role="Engineer", location="", canonical_url=CANONICAL,
                         apply_url=CANONICAL, jd_text="x" * 300, questions=[], job_id="1", board="acme", source="api",
                         captcha_possible=False)
        msg = self.run_with(yaml.safe_dump(persona.profile(), allow_unicode=True), job)
        self.assertIn("Only Greenhouse and Lever", msg)


def questions():
    return [Question("Full name", True, [FieldSpec("name", "text")]), Question("Email", True, [FieldSpec("email", "email")]),
            Question("Cover letter", False, [FieldSpec("cards[1][cover]", "textarea")])]


class ResumeFrom(unittest.TestCase):
    def go(self, *, with_pdf=True, saved_letter=True, folder_name="2026-10-03_Acme_Backend_Engineer"):
        import apply
        job = intake.Job(platform="lever", company="Acme", role="Backend Engineer", location="Bengaluru",
                         canonical_url=CANONICAL, apply_url=CANONICAL + "/apply", jd_text=JD, questions=questions(),
                         job_id="1", board="acme", source="api", captcha_possible=True)
        calls = {"tailor": [], "llm": 0}

        def fake_tailor(company, role, jd, url, **kw):
            calls["tailor"].append(kw)
            pdf = Path(kw["job_dir"]) / tailor.PDF_NAME
            pdf.write_bytes(b"%PDF-1.4 new")
            return {"job_dir": str(kw["job_dir"]), "resume_pdf": str(pdf)}

        def llm(prompt):
            calls["llm"] += 1
            return {"letter": GOOD, "jd_quotes": QUOTES, "answers": {}}

        with tempfile.TemporaryDirectory(prefix="resume from ") as tmp:
            tmp = Path(tmp)
            out = tmp / "output"
            folder = out / folder_name
            folder.mkdir(parents=True)
            (folder / "job_info.json").write_text(json.dumps({"company": "Acme", "role": "Backend Engineer",
                                                              "url": CANONICAL}), encoding="utf-8")
            if with_pdf:
                (folder / tailor.PDF_NAME).write_bytes(b"%PDF-1.4 old")
            if saved_letter:
                (folder / "cover_letter.txt").write_text(C.full_letter(RESUME, GOOD) + "\n", encoding="utf-8")
            prof = tmp / "profile.yaml"
            profile = persona.profile()
            prof.write_text(yaml.safe_dump(profile, allow_unicode=True), encoding="utf-8")
            with mock.patch.object(apply, "PROFILE_FILE", prof), mock.patch.object(apply, "CONFIG_FILE", tmp / "nc.yaml"), \
                    mock.patch.object(tailor, "OUTPUT_DIR", out), \
                    mock.patch.object(tailor, "load_resume_data", lambda today=None: persona.resume()), \
                    mock.patch.object(tailor, "tailor_job", fake_tailor), mock.patch.object(tailor, "call_claude", llm), \
                    mock.patch.object(intake, "fetch_job", lambda url: job), \
                    mock.patch("jobbot.browser.BrowserSession", side_effect=AssertionError("no browser in prepare mode")):
                result = apply.run(CANONICAL, mode="prepare", resume_from=folder, input_fn=lambda p: "no",
                                   opener=lambda u: True)
            answers = {a["label"]: a for a in result["answers"]}
            files = sorted(p.name for p in folder.iterdir())
            pdf_bytes = (folder / tailor.PDF_NAME).read_bytes() if (folder / tailor.PDF_NAME).exists() else b""
        return result, answers, calls, files, pdf_bytes

    def test_an_existing_resume_and_letter_are_reused_not_remade(self):
        result, answers, calls, files, pdf = self.go()
        self.assertEqual(calls["tailor"], [])                        # no tailoring
        self.assertEqual(pdf, b"%PDF-1.4 old")
        self.assertEqual(answers["Cover letter"]["value"], C.full_letter(RESUME, GOOD))
        self.assertEqual(calls["llm"], 0)                            # no cover letter, no LLM answers
        self.assertIn("apply_result.json", files)                    # the result is written into that same folder
        self.assertEqual(answers["Cover letter"]["status"], "filled")

    def test_a_folder_without_a_resume_is_tailored_into_that_same_folder(self):
        result, answers, calls, files, pdf = self.go(with_pdf=False)
        self.assertEqual(len(calls["tailor"]), 1)
        self.assertEqual(Path(calls["tailor"][0]["job_dir"]).name, "2026-10-03_Acme_Backend_Engineer")
        self.assertEqual(pdf, b"%PDF-1.4 new")

    def test_url_of_folder(self):
        import apply
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            self.assertIsNone(apply.url_of_folder(d))
            (d / "apply_result.json").write_text(json.dumps({"url": "https://a.example/b"}), encoding="utf-8")
            self.assertEqual(apply.url_of_folder(d), "https://a.example/b")
            (d / "job_info.json").write_text(json.dumps({"url": CANONICAL}), encoding="utf-8")
            self.assertEqual(apply.url_of_folder(d), CANONICAL)               # tailoring's record comes first

    def test_resume_from_needs_no_url_on_the_command_line(self):
        import apply
        seen = {}
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "job_info.json").write_text(json.dumps({"url": CANONICAL}), encoding="utf-8")
            with mock.patch.object(apply, "run", lambda url, **kw: seen.update(url=url, **kw)), \
                    mock.patch.object(sys, "argv", ["apply.py", "--resume-from", str(d), "--mode", "prepare"]):
                apply.main()
            self.assertEqual((seen["url"], seen["resume_from"], seen["mode"]), (CANONICAL, d, "prepare"))

    def test_a_folder_that_does_not_exist_is_a_plain_message(self):
        import apply
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            prof = tmp / "profile.yaml"
            prof.write_text(yaml.safe_dump(persona.profile(), allow_unicode=True), encoding="utf-8")
            job = intake.Job(platform="lever", company="Acme", role="Backend Engineer", location="", canonical_url=CANONICAL,
                             apply_url=CANONICAL, jd_text=JD, questions=questions(), job_id="1", board="acme",
                             source="api", captcha_possible=False)
            with mock.patch.object(apply, "PROFILE_FILE", prof), mock.patch.object(apply, "CONFIG_FILE", tmp / "nc.yaml"), \
                    mock.patch.object(tailor, "OUTPUT_DIR", tmp / "out"), \
                    mock.patch.object(tailor, "load_resume_data", lambda today=None: persona.resume()), \
                    mock.patch.object(intake, "fetch_job", lambda url: job):
                with self.assertRaises(SystemExit) as cm:
                    apply.run(CANONICAL, mode="prepare", resume_from=tmp / "nope")
        self.assertIn("is not a folder", str(cm.exception))


class SavedLetterReuse(unittest.TestCase):
    def provider(self, tmp, letter_file_text, llm, ended=None):
        job_dir = Path(tmp) / "job"
        job_dir.mkdir()
        if letter_file_text is not None:
            (job_dir / "cover_letter.txt").write_text(letter_file_text, encoding="utf-8")
        return C.make_provider(job_dir, "Acme", "Backend", JD, RESUME, llm, cfg=CFG, samples=["Hi,\n\nHello."],
                               history_path=Path(tmp) / "h.json", model="", ended=ended), Path(tmp) / "h.json"

    def field(self):
        return Field("cl", "Cover Letter", "textarea", False)

    def test_a_saved_valid_letter_is_reused_without_the_llm_or_the_closings_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            prov, hist = self.provider(tmp, C.full_letter(RESUME, GOOD) + "\n", lambda p: calls.append(p) or {})
            value, note = prov(self.field())
            self.assertEqual(value, C.full_letter(RESUME, GOOD))
            self.assertEqual(calls, [])
            self.assertFalse(hist.exists())

    def test_a_malformed_or_failing_saved_letter_is_regenerated(self):
        for saved in ("not a letter at all", C.full_letter(RESUME, GOOD).replace("Hello,", "Dear team,"),
                      C.full_letter(RESUME, GOOD.replace("Backend Engineer.", "Backend Engineer!!! \"quoted\" words."))):
            with tempfile.TemporaryDirectory() as tmp:
                calls = []
                prov, _ = self.provider(tmp, saved, lambda p: calls.append(p) or {"letter": GOOD, "jd_quotes": QUOTES})
                value, note = prov(self.field())
                self.assertEqual(len(calls), 1, saved[:40])
                self.assertIsNotNone(value, note)

    def test_after_the_job_ends_a_saved_present_tense_letter_is_not_reused(self):
        from datetime import date
        present = GOOD.replace("At Acme Test Co I built", "At Acme Test Co I currently build")
        ended = ("Acme Test Co", date(2031, 3, 5))
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            prov, _ = self.provider(tmp, C.full_letter(RESUME, present), lambda p: calls.append(p) or
                                    {"letter": GOOD, "jd_quotes": QUOTES}, ended=ended)
            prov(self.field())
            self.assertEqual(len(calls), 1)
        with tempfile.TemporaryDirectory() as tmp:
            calls = []
            prov, _ = self.provider(tmp, C.full_letter(RESUME, present), lambda p: calls.append(p) or {}, ended=None)
            prov(self.field())
            self.assertEqual(calls, [])                                       # before the end it is still fine


if __name__ == "__main__":
    unittest.main()
