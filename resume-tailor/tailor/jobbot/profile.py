"""Load and validate profile.yaml (the user's own form answers).

Nothing is inferred or defaulted: a value is either supplied by the user or is
the placeholder TODO. Sensitive answers come only from here (plan section 3.2).
"""
from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml

TODO = "TODO"

# Every key that must exist (values may still be TODO until a form needs them).
REQUIRED_KEYS: dict[str, list[str]] = {
    "personal": ["first_name", "last_name", "email", "phone", "current_location",
                 "linkedin", "github", "portfolio"],
    "employment": ["current_company", "current_title", "fulltime_start",
                   "zintlr_internship_start", "zintlr_internship_end", "serving_notice",
                   "notice_period_days", "last_working_day", "earliest_start_date"],
    "compensation": ["current_ctc", "expected_ctc_inr_lpa"],
    "work_authorization": ["default_other"],
    "preferences": ["willing_to_relocate", "work_modes"],
    "education": ["degree", "institution", "institution_location", "affiliation", "start_year",
                  "graduation", "cgpa", "percentage", "twelfth", "tenth"],
    "eeo": ["gender", "ethnicity", "ethnicity_broad", "veteran_status", "disability_status",
            "if_no_matching_option"],
    "custom_answers": [],
}

_AUTH_KEYS = ("authorized", "needs_sponsorship")


class ProfileError(Exception):
    """profile.yaml is malformed (missing sections/keys, wrong types)."""


class ProfileIncomplete(ProfileError):
    """A field the current form needs still holds TODO."""

    def __init__(self, paths: list[str]):
        self.paths = paths
        super().__init__("profile.yaml still has TODO in: " + ", ".join(paths))


def _get(data: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        if not isinstance(data, dict) or part not in data:
            raise KeyError(dotted)
        data = data[part]
    return data


def _contains_todo(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip() == TODO
    if isinstance(value, dict):
        return any(_contains_todo(v) for v in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_todo(v) for v in value)
    return False


def find_todos(profile: dict, prefix: str = "") -> list[str]:
    """Dotted paths of every leaf that is still TODO."""
    out: list[str] = []
    if isinstance(profile, dict):
        for k, v in profile.items():
            out += find_todos(v, f"{prefix}{k}.")
    elif isinstance(profile, (list, tuple)):
        if any(_contains_todo(v) for v in profile):
            out.append(prefix.rstrip("."))
    elif _contains_todo(profile):
        out.append(prefix.rstrip("."))
    return out


CTC_REPORT_AS = ("fixed", "fixed_plus_variable", "stated_total")
WORK_MODES = ("onsite", "hybrid", "remote")


def _is_year(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool) and 1900 <= v <= 2100


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _ym(v: Any) -> tuple[int, int]:
    """'YYYY-MM' (also accepts a date or 'YYYY-MM-DD') -> (year, month)."""
    if isinstance(v, (date, datetime)):
        return v.year, v.month
    m = re.fullmatch(r"(\d{4})-(\d{2})(?:-\d{2})?", str(v).strip())
    if not m or not 1 <= int(m.group(2)) <= 12:
        raise ValueError(v)
    return int(m.group(1)), int(m.group(2))


def _day(v: Any) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return date.fromisoformat(str(v).strip())


def _valid(parse, v: Any) -> bool:
    try:
        parse(v)
        return True
    except (ValueError, TypeError):
        return False


def validate(profile: Any) -> dict:
    """Structural check only. TODO values are allowed here; see require()."""
    if not isinstance(profile, dict):
        raise ProfileError("profile must be a YAML mapping")
    problems: list[str] = []
    for section, keys in REQUIRED_KEYS.items():
        body = profile.get(section)
        if not isinstance(body, dict):
            problems.append(f"missing section: {section}")
            continue
        problems += [f"missing key: {section}.{k}" for k in keys if k not in body]

    wa = profile.get("work_authorization")
    if isinstance(wa, dict):
        for country, entry in wa.items():
            if not isinstance(entry, dict) or any(k not in entry for k in _AUTH_KEYS):
                problems.append(
                    f"work_authorization.{country} needs {' and '.join(_AUTH_KEYS)}")
                continue
            for k in _AUTH_KEYS:
                if entry[k] != TODO and not isinstance(entry[k], bool):
                    problems.append(
                        f"work_authorization.{country}.{k} must be true/false or TODO")
    emp = profile.get("employment")
    if isinstance(emp, dict):
        for k in ("fulltime_start", "zintlr_internship_start", "zintlr_internship_end"):
            if k in emp and emp[k] != TODO and not _valid(_ym, emp[k]):
                problems.append(f"employment.{k} must be YYYY-MM or TODO")
        for k in ("last_working_day", "earliest_start_date"):
            if k in emp and emp[k] != TODO and not _valid(_day, emp[k]):
                problems.append(f"employment.{k} must be YYYY-MM-DD or TODO")
        if "serving_notice" in emp and emp["serving_notice"] != TODO \
                and not isinstance(emp["serving_notice"], bool):
            problems.append("employment.serving_notice must be true/false or TODO")
        nd = emp.get("notice_period_days")
        if nd is not None and nd != TODO and (isinstance(nd, bool) or not isinstance(nd, int)):
            problems.append("employment.notice_period_days must be a whole number or TODO")
    eeo = profile.get("eeo")
    if isinstance(eeo, dict) and eeo.get("if_no_matching_option", TODO) not in (TODO, "decline", "flag"):
        problems.append("eeo.if_no_matching_option must be 'decline', 'flag' or TODO")
    edu = profile.get("education")
    if isinstance(edu, dict):
        if "graduation" in edu and edu["graduation"] != TODO and not _valid(_ym, edu["graduation"]):
            problems.append("education.graduation must be YYYY-MM or TODO")
        for k in ("start_year",):
            if k in edu and edu[k] != TODO and not _is_year(edu[k]):
                problems.append(f"education.{k} must be a year or TODO")
        if "percentage" in edu and edu["percentage"] != TODO and not _is_number(edu["percentage"]):
            problems.append("education.percentage must be a number or TODO")
        for level in ("twelfth", "tenth"):
            entry = edu.get(level)
            if entry is None:
                continue
            if not isinstance(entry, dict) or any(k not in entry for k in ("board", "year", "percentage")):
                problems.append(f"education.{level} needs board, year and percentage")
                continue
            if entry["year"] != TODO and not _is_year(entry["year"]):
                problems.append(f"education.{level}.year must be a year or TODO")
            if entry["percentage"] != TODO and not _is_number(entry["percentage"]):
                problems.append(f"education.{level}.percentage must be a number or TODO")
            if entry["board"] != TODO and (not isinstance(entry["board"], str) or entry["board"].strip().isdigit()):
                problems.append(f"education.{level}.board must be a board name (e.g. CBSE) or TODO")
    prefs = profile.get("preferences")
    if isinstance(prefs, dict):
        if "willing_to_relocate" in prefs and prefs["willing_to_relocate"] != TODO \
                and not isinstance(prefs["willing_to_relocate"], bool):
            problems.append("preferences.willing_to_relocate must be true/false or TODO")
        modes = prefs.get("work_modes", [])
        if not isinstance(modes, list) or any(m != TODO and m not in WORK_MODES for m in modes):
            problems.append(f"preferences.work_modes must be a list drawn from {WORK_MODES}")
    comp = profile.get("compensation")
    if isinstance(comp, dict):
        cur = comp.get("current_ctc")
        if isinstance(cur, dict):
            for k in ("fixed_lpa", "variable_lpa", "stated_total_lpa"):
                if k not in cur:
                    problems.append(f"missing key: compensation.current_ctc.{k}")
                elif cur[k] != TODO and not _is_number(cur[k]):
                    problems.append(f"compensation.current_ctc.{k} must be a number or TODO")
            if cur.get("report_as", TODO) not in (TODO, *CTC_REPORT_AS):
                problems.append(f"compensation.current_ctc.report_as must be one of {CTC_REPORT_AS} or TODO")
        elif cur is not None:
            problems.append("compensation.current_ctc must be a mapping")
        exp = comp.get("expected_ctc_inr_lpa")
        if exp is not None and exp != TODO and not _is_number(exp):
            problems.append("compensation.expected_ctc_inr_lpa must be a number or TODO")
        fx = comp.get("expected_fixed_ctc_lpa")        # optional: absent / TODO means "ask the user"
        if fx is not None and fx != TODO and not (_is_number(fx) and fx > 0):
            problems.append("compensation.expected_fixed_ctc_lpa must be a positive number or TODO")
    sy = profile.get("skill_years")                    # optional section: tool -> years of the user's own
    if sy is not None:
        if not isinstance(sy, dict):
            problems.append("skill_years must be a mapping of tool -> years")
        else:
            for tool, years in sy.items():
                if years != TODO and not (_is_number(years) and 0 <= years <= 60):
                    problems.append(f"skill_years.{tool} must be a number of years (0-60) or TODO")
    if problems:
        raise ProfileError("; ".join(problems))
    return profile


def load_profile(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return validate(yaml.safe_load(f))


def require(profile: dict, paths: list[str]) -> None:
    """Refuse to continue if any needed field is missing or still TODO."""
    bad: list[str] = []
    for p in paths:
        try:
            if _contains_todo(_get(profile, p)):
                bad.append(p)
        except KeyError:
            bad.append(p)
    if bad:
        raise ProfileIncomplete(bad)


def work_auth_for(profile: dict, country: str) -> dict | None:
    """{'authorized': bool, 'needs_sponsorship': bool} for a country, else None.

    None means "flag it, don't guess". A country entry that still has TODO is
    treated as unknown. default_other is used only when the user has filled it in.
    """
    wa = profile.get("work_authorization", {})
    wanted = country.strip().casefold()
    for name, entry in wa.items():
        if name != "default_other" and name.casefold() == wanted:
            return None if _contains_todo(entry) else dict(entry)
    fallback = wa.get("default_other")
    if fallback is None or _contains_todo(fallback):
        return None
    return dict(fallback)


# ─── Employment answers, computed at fill time ───────────────────────────────
# Every function takes `today` so it is testable. None means "flag it, don't guess".

def _first_of(ym: tuple[int, int]) -> date:
    return date(ym[0], ym[1], 1)


def _completed_months(start: date, end: date) -> int:
    months = (end.year - start.year) * 12 + end.month - start.month
    if end.day < start.day:
        months -= 1
    return max(months, 0)


def _emp(profile: dict, keys: list[str]) -> dict:
    require(profile, [f"employment.{k}" for k in keys])
    return profile["employment"]


def experience_months(profile: dict, today: date | None = None, *,
                      include_internship: bool = True) -> int:
    """Completed months of experience. Full-time runs from the 1st of fulltime_start to
    today, but stops at the last working day while serving notice. The Zintlr internship
    runs from the 1st of its start month to the end of its end month."""
    today = today or date.today()
    e = _emp(profile, ["fulltime_start", "serving_notice"])
    end = today
    if e["serving_notice"]:
        end = min(today, _day(_emp(profile, ["last_working_day"])["last_working_day"]))
    months = _completed_months(_first_of(_ym(e["fulltime_start"])), end)
    if include_internship:
        e = _emp(profile, ["zintlr_internship_start", "zintlr_internship_end"])
        ey, em = _ym(e["zintlr_internship_end"])
        after_end = _first_of((ey + (em == 12), em % 12 + 1))
        months += _completed_months(_first_of(_ym(e["zintlr_internship_start"])), after_end)
    return months


def internship_months(profile: dict) -> int:
    """For forms that ask specifically about internship experience."""
    e = _emp(profile, ["zintlr_internship_start", "zintlr_internship_end"])
    ey, em = _ym(e["zintlr_internship_end"])
    return _completed_months(_first_of(_ym(e["zintlr_internship_start"])),
                             _first_of((ey + (em == 12), em % 12 + 1)))


def experience_years(profile: dict, today: date | None = None, *,
                     include_internship: bool = True) -> float:
    """Years with one decimal, e.g. 1.7."""
    return round(experience_months(profile, today, include_internship=include_internship) / 12, 1)


def experience_years_months(profile: dict, today: date | None = None, *,
                            include_internship: bool = True) -> tuple[int, int]:
    return divmod(experience_months(profile, today, include_internship=include_internship), 12)


_NUM = r"(\d+(?:\.\d+)?)"
_RANGE = re.compile(_NUM + r"\s*(?:-|–|to)\s*" + _NUM)
_LESS = re.compile(r"(?:less than|under|below|<)\s*" + _NUM)
_MORE = re.compile(_NUM + r"\s*\+|(?:more than|over|above|>)\s*" + _NUM + r"|" + _NUM + r"\s*(?:years?\s*)?or more")
_SINGLE = re.compile(_NUM)


def _year_range(option: str) -> tuple[float, float | None] | None:
    """Parse a dropdown option like '1-3 years', '3+', 'Less than 1', '2 years' -> (lo, hi).
    hi None = open-ended. A bare '2 years' means 2 <= years < 3."""
    s = option.lower()
    if m := _RANGE.search(s):
        return float(m.group(1)), float(m.group(2))
    if m := _LESS.search(s):
        return 0.0, float(m.group(1))
    if m := _MORE.search(s):
        return float(next(g for g in m.groups() if g)), None
    if m := _SINGLE.search(s):
        return float(m.group(1)), float(m.group(1)) + 1
    return None


def experience_for_options(profile: dict, options: list[str], today: date | None = None, *,
                           include_internship: bool = True) -> str | None:
    """The dropdown option whose range contains the computed years, or None (flag)
    when no option or more than one option matches."""
    return option_for_years(options, experience_years(profile, today, include_internship=include_internship))


def option_for_years(options: list[str], years: float) -> str | None:
    """The dropdown option ('1-3 years', '3+', 'Less than 1') whose range contains `years`, else None."""
    hits: list[tuple[str, tuple[float, float | None]]] = []
    for opt in options:
        r = _year_range(opt)
        if r and r[0] <= years and (r[1] is None or years <= r[1]):
            hits.append((opt, r))
    if len(hits) > 1:  # boundary overlap such as '1-2' and '2-3' at exactly 2.0: prefer half-open
        hits = [h for h in hits if h[1][1] is None or years < h[1][1]]
    return hits[0][0] if len(hits) == 1 else None


def _notice_days(option: str) -> tuple[int, bool] | None:
    """(days, is_range) for an option such as '15 days', '1 month', 'Immediate'."""
    s = option.lower()
    if "immediate" in s:
        return 0, False
    m = re.search(_NUM + r"\s*(?:-|–|to)\s*" + _NUM + r"\s*(day|week|month)", s) \
        or re.search(r"()" + _NUM + r"\s*(day|week|month)", s)
    if not m:
        return None
    mult = {"day": 1, "week": 7, "month": 30}[m.group(3)]
    return int(float(m.group(2)) * mult), bool(m.group(1))


def is_serving_notice(profile: dict, today: date | None = None) -> bool | None:
    """True while serving; False if not serving; None once the last working day has
    passed (no rule was given for that, so the form is flagged)."""
    today = today or date.today()
    e = _emp(profile, ["serving_notice"])
    if not e["serving_notice"]:
        return False
    lwd = _day(_emp(profile, ["last_working_day"])["last_working_day"])
    return True if today <= lwd else None


def last_working_day_answer(profile: dict, today: date | None = None, *, iso: bool = False) -> str | None:
    if is_serving_notice(profile, today) is not True:
        return None
    d = _day(profile["employment"]["last_working_day"])
    return d.isoformat() if iso else human_date(d)


def serving_notice_answer(profile: dict, today: date | None = None) -> str | None:
    state = is_serving_notice(profile, today)
    return None if state is None else ("Yes" if state else "No")


def notice_period_answer(profile: dict, today: date | None = None, *,
                         options: list[str] | None = None) -> str | None:
    """Free text: 'Serving notice, last working day <date>'. With options: the option equal to
    notice_period_days, else the shortest option covering the last working day. None -> flag."""
    today = today or date.today()
    if is_serving_notice(profile, today) is not True:
        return None
    e = _emp(profile, ["notice_period_days", "last_working_day"])
    lwd = _day(e["last_working_day"])
    if options is None:
        return f"Serving notice, last working day {human_date(lwd)}"
    parsed = [(o, _notice_days(o)) for o in options]
    parsed = [(o, p) for o, p in parsed if p]
    for o, (days, is_range) in parsed:
        if days == e["notice_period_days"] and not is_range:
            return o
    need = (lwd - today).days
    covering = sorted((p[0], o) for o, p in parsed if p[0] >= need)
    return covering[0][1] if covering else None


def human_date(d: date) -> str:
    """13 Oct 2026 (no leading zero on the day). Used for dates written in free text."""
    return f"{d.day} {d:%b} {d.year}"


def earliest_start_answer(profile: dict, today: date | None = None, *, iso: bool = False) -> str:
    """The stored date while it is in the future, otherwise 'Immediately'. Free text gets '19 Oct 2026';
    date inputs (iso=True) keep their own YYYY-MM-DD format."""
    today = today or date.today()
    start = _day(_emp(profile, ["earliest_start_date"])["earliest_start_date"])
    if start > today:
        return start.isoformat() if iso else human_date(start)
    return "Immediately"


def current_company_answer(profile: dict) -> str:
    """Stays the stored company even after the last working day."""
    return _emp(profile, ["current_company"])["current_company"]


# ─── EEO / diversity (sensitive: profile only, never the LLM) ────────────────

_DECLINE = re.compile(r"decline|prefer not|do not (wish|want)|don'?t (wish|want)|choose not|not to "
                      r"(say|answer|disclose|identify|specify)|rather not|no answer", re.I)
_SYNONYMS = {"male": {"man"}, "female": {"woman"}}


def _norm(option: str) -> str:
    return re.sub(r"\s+", " ", option.strip().casefold())


def _decline_option(options: list[str]) -> str | None:
    return next((o for o in options if _DECLINE.search(o)), None)


def _pick(options: list[str], exact: set[str], matches) -> str | None:
    """One option: an exact hit wins; otherwise a single fuzzy hit; several fuzzy hits -> None."""
    hits = [o for o in options if not _DECLINE.search(o) and matches(_norm(o))]
    exacts = [o for o in hits if _norm(o) in exact]
    if exacts:
        return exacts[0]
    return hits[0] if len(hits) == 1 else None


def eeo_answer(profile: dict, field: str, options: list[str] | None = None) -> str | None:
    """field: gender | ethnicity | veteran_status | disability_status.
    Free text (options None) -> the stored answer. With options -> the option that means
    the stored answer; else the decline option if eeo.if_no_matching_option is 'decline';
    else None (flag). Ethnicity never matches 'American Indian ...' for 'Indian'."""
    if field not in ("gender", "ethnicity", "veteran_status", "disability_status"):
        raise ValueError(field)
    require(profile, [f"eeo.{field}"])
    eeo = profile["eeo"]
    value = eeo[field]
    if options is None:
        return value
    v = _norm(value)
    if field == "gender":
        exact = {v} | _SYNONYMS.get(v, set())
        found = _pick(options, exact, lambda n: n in exact or re.match(rf"{re.escape(v)}\b", n) is not None)
    elif field == "ethnicity":
        found = _pick(options, {v}, lambda n: n == v or (n.startswith(v) and not n.startswith("american")))
        broad = eeo.get("ethnicity_broad")
        if found is None and broad not in (None, TODO, ""):
            b = _norm(broad)
            found = _pick(options, {b}, lambda n: n == b or re.match(rf"{re.escape(b)}\b", n) is not None)
    elif not v.startswith(("no", "not ")):
        # veteran/disability matching below only knows how to find the *negative* options. Any other
        # stored answer (e.g. "Protected veteran") is not guessed at: decline/flag instead.
        found = None
    elif field == "veteran_status":
        found = _pick(options, {"no", v},
                      lambda n: n == "no" or re.search(r"\b(not|no)\b.*\bveteran", n) is not None)
    else:
        found = _pick(options, {"no", v},
                      lambda n: n == "no" or re.search(
                          r"(^no\b|\b(not|don'?t)\b.*\b(have|had)\b.*disab|\bno disab)", n) is not None)
    if found:
        return found
    return _decline_option(options) if eeo.get("if_no_matching_option") == "decline" else None


# ─── How did you hear about us / reasons for leaving ─────────────────────────

_SOURCE_BY_PLATFORM = {"linkedin": "LinkedIn", "naukri": "Naukri", "indeed": "Indeed",
                       "greenhouse": "Company website", "lever": "Company website",
                       "company": "Company website"}
_SOURCE_ALIASES = {"Company website": ["company website", "company career", "careers page", "career page",
                                       "corporate website", "company site", "company's website",
                                       "our website", "direct"]}
_REFERRER_Q = re.compile(r"\breferr|referred by|employee (name|referral)|who referred", re.I)
_LEAVING_Q = re.compile(r"why\s+(are|do)\s+you\s+(looking|leaving|moving|want to (leave|move|change|switch))"
                        r"|reason(s)?\s+(for|of)\s+(leaving|change|job change|switching|looking)"
                        r"|why\s+(change|switch|move)|looking to (move|change|switch)", re.I)


def how_did_you_hear_answer(platform: str, options: list[str] | None = None,
                            label: str = "") -> str | None:
    """The platform the job came from -> the source name. None (flag) when the question asks
    for a referrer's name, the platform is unknown, or no option fits. A 'Referral' option
    is never chosen: only the user knows whether he was referred."""
    if _REFERRER_Q.search(label):
        return None
    source = _SOURCE_BY_PLATFORM.get((platform or "").strip().casefold())
    if source is None:
        return None
    if options is None:
        return source
    wanted = [_norm(source)] + _SOURCE_ALIASES.get(source, [])
    for o in options:
        if _REFERRER_Q.search(o):
            continue
        if any(w in _norm(o) for w in wanted):
            return o
    return None


def is_reason_for_leaving_question(label: str) -> bool:
    """'Why are you looking to move?'-style questions: always flagged for the user."""
    return bool(_LEAVING_Q.search(label))


# ─── Education ───────────────────────────────────────────────────────────────

def academic_answer(profile: dict, level: str, field: str) -> str | None:
    """level: 'degree' | '12th' | '10th'; field: 'board' | 'year' | 'percentage' (12th/10th),
    or 'cgpa' | 'percentage' | 'start_year' | 'graduation_year' (degree).
    Returns None (-> flag) when the value is TODO, blank or missing. Never computes a value,
    e.g. a percentage is never derived from the CGPA."""
    edu = profile.get("education", {})
    if level == "degree":
        if field == "graduation_year":
            raw = edu.get("graduation")
            raw = None if raw in (None, "", TODO) else str(_ym(raw)[0])
        else:
            raw = edu.get(field)
    elif level in ("12th", "10th"):
        raw = (edu.get({"12th": "twelfth", "10th": "tenth"}[level]) or {}).get(field)
    else:
        raise ValueError(level)
    return None if raw in (None, "", TODO) else str(raw)


# ─── Salary (sensitive: profile only, never the LLM) ─────────────────────────

def _is_india(country: str | None) -> bool:
    return (country or "").strip().casefold() in {"india", "in", "bharat"}


def _lpa_text(lpa: float) -> str:
    return f"{lpa:g} LPA"


def current_ctc_lpa(profile: dict) -> float:
    """The current-CTC figure the user chose to report (compensation.current_ctc.report_as)."""
    require(profile, ["compensation.current_ctc.report_as"])
    c = profile["compensation"]["current_ctc"]
    choice = c["report_as"]
    needed = {"fixed": ["fixed_lpa"], "fixed_plus_variable": ["fixed_lpa", "variable_lpa"],
              "stated_total": ["stated_total_lpa"]}[choice]
    require(profile, [f"compensation.current_ctc.{k}" for k in needed])
    return round(sum(c[k] for k in needed), 2)


def expected_ctc_lpa(profile: dict) -> float:
    require(profile, ["compensation.expected_ctc_inr_lpa"])
    return profile["compensation"]["expected_ctc_inr_lpa"]


def expected_fixed_ctc_lpa(profile: dict) -> float | None:
    """The user's own expected FIXED / base CTC in LPA, or None when it has not been given (-> flag)."""
    v = profile.get("compensation", {}).get("expected_fixed_ctc_lpa")
    return v if _is_number(v) and v > 0 else None


def current_fixed_ctc_lpa(profile: dict) -> float | None:
    v = profile.get("compensation", {}).get("current_ctc", {}).get("fixed_lpa")
    return v if _is_number(v) else None


def skill_years_for(profile: dict, tool: str) -> float | None:
    """The user's own years with a tool (profile.yaml: skill_years), looked up through the technology
    aliases (postgres -> postgresql). None for a tool that is not listed (-> flag). Never estimated."""
    import guard
    wanted = guard.canon(tool.strip().lower())
    for name, years in (profile.get("skill_years") or {}).items():
        if guard.canon(str(name).strip().lower()) == wanted and _is_number(years):
            return years
    return None


def salary_answer(profile: dict, kind: str, country: str | None, *, required: bool = True,
                  free_text: bool = False, unit: str = "lpa", lpa: float | None = None) -> tuple[str, str | None]:
    """(action, value) for a salary field. kind is 'current' or 'expected'.
    action: 'fill' (use value), 'blank' (leave the field empty), 'flag' (user answers).
    - Non-Indian forms: expected salary -> flag; current CTC -> fill only if required, else blank.
    - Indian forms: expected is '15 LPA (negotiable)' in free text, else the bare number
      (unit 'lpa') or the absolute rupee amount (unit 'inr'). Current is the chosen figure."""
    if kind not in ("current", "expected"):
        raise ValueError(kind)
    if not _is_india(country):
        if kind == "expected":
            return "flag", None
        if not required:
            return "blank", None
    override = lpa is not None                    # a specific figure, e.g. the FIXED part: no total, no breakup text
    if lpa is None:
        lpa = expected_ctc_lpa(profile) if kind == "expected" else current_ctc_lpa(profile)
    if free_text:
        text = _lpa_text(lpa) + ("" if _is_india(country) else " INR")
        if kind == "expected":
            return "fill", f"{text} (negotiable)"
        if override:
            return "fill", text
        c = profile["compensation"]["current_ctc"]
        if c["report_as"] == "stated_total":      # make the breakup visible
            require(profile, ["compensation.current_ctc.fixed_lpa", "compensation.current_ctc.variable_lpa"])
            text += f" ({c['fixed_lpa']:g} fixed + {c['variable_lpa']:g} variable + benefits)"
        return "fill", text
    if unit == "inr":
        return "fill", str(int(round(lpa * 100000)))
    return "fill", f"{lpa:g}"


_EMPLOYED_Q = re.compile(
    r"(currently|presently)\s+(employed|working)|employment\s+status|are\s+you\s+(currently\s+)?employed",
    re.I)


def is_employment_status_question(label: str) -> bool:
    """True for 'Are you currently employed?'-style questions: the user answers these
    himself, so the filler must flag them rather than answer."""
    return bool(_EMPLOYED_Q.search(label))
