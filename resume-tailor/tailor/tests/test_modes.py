"""M5 modes: dry-run / assist / auto, and the rule that a real submission needs --allow-submit for THIS job.
Headless Chromium + a local server standing in for the employer + a scripted "user". Nothing leaves the machine."""
import http.server
import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as _p:
        _p.chromium.launch(headless=True).close()
    HAVE_CHROMIUM = True
except Exception:
    HAVE_CHROMIUM = False

import persona  # noqa: E402
from jobbot import config as cfgmod  # noqa: E402
from jobbot.ats.base import ATS  # noqa: E402

BASE_FIELDS = """
<li class="application-question"><div class="application-label">Resume/CV</div><div><input type="file" name="resume"></div></li>
<li class="application-question"><div class="application-label">Full name <span>✱</span></div><div><input type="text" name="name"></div></li>
<li class="application-question"><div class="application-label">Email <span>✱</span></div><div><input type="email" name="email"></div></li>
<li class="application-question"><div class="application-label">Phone <span>✱</span></div><div><input type="text" name="phone"></div></li>
<li class="application-question"><div class="application-label">Current company <span>✱</span></div><div><input type="text" name="org"></div></li>
"""
EXTRA_REQUIRED = """<li class="application-question"><div class="application-label">Why do you want to work here? <span>✱</span></div>
<div><textarea name="cards[1][f1]"></textarea></div></li>"""
CAPTCHA = ('<iframe src="https://newassets.hcaptcha.com/captcha/v1/x/static/hcaptcha.html" title="hCaptcha challenge" '
           'style="width:400px;height:500px"></iframe>')


def page(extra="", head=""):
    return (f"<html><head><title>Job</title>{head}</head><body><form id='application-form' method='POST' action='/submit'>"
            f"<ul>{BASE_FIELDS}{extra}</ul><button type='submit'>Submit application</button></form></body></html>")


PAGES = {"/form": page(), "/form-needs-you": page(EXTRA_REQUIRED), "/form-captcha": page(body_extra := CAPTCHA) if False else page(CAPTCHA)}


class Server(http.server.BaseHTTPRequestHandler):
    seen = []
    submit_response = "<html><body><h1>Thank you for applying!</h1><p>Your application has been submitted.</p></body></html>"

    def _send(self, body, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def do_GET(self):
        Server.seen.append(("GET", self.path))
        self._send(PAGES.get(self.path.split("?")[0], "<html>404</html>"), 200 if self.path.split("?")[0] in PAGES else 404)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        self.rfile.read(n)
        Server.seen.append(("POST", self.path))
        self._send(Server.submit_response)

    def log_message(self, *a):
        pass


def apply_mod():
    import apply
    return apply


class Config(unittest.TestCase):
    def test_defaults_are_safe(self):
        cfg = cfgmod.load_config(Path("does-not-exist.yaml"))
        self.assertEqual(cfg["default_mode"], "dry-run")
        for platform in ("greenhouse", "linkedin", "anything"):      # Lever's prepare default: see test_prepare.py
            self.assertEqual(cfgmod.requested_mode(cfg, platform), "dry-run", platform)

    def test_precedence(self):
        cfg = {"default_mode": "dry-run", "platforms": {"lever": {"mode": "auto"}}}
        self.assertEqual(cfgmod.requested_mode(cfg, "lever"), "auto")
        self.assertEqual(cfgmod.requested_mode(cfg, "lever", cli_mode="assist"), "assist")
        self.assertEqual(cfgmod.requested_mode(cfg, "lever", cli_mode="assist", dry_run_flag=True), "dry-run")
        self.assertEqual(cfgmod.requested_mode(cfg, "workday"), "dry-run")                  # unknown platform: default

    def test_file_is_merged_and_validated(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "config.yaml"
            f.write_text("default_mode: dry-run\nplatforms:\n  lever: {mode: auto}\n  greenhouse: {mode: dry-run}\n", encoding="utf-8")
            cfg = cfgmod.load_config(f)
            self.assertEqual((cfg["platforms"]["lever"]["mode"], cfg["platforms"]["greenhouse"]["mode"]), ("auto", "dry-run"))
            f.write_text("platforms:\n  lever: {mode: yolo}\n", encoding="utf-8")
            with self.assertRaises(cfgmod.ConfigError):
                cfgmod.load_config(f)
            f.write_text("default_mode: submit-everything\n", encoding="utf-8")
            with self.assertRaises(cfgmod.ConfigError):
                cfgmod.load_config(f)

    def test_the_committed_example_is_valid(self):
        cfg = cfgmod.load_config(ROOT / "config.example.yaml")
        self.assertEqual(cfg["default_mode"], "dry-run")


class DecideMode(unittest.TestCase):
    URL = "https://jobs.lever.co/acme/0a1b2c3d-1111"

    def decide(self, requested, allow):
        import apply
        return apply.decide_mode(requested, self.URL, allow)

    def test_dry_run_is_always_allowed(self):
        self.assertEqual(self.decide("dry-run", None), ("dry-run", ""))

    def test_real_modes_need_the_matching_url(self):
        for mode in ("assist", "auto"):
            used, note = self.decide(mode, None)
            self.assertEqual(used, "dry-run")
            self.assertIn("--allow-submit", note)
            self.assertIn(self.URL, note)
            self.assertEqual(self.decide(mode, "https://jobs.lever.co/other/9999")[0], "dry-run")
            for ok in (self.URL, self.URL + "/", self.URL + "/apply", self.URL.upper(), self.URL + "/apply?lever-source=x"):
                self.assertEqual(self.decide(mode, ok)[0], mode, ok)

    def test_a_different_job_on_the_same_board_does_not_match(self):
        self.assertEqual(self.decide("auto", "https://jobs.lever.co/acme/0a1b2c3d-2222")[0], "dry-run")


@unittest.skipUnless(HAVE_CHROMIUM, "Playwright Chromium not installed")
class Runs(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Server)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        Server.seen.clear()
        Server.submit_response = ("<html><body><h1>Thank you for applying!</h1><p>Your application has been "
                                  "submitted.</p></body></html>")
        self.pauses = []          # the messages the user was shown while the run was paused

    def go(self, path="/form", *, mode=None, allow="auto-url", user=None, dry_run=False, llm=None, headless_flag=False,
           interactive=None, poll_hook=None, unattended=False):
        """Run apply.run against the local employer. `user(session)` is what the human does during a pause."""
        import apply
        import tailor
        from jobbot import browser, intake
        from jobbot.browser import BrowserSession

        with tempfile.TemporaryDirectory(prefix="modes test ") as tmp:
            tmp = Path(tmp)
            prof = tmp / "profile.yaml"
            prof.write_text(yaml.safe_dump(persona.profile(), allow_unicode=True), encoding="utf-8")
            pdf = tmp / "resume.pdf"
            pdf.write_bytes(b"%PDF-1.4\n%fake\n")
            canonical = "https://jobs.lever.co/acme/1"
            job = intake.Job(platform="lever", company="Acme", role="Backend Engineer", location="Bengaluru",
                             canonical_url=canonical, apply_url=self.base + path, jd_text="Build Python APIs. " * 20,
                             questions=[], job_id="1", board="acme", source="api", captcha_possible=True)
            holder = {}

            shown = []

            def input_fn(prompt):
                self.pauses.append(" ".join(shown))
                shown.clear()
                if user:
                    user(holder["s"])
                return ""

            def factory(job_dir):
                holder["s"] = BrowserSession(job_dir, headless=True, input_fn=input_fn, interactive=interactive,
                                             poll_hook=poll_hook, output_fn=lambda m="": shown.append(str(m)))
                return holder["s"]

            with mock.patch.object(apply, "PROFILE_FILE", prof), \
                    mock.patch.object(apply, "CONFIG_FILE", tmp / "no-config.yaml"), \
                    mock.patch.object(tailor, "load_resume_data", lambda: persona.resume()), \
                    mock.patch.object(tailor, "OUTPUT_DIR", tmp / "output"), \
                    mock.patch.object(tailor, "call_claude", llm or (lambda p: {"answers": {}})), \
                    mock.patch.object(intake, "fetch_job", lambda url: job), \
                    mock.patch.object(browser, "DEFAULT_PROFILE_DIR", tmp / "browser profile"), \
                    mock.patch.object(apply, "RESULT_WAIT_S", 6), \
                    mock.patch.object(apply, "ASSIST_VERIFY_WAIT_S", 1), \
                    mock.patch.object(ATS, "after_upload_ms", 100):
                out = apply.run(self.base + path, mode=mode, dry_run=dry_run, resume_pdf=pdf,
                                allow_submit=canonical if allow == "auto-url" else allow, session_factory=factory,
                                headless=headless_flag, unattended=unattended)
            job_dir = tmp / "output" / "dryrun_Acme_Backend_Engineer"
            result = json.loads((job_dir / "apply_result.json").read_text(encoding="utf-8"))
            shots = sorted(p.name for p in (job_dir / "screenshots").iterdir())
            self.last_files = sorted(p.name for p in job_dir.iterdir() if p.is_file())
            sheet = job_dir / "prepare_sheet.txt"
            self.last_sheet = sheet.read_text(encoding="utf-8") if sheet.exists() else ""
        return out, result, shots

    posts = property(lambda self: [m for m, _ in Server.seen if m == "POST"])

    # ── dry run ──
    def test_default_is_a_dry_run_that_never_submits(self):
        out, result, _ = self.go(mode="dry-run")
        self.assertEqual((result["status"], result["mode"]), ("dry_run", "dry-run"))
        self.assertEqual(self.posts, [])
        self.assertEqual(self.pauses, [])
        self.assertEqual(out["meta"]["blocked_submit_attempts"], 0)

    def test_dry_run_flag_beats_a_real_mode_and_a_valid_approval(self):
        out, result, _ = self.go(mode="auto", dry_run=True)
        self.assertEqual((result["status"], result["mode"]), ("dry_run", "dry-run"))
        self.assertEqual(self.posts, [])

    def test_real_mode_without_approval_is_downgraded(self):
        for allow in (None, "https://jobs.lever.co/acme/999"):
            Server.seen.clear()
            out, result, _ = self.go(mode="auto", allow=allow)
            self.assertEqual((result["status"], result["mode"], result["mode_requested"]), ("dry_run", "dry-run", "auto"))
            self.assertIn("dry run", out["meta"]["mode_note"])
            self.assertEqual(self.posts, [])
            self.assertEqual(self.pauses, [])

    def test_a_dry_run_never_pauses_for_the_user(self):
        clicked = []
        out, result, _ = self.go(mode="dry-run", user=lambda s: clicked.append(1))
        self.assertEqual((clicked, self.pauses, self.posts), ([], [], []))

    # ── assist ──
    def test_assist_fills_pauses_and_the_user_submits(self):
        out, result, shots = self.go(mode="assist", user=lambda s: s.page.click("button[type=submit]"))
        self.assertEqual(len(self.pauses), 1)
        self.assertIn("click Submit yourself", self.pauses[0])
        self.assertEqual(self.posts, ["POST"])                  # the USER's click went through: no guard in real modes
        self.assertEqual((result["status"], result["mode"], result["confirmation"]), ("submitted", "assist", True))
        self.assertIn("02_filled_and_flagged.png", shots)
        self.assertTrue(any(s.endswith("after_submit.png") for s in shots), shots)

    def test_assist_where_the_user_does_not_submit_needs_review(self):
        out, result, _ = self.go(mode="assist", user=None)
        self.assertEqual(self.posts, [])
        self.assertEqual(result["status"], "needs_review")

    def test_the_employers_whole_form_refusal_is_reported_as_failed_with_its_reason(self):
        """Lever's reply when its anti-bot check rejects an automated browser (seen live on a Hevo Data form)."""
        Server.submit_response = ("<html><body><form><h3>Submit</h3><p>✱ There was an error verifying your "
                                  "application. Please try again.</p></form></body></html>")
        out, result, _ = self.go(mode="assist", user=lambda s: s.page.click("button[type=submit]"))
        self.assertEqual(result["status"], "failed")
        self.assertIn("error verifying your application", " ".join(result["validation_errors"]).lower())

    def test_failure_text_patterns(self):
        from jobbot.ats.base import FAILURE_TEXT
        for text in ("There was an error verifying your application. Please try again.", "Something went wrong",
                     "We couldn't submit your application"):
            self.assertTrue(FAILURE_TEXT.search(text), text)
        self.assertFalse(FAILURE_TEXT.search("Thank you for applying!"))

    def test_assist_with_validation_errors_is_failed(self):
        Server.submit_response = "<html><body><form><input aria-invalid='true'><p role='alert'>Email is required</p></form></body></html>"
        out, result, _ = self.go(mode="assist", user=lambda s: s.page.click("button[type=submit]"))
        self.assertEqual(result["status"], "failed")
        self.assertIn("Email is required", " ".join(result["validation_errors"]))

    # ── auto ──
    def test_auto_submits_when_everything_is_filled(self):
        out, result, shots = self.go(mode="auto")
        self.assertEqual(self.posts, ["POST"])
        self.assertEqual(self.pauses, [])                       # nobody was needed
        self.assertEqual((result["status"], result["mode"]), ("submitted", "auto"))
        self.assertIsNone(result["switched_to_assist_because"])
        self.assertIn("before_submit.png", " ".join(shots))
        self.assertEqual(out["meta"]["would_need_assist"], [])

    def test_auto_switches_to_assist_when_a_required_field_is_flagged(self):
        out, result, _ = self.go("/form-needs-you", mode="auto")                # the LLM returns nothing -> flagged
        self.assertEqual(self.posts, [])                         # NOT submitted automatically
        self.assertEqual(len(self.pauses), 1)
        self.assertIn("switched to assist", self.pauses[0])
        self.assertTrue(any("Why do you want to work here?" in r for r in result["switched_to_assist_because"]))
        self.assertEqual(result["status"], "needs_review")

    def test_auto_that_switched_to_assist_still_lets_the_user_finish(self):
        out, result, _ = self.go("/form-needs-you", mode="auto", user=lambda s: s.page.click("button[type=submit]"))
        self.assertEqual(self.posts, ["POST"])
        self.assertEqual(result["status"], "submitted")

    def test_auto_switches_to_assist_when_a_captcha_challenge_is_visible(self):
        out, result, _ = self.go("/form-captcha", mode="auto")
        self.assertEqual(self.posts, [])
        self.assertIn("a CAPTCHA challenge is visible", result["switched_to_assist_because"])
        self.assertEqual(len(self.pauses), 1)

    def test_a_possible_captcha_alone_does_not_stop_auto(self):
        # the job says captcha_possible=True (Lever loads hCaptcha) but /form shows no challenge
        out, result, _ = self.go("/form", mode="auto")
        self.assertTrue(out["meta"]["captcha_possible"])
        self.assertEqual((result["status"], self.posts), ("submitted", ["POST"]))

    def test_unclear_result_after_auto_submit_is_needs_review(self):
        Server.submit_response = "<html><body><p>Processing...</p></body></html>"
        out, result, _ = self.go(mode="auto")
        self.assertEqual(self.posts, ["POST"])
        self.assertEqual(result["status"], "needs_review")

    # ── assist with no terminal to press Enter in (how a run started by Claude Code works) ──
    def test_assist_without_a_terminal_detects_the_users_submit_by_itself(self):
        calls = []

        def hook(session):                       # plays the user: clicks Submit on the second look
            calls.append(1)
            if len(calls) == 2:
                session.page.click("button[type=submit]")
        with mock.patch.object(apply_mod(), "ASSIST_WAIT_S", 30):
            out, result, shots = self.go(mode="assist", interactive=False, poll_hook=hook)
        self.assertEqual(self.pauses, [])                       # nobody pressed Enter: there was no Enter prompt
        self.assertEqual(self.posts, ["POST"])
        self.assertEqual((result["status"], result["mode"], result["confirmation"]), ("submitted", "assist", True))
        self.assertTrue(any(s.endswith("after_submit.png") for s in shots), shots)

    def test_assist_when_isatty_lies_and_stdin_is_closed_still_watches_the_page(self):
        """The real failure: stdin.isatty() was True but input() raised EOFError. That must fall back, not crash."""
        from jobbot.browser import BrowserSession
        calls = []

        def hook(session):
            calls.append(1)
            if len(calls) == 2:
                session.page.click("button[type=submit]")
        import apply
        original = BrowserSession.__init__

        def eof_input(prompt):
            raise EOFError

        # build the session ourselves: interactive (an input function exists) but reading it fails
        def factory_patch(self, *a, **k):
            k["input_fn"], k["poll_hook"] = eof_input, hook
            k.pop("interactive", None)
            original(self, *a, **k)
        with mock.patch.object(BrowserSession, "__init__", factory_patch), \
                mock.patch.object(apply, "ASSIST_WAIT_S", 30):
            out, result, _ = self.go(mode="assist")
        self.assertEqual(self.posts, ["POST"])
        self.assertEqual((result["status"], result["confirmation"]), ("submitted", True))

    def test_assist_without_a_terminal_times_out_to_needs_review_and_never_submits(self):
        with mock.patch.object(apply_mod(), "ASSIST_WAIT_S", 3):
            out, result, _ = self.go(mode="assist", interactive=False)
        self.assertEqual((self.posts, result["status"]), ([], "needs_review"))

    def test_assist_without_a_terminal_when_the_user_closes_the_window(self):
        def hook(session):
            session.page.close()
        with mock.patch.object(apply_mod(), "ASSIST_WAIT_S", 30):
            out, result, _ = self.go(mode="assist", interactive=False, poll_hook=hook)
        self.assertEqual((self.posts, result["status"]), ([], "needs_review"))

    def test_headless_is_refused_for_real_modes(self):
        with self.assertRaises(SystemExit) as cm:
            self.go(mode="auto", headless_flag=True)
        self.assertIn("visible browser", str(cm.exception))
        self.assertEqual(self.posts, [])


@unittest.skipUnless(HAVE_CHROMIUM, "Playwright Chromium not installed")
class UnattendedAuto(Runs):
    """python run.py: nobody is at the keyboard. Greenhouse auto submits only when every required field is filled and
    nothing is flagged; otherwise the materials are prepared and the job is Needs review with the reason. Never a wait."""

    def test_everything_filled_is_submitted_without_a_pause(self):
        out, result, _ = self.go(mode="auto", unattended=True)
        self.assertEqual((result["status"], self.posts, self.pauses), ("submitted", ["POST"], []))

    def test_a_flagged_required_field_means_needs_review_with_the_materials_and_no_submit(self):
        out, result, shots = self.go("/form-needs-you", mode="auto", unattended=True)
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual((self.posts, self.pauses), ([], []))                          # no submit, nobody waited for
        self.assertIn("not submitted", result["reason"])
        self.assertIn("Why do you want to work here", result["reason"])
        self.assertIn("prepare_sheet.html", self.last_files)
        self.assertIn("NEEDS YOUR REVIEW", self.last_sheet)
        self.assertIn("Why do you want to work here", self.last_sheet)
        self.assertTrue(any(x.startswith("0") and x.endswith("needs_review.png") for x in shots), shots)

    def test_a_visible_captcha_means_needs_review_and_is_never_bypassed(self):
        out, result, _ = self.go("/form-captcha", mode="auto", unattended=True)
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual((self.posts, self.pauses), ([], []))
        self.assertIn("CAPTCHA", result["reason"])

    def test_an_unclear_result_after_submit_means_needs_review_not_submitted(self):
        Server.submit_response = "<html><body><p>Processing...</p></body></html>"
        out, result, _ = self.go(mode="auto", unattended=True)
        self.assertEqual((result["status"], self.posts, self.pauses), ("needs_review", ["POST"], []))
        self.assertIn("nothing says the application was received", result["reason"])

    def test_a_security_code_step_after_submit_is_recorded_not_waited_for(self):
        Server.submit_response = "<html><body><p>Enter the 8-character code sent to your email to finish.</p></body></html>"
        out, result, _ = self.go(mode="auto", unattended=True)
        self.assertEqual((result["status"], self.posts, self.pauses), ("needs_review", ["POST"], []))
        self.assertIn("security code", result["reason"])
        self.assertIn("nothing was bypassed", result["reason"])

    def test_validation_errors_after_submit_are_failed_with_the_errors(self):
        Server.submit_response = ("<html><body><form><label>Email</label><input aria-invalid='true'>"
                                  "<p role='alert'>Email is required</p></form></body></html>")
        out, result, _ = self.go(mode="auto", unattended=True)
        self.assertEqual(result["status"], "failed")
        self.assertIn("Email is required", " ".join(result["validation_errors"]))

    def test_unattended_assist_never_waits_for_a_person(self):
        out, result, _ = self.go(mode="assist", unattended=True)
        self.assertEqual((result["status"], self.posts, self.pauses), ("needs_review", [], []))
        self.assertIn("nobody is at the keyboard", result["reason"])

    def test_the_attended_behaviour_is_unchanged(self):
        out, result, _ = self.go("/form-needs-you", mode="auto", user=lambda s: s.page.click("button[type=submit]"))
        self.assertEqual(len(self.pauses), 1)                                           # it still hands over to a person
        self.assertEqual(result["status"], "submitted")


for _name in [n for n in dir(Runs) if n.startswith("test_") and n not in UnattendedAuto.__dict__]:
    setattr(UnattendedAuto, _name, None)             # only the harness is inherited, not the attended tests again


class WaitForResult(unittest.TestCase):
    """The post-submit loop, with a fake adapter and session (no browser)."""

    class Page:
        url = "https://x"

        def wait_for_timeout(self, ms):
            pass

    class Session:
        def __init__(self):
            self.page = WaitForResult.Page()
            self.pauses = []          # the messages the user was shown while the run was paused

        interactive = True

        def pause_for_user(self, msg):
            self.pauses.append(msg)

        def hand_over(self, msg, still_waiting, **kw):
            self.pauses.append(msg)
            return "done"

    class Adapter:
        def __init__(self, challenges, verdicts):
            self.c, self.v = list(challenges), list(verdicts)

        def challenge(self, page):
            return self.c.pop(0) if self.c else None

        def is_confirmed(self, page):
            return self.v.pop(0) if self.v else None

    def test_a_visible_challenge_hands_control_to_the_user_then_continues(self):
        import apply
        s = self.Session()
        verdict = apply.wait_for_result(s, self.Adapter(["captcha", None], [None, True]), timeout_s=5, poll_s=0.01)
        self.assertTrue(verdict)
        self.assertEqual(len(s.pauses), 1)
        self.assertIn("captcha", s.pauses[0])
        self.assertIn("never bypassed", s.pauses[0])

    def test_security_code_step_is_handed_over_too(self):
        import apply
        s = self.Session()
        apply.wait_for_result(s, self.Adapter(["security-code"], [True]), timeout_s=5, poll_s=0.01)
        self.assertIn("security code", s.pauses[0])

    def test_unclear_until_the_timeout_returns_none(self):
        import apply
        s = self.Session()
        self.assertIsNone(apply.wait_for_result(s, self.Adapter([], []), timeout_s=0.05, poll_s=0.01))
        self.assertEqual(s.pauses, [])

    def test_unattended_a_visible_challenge_is_not_waited_for_and_the_answer_is_unclear(self):
        import apply
        s = self.Session()
        verdict = apply.wait_for_result(s, self.Adapter(["captcha"], [True]), timeout_s=5, poll_s=0.01, unattended=True)
        self.assertIsNone(verdict)
        self.assertEqual(s.pauses, [])

    def test_validation_errors_end_the_wait_as_false(self):
        import apply
        self.assertFalse(apply.wait_for_result(self.Session(), self.Adapter([], [False]), timeout_s=5, poll_s=0.01))


if __name__ == "__main__":
    unittest.main()
