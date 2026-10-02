"""The ATS adapter interface (plan M5) and the behaviour every adapter shares.

    open_application(session, job)  -> HTTP status; leaves the form on screen
    discover_fields(page)           -> list[Field]
    fill(page, fields, answers)     -> list[FillResult]   (uploads first, then everything else)
    read_back(page, fields, answers)-> list[FillResult]   (re-reads the page without changing it)
    submit(page)                    -> clicks the form's submit button. NEVER called in a dry run.
    is_confirmed(page)              -> True / False / None ("submitted" / "validation errors" / "unclear")
    challenge(page)                 -> "captcha" | "security-code" | None: something only the user can do
"""
from __future__ import annotations

import re

from jobbot import fields as F
from jobbot import filler
from jobbot import verify as V
from jobbot.verify import CODE_TEXT, FAILURE_TEXT, SUCCESS_TEXT  # noqa: F401  (re-exported: adapters and tests use them here)

SUBMIT_LABEL = re.compile(r"submit( application)?|apply( now)?|send application", re.I)


class ATS:
    name = "generic"
    form_url: "str | None" = None       # where the form was opened: a later different address is evidence
    success_url = re.compile(r"/(thanks|thank-you|confirmation|submitted)(/|\?|$)", re.I)

    # ── opening and reading the form ──
    def open_application(self, session, job) -> int:
        status = session.goto(job.apply_url)
        self.form_url = session.page.url
        if status < 400:
            session.page.wait_for_selector("form", timeout=20000)
            session.human_delay(1.0, 1.5)
        return status

    def discover_fields(self, page) -> list[F.Field]:
        return F.discover_fields(page)

    # ── filling ──
    def fill(self, page, fields: list[F.Field], answers) -> list[filler.FillResult]:
        uploads = [a for a in answers if a.type == "file"]
        rest = [a for a in answers if a.type != "file"]
        results = self.fill_some(page, fields, uploads)
        if any(a.value for a in uploads):
            page.wait_for_timeout(self.after_upload_ms)          # a resume auto-parser may overwrite fields
        results += self.fill_some(page, fields, rest)
        page.wait_for_timeout(1000)
        return results

    after_upload_ms = 4000

    def fill_some(self, page, fields, answers) -> list[filler.FillResult]:
        return filler.fill_fields(page, fields, answers)

    def read_back(self, page, fields, answers) -> list[filler.FillResult]:
        return filler.read_back(page, fields, answers)

    def highlight(self, page, answers) -> int:
        return filler.highlight_flagged(page, answers)

    # ── submitting (never in a dry run) ──
    def submit_button(self, page):
        buttons = page.locator("button[type=submit], input[type=submit]")
        usable = [buttons.nth(i) for i in range(buttons.count())
                  if buttons.nth(i).is_visible() and SUBMIT_LABEL.search(
                      (buttons.nth(i).inner_text() if buttons.nth(i).evaluate("e => e.tagName") == "BUTTON"
                       else buttons.nth(i).get_attribute("value")) or "")]
        return usable[0] if len(usable) == 1 else None

    def submit(self, page) -> None:
        btn = self.submit_button(page)
        if btn is None:
            raise RuntimeError("could not find exactly one visible submit button")
        btn.click(timeout=10000)

    # ── what happened? ──
    def validation_errors(self, page) -> list[str]:
        try:
            return self._validation_errors(page)
        except Exception:                      # the page is gone (the user closed the window): nothing to report
            return []

    def _validation_errors(self, page) -> list[str]:
        found = self._field_errors(page)
        try:
            text = page.evaluate("document.body ? document.body.innerText : ''")
            found += [m.group(0) for m in [FAILURE_TEXT.search(text)] if m]
        except Exception:
            pass
        return found

    def _field_errors(self, page) -> list[str]:
        return list(page.evaluate("""() => {
            const out = [];
            document.querySelectorAll('[aria-invalid="true"], [role="alert"], .error, .field-error, .application-error')
              .forEach(e => { const t = (e.innerText || e.getAttribute('aria-errormessage') || '').trim();
                              if (t && e.offsetParent !== null) out.push(t.slice(0, 120)); });
            return Array.from(new Set(out)).slice(0, 10); }"""))

    def verify(self, page) -> V.Verdict:
        """What the page shows right now, classified by `verify.classify` (M6)."""
        try:
            return V.classify(V.collect(page, self.form_url), self.success_url)
        except Exception:                      # the page is gone (the user closed the window): nothing to judge
            return V.Verdict(V.NEEDS_REVIEW, "the page could not be read (the window may have been closed)")

    def is_confirmed(self, page) -> "bool | None":
        """True: a success signal. False: the form is still there with validation errors. None: unclear."""
        return self.verify(page).confirmed

    def challenge(self, page) -> "str | None":
        if filler.captcha_challenge_visible(page):
            return "captcha"
        try:
            text = page.evaluate("document.body ? document.body.innerText : ''")
        except Exception:
            return None
        return "security-code" if CODE_TEXT.search(text) else None
