"""M6: did the application really go through? "Submitted" means a confirmation signal was SEEN (plan rule 3).

`classify(Evidence)` is a pure function of what the page showed, so it is tested on saved confirmation and
error pages. `collect(page, form_url)` reads that evidence from a live page.

Signals, strongest first:
  confirmation_url   the URL moved to a confirmation route (/thanks, /confirmation ...)
  confirmation_text  a thank-you / "application received" message
Only those two make `submitted`. The form disappearing (with the URL changed and no error) is NOT enough on its own:
a "Processing..." page looks exactly like that, so it is `needs_review` with signal `form_gone`, for the user to check.
Field errors or a whole-form refusal are `failed`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

SUCCESS_TEXT = re.compile(
    r"thank you for (applying|your application|submitting|your interest)|application (has been |was )?"
    r"(submitted|received|sent)|we('ve| have) received your application|successfully submitted", re.I)
# The employer's site refusing the submission as a whole (not one field): Lever answers an automated browser that
# fails its hCaptcha check with "There was an error verifying your application. Please try again."
FAILURE_TEXT = re.compile(r"error verifying your application|verification (has )?failed|couldn'?t (submit|verify) "
                          r"your application|unable to (submit|verify)|error (submitting|processing) your application|"
                          r"something went wrong", re.I)
CODE_TEXT = re.compile(r"security code|verification code|enter the (\d+|eight|six)[- ]?(character|digit)|"
                       r"code (was |we )?sent to", re.I)

SUBMITTED, FAILED, NEEDS_REVIEW = "submitted", "failed", "needs_review"

_EVIDENCE_JS = """() => {
  const vis = e => e.offsetParent !== null || (e.getClientRects && e.getClientRects().length > 0);
  const inputs = Array.from(document.querySelectorAll('form input, form textarea, form select'))
    .filter(e => e.type !== 'hidden' && vis(e));
  const errors = [];
  document.querySelectorAll('[aria-invalid="true"], [role="alert"], .error, .field-error, .application-error')
    .forEach(e => { const t = (e.innerText || e.getAttribute('aria-errormessage') || '').trim();
                    if (t && vis(e)) errors.push(t.slice(0, 120)); });
  return {url: location.href, text: document.body ? document.body.innerText : '',
          form_visible: inputs.length >= 3, errors: Array.from(new Set(errors)).slice(0, 10)};
}"""


@dataclass
class Evidence:
    url: str
    text: str = ""
    form_visible: bool = False
    field_errors: list[str] = field(default_factory=list)
    form_url: "str | None" = None            # where the form was opened; None = unknown


@dataclass
class Verdict:
    status: str                              # submitted | failed | needs_review
    reason: str
    signal: "str | None" = None              # which signal decided it (confirmation_url / confirmation_text / form_gone)
    errors: list[str] = field(default_factory=list)
    challenge: "str | None" = None           # "security-code" when the employer asks for an emailed code
    excerpt: str = ""                        # the words that decided it, for the audit trail

    @property
    def confirmed(self) -> "bool | None":
        return {SUBMITTED: True, FAILED: False}.get(self.status)

    def to_dict(self) -> dict:
        return {"status": self.status, "reason": self.reason, "signal": self.signal, "errors": self.errors,
                "challenge": self.challenge, "excerpt": self.excerpt}


def _norm_url(url: "str | None") -> str:
    return (url or "").split("#")[0].rstrip("/").lower()


def _excerpt(m: "re.Match | None", text: str) -> str:
    if not m:
        return ""
    return re.sub(r"\s+", " ", text[max(0, m.start() - 20):m.end() + 60]).strip()


def classify(ev: Evidence, success_url: "re.Pattern | None" = None) -> Verdict:
    failure = FAILURE_TEXT.search(ev.text)
    errors = list(ev.field_errors) + ([failure.group(0)] if failure else [])
    url_moved = bool(ev.form_url) and _norm_url(ev.url) != _norm_url(ev.form_url)
    by_url = bool(success_url and success_url.search(ev.url) and (url_moved or not ev.form_url))
    by_text = SUCCESS_TEXT.search(ev.text)

    if (by_url or by_text) and not (errors and ev.form_visible):
        return Verdict(SUBMITTED, "confirmation page seen", "confirmation_url" if by_url else "confirmation_text",
                       excerpt=ev.url if by_url else _excerpt(by_text, ev.text))
    if by_url or by_text:
        return Verdict(NEEDS_REVIEW, "a confirmation message is showing but the form is still there with errors: "
                       "look at the page", errors=errors, excerpt=_excerpt(by_text, ev.text))
    if errors:
        return Verdict(FAILED, "the employer refused the submission" if failure and not ev.field_errors
                       else "validation errors on the form", errors=errors, excerpt=_excerpt(failure, ev.text))
    if CODE_TEXT.search(ev.text):
        return Verdict(NEEDS_REVIEW, "the employer is asking for an emailed security code", challenge="security-code",
                       excerpt=_excerpt(CODE_TEXT.search(ev.text), ev.text))
    if url_moved and not ev.form_visible and ev.text.strip():
        return Verdict(NEEDS_REVIEW, "the form is gone and the page moved on with no error, but nothing says the "
                       "application was received: check the page and your email", "form_gone", excerpt=ev.url)
    return Verdict(NEEDS_REVIEW, "no confirmation seen yet" if ev.form_visible else
                   "the form is gone but the page did not change address or say anything: look at the page")


def collect(page, form_url: "str | None" = None) -> Evidence:
    raw = page.evaluate(_EVIDENCE_JS)
    return Evidence(url=raw["url"], text=raw["text"], form_visible=raw["form_visible"],
                    field_errors=raw["errors"], form_url=form_url)
