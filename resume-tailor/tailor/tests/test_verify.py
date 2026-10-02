"""M6 verification: saved confirmation and error pages are classified correctly. `classify` is tested on page text;
the same fixtures are also loaded into real headless Chromium behind the employer's real URL shapes. The Greenhouse
confirmation and the Lever refusal wording were seen in live runs (2026-10-01/02); the other pages are synthetic."""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

try:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as _p:
        _p.chromium.launch(headless=True).close()
    HAVE_CHROMIUM = True
except Exception:
    HAVE_CHROMIUM = False

from jobbot import verify as V  # noqa: E402
from jobbot.ats import adapter_for  # noqa: E402

FIX = ROOT / "tests" / "fixtures" / "verify"
GH_FORM, GH_THANKS = "https://job-boards.greenhouse.io/acme/jobs/1", "https://job-boards.greenhouse.io/acme/jobs/1/confirmation"
LV_FORM, LV_THANKS = "https://jobs.lever.co/acme/1/apply", "https://jobs.lever.co/acme/1/thanks"


class Classify(unittest.TestCase):
    """Pure: what the page showed -> a verdict."""
    GH = adapter_for("greenhouse").success_url

    def ev(self, text="", url=GH_FORM, form=False, errors=(), form_url=GH_FORM):
        return V.Evidence(url=url, text=text, form_visible=form, field_errors=list(errors), form_url=form_url)

    def test_each_success_signal(self):
        v = V.classify(self.ev("Thank you for applying! Your application has been received."), self.GH)
        self.assertEqual((v.status, v.signal), ("submitted", "confirmation_text"))
        self.assertIn("Thank you for applying", v.excerpt)
        v = V.classify(self.ev("Welcome", url=GH_THANKS), self.GH)
        self.assertEqual((v.status, v.signal), ("submitted", "confirmation_url"))

    def test_the_form_disappearing_is_never_enough_to_say_submitted(self):
        """A "Processing..." page after Submit looks exactly like a form that went away."""
        for text in ("Browse other roles", "Processing..."):
            v = V.classify(self.ev(text, url="https://acme.com/careers"), self.GH)
            self.assertEqual((v.status, v.signal), ("needs_review", "form_gone"), text)
            self.assertIn("check", v.reason)

    def test_validation_errors_and_refusals_are_failed(self):
        v = V.classify(self.ev("Apply", form=True, errors=["Email is required"]), self.GH)
        self.assertEqual((v.status, v.errors), ("failed", ["Email is required"]))
        v = V.classify(self.ev("There was an error verifying your application. Please try again.", form=True), self.GH)
        self.assertEqual(v.status, "failed")
        self.assertIn("refused", v.reason)

    def test_nothing_to_go_on_is_needs_review_never_submitted(self):
        for ev in (self.ev("Apply", form=True),                                     # form untouched
                   self.ev("Loading..."),                                           # form gone, same address
                   self.ev("", url="https://acme.com/careers"),                     # moved, but a blank page
                   self.ev("Enter the 8-character code sent to your email", form=True)):
            self.assertEqual(V.classify(ev, self.GH).status, "needs_review", ev)

    def test_a_security_code_request_is_named(self):
        v = V.classify(self.ev("We emailed you a security code", form=True), self.GH)
        self.assertEqual((v.status, v.challenge), ("needs_review", "security-code"))

    def test_a_confirmation_next_to_a_form_with_errors_is_not_trusted(self):
        v = V.classify(self.ev("Thank you for applying!", form=True, errors=["Email is required"]), self.GH)
        self.assertEqual(v.status, "needs_review")

    def test_a_success_url_that_is_just_the_form_address_does_not_count(self):
        v = V.classify(self.ev("Apply", url=GH_THANKS, form=True, form_url=GH_THANKS), self.GH)
        self.assertEqual(v.status, "needs_review")

    def test_confirmed_maps_to_the_old_tri_state(self):
        self.assertEqual([V.Verdict(s, "").confirmed for s in ("submitted", "failed", "needs_review")],
                         [True, False, None])

    def test_the_patterns_match_the_wording_seen_live(self):
        self.assertTrue(V.SUCCESS_TEXT.search("Thank you for applying!\nYour application has been received."))
        self.assertTrue(V.FAILURE_TEXT.search("✱ There was an error verifying your application. Please try again."))
        self.assertFalse(V.SUCCESS_TEXT.search("Submit your application"))
        self.assertFalse(V.FAILURE_TEXT.search("Thank you for applying!"))


@unittest.skipUnless(HAVE_CHROMIUM, "Playwright Chromium not installed")
class SavedPages(unittest.TestCase):
    """The fixture pages in a real browser, served at the employer's URL shapes (no network)."""

    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def verdict(self, platform, fixture, at, form_url):
        page = self.browser.new_page()
        try:
            page.route("**/*", lambda route: route.fulfill(
                status=200, content_type="text/html; charset=utf-8", body=(FIX / fixture).read_text(encoding="utf-8")))
            page.goto(at)
            adapter = adapter_for(platform)
            adapter.form_url = form_url
            return adapter.verify(page)
        finally:
            page.close()

    def test_greenhouse_confirmation_page(self):
        v = self.verdict("greenhouse", "gh_confirmation.html", GH_THANKS, GH_FORM)
        self.assertEqual((v.status, v.signal), ("submitted", "confirmation_url"))
        v = self.verdict("greenhouse", "gh_confirmation.html", GH_FORM, GH_FORM)          # text alone is enough
        self.assertEqual((v.status, v.signal), ("submitted", "confirmation_text"))

    def test_lever_confirmation_pages(self):
        self.assertEqual(self.verdict("lever", "lever_thanks_text.html", LV_THANKS, LV_FORM).status, "submitted")
        self.assertEqual(self.verdict("lever", "lever_thanks_url_only.html", LV_THANKS, LV_FORM).signal, "confirmation_url")

    def test_the_lever_refusal_is_failed_with_its_reason(self):
        v = self.verdict("lever", "lever_refusal.html", LV_FORM, LV_FORM)
        self.assertEqual(v.status, "failed")
        self.assertTrue(any("error verifying your application" in e.lower() for e in v.errors), v.errors)

    def test_field_errors_are_failed(self):
        v = self.verdict("greenhouse", "gh_field_errors.html", GH_FORM, GH_FORM)
        self.assertEqual(v.status, "failed")
        self.assertIn("Email is required", v.errors)

    def test_an_untouched_form_is_not_a_confirmation(self):
        self.assertEqual(self.verdict("greenhouse", "form_untouched.html", GH_FORM, GH_FORM).status, "needs_review")

    def test_a_security_code_page_is_named_not_submitted(self):
        v = self.verdict("greenhouse", "gh_security_code.html", GH_FORM, GH_FORM)
        self.assertEqual((v.status, v.challenge), ("needs_review", "security-code"))

    def test_blank_and_conflicting_pages_need_review(self):
        self.assertEqual(self.verdict("greenhouse", "blank_page.html", GH_FORM, GH_FORM).status, "needs_review")
        self.assertEqual(self.verdict("greenhouse", "conflicting.html", GH_FORM, GH_FORM).status, "needs_review")

    def test_the_form_gone_and_moved_on_is_flagged_for_review_not_submitted(self):
        v = self.verdict("greenhouse", "form_gone_no_message.html", "https://job-boards.greenhouse.io/acme", GH_FORM)
        self.assertEqual((v.status, v.signal), ("needs_review", "form_gone"))

    def test_a_closed_page_is_needs_review_not_a_crash(self):
        page = self.browser.new_page()
        page.close()
        v = adapter_for("lever").verify(page)
        self.assertEqual(v.status, "needs_review")


if __name__ == "__main__":
    unittest.main()
