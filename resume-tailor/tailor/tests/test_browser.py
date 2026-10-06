"""M3 browser session, against real headless Chromium and a local HTTP server. Dirs contain spaces on
purpose (the user's Windows profile path has one)."""
import http.server
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("JOBBOT_NO_SHEET", "1")      # a test must never reach the real Google Sheet

try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as _p:
        _p.chromium.launch(headless=True).close()
    HAVE_CHROMIUM = True
except Exception:          # playwright/Chromium not installed
    HAVE_CHROMIUM = False

from jobbot.browser import BrowserError, BrowserSession  # noqa: E402

PAGE = b"<html><head><title>Test Job</title></head><body><h1>Backend Engineer</h1><input id=x></body></html>"


class Handler(http.server.BaseHTTPRequestHandler):
    hits = 0
    flaky_failed = False
    seen_down = 0

    def do_GET(self):
        Handler.hits += 1
        code = 200
        if self.path.endswith("-missing"):
            code = 404
        elif self.path.endswith("-down"):
            code, Handler.seen_down = 503, Handler.seen_down + 1
        elif self.path.endswith("-flaky") and not Handler.flaky_failed:
            code, Handler.flaky_failed = 502, True
        self.send_response(code)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(PAGE)

    def log_message(self, *a):
        pass


@unittest.skipUnless(HAVE_CHROMIUM, "Playwright Chromium not installed")
class Browser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/job"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="job apply ")
        self.root = Path(self.tmp.name)

    def tearDown(self):
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    def session(self, name="run one", **kw):
        return BrowserSession(self.root / name, profile_dir=self.root / "browser profile",
                              headless=True, output_fn=lambda *_: None, **kw)

    def test_opens_a_page_and_screenshots_land_in_the_job_folder(self):
        with self.session() as b:
            b.goto(self.url)
            self.assertEqual(b.page.title(), "Test Job")
            p1, p2 = b.screenshot("form top"), b.screenshot("after/fill")
        shots = self.root / "run one" / "screenshots"
        self.assertEqual([p.name for p in sorted(shots.iterdir())], ["01_form_top.png", "02_after_fill.png"])
        self.assertEqual(p1.parent, shots)
        self.assertGreater(p2.stat().st_size, 1000)
        self.assertEqual(p1.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")

    def test_profile_persists_cookies_between_runs(self):
        with self.session("run a") as b:
            b.goto(self.url)
            b.context.add_cookies([{"name": "login", "value": "abc", "url": self.url,
                                    "expires": time.time() + 86400}])
        with self.session("run b") as b:
            b.goto(self.url)
            self.assertEqual({c["name"]: c["value"] for c in b.context.cookies()}.get("login"), "abc")

    def test_pause_prints_the_reason_and_resumes_on_enter(self):
        seen = {}
        lines = []
        b = BrowserSession(self.root / "p", profile_dir=self.root / "browser profile", headless=True,
                           input_fn=lambda prompt: seen.setdefault("prompt", prompt) and "",
                           output_fn=lines.append)
        with b:
            b.goto(self.url)
            b.pause_for_user("CAPTCHA appeared")
            self.assertEqual(b.page.title(), "Test Job")          # still usable afterwards
        self.assertTrue(any("PAUSED: CAPTCHA appeared" in l for l in lines))
        self.assertIn("Enter", seen["prompt"])

    def test_pause_survives_the_user_closing_the_tab(self):
        with self.session() as b:
            b.goto(self.url)
            b._input = lambda prompt: b.page.close() or ""        # user closes the tab during the pause
            b.pause_for_user("solve it")
            b.goto(self.url)                                      # a fresh page is opened transparently
            self.assertEqual(b.page.title(), "Test Job")

    def test_hand_over_without_a_terminal_watches_the_page_instead_of_waiting_for_enter(self):
        ticks = []

        def hook(s):                                    # plays the user: does the thing on the 3rd look
            ticks.append(1)
            if len(ticks) == 3:
                s.page.evaluate("window.userDone = true")
        with self.session(interactive=False, poll_hook=hook) as b:
            b.goto(self.url)
            self.assertFalse(b.interactive)
            result = b.hand_over("do the thing", lambda: not b.page.evaluate("!!window.userDone"), timeout_s=30, poll_s=0.05)
        self.assertEqual((result, len(ticks)), ("done", 3))

    def test_the_user_is_told_in_the_page_that_the_bot_is_waiting(self):
        seen = {}

        def hook(s):
            seen["banner"] = s.page.evaluate("(document.getElementById('jobbot-banner') || {}).textContent || null")
        with self.session(interactive=False, poll_hook=hook) as b:
            b.goto(self.url)
            b.hand_over("Click Submit yourself", lambda: False, timeout_s=5, poll_s=0.05)
        self.assertIn("WAITING FOR YOU", seen["banner"])
        self.assertIn("Click Submit yourself", seen["banner"])

    def test_hand_over_times_out_and_notices_a_closed_window(self):
        with self.session(interactive=False) as b:
            b.goto(self.url)
            self.assertEqual(b.hand_over("x", lambda: True, timeout_s=0.3, poll_s=0.05), "timeout")
        with self.session(interactive=False, poll_hook=lambda s: s.page.close()) as b:
            b.goto(self.url)
            self.assertEqual(b.hand_over("x", lambda: True, timeout_s=10, poll_s=0.05), "closed")

    def test_hand_over_with_a_terminal_uses_enter(self):
        asked = []
        b = BrowserSession(self.root / "h", profile_dir=self.root / "browser profile", headless=True,
                           input_fn=lambda prompt: asked.append(prompt) or "", output_fn=lambda *_: None)
        with b:
            b.goto(self.url)
            self.assertTrue(b.interactive)
            self.assertEqual(b.hand_over("press it", lambda: True), "done")
        self.assertEqual(len(asked), 1)

    def test_pause_without_a_terminal_is_a_clear_error(self):
        def eof(_):
            raise EOFError
        with self.session() as b:
            b._input = eof
            with self.assertRaises(BrowserError):
                b.pause_for_user("x")

    def test_goto_returns_the_http_status_and_retries_5xx_once(self):
        with self.session() as b:
            self.assertEqual(b.goto(self.url), 200)
            self.assertEqual(b.goto(self.url + "-missing"), 404)            # 4xx returned, not retried
            before = Handler.hits
            self.assertEqual(b.goto(self.url + "-flaky", retries=1), 200)   # 502 first, then 200
            self.assertEqual(Handler.hits - before, 2)
            self.assertEqual(b.goto(self.url + "-down", retries=1), 503)    # still failing: returned
        self.assertTrue(Handler.seen_down >= 2)

    def test_unreachable_page_is_a_browser_error(self):
        with self.session() as b:
            with self.assertRaises(BrowserError):
                b.goto("http://127.0.0.1:9/nothing", timeout_ms=5000)

    def test_second_process_on_the_same_profile_gets_a_clear_error(self):
        import subprocess
        code = ("import sys; sys.path.insert(0, sys.argv[1]);"
                "from jobbot.browser import BrowserSession, BrowserError\n"
                "try:\n"
                "    with BrowserSession(sys.argv[2], profile_dir=sys.argv[3], headless=True): pass\n"
                "except BrowserError as e:\n"
                "    print('BrowserError:', e); sys.exit(3)\n")
        with self.session("first"):
            r = subprocess.run([sys.executable, "-c", code, str(ROOT), str(self.root / "second"),
                                str(self.root / "browser profile")],
                               capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("BrowserError:", r.stdout)


if __name__ == "__main__":
    unittest.main()
