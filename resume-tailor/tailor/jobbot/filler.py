"""M4 filler: put the answers into the form. DRY RUN ONLY: there is no submit code in this module.

- `install_submit_guard(context)` makes submitting impossible for the whole browser context: form `submit`
  events, `form.submit()`, `requestSubmit()` and clicks on submit-style buttons are all blocked and counted.
  It is a second layer under "we never click submit".
- `fill_fields` fills text, textarea, select, radio, checkbox and file controls and reads every value back.
- `highlight_flagged` outlines the fields the user still has to answer.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from jobbot.answers import FILLED, FLAGGED, Answer
from jobbot.fields import Field

SUBMIT_GUARD_JS = r"""
(() => {
  if (window.__jobbotGuard) return;
  window.__jobbotGuard = true;
  window.__jobbotBlockedSubmits = 0;
  window.__jobbotBlockedLog = [];
  const count = (what) => { window.__jobbotBlockedSubmits += 1;
    window.__jobbotBlockedLog.push(String(what || '').slice(0, 160)); };
  const describe = (el) => el && el.tagName ? (el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') +
    (el.className && el.className.toString ? '.' + el.className.toString().split(' ').join('.') : '') +
    ' "' + ((el.innerText || el.value || '') + '').trim().slice(0, 40) + '"') : String(el);
  document.addEventListener('submit', (e) => { e.preventDefault(); e.stopImmediatePropagation(); count('submit event on ' + describe(e.target)); }, true);
  const isSubmitControl = (el) => {
    if (!el || !el.closest) return null;
    const b = el.closest('button, input[type=submit], input[type=image], [role=button], a.btn, a[class*=submit]');
    if (!b) return null;
    const type = (b.getAttribute('type') || '').toLowerCase();
    const label = ((b.innerText || b.value || b.getAttribute('aria-label') || '') + '').trim();
    if (type === 'submit' || type === 'image') return b;
    if (b.tagName === 'BUTTON' && type !== 'button' && type !== 'reset' && b.form) return b;
    if (/^(submit|submit application|apply|apply now|send application|finish|complete application)$/i.test(label)) return b;
    return null;
  };
  document.addEventListener('click', (e) => {
    const c = isSubmitControl(e.target);
    if (c) { e.preventDefault(); e.stopImmediatePropagation(); count('click on ' + describe(c)); }
  }, true);
  const where = () => (new Error().stack || '').split(String.fromCharCode(10)).slice(2, 4).join(' | ');
  HTMLFormElement.prototype.submit = function () { count('form.submit() ' + where()); };
  HTMLFormElement.prototype.requestSubmit = function () { count('requestSubmit() ' + where()); };
})();
"""


def install_submit_guard(context_or_page) -> None:
    """Call before navigating. Applies to every page of a context (or one page)."""
    context_or_page.add_init_script(SUBMIT_GUARD_JS)


def blocked_submits(page) -> int:
    return int(page.evaluate("window.__jobbotBlockedSubmits || 0"))


def blocked_details(page) -> list[str]:
    return list(page.evaluate("window.__jobbotBlockedLog || []"))


_CAPTCHA_JS = r"""() => {
  const vendors = /hcaptcha\.com|google\.com\/recaptcha|recaptcha\.net|challenges\.cloudflare\.com|arkoselabs|funcaptcha|geetest/i;
  return Array.from(document.querySelectorAll('iframe')).some(f => {
    if (!vendors.test(f.src || '')) return false;
    if (f.closest('.grecaptcha-badge')) return false;                 // reCAPTCHA v3 badge: not a challenge
    const s = getComputedStyle(f), r = f.getBoundingClientRect();
    if (s.display === 'none' || s.visibility === 'hidden' || Number(s.opacity) === 0) return false;
    const box = f.parentElement ? getComputedStyle(f.parentElement) : null;
    if (box && (box.display === 'none' || box.visibility === 'hidden')) return false;
    return r.width >= 150 && r.height >= 50;                           // a checkbox widget or a challenge popup
  });
}"""


def captcha_challenge_visible(page) -> bool:
    """True only when a CAPTCHA the user must act on is on screen (a visible checkbox widget or challenge
    popup). A CAPTCHA that merely *might* appear (an invisible one, a hidden iframe, a v3 badge) is not a
    reason to stop: plan.md rule for M5."""
    return bool(page.evaluate(_CAPTCHA_JS))


@dataclass
class FillResult:
    key: str
    label: str
    ok: bool
    detail: str = ""


SUGGESTION_WAIT_MS = 6000    # how long to wait for the location widget's suggestions
TIMEOUT_MS = 5000          # every action: a stuck control must fail quickly, not stall the run


def _esc(key: str) -> str:
    return key.replace("\\", "\\\\").replace('"', '\\"')


def _loc(page, key: str):
    """Controls are keyed by name (Lever) or id (Greenhouse's React form); accept either."""
    k = _esc(key)
    return page.locator(f'[name="{k}"], [id="{k}"]')


def _choice_group(page, key: str, kind: str):
    """The radio / checkbox inputs of a group (by name, or by id for a lone one). Only real inputs: a wrapper
    element can share the id."""
    k = _esc(key)
    return page.locator(f'input[type="{kind}"][name="{k}"], input[type="{kind}"][id="{k}"]')


def _norm(s: str) -> str:
    return " ".join(str(s).split()).casefold()


def _is_location_widget(loc) -> bool:
    """Lever's "Current location" box: an autocomplete that wipes typed text unless a suggestion is picked."""
    return loc.first.get_attribute("data-qa") == "location-input"


def _fill_location_widget(page, f: Field, loc, value: str) -> FillResult:
    from jobbot.answers import _city_names
    parts = [x.strip() for x in value.split(",") if x.strip()]
    city, state = (parts + ["", ""])[0].casefold(), (parts + ["", ""])[1].casefold()
    names = _city_names(city) if city else set()
    options = page.locator(".dropdown-location")
    # The suggestion service is picky about longer queries: try "City, State", then just the city.
    queries = list(dict.fromkeys(q for q in (", ".join(parts[:2]), parts[0] if parts else "") if q))
    texts: list[str] = []
    for query in queries:
        loc.first.click(timeout=TIMEOUT_MS)
        loc.first.fill("", timeout=TIMEOUT_MS)
        loc.first.press_sequentially(query, delay=40)
        # The list is refreshed while/after typing, so a first read can show results for "B" or "Ba". Keep
        # re-reading until a suggestion naming the city (and state) appears, or the time is up.
        deadline = time.time() + SUGGESTION_WAIT_MS / 1000
        pick = None
        while time.time() < deadline:
            texts = [t.strip() for t in options.all_inner_texts()]
            # With a state given, a suggestion must name it: "Bangalore, Oregon" is not the user's Bangalore.
            pick = next((i for i, t in enumerate(texts)
                         if any(n in t.casefold() for n in names) and (not state or state in t.casefold())), None)
            if pick is not None:
                break
            page.wait_for_timeout(300)
        if pick is not None:
            options.nth(pick).click(timeout=TIMEOUT_MS)
            chosen = page.locator('[name="selectedLocation"]').evaluate("e => e.value")
            ok = bool(chosen)
            return FillResult(f.key, f.label, ok, f"picked suggestion: {texts[pick]}" if ok
                              else "suggestion click did not register")
    if not texts:
        return FillResult(f.key, f.label, False, "no location suggestions appeared")
    return FillResult(f.key, f.label, False, f"no suggestion matches {city!r}: {texts[:3]}")


def _file_shown(page, key: str, name: str, wait_ms: int = 800) -> bool:
    """Is this upload in place? Lever keeps the <input type=file> (its value ends with the file name); Greenhouse
    REMOVES the input after an upload and shows the file name instead. Either counts."""
    try:
        loc = _loc(page, key)
        if loc.count():
            got = loc.first.input_value(timeout=1500)
            if got.replace("\\", "/").rsplit("/", 1)[-1] == name:
                return True
    except Exception:
        pass
    try:
        page.get_by_text(name, exact=False).first.wait_for(state="attached", timeout=wait_ms)
        return True
    except Exception:
        return False


def _fill_one(page, f: Field, a: Answer) -> FillResult:
    loc = _loc(page, f.key)
    if loc.count() == 0:
        loc = page.locator(f'[id="{_esc(f.key)}"]')
    if loc.count() == 0:
        return FillResult(f.key, f.label, False, "control not found")
    t, v = f.type, a.value
    if t == "file":
        path = Path(str(v))
        if not path.exists():
            return FillResult(f.key, f.label, False, f"file not found: {path}")
        loc.first.set_input_files(str(path), timeout=TIMEOUT_MS)
        ok = _file_shown(page, f.key, path.name, wait_ms=6000)
        return FillResult(f.key, f.label, ok, "" if ok else f"{path.name} is not shown on the page after the upload")
    if t in ("radio", "checkbox"):
        wanted = v if isinstance(v, list) else [v]
        idx = []
        for w in wanted:
            hit = next((i for i, o in enumerate(f.options) if _norm(o) == _norm(w)), None)
            if hit is None:
                return FillResult(f.key, f.label, False, f"option {w!r} not on the form")
            idx.append(hit)
        group = _choice_group(page, f.key, t)
        for i in idx:
            group.nth(i).check(timeout=TIMEOUT_MS)
        ok = all(group.nth(i).is_checked() for i in idx)
        return FillResult(f.key, f.label, ok, "" if ok else "not checked after check()")
    if t in ("select", "multiselect"):
        wanted = v if isinstance(v, list) else [v]
        known = {_norm(o): o for o in f.options}
        missing = [w for w in wanted if _norm(w) not in known]
        if missing:                                             # fail fast instead of waiting for the option
            return FillResult(f.key, f.label, False, f"option(s) {missing!r} not in the dropdown")
        labels = [known[_norm(w)] for w in wanted]
        loc.first.select_option(label=labels if t == "multiselect" else labels[0], timeout=TIMEOUT_MS)
        chosen = loc.first.evaluate("e => Array.from(e.selectedOptions).map(o => o.textContent.trim())")
        ok = {_norm(c) for c in chosen} >= {_norm(w) for w in wanted}
        return FillResult(f.key, f.label, ok, "" if ok else f"selected {chosen!r}")
    if t == "text" and _is_location_widget(loc):
        return _fill_location_widget(page, f, loc, str(v))
    loc.first.fill(str(v), timeout=TIMEOUT_MS)
    got = loc.first.input_value()
    ok = got == str(v)
    return FillResult(f.key, f.label, ok, "" if ok else f"read back {got!r}")


def fill_fields(page, fields: list[Field], answers: list[Answer]) -> list[FillResult]:
    """Fill every `filled` answer; never touches anything that could submit. Returns one result per answer."""
    by_key = {f.key: f for f in fields}
    results: list[FillResult] = []
    for a in answers:
        f = by_key.get(a.key)
        if a.status != FILLED or f is None:
            continue
        try:
            results.append(_fill_one(page, f, a))
        except Exception as e:                  # one bad control must not stop the rest
            results.append(FillResult(a.key, a.label, False, f"{type(e).__name__}: {str(e).splitlines()[0][:120]}"))
    return results


def read_back(page, fields: list[Field], answers: list[Answer]) -> list[FillResult]:
    """Re-read the page WITHOUT changing it, to catch anything that overwrote our values after filling
    (for example a resume auto-parser). One result per filled answer."""
    by_key = {f.key: f for f in fields}
    out: list[FillResult] = []
    for a in answers:
        f = by_key.get(a.key)
        if a.status != FILLED or f is None:
            continue
        try:
            loc = _loc(page, f.key)
            if f.type == "file":
                ok = _file_shown(page, f.key, Path(str(a.value)).name)
                out.append(FillResult(a.key, a.label, ok, "" if ok else "the uploaded file is no longer shown"))
                continue
            if loc.count() == 0:
                out.append(FillResult(a.key, a.label, False, "control disappeared"))
                continue
            if f.type in ("radio", "checkbox"):
                wanted = a.value if isinstance(a.value, list) else [a.value]
                idx = [next((i for i, o in enumerate(f.options) if _norm(o) == _norm(w)), -1) for w in wanted]
                group = _choice_group(page, f.key, f.type)
                ok = all(i >= 0 and group.nth(i).is_checked() for i in idx)
            elif f.type in ("select", "multiselect"):
                chosen = loc.first.evaluate("e => Array.from(e.selectedOptions).map(o => o.textContent.trim())")
                wanted = a.value if isinstance(a.value, list) else [a.value]
                ok = {_norm(c) for c in chosen} >= {_norm(w) for w in wanted}
            elif f.type == "text" and _is_location_widget(loc):
                ok = bool(page.locator('[name="selectedLocation"]').evaluate("e => e.value")) and bool(loc.first.input_value())
            else:
                ok = loc.first.input_value() == str(a.value)
            out.append(FillResult(a.key, a.label, ok, "" if ok else "value changed after filling"))
        except Exception as e:
            out.append(FillResult(a.key, a.label, False, f"{type(e).__name__}"))
    return out


_HIGHLIGHT_JS = """
([key, text]) => {
  const q = key.replace(/"/g, '\\\\"');
  const els = document.querySelectorAll('[name="' + q + '"], [id="' + q + '"]');      // forms key controls by name or id
  els.forEach(el => {
    const box = el.closest('.select__control') || el.closest('li, .field, .form-group, fieldset, div') || el;
    box.style.outline = '3px solid #e67e22'; box.style.outlineOffset = '4px';
    box.title = 'YOU: ' + text;
    el.title = 'YOU: ' + text;
  });
  return els.length;
}
"""


def highlight_flagged(page, answers: list[Answer]) -> int:
    """Outline the fields the user must answer himself, with the reason as a tooltip. Returns how many."""
    n = 0
    for a in answers:
        if a.status == FLAGGED:
            n += 1 if page.evaluate(_HIGHLIGHT_JS, [a.key, a.note or "needs your answer"]) else 0
    return n
