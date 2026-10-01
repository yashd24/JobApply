"""Greenhouse adapter against a synthetic page that behaves like the real React form (behaviour observed on live
postings): React-Select dropdowns, an async city autocomplete, an intl-tel-input style phone box, hidden helper
inputs. Headless Chromium, no network."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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
from jobbot import filler  # noqa: E402
from jobbot.ats import adapter_for  # noqa: E402
from jobbot.ats.greenhouse import Greenhouse, _match, _phone_ok  # noqa: E402

COUNTRIES = ["United States +1", "British Indian Ocean Territory +246", "India +91", "Indonesia +62", "Germany +49"]
HEARD = ["BuiltIn", "LinkedIn", "Company Careers Page", "Referral", "Other"]
CITIES = ["Bangalore, Oregon, USA", "Bangalore, Karnataka, India", "Mumbai, Maharashtra, India"]

HTML = """<html><head><style>.visually-hidden{position:absolute;opacity:0;width:1px;height:1px}</style></head><body>
<form id="f" novalidate>
 <div class="field"><label for="first_name">First Name*</label><input id="first_name" name="first_name" class="input" required></div>
 <div class="field"><label for="last_name">Last Name*</label><input id="last_name" name="last_name" class="input" required></div>
 <div class="field"><label for="email">Email*</label><input id="email" name="email" class="input" required></div>
 <div class="field"><label id="country-label" for="country">Country*</label><div data-w="country"></div></div>
 <input class="remix-css-1a0ro4n-requiredInput" required>
 <div class="field"><label for="phone">Phone*</label><input id="phone" name="phone" type="tel" class="input iti__tel-input" required>
   <input class="iti__search-input" type="search" id="iti-0__search-input" role="combobox"></div>
 <div class="field"><label id="candidate-location-label" for="candidate-location">Location (City)*</label><div data-w="loc"></div></div>
 <div class="field"><label for="resume">Resume/CV*</label><input id="resume" name="resume" type="file" class="visually-hidden"></div>
 <div class="field"><label id="question_1-label" for="question_1">Are you eligible to work in India?*</label><div data-w="q1"></div></div>
 <div class="field"><label id="question_2[]-label" for="question_2[]">How did you learn about this job?*</label><div data-w="q2"></div></div>
 <div class="field"><label id="question_4-label" for="question_4">Do you have a family member who works here?</label><div data-w="q4"></div></div>
 <div class="field"><label id="q5_gender-label" for="q5_gender">How would you describe your gender identity? (mark all that apply)</label><div data-w="q5"></div></div>
 <fieldset id="question_3[]"><legend>Acme Privacy Policy*</legend><label><input type="checkbox" name="question_3[]" value="1" required> I Agree</label></fieldset>
 <button type="submit">Submit application</button>
</form>
<script>
function makeSelect(host, id, opts, o) {
  o = o || {};
  host.innerHTML = '<div class="select__control"><div class="select__value-container"><div class="select__placeholder">Select...</div>' +
    '<div class="select__input-container"><input class="select__input" id="' + id + '" type="text" role="combobox" ' +
    'aria-labelledby="' + id + '-label" aria-required="true" autocomplete="off"></div></div></div>';
  const control = host.querySelector('.select__control'), vc = host.querySelector('.select__value-container');
  const input = host.querySelector('input'); let menu = null; const chosen = [];
  const close = () => { if (menu) { menu.remove(); menu = null; } };
  const show = () => { const ph = vc.querySelector('.select__placeholder'); if (ph) ph.style.display = chosen.length ? 'none' : ''; };
  const draw = (q) => {
    if (!menu) { menu = document.createElement('div'); menu.className = 'select__menu'; control.after(menu); }
    q = (q || '').toLowerCase(); menu.innerHTML = '';
    let list = opts.filter(x => x.toLowerCase().includes(q));
    if (o.async) list = q.length >= 3 ? list : [];
    if (o.multi) list = list.filter(x => !chosen.includes(x));
    if (!list.length) { menu.innerHTML = '<div class="select__menu-notice">No options</div>'; return; }
    list.forEach(x => { const d = document.createElement('div'); d.className = 'select__option'; d.setAttribute('role', 'option');
      d.textContent = x; d.addEventListener('click', () => { pick(x); }); menu.appendChild(d); });
  };
  const pick = (x) => {
    if (o.multi) { chosen.push(x); const m = document.createElement('div'); m.className = 'select__multi-value';
      m.innerHTML = '<div class="select__multi-value__label"></div>'; m.firstChild.textContent = x; vc.prepend(m);
    } else { chosen.length = 0; chosen.push(x); let s = vc.querySelector('.select__single-value');
      if (!s) { s = document.createElement('div'); s.className = 'select__single-value'; vc.prepend(s); } s.textContent = x; }
    input.value = ''; show(); close();
  };
  input.addEventListener('click', () => draw(''));
  input.addEventListener('input', () => { const q = input.value; if (o.async) setTimeout(() => draw(q), 200); else draw(q); });
  input.addEventListener('keydown', (e) => { if (e.key === 'Escape') close(); });
}
makeSelect(document.querySelector('[data-w=country]'), 'country', %(countries)s);
makeSelect(document.querySelector('[data-w=loc]'), 'candidate-location', %(cities)s, {async: true});
makeSelect(document.querySelector('[data-w=q1]'), 'question_1', ['Yes', 'No']);
makeSelect(document.querySelector('[data-w=q2]'), 'question_2[]', %(heard)s, {multi: true});
makeSelect(document.querySelector('[data-w=q4]'), 'question_4', ['Yes', 'No']);
makeSelect(document.querySelector('[data-w=q5]'), 'q5_gender', ['Man', 'Woman', 'Non-binary', 'I do not wish to answer'], {multi: true});
document.getElementById('resume').addEventListener('change', (e) => {      // like Greenhouse: the input is REPLACED by the name
  const chip = document.createElement('div'); chip.className = 'file-chip'; chip.textContent = e.target.files[0].name; e.target.replaceWith(chip); });
const phone = document.getElementById('phone');
phone.addEventListener('input', () => { phone.value = phone.value.replace(/^[+]00 /, ''); });   // like intl-tel-input
</script></body></html>""" % {"countries": str(COUNTRIES), "cities": str(CITIES), "heard": str(HEARD)}


def pdf(tmp):
    p = Path(tmp) / "resume.pdf"
    p.write_bytes(b"%PDF-1.4\n%fake\n")
    return p


def ctx(pdf_path):
    c = A.Context(profile=persona.profile(), resume=persona.resume(), today=persona.TODAY, job_location="Bengaluru",
                  platform="greenhouse", company="Acme", role="Backend", resume_pdf=pdf_path,
                  llm=lambda p: {"answers": {}})
    c.profile["personal"]["current_location"] = {"city": "Bangalore", "state": "Karnataka", "country": "India"}
    return c


@unittest.skipUnless(HAVE_CHROMIUM, "Playwright Chromium not installed")
class GreenhouseAdapter(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def open(self):
        ctx_ = self.browser.new_context()
        self.addCleanup(ctx_.close)
        filler.install_submit_guard(ctx_)
        pg = ctx_.new_page()
        pg.route("**/*", lambda r: r.fulfill(body=HTML, content_type="text/html; charset=utf-8")
                 if r.request.url == "http://gh.test/form" else r.abort())
        pg.goto("http://gh.test/form", wait_until="domcontentloaded")
        return pg

    # ── discovery ──
    def test_discovery_skips_helpers_reads_options_and_spots_the_async_autocomplete(self):
        pg = self.open()
        fields = {f.key: f for f in Greenhouse().discover_fields(pg)}
        self.assertEqual(list(fields), ["first_name", "last_name", "email", "country", "phone", "candidate-location",
                                        "resume", "question_1", "question_2[]", "question_4", "q5_gender", "question_3[]"])
        self.assertEqual(fields["country"].options, COUNTRIES)
        self.assertEqual((fields["country"].type, fields["country"].widget, fields["country"].required),
                         ("select", "combobox", True))
        self.assertEqual((fields["question_2[]"].type, fields["question_2[]"].options), ("multiselect", HEARD))
        loc = fields["candidate-location"]
        self.assertEqual((loc.widget, loc.type, loc.options), ("autocomplete", "text", []))
        self.assertEqual(fields["first_name"].label, "First Name")
        self.assertEqual((fields["question_3[]"].type, fields["question_3[]"].options), ("checkbox", ["I Agree"]))
        self.assertFalse(filler.blocked_submits(pg))

    # ── filling ──
    def test_a_full_fill_and_read_back(self):
        pg = self.open()
        gh = Greenhouse()
        with tempfile.TemporaryDirectory(prefix="gh test ") as tmp, mock.patch.object(gh, "after_upload_ms", 50):
            fields = gh.discover_fields(pg)
            answers = A.answer_fields(fields, ctx(pdf(tmp)))
            by = {a.label: a for a in answers}
            results = gh.fill(pg, fields, answers)
            recheck = gh.read_back(pg, fields, answers)
        bad = [r for r in results + recheck if not r.ok]
        self.assertEqual(bad, [])
        shown = pg.evaluate("""() => { const s = (id) => { const c = document.getElementById(id).closest('.select__control');
            const sv = c.querySelector('.select__single-value');
            return sv ? sv.innerText : Array.from(c.querySelectorAll('.select__multi-value__label')).map(x => x.innerText); };
          return {country: s('country'), loc: s('candidate-location'), q1: s('question_1'), q2: s('question_2[]'),
                  q5: s('q5_gender'), phone: document.getElementById('phone').value, first: document.getElementById('first_name').value,
                  file: (document.querySelector('.file-chip') || {}).innerText,
                  privacy: document.querySelector('[name="question_3[]"]').checked}; }""")
        self.assertEqual(shown, {"country": "India +91", "loc": "Bangalore, Karnataka, India", "q1": "Yes",
                                 "q2": ["Company Careers Page"], "q5": ["Woman"], "phone": "000 000 0000", "first": "Test",
                                 "file": "resume.pdf", "privacy": True})
        self.assertEqual(by["Country"].value, "India +91")                         # not "British Indian Ocean Territory"
        self.assertEqual(by["Location (City)"].value, "Bangalore")
        self.assertEqual(by["Do you have a family member who works here?"].status, A.FLAGGED)
        self.assertEqual(filler.blocked_submits(pg), 0)

    def test_flagged_dropdowns_are_left_untouched_and_outlined(self):
        pg = self.open()
        gh = Greenhouse()
        with tempfile.TemporaryDirectory() as tmp:
            fields = gh.discover_fields(pg)
            answers = A.answer_fields(fields, ctx(pdf(tmp)))
            gh.fill(pg, fields, answers)
            n = gh.highlight(pg, answers)
        self.assertGreaterEqual(n, 1)
        outlined = pg.evaluate("document.getElementById('question_4').closest('.select__control').style.outline")
        self.assertRegex(outlined, r"3px")
        self.assertIsNone(pg.evaluate("""() => { const sv = document.getElementById('question_4').closest('.select__control')
            .querySelector('.select__single-value'); return sv ? sv.innerText : null; }"""))      # nothing was chosen

    def test_values_that_are_not_offered_are_reported_not_forced(self):
        pg = self.open()
        gh = Greenhouse()
        fields = gh.discover_fields(pg)
        mk = lambda key, label, value: A.Answer(key, label, "select", True, "x", value, "t", "high", A.FILLED)
        with mock.patch.object(filler, "SUGGESTION_WAIT_MS", 500):
            res = gh.fill_some(pg, fields, [mk("question_1", "Eligible", "Maybe"),
                                            mk("candidate-location", "City", "Atlantis"),
                                            mk("question_2[]", "Heard", ["Carrier pigeon"])])
        self.assertEqual([r.ok for r in res], [False, False, False])
        self.assertIn("not offered", res[0].detail)
        self.assertIn("no location suggestions appeared", res[1].detail)      # nothing matches "Atlantis"

    def test_a_city_is_never_picked_from_the_wrong_state_or_country(self):
        pg = self.open()
        gh = Greenhouse()
        fields = gh.discover_fields(pg)
        mk = lambda hints: A.Answer("candidate-location", "City", "text", True, "x", "Bangalore", "t", "high", A.FILLED,
                                    hints=hints)
        res = gh.fill_some(pg, fields, [mk(["Karnataka", "India"])])
        self.assertTrue(res[0].ok, res[0])
        self.assertEqual(pg.evaluate("document.getElementById('candidate-location').closest('.select__control')"
                                     ".querySelector('.select__single-value').innerText"), "Bangalore, Karnataka, India")
        pg2 = self.open()
        res2 = gh.fill_some(pg2, gh.discover_fields(pg2), [mk(["Gujarat"])])           # only Oregon / Karnataka offered
        self.assertFalse(res2[0].ok)
        self.assertIn("no suggestion names 'Bangalore' in Gujarat", res2[0].detail)
        self.assertIsNone(pg2.evaluate("document.getElementById('candidate-location').closest('.select__control')"
                                       ".querySelector('.select__single-value')"))        # nothing was chosen
        pg3 = self.open()
        res3 = gh.fill_some(pg3, gh.discover_fields(pg3), [mk(["USA"])])                # 'USA' is a real alias group
        self.assertTrue(res3[0].ok)                                                     # (the user's own hint decides)

    def test_a_city_autocomplete_that_never_answers_fails_fast(self):
        pg = self.open()
        gh = Greenhouse()
        fields = gh.discover_fields(pg)
        a = A.Answer("candidate-location", "City", "text", True, "x", "ab", "t", "high", A.FILLED)     # < 3 chars: no results
        with mock.patch.object(filler, "SUGGESTION_WAIT_MS", 500):
            res = gh.fill_some(pg, fields, [a])
        self.assertEqual((res[0].ok, res[0].detail), (False, "no location suggestions appeared"))

    # ── matching helpers ──
    def test_option_matching(self):
        self.assertEqual(COUNTRIES[_match(COUNTRIES, "India")], "India +91")
        self.assertEqual(COUNTRIES[_match(COUNTRIES, "india +91")], "India +91")
        self.assertIsNone(_match(["Yes", "No"], "Maybe"))
        self.assertEqual(_match(["Linkedin", "Zscaler Careers Page"], "linkedin"), 0)
        self.assertEqual(_match(["Zscaler Careers Page", "Other"], "Careers"), 0)         # a unique 'contains'
        self.assertIsNone(_match(["Yes, in Canada", "Yes, in Spain"], "Yes"))             # ambiguous: not guessed

    def test_the_phone_country_box_shows_only_the_dial_code_after_choosing(self):
        from jobbot.ats.greenhouse import _same_choice
        self.assertTrue(_same_choice("India +91", "+91"))                  # what the live widget displays
        self.assertTrue(_same_choice("India +91", "India +91"))
        self.assertTrue(_same_choice("India +91", "India"))
        self.assertFalse(_same_choice("India +91", "+44"))
        self.assertFalse(_same_choice("India +91", ""))
        self.assertFalse(_same_choice("Yes", "No"))

    def test_the_phone_input_with_the_iti_class_is_a_real_field_not_a_helper(self):
        pg = self.open()
        keys = [f.key for f in Greenhouse().discover_fields(pg)]
        self.assertIn("phone", keys)                                        # class "iti__tel-input" must not hide it
        self.assertNotIn("iti-0__search-input", keys)

    def test_controls_keyed_by_id_are_found_again_when_reading_back(self):
        pg = self.open()
        gh = Greenhouse()
        fields = gh.discover_fields(pg)
        a = A.Answer("first_name", "First Name", "text", True, "x", "Test", "t", "high", A.FILLED)
        gh.fill_some(pg, fields, [a])
        res = gh.read_back(pg, fields, [a])
        self.assertEqual([(r.ok, r.detail) for r in res], [(True, "")])      # was "control disappeared" on a live form

    def test_phone_comparison_ignores_the_widgets_reformatting(self):
        self.assertTrue(_phone_ok("87707 79761", "+91 8770779761"))
        self.assertTrue(_phone_ok("+91 8770779761", "+91 8770779761"))
        self.assertFalse(_phone_ok("12345", "+91 8770779761"))
        self.assertFalse(_phone_ok("", "+91 8770779761"))


class Adapters(unittest.TestCase):
    def test_registry(self):
        self.assertEqual(adapter_for("greenhouse").name, "greenhouse")
        self.assertEqual(adapter_for("lever").name, "lever")
        with self.assertRaises(KeyError):
            adapter_for("workday")

    def test_confirmation_urls(self):
        gh, lv = adapter_for("greenhouse"), adapter_for("lever")
        self.assertTrue(gh.success_url.search("https://job-boards.greenhouse.io/acme/jobs/1/confirmation"))
        self.assertTrue(lv.success_url.search("https://jobs.lever.co/acme/1/thanks"))
        self.assertFalse(lv.success_url.search("https://jobs.lever.co/acme/1/apply"))

    def test_text_signals(self):
        from jobbot.ats.base import CODE_TEXT, SUCCESS_TEXT
        for ok in ("Thank you for applying!", "Your application has been submitted.", "Application received"):
            self.assertTrue(SUCCESS_TEXT.search(ok), ok)
        for code in ("Enter the 8-character code sent to your email", "We sent you a security code"):
            self.assertTrue(CODE_TEXT.search(code), code)
        self.assertFalse(SUCCESS_TEXT.search("Submit your application"))


if __name__ == "__main__":
    unittest.main()
