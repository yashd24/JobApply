"""M4 field discovery + filler + the dry-run submit guard, in headless Chromium on saved/synthetic HTML.
Nothing here touches the network (every http request from the page is aborted)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

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
from jobbot import answers as A  # noqa: E402
from jobbot import fields as F  # noqa: E402
from jobbot import filler as FL  # noqa: E402
from jobbot import intake as I  # noqa: E402

REAL = ROOT / "tests" / "fixtures" / "real"

FORM = """<html><body><form id=f novalidate onsubmit="window.ownHandlerRan = true; return false">
<label for=fn>First name *</label><input id=fn name=first_name type=text>
<input name=email type=email aria-label="Email address" required>
<span id=lbl>Phone number</span><input name=phone type=tel aria-labelledby=lbl>
<label for=bio>About you</label><textarea id=bio name=bio></textarea>
<label for=city>City</label><select id=city name=city><option value="">Select...</option><option>Pune</option><option>Delhi</option></select>
<fieldset><legend>Work mode *</legend>
  <label><input type=radio name=mode value=a> Onsite</label><label><input type=radio name=mode value=b> Remote</label></fieldset>
<div><div class=q>Which tools do you use?</div><div><label><input type=checkbox name=tools value=1> Docker</label>
  <label><input type=checkbox name=tools value=2> Git</label><label><input type=checkbox name=tools value=3> Jenkins</label></div></div>
<label><input type=checkbox name="consent[marketing]" value=1> Yes, Acme may contact me about future roles</label>
<label for=cv>Resume/CV *</label><input id=cv name=resume type=file>
<input type=hidden name=csrf value=abc><input name=ghost type=text style="display:none">
<button type=submit id=go>Submit application</button></form></body></html>"""


FAKE_LOCATION_WIDGET = """() => {
  const inp = document.querySelector('input[name=location]'), hidden = document.querySelector('[name=selectedLocation]');
  const box = document.createElement('div'); inp.parentElement.appendChild(box);
  inp.addEventListener('input', () => {
    box.innerHTML = '';
    const d = document.createElement('div'); d.className = 'dropdown-location'; d.textContent = inp.value + ', IND';
    d.addEventListener('click', () => { inp.value = d.textContent; hidden.value = '{"id":"abc"}'; box.innerHTML = ''; });
    box.appendChild(d);
  });
  inp.addEventListener('blur', () => { if (!hidden.value) inp.value = ''; });     // like the real widget: unpicked text is wiped
}"""


def pdf(tmp, name="resume.pdf"):
    p = Path(tmp) / name
    p.write_bytes(b"%PDF-1.4\n%fake\n")
    return p


@unittest.skipUnless(HAVE_CHROMIUM, "Playwright Chromium not installed")
class Browserish(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def page(self, html, guard=True):
        ctx = self.browser.new_context()
        self.addCleanup(ctx.close)
        if guard:
            FL.install_submit_guard(ctx)
        pg = ctx.new_page()
        served = "http://fixture.test/form"
        pg.route("**/*", lambda r: r.fulfill(body=html, content_type="text/html; charset=utf-8")
                 if r.request.url == served else r.abort() if r.request.url.startswith("http") else r.continue_())
        pg.goto(served, wait_until="domcontentloaded")      # a real navigation, so init scripts run
        return pg


class Discovery(Browserish):
    def test_labels_types_required_and_options(self):
        fields = {f.key: f for f in F.discover_fields(self.page(FORM))}
        self.assertEqual(list(fields), ["first_name", "email", "phone", "bio", "city", "mode", "tools",
                                        "consent[marketing]", "resume"])       # hidden / display:none / button ignored
        self.assertEqual((fields["first_name"].label, fields["first_name"].required), ("First name", True))
        self.assertEqual((fields["email"].label, fields["email"].type, fields["email"].required),
                         ("Email address", "email", True))
        self.assertEqual((fields["phone"].label, fields["phone"].type), ("Phone number", "tel"))
        self.assertEqual((fields["bio"].label, fields["bio"].type, fields["bio"].required), ("About you", "textarea", False))
        self.assertEqual((fields["city"].type, fields["city"].options), ("select", ["Pune", "Delhi"]))   # placeholder dropped
        self.assertEqual((fields["mode"].label, fields["mode"].type, fields["mode"].options, fields["mode"].required),
                         ("Work mode", "radio", ["Onsite", "Remote"], True))
        self.assertEqual((fields["tools"].label, fields["tools"].type, fields["tools"].multiple, fields["tools"].options),
                         ("Which tools do you use?", "checkbox", True, ["Docker", "Git", "Jenkins"]))
        c = fields["consent[marketing]"]
        self.assertEqual((c.group, c.type, c.multiple), ("consent", "checkbox", False))
        self.assertEqual((fields["resume"].type, fields["resume"].required), ("file", True))

    def test_discovery_matches_intake_on_the_real_lever_forms(self):
        for name in ("lv_gushwork", "lv_portcast", "lv_kwalee_eu"):
            with self.subTest(fixture=name):
                d = json.loads((REAL / name / "urls.json").read_text(encoding="utf-8"))
                html = next(t for u, t in d["responses"].items() if u.endswith("/apply"))
                dom = {f.key: f for f in F.discover_fields(self.page(html))}
                job = I.fetch_job(d["input"], http=lambda u, d=d: (200, {k: v for k, v in d["responses"].items() if v}[u])
                                  if u in {k for k, v in d["responses"].items() if v} else (404, ""))
                intake = {f.key: f for f in F.fields_from_questions(job.questions) if f.type != "hidden"}
                self.assertEqual(set(dom), set(intake))
                for key, q in intake.items():
                    if q.type == "file":
                        self.assertEqual(dom[key].type, "file")
                        continue
                    self.assertEqual(dom[key].label, q.label, key)
                    self.assertEqual(dom[key].type, q.type, key)
                    self.assertEqual(dom[key].options, q.options, key)
                    if q.required:
                        self.assertTrue(dom[key].required, key)


class Filling(Browserish):
    def test_every_control_type_is_filled_and_read_back(self):
        pg = self.page(FORM)
        fields = F.discover_fields(pg)
        with tempfile.TemporaryDirectory(prefix="fill test ") as tmp:
            cv = pdf(tmp)
            values = {"first_name": "Test", "email": "test.user@example.com", "phone": "+00 000", "bio": "Hello there.",
                      "city": "Delhi", "mode": "Remote", "tools": ["Docker", "Jenkins"], "resume": str(cv)}
            answers = [A.Answer(f.key, f.label, f.type, f.required, "x", values[f.key], "test", "high", A.FILLED)
                       for f in fields if f.key in values]
            results = FL.fill_fields(pg, fields, answers)
        self.assertTrue(all(r.ok for r in results), [r for r in results if not r.ok])
        self.assertEqual(len(results), len(values))
        got = pg.evaluate("""() => ({fn: document.querySelector('[name=first_name]').value,
            city: document.querySelector('[name=city]').value, mode: document.querySelector('[name=mode]:checked').value,
            tools: Array.from(document.querySelectorAll('[name=tools]:checked')).map(e => e.value),
            consent: document.querySelector('[name="consent[marketing]"]').checked,
            file: document.querySelector('[name=resume]').files[0].name})""")
        self.assertEqual(got, {"fn": "Test", "city": "Delhi", "mode": "b", "tools": ["1", "3"],
                               "consent": False, "file": "resume.pdf"})           # consent stays unticked

    def test_bad_values_are_reported_not_hidden(self):
        pg = self.page(FORM)
        fields = F.discover_fields(pg)
        bad = [A.Answer("city", "City", "select", False, "x", "Atlantis", "t", "high", A.FILLED),
               A.Answer("mode", "Work mode", "radio", True, "x", "Hybrid", "t", "high", A.FILLED),
               A.Answer("resume", "Resume", "file", True, "x", "no/such/file.pdf", "t", "high", A.FILLED),
               A.Answer("nope", "Missing", "text", False, "x", "v", "t", "high", A.FILLED)]
        results = FL.fill_fields(pg, fields + [F.Field("nope", "Missing", "text", False)], bad)
        self.assertEqual([r.ok for r in results], [False] * 4)

    def test_flagged_and_blank_answers_are_not_touched_and_are_highlighted(self):
        pg = self.page(FORM)
        fields = F.discover_fields(pg)
        answers = [A.Answer("bio", "About you", "textarea", False, "x", None, "none", "low", A.FLAGGED, "you write this"),
                   A.Answer("phone", "Phone number", "tel", False, "x", None, "p", "high", A.BLANK)]
        self.assertEqual(FL.fill_fields(pg, fields, answers), [])
        self.assertEqual(pg.input_value("[name=bio]"), "")
        self.assertEqual(FL.highlight_flagged(pg, answers), 1)
        self.assertRegex(pg.evaluate("document.querySelector('[name=bio]').closest('div, body').style.outline || "
                                              "document.querySelector('[name=bio]').style.outline"), r"3px.*solid|solid.*3px")
        self.assertIn("you write this", pg.evaluate("document.querySelector('[name=bio]').title"))


class CaptchaRule(Browserish):
    """A CAPTCHA that merely might appear must not stop a run; only a visible challenge does."""

    def captcha(self, body):
        return FL.captcha_challenge_visible(self.page("<html><body><form>" + body + "</form></body></html>"))

    def test_visible_challenges_count(self):
        self.assertTrue(self.captcha('<iframe src="https://newassets.hcaptcha.com/captcha/v1/x/static/hcaptcha.html" '
                                     'title="Main content of the hCaptcha challenge" style="width:400px;height:500px"></iframe>'))
        self.assertTrue(self.captcha('<iframe src="https://newassets.hcaptcha.com/captcha/v1/x/static/hcaptcha.html" '
                                     'title="Widget containing checkbox for hCaptcha security challenge" '
                                     'style="width:303px;height:78px"></iframe>'))
        self.assertTrue(self.captcha('<iframe src="https://www.google.com/recaptcha/api2/anchor" '
                                     'style="width:304px;height:78px"></iframe>'))

    def test_possible_but_not_visible_does_not_count(self):
        hc = 'src="https://newassets.hcaptcha.com/captcha/v1/x/static/hcaptcha.html"'
        self.assertFalse(self.captcha(""))                                                      # nothing at all
        self.assertFalse(self.captcha(f'<iframe {hc} style="width:0;height:0"></iframe>'))     # invisible mode
        self.assertFalse(self.captcha(f'<iframe {hc} style="display:none;width:400px;height:500px"></iframe>'))
        self.assertFalse(self.captcha(f'<div style="display:none"><iframe {hc} style="width:400px;height:500px"></iframe></div>'))
        self.assertFalse(self.captcha('<div class="grecaptcha-badge"><iframe src="https://www.google.com/recaptcha/api2/anchor" '
                                      'style="width:256px;height:60px"></iframe></div>'))        # reCAPTCHA v3 badge
        self.assertFalse(self.captcha('<script src="https://js.hcaptcha.com/1/api.js"></script><div class="h-captcha"></div>'))

    def test_the_saved_lever_forms_show_no_challenge_before_submitting(self):
        for name in ("lv_gushwork", "lv_portcast", "lv_kwalee_eu"):
            d = json.loads((REAL / name / "urls.json").read_text(encoding="utf-8"))
            html = next(t for u, t in d["responses"].items() if u.endswith("/apply"))
            self.assertFalse(FL.captcha_challenge_visible(self.page(html)), name)


class LocationWidgetRetry(Browserish):
    """The live suggestion service sometimes answers only to the bare city; try "City, State", then the city."""
    PICKY = FAKE_LOCATION_WIDGET.replace("inp.addEventListener('input', () => {",
                                         "inp.addEventListener('input', () => { if (inp.value.includes(',')) { box.innerHTML = ''; return; }") \
        .replace("d.textContent = inp.value + ', IND'", "d.textContent = inp.value + ', Karnataka, IND'")

    def test_falls_back_to_the_bare_city_and_aliases_count(self):
        d = json.loads((REAL / "lv_gushwork" / "urls.json").read_text(encoding="utf-8"))
        html = next(t for u, t in d["responses"].items() if u.endswith("/apply"))
        pg = self.page(html)
        pg.evaluate(self.PICKY)
        fields = [f for f in F.discover_fields(pg) if f.key == "location"]
        a = A.Answer("location", "Current location", "text", True, "location", "Bangalore, Karnataka, India", "p", "high", A.FILLED)
        from unittest import mock
        with mock.patch.object(FL, "SUGGESTION_WAIT_MS", 800):
            res = FL.fill_fields(pg, fields, [a])
        self.assertEqual((res[0].ok, res[0].detail[:17]), (True, "picked suggestion"), res)
        self.assertTrue(pg.input_value('[name="selectedLocation"]'))


class LocationWidget(Browserish):
    def test_no_suggestions_is_reported_as_a_failed_fill(self):
        from unittest import mock
        d = json.loads((REAL / "lv_gushwork" / "urls.json").read_text(encoding="utf-8"))
        html = next(t for u, t in d["responses"].items() if u.endswith("/apply"))
        pg = self.page(html)
        fields = [f for f in F.discover_fields(pg) if f.key == "location"]
        ans = [A.Answer("location", "Current location", "text", True, "location", "Nowhere, Nothing", "p", "high", A.FILLED)]
        with mock.patch.object(FL, "SUGGESTION_WAIT_MS", 400):
            res = FL.fill_fields(pg, fields, ans)
        self.assertEqual([(r.ok, r.detail) for r in res], [(False, "no location suggestions appeared")])


class SubmitGuard(Browserish):
    """Dry run: nothing may ever submit, even if our own code or a stray key press tried to."""

    def test_clicking_the_submit_button_is_blocked(self):
        pg = self.page(FORM)
        pg.click("#go")
        self.assertEqual(FL.blocked_submits(pg), 1)
        self.assertFalse(pg.evaluate("!!window.ownHandlerRan"))

    def test_enter_key_implicit_submission_is_blocked(self):
        pg = self.page(FORM)
        pg.fill("[name=first_name]", "x")
        pg.press("[name=first_name]", "Enter")
        self.assertGreaterEqual(FL.blocked_submits(pg), 1)
        self.assertFalse(pg.evaluate("!!window.ownHandlerRan"))

    def test_programmatic_submits_are_blocked(self):
        pg = self.page(FORM)
        pg.evaluate("document.getElementById('f').submit()")
        pg.evaluate("document.getElementById('f').requestSubmit()")
        self.assertEqual(FL.blocked_submits(pg), 2)

    def test_without_the_guard_the_same_click_would_submit(self):            # proves the test is meaningful
        pg = self.page(FORM, guard=False)
        pg.click("#go")
        self.assertTrue(pg.evaluate("!!window.ownHandlerRan"))

    def test_the_filler_has_no_submit_code(self):
        import ast
        tree = ast.parse((ROOT / "jobbot" / "filler.py").read_text(encoding="utf-8"))
        def calls(node):
            return {n.func.attr for n in ast.walk(node) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
        forbidden = {"click", "dblclick", "tap", "press", "dispatch_event", "submit", "requestSubmit"}
        # the only clicks allowed are inside the location-autocomplete helper: they target the location input and
        # its ".dropdown-location" suggestions, never a button
        for fn in (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)):
            if fn.name != "_fill_location_widget":
                self.assertFalse(calls(fn) & forbidden, (fn.name, calls(fn) & forbidden))
        widget = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_fill_location_widget")
        self.assertEqual(calls(widget) & forbidden, {"click"})
        src = (ROOT / "jobbot" / "filler.py").read_text(encoding="utf-8")
        self.assertIn('page.locator(".dropdown-location")', src)
        self.assertNotIn("button", src.split("def _fill_location_widget")[1].split("def _fill_one")[0].lower())
        defined = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        self.assertEqual({d for d in defined if "submit" in d}, {"install_submit_guard", "blocked_submits"})


class GushworkEndToEnd(Browserish):
    def test_fill_the_saved_gushwork_form_with_the_persona(self):
        d = json.loads((REAL / "lv_gushwork" / "urls.json").read_text(encoding="utf-8"))
        rs = {u: t for u, t in d["responses"].items() if t}
        html = next(t for u, t in rs.items() if u.endswith("/apply"))
        job = I.fetch_job(d["input"], http=lambda u: (200, rs[u]) if u in rs else (404, ""))
        pg = self.page(html)
        pg.evaluate(FAKE_LOCATION_WIDGET)                       # the real one needs Lever's network service
        fields = F.discover_fields(pg)
        with tempfile.TemporaryDirectory(prefix="gush test ") as tmp:
            ctx = A.Context(profile=persona.profile(), resume=persona.resume(), today=persona.TODAY,
                            company=job.company, role=job.role, job_location=job.location, platform="lever",
                            jd_text=job.jd_text, resume_pdf=pdf(tmp), llm=lambda p: {"answers": {}})
            answers = A.answer_fields(fields, ctx)
            results = FL.fill_fields(pg, fields, answers)
            n_flagged = FL.highlight_flagged(pg, answers)
        self.assertTrue(all(r.ok for r in results), [r for r in results if not r.ok])
        self.assertEqual(len(results), sum(a.status == A.FILLED for a in answers))
        self.assertEqual(n_flagged, 1)                                             # the referral question
        for a in answers:                                     # every filled text answer reads back from the page
            if a.key != "location" and a.status == A.FILLED and a.type in ("text", "email", "tel", "textarea"):
                self.assertEqual(pg.input_value(f'[name="{a.key}"]'), a.value, a.label)
        by = {a.label: a for a in answers}
        self.assertEqual(pg.input_value('[name="name"]'), "Test User")
        self.assertEqual(pg.input_value('[name="org"]'), "Acme Test Co")
        self.assertEqual(by["LinkedIn URL"].value, "https://www.linkedin.com/in/test-user")
        self.assertEqual(pg.evaluate("document.querySelector('[name=resume]').files[0].name"), "resume.pdf")
        ticked = pg.evaluate("Array.from(document.querySelectorAll('input[type=checkbox]:checked')).map(e => e.closest('label').innerText.trim())")
        self.assertEqual(sorted(ticked), ["AWS", "Java", "MongoDB", "Postgresql", "Python"])
        self.assertEqual(FL.blocked_submits(pg), 0)                                # nothing tried to submit
        loc = next(r for r in results if r.label == "Current location")
        self.assertIn("picked suggestion", loc.detail)
        self.assertTrue(pg.input_value('[name="location"]').endswith(", IND"))
        self.assertTrue(pg.input_value('[name="selectedLocation"]'))


if __name__ == "__main__":
    unittest.main()
