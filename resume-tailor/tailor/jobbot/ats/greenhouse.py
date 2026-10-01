"""Greenhouse (job-boards.greenhouse.io; the older boards.greenhouse.io form is plain HTML and also works with
the generic discovery). The newer React form has custom dropdowns (React-Select):

  - a dropdown is `input[role=combobox]#<key>`; clicking it opens `.select__menu` with `.select__option` rows,
    typing filters them, and the choice shows in `.select__single-value` / `.select__multi-value__label`;
  - the phone country list is "India +91": dial codes are ignored when matching;
  - "Location (City)" is an async autocomplete: its menu is empty until you type, then you pick a suggestion;
  - the phone box is intl-tel-input, which may reformat the number, so it is compared by digits.

Nothing here clicks submit; base.ATS.submit does, and only in a real (assist/auto) run.
"""
from __future__ import annotations

import re

from jobbot import answers as A
from jobbot import filler
from jobbot.ats.base import ATS
from jobbot.fields import Field
from jobbot.answers import FILLED
from jobbot.filler import FillResult, TIMEOUT_MS

MENU = ".select__menu .select__option"
_DIAL = re.compile(r"\s*\(?\+\d[\d\s-]*\)?\s*$")

_SHOWN_JS = """(id) => {
  const e = document.getElementById(id); if (!e) return null;
  const c = e.closest('.select__control') || e.parentElement;
  const sv = c.querySelector('.select__single-value');
  return {single: sv ? sv.innerText.trim() : null,
          multi: Array.from(c.querySelectorAll('.select__multi-value__label')).map(x => x.innerText.trim())};
}"""


def _norm(s: str) -> str:
    return " ".join(str(s).split()).casefold()


def _sel(key: str) -> str:
    return "[id='" + key.replace("'", "\\'") + "']"


def _digits(s: str) -> str:
    return re.sub(r"\D", "", str(s))


def _match(options: list[str], wanted: str) -> "int | None":
    """Index of the option equal to `wanted` (dial codes ignored), else the one option containing it."""
    w = _norm(wanted)
    for i, o in enumerate(options):
        if _norm(o) == w or _norm(_DIAL.sub("", o)) == w:
            return i
    hits = [i for i, o in enumerate(options) if w and w in _norm(o)]
    return hits[0] if len(hits) == 1 else None


def _open(page, key: str):
    loc = page.locator(_sel(key)).first
    loc.scroll_into_view_if_needed(timeout=TIMEOUT_MS)
    loc.click(timeout=TIMEOUT_MS)
    page.wait_for_timeout(250)
    return loc


def _menu_texts(page) -> list[str]:
    return [t.strip() for t in page.locator(MENU).all_inner_texts()]


def read_options(page, key: str) -> list[str]:
    """Open a dropdown, read its options, close it. Empty for an async autocomplete."""
    _open(page, key)
    page.wait_for_timeout(200)
    texts = _menu_texts(page)
    page.keyboard.press("Escape")
    return texts


def shown(page, key: str) -> dict:
    return page.evaluate(_SHOWN_JS, key) or {"single": None, "multi": []}


def _choose(page, key: str, wanted: str) -> tuple[bool, str]:
    _open(page, key)
    opts = _menu_texts(page)
    idx = _match(opts, wanted)
    if idx is None and len(opts) > 8:                      # a long list: type to filter it
        page.keyboard.type(wanted, delay=30)
        page.wait_for_timeout(600)
        opts = _menu_texts(page)
        idx = _match(opts, wanted)
    if idx is None:
        page.keyboard.press("Escape")
        return False, f"option {wanted!r} not offered (menu has {opts[:5]})"
    page.locator(MENU).nth(idx).click(timeout=TIMEOUT_MS)
    return True, opts[idx]


def _names_place(option: str, hint: str) -> bool:
    """Does the suggestion mention this state/country (India = IND)?"""
    h, o = _norm(hint), _norm(option)
    aliases = next((a for a in A._COUNTRY_ALIASES if h in a), {h})
    return any(re.search(rf"(?<![a-z]){re.escape(x)}(?![a-z])", o) for x in aliases)


def _autocomplete(page, key: str, text: str, hints: "list[str] | None" = None) -> tuple[bool, str]:
    """City autocomplete: type, wait for suggestions, pick the one naming the city (Bangalore = Bengaluru) AND the
    user's state or country. If only a different place with that name is offered, fail: never guess."""
    loc = _open(page, key)
    loc.press_sequentially(text, delay=40)
    try:
        page.wait_for_selector(MENU, timeout=filler.SUGGESTION_WAIT_MS)
    except Exception:
        page.keyboard.press("Escape")
        return False, "no location suggestions appeared"
    opts = _menu_texts(page)
    city = text.split(",")[0].strip()
    names = A._city_names(city)
    named = [i for i, o in enumerate(opts) if any(re.search(rf"(?<![a-z]){re.escape(n)}(?![a-z])", _norm(o))
                                                  for n in names)]
    pick = None
    if hints:
        pick = next((i for i in named if any(_names_place(opts[i], h) for h in hints)), None)
    elif named:
        pick = named[0]
    if pick is None:
        page.keyboard.press("Escape")
        return False, (f"no suggestion names {city!r} in {', '.join(hints)}: {opts[:3]}" if hints
                       else f"no suggestion names {city!r}: {opts[:3]}")
    page.locator(MENU).nth(pick).click(timeout=TIMEOUT_MS)
    return True, opts[pick]


def _same_choice(picked: str, shown_text: str) -> bool:
    """Is the control showing what we picked? The phone-country box shows only '+91' for 'India +91'."""
    p, s = _norm(picked), _norm(shown_text)
    return bool(s) and (p == s or _norm(_DIAL.sub("", picked)) == _norm(_DIAL.sub("", shown_text))
                        or (s.startswith("+") and p.endswith(s)))


def _phone_ok(got: str, want: str) -> bool:
    g, w = _digits(got), _digits(want)
    return bool(g) and (g == w or w.endswith(g))


class Greenhouse(ATS):
    name = "greenhouse"
    success_url = re.compile(r"/(confirmation|thank[-_]?you|thanks)(/|\?|$)", re.I)

    def discover_fields(self, page) -> list[Field]:
        fields = super().discover_fields(page)
        for f in fields:
            if f.widget != "combobox":
                continue
            try:
                f.options = read_options(page, f.key)
            except Exception:
                f.options = []
            if not f.options:                              # empty until you type: an async autocomplete
                f.widget, f.type = "autocomplete", "text"
        return fields

    def fill_some(self, page, fields, answers) -> list[FillResult]:
        by_key = {f.key: f for f in fields}
        results: list[FillResult] = []
        plain = []
        for a in answers:
            f = by_key.get(a.key)
            if a.status != FILLED or f is None:
                continue
            try:
                if f.widget in ("combobox", "autocomplete"):
                    results.append(self._fill_combo(page, f, a))
                elif f.type == "tel":
                    results.append(self._fill_phone(page, f, a))
                else:
                    plain.append(a)
            except Exception as e:                         # one stubborn control must not stop the rest
                results.append(FillResult(a.key, a.label, False, f"{type(e).__name__}: {str(e).splitlines()[0][:100]}"))
        return results + filler.fill_fields(page, fields, plain)

    def _fill_combo(self, page, f: Field, a) -> FillResult:
        values = a.value if isinstance(a.value, list) else [a.value]
        if f.widget == "autocomplete":
            ok, detail = _autocomplete(page, f.key, str(values[0]), getattr(a, "hints", None))
            return FillResult(f.key, f.label, ok, f"picked suggestion: {detail}" if ok else detail)
        picked: list[str] = []
        for v in values:
            ok, detail = _choose(page, f.key, str(v))
            if not ok:
                return FillResult(f.key, f.label, False, detail)
            picked.append(detail)
        got = shown(page, f.key)
        shown_values = got["multi"] + ([got["single"]] if got["single"] else [])     # chips or a single value
        ok = all(any(_same_choice(p, s) for s in shown_values) for p in picked)
        return FillResult(f.key, f.label, ok, "" if ok else f"page shows {shown_values!r}, picked {picked!r}")

    def _fill_phone(self, page, f: Field, a) -> FillResult:
        loc = page.locator(_sel(f.key)).first
        loc.fill(str(a.value), timeout=TIMEOUT_MS)
        got = loc.input_value()
        ok = _phone_ok(got, str(a.value))
        return FillResult(f.key, f.label, ok, "" if ok else f"read back {got!r}")

    def read_back(self, page, fields, answers) -> list[FillResult]:
        by_key = {f.key: f for f in fields}
        out: list[FillResult] = []
        plain = []
        for a in answers:
            f = by_key.get(a.key)
            if a.status != FILLED or f is None:
                continue
            if f.widget in ("combobox", "autocomplete"):
                got = shown(page, f.key)
                values = a.value if isinstance(a.value, list) else [a.value]
                if f.widget == "autocomplete":
                    ok = bool(got["single"])
                else:
                    seen = got["multi"] + ([got["single"]] if got["single"] else [])
                    ok = all(any(_same_choice(v, x) for x in seen) for v in values)
                out.append(FillResult(a.key, a.label, ok, "" if ok else "value changed after filling"))
            elif f.type == "tel":
                ok = _phone_ok(page.locator(_sel(f.key)).first.input_value(), str(a.value))
                out.append(FillResult(a.key, a.label, ok, "" if ok else "value changed after filling"))
            else:
                plain.append(a)
        return out + filler.read_back(page, fields, plain)

    def highlight(self, page, answers) -> int:
        return filler.highlight_flagged(page, answers)
