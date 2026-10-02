"""apply.py end to end (headless Chromium, a local form server, a fictional persona, a fake LLM).
Proves the M4 guarantees: the run is a dry run, answers.json records every answer and source, and the
server never receives a submission."""
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

FORM = """<html><head><title>Job</title></head><body><form id="application-form" method="POST" action="/submit">
<ul>
<li class="application-question"><div class="application-label">Resume/CV</div><div><input type="file" name="resume"></div></li>
<li class="application-question"><div class="application-label">Full name <span>✱</span></div><div><input type="text" name="name"></div></li>
<li class="application-question"><div class="application-label">Email <span>✱</span></div><div><input type="email" name="email"></div></li>
<li class="application-question"><div class="application-label">Phone <span>✱</span></div><div><input type="text" name="phone"></div></li>
<li class="application-question"><div class="application-label">Current company <span>✱</span></div><div><input type="text" name="org"></div></li>
<li class="application-question"><div class="application-label">What is your Expected CTC? <span>✱</span></div><div><input type="text" name="cards[1][f0]"></div></li>
<li class="application-question"><div class="application-label">Were you referred by someone at Acme?</div><div><textarea name="cards[1][f1]"></textarea></div></li>
<li class="application-question"><div class="application-label">Why do you want to work here? <span>✱</span></div><div><textarea name="cards[1][f2]"></textarea></div></li>
</ul><button type="submit">Submit application</button></form></body></html>"""


class Handler(http.server.BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):
        Handler.seen.append(("GET", self.path))
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(FORM.encode("utf-8"))

    def do_POST(self):
        Handler.seen.append(("POST", self.path))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


@unittest.skipUnless(HAVE_CHROMIUM, "Playwright Chromium not installed")
class DryRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def test_a_full_dry_run(self):
        import apply
        import tailor
        from jobbot import browser, intake

        Handler.seen.clear()
        with tempfile.TemporaryDirectory(prefix="apply test ") as tmp:
            tmp = Path(tmp)
            prof = tmp / "profile.yaml"
            prof.write_text(yaml.safe_dump(persona.profile(), allow_unicode=True), encoding="utf-8")
            pdf = tmp / "resume.pdf"
            pdf.write_bytes(b"%PDF-1.4\n%fake\n")
            job = intake.Job(platform="lever", company="Acme", role="Backend Engineer", location="Bengaluru",
                             canonical_url="https://jobs.lever.co/acme/1", apply_url=self.base + "/form",
                             jd_text="Build Python APIs. " * 20, questions=[], job_id="1", board="acme",
                             source="api", captcha_possible=True)
            llm_calls = []

            def fake_llm(prompt):
                llm_calls.append(prompt)
                return {"answers": {"cards[1][f2]": {"answer": "I built a payments API serving 50,000+ requests per week.",
                                                     "confidence": "high", "based_on": ["a1"]}}}

            with mock.patch.object(apply, "PROFILE_FILE", prof), \
                    mock.patch.object(tailor, "load_resume_data", lambda: persona.resume()), \
                    mock.patch.object(tailor, "OUTPUT_DIR", tmp / "output"), \
                    mock.patch.object(tailor, "call_claude", fake_llm), \
                    mock.patch.object(intake, "fetch_job", lambda url: job), \
                    mock.patch.object(browser, "DEFAULT_PROFILE_DIR", tmp / "browser profile"):
                out = apply.run(self.base + "/form", resume_pdf=pdf, headless=True, mode="dry-run")

            job_dir = tmp / "output" / "dryrun_Acme_Backend_Engineer"
            data = json.loads((job_dir / "answers.json").read_text(encoding="utf-8"))
            shots = sorted(p.name for p in (job_dir / "screenshots").iterdir())

        self.assertEqual(data["meta"]["mode"], "dry-run")
        self.assertEqual(data["meta"]["blocked_submit_attempts"], 0)
        self.assertEqual(shots, ["01_form_loaded.png", "02_filled_and_flagged.png"])
        self.assertEqual([m for m, _ in Handler.seen], ["GET"])                      # nothing was ever POSTed
        by = {a["label"]: a for a in data["answers"]}
        self.assertEqual(by["Full name"]["value"], "Test User")
        self.assertEqual(by["Full name"]["source"], "profile:personal.name")
        self.assertEqual(by["What is your Expected CTC?"]["value"], "18 LPA (negotiable)")
        self.assertEqual(by["Why do you want to work here?"]["source"], "llm (from the visible resume + JD)")
        self.assertEqual(by["Were you referred by someone at Acme?"]["status"], "flagged")
        self.assertTrue(by["Resume/CV"]["value"].endswith("resume.pdf"))
        self.assertEqual(data["summary"]["sent_to_llm"], ["Why do you want to work here?"])
        self.assertEqual(len(llm_calls), 1)                                          # one batched call
        self.assertNotIn("Expected CTC", llm_calls[0])
        self.assertEqual(data["meta"]["highlighted_flagged_fields"], 1)
        self.assertTrue(all(r["ok"] for r in data["meta"]["fill_results"]))
        self.assertTrue(all(r["ok"] for r in data["meta"]["read_back_after_fill"]))
        # captcha_possible=True on the job, but no challenge is visible: that must NOT force assist
        self.assertEqual(data["meta"]["would_need_assist"], [])
        self.assertFalse(data["meta"]["captcha_challenge_visible"])
        self.assertTrue(data["meta"]["captcha_possible"])
        for a in data["answers"]:
            self.assertTrue(a["source"])
        self.assertEqual(out["summary"]["total"], 8)


class Helpers(unittest.TestCase):
    def test_would_need_assist(self):
        import apply
        from jobbot import answers as A
        ok = A.Answer("a", "A", "text", True, "x", "v", "s", "high", A.FILLED)
        bad = A.Answer("b", "B", "text", True, "x", None, "s", "low", A.FLAGGED)
        opt = A.Answer("c", "C", "text", False, "x", None, "s", "low", A.FLAGGED)
        self.assertEqual(apply.would_need_assist([ok, opt], False), [])
        self.assertEqual(apply.would_need_assist([ok, bad], True),
                         ["required field not answered: B", "a CAPTCHA challenge is visible"])

    def test_the_only_submit_call_is_behind_the_auto_mode_branch(self):
        import ast
        tree = ast.parse((ROOT / "apply.py").read_text(encoding="utf-8"))
        calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)]
        self.assertFalse({c.func.attr for c in calls} & {"click", "press", "dispatch_event", "requestSubmit"})
        submits = [c for c in calls if c.func.attr == "submit"]
        self.assertEqual(len(submits), 1)
        run_fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "run")
        auto_branches = [n for n in ast.walk(run_fn) if isinstance(n, ast.If)
                         and "used == 'auto'" in ast.unparse(n.test)]
        self.assertTrue(any(submits[0] in set(ast.walk(b)) for b in auto_branches), "submit must sit under used == 'auto'")


if __name__ == "__main__":
    unittest.main()
