"""Job discovery: saved searches (python-jobspy) -> normalise -> cheap filters (no Claude) -> route -> tracker.

    scrape      one jobspy.scrape_jobs call per (search term x location), sites together, pauses between calls
    normalise   one flat Found record per posting (title, company, location, dates, links, text)
    filter      drop what cannot fit you, cheaply and with a stated reason: title, age, location, experience asked,
                duplicates of each other and of anything already in the tracker
    route       by the DIRECT apply URL: Greenhouse -> apply, Lever -> prepare, anything else -> manual
    save        status "found" in the tracker; the user then approves from the shortlist (discover.py)

JobSpy 1.2.0 facts this relies on (read from its source, 2026-10-03): Indeed returns the description and a direct URL
(`job_url_direct`); Naukri returns `experience_range` ("0-2 Yrs") and needs fetch_description for the description and
its external apply link; LinkedIn needs fetch_description for the description and NEVER supplies a direct apply URL.
"""
from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Callable
from urllib.parse import urlparse

from jobbot import intake
from jobbot import tracker as T

INSTALL_HINT = "pip install -U python-jobspy"
KNOWN_SITES = ("indeed", "naukri", "linkedin", "glassdoor", "zip_recruiter", "google", "bayt", "bdjobs")
BOARD_HOSTS = ("indeed.", "naukri.com", "linkedin.com", "glassdoor.", "ziprecruiter.", "bayt.com", "bdjobs.com",
               "google.com")


class DiscoveryError(Exception):
    pass


# ─── settings (config.yaml: discovery) ───────────────────────────────────────

# A title must contain one of these words to be a software role we are looking for ...
DEFAULT_TITLE_INCLUDE_WORDS = ["software", "sde", "swe", "backend", "back-end", "back end", "python", "django", "full stack",
                               "full-stack", "fullstack", "developer", "programmer", "java", "golang", "node", "api"]
# ... and none of these (seniority, interns, QA/test, data science/analyst, frontend-only, mobile-only, DevOps-only, embedded).
DEFAULT_TITLE_EXCLUDE_WORDS = [
    "senior", "sr", "lead", "staff", "principal", "architect", "manager", "director", "head", "intern", "internship",
    "trainee", "trainees", "apprentice",
    "qa", "test", "tester", "testing", "sdet", "quality",
    "data scientist", "data science", "data analyst", "analyst",
    "frontend", "front end", "front-end", "ui developer", "ui engineer", "react developer", "angular developer", "vue developer",
    "mobile", "android", "ios", "flutter", "react native",
    "devops", "dev ops", "sre", "site reliability",
    "embedded", "firmware", "fpga", "vlsi", "rtos", "bsp", "hardware", "device driver"]
# Descriptions dominated by embedded/hardware work (2 or more of these) are dropped. The extras beyond I2C/SPI/UART/firmware/
# RTOS/microcontroller/embedded C/BSP/FPGA/Verilog are common companions of those.
DEFAULT_EMBEDDED_TERMS = ["i2c", "spi", "uart", "firmware", "rtos", "microcontroller", "embedded c", "bsp", "fpga", "verilog",
                          "vhdl", "device driver", "can bus", "bootloader", "yocto", "embedded linux", "arm cortex",
                          "linux kernel"]
DEFAULT_EMBEDDED_MIN_TERMS = 2
# Frontend-only: two or more frontend terms and no backend/API term at all.
DEFAULT_FRONTEND_TERMS = ["react", "angular", "vue", "javascript", "typescript", "css", "html", "ui/ux", "frontend", "front end",
                          "front-end", "jquery", "redux", "next.js", "tailwind", "bootstrap"]
DEFAULT_BACKEND_TERMS = ["api", "apis", "backend", "back-end", "back end", "server", "database", "sql", "postgres", "postgresql",
                         "mysql", "mongodb", "redis", "kafka", "django", "flask", "fastapi", "node", "spring", "microservice",
                         "microservices", "rest", "restful", "graphql", "python", "java", "golang", "ruby", "rails", "php",
                         "laravel", "asp.net", "celery"]


def _term_pattern(term: str) -> str:
    """A whole-word pattern for a term; a space also matches a hyphen or nothing ("front end" ~ front-end, frontend)."""
    return r"(?<![\w.])" + re.escape(term.strip().lower()).replace(r"\ ", r"[\s\-]*") + r"(?![\w])"


def term_regex(terms: "list[str]") -> "re.Pattern":
    return re.compile("|".join(_term_pattern(t) for t in terms if str(t).strip()) or r"(?!x)x", re.I)


def terms_in(text: str, terms: "list[str]") -> list[str]:
    low = (text or "").lower()
    return sorted({t for t in terms if t.strip() and re.search(_term_pattern(t), low)})


@dataclass
class Location:
    name: str
    query: str
    remote: bool = False


@dataclass
class Settings:
    terms: list[str]
    locations: list[Location]
    sites: list[str]
    max_age_days: int = 7
    results_per_search: int = 30
    country: str = "india"                      # JobSpy's country_indeed
    distance_miles: int = 25
    fetch_description: bool = True
    max_min_experience_years: float = 3
    accept_cities: list[str] = field(default_factory=lambda: ["bengaluru", "bangalore"])
    accept_remote: bool = True
    # Indeed reports "KA, IN" (state, country) with no city. Accepted ONLY for results of a city search for one of
    # accept_cities (the Bengaluru search), where such a posting is within 25 miles of it.
    accept_states: list[str] = field(default_factory=lambda: ["ka", "karnataka"])
    country_name: str = "India"
    title_include_words: list[str] = field(default_factory=lambda: list(DEFAULT_TITLE_INCLUDE_WORDS))
    title_exclude_words: list[str] = field(default_factory=lambda: list(DEFAULT_TITLE_EXCLUDE_WORDS))
    embedded_terms: list[str] = field(default_factory=lambda: list(DEFAULT_EMBEDDED_TERMS))
    # Recruiting platforms / staffing firms / aggregators (not the employer). "cap": scored but at most 6 (never auto-approved);
    # "drop": removed before scoring. Matched against the company name, ignoring case and punctuation.
    aggregators: list[str] = field(default_factory=lambda: ["Uplers", "Ibrowsejobs", "Jobgether"])
    aggregator_action: str = "cap"
    embedded_min_terms: int = DEFAULT_EMBEDDED_MIN_TERMS
    frontend_terms: list[str] = field(default_factory=lambda: list(DEFAULT_FRONTEND_TERMS))
    backend_terms: list[str] = field(default_factory=lambda: list(DEFAULT_BACKEND_TERMS))
    delay_between_searches_s: tuple[float, float] = (5.0, 10.0)
    resolve_links: bool = True
    max_link_resolutions: int = 60


def _words(d: dict, name: str, default: "list[str]") -> "list[str]":
    v = d.get(name, default)
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise DiscoveryError(f"discovery.{name} must be a list of words")
    return [x.strip() for x in v if x.strip()]


def _choice(d: dict, name: str, default: str, allowed: tuple) -> str:
    v = str(d.get(name, default))
    if v not in allowed:
        raise DiscoveryError(f"discovery.{name} must be one of {list(allowed)}")
    return v


def aggregator_of(company: str, s: Settings) -> "str | None":
    """Which listed recruiting platform / aggregator this company is, or None (whole-word match)."""
    from jobbot.relevance import is_aggregator
    return is_aggregator(company, s.aggregators)


def load_settings(cfg: dict) -> Settings:
    """From the loaded config.yaml. Raises DiscoveryError naming exactly what to fix."""
    d = (cfg or {}).get("discovery")
    if not isinstance(d, dict) or not d:
        raise DiscoveryError("config.yaml has no `discovery:` section. Copy the example from config.example.yaml.")

    def need_list(name):
        v = d.get(name)
        if not isinstance(v, list) or not v:
            raise DiscoveryError(f"discovery.{name} must be a non-empty list")
        return v

    terms = [str(t).strip() for t in need_list("search_terms") if str(t).strip()]
    locations = []
    for item in need_list("locations"):
        if isinstance(item, str):
            locations.append(Location(item, item, "remote" in item.lower()))
        elif isinstance(item, dict) and (item.get("query") or item.get("name")):
            locations.append(Location(str(item.get("name") or item["query"]), str(item.get("query") or item["name"]),
                                      bool(item.get("remote", False))))
        else:
            raise DiscoveryError(f"discovery.locations entry {item!r} needs a `name` and a `query`")
    sites = [str(s).strip().lower() for s in need_list("sites")]
    bad = [s for s in sites if s not in KNOWN_SITES]
    if bad:
        raise DiscoveryError(f"discovery.sites: unknown {bad}; JobSpy supports {list(KNOWN_SITES)}")

    def num(name, default, lo, hi, kind=int):
        v = d.get(name, default)
        try:
            v = kind(v)
        except (TypeError, ValueError):
            raise DiscoveryError(f"discovery.{name} must be a number") from None
        if not lo <= v <= hi:
            raise DiscoveryError(f"discovery.{name} must be between {lo} and {hi}")
        return v

    delay = d.get("delay_between_searches_s", [5, 10])
    if not (isinstance(delay, (list, tuple)) and len(delay) == 2 and 0 <= float(delay[0]) <= float(delay[1])):
        raise DiscoveryError("discovery.delay_between_searches_s must be [min, max] seconds")
    return Settings(
        terms=terms, locations=locations, sites=sites,
        max_age_days=num("max_age_days", 7, 1, 90), results_per_search=num("results_per_search", 30, 1, 100),
        country=str(d.get("country", "india")), distance_miles=num("distance_miles", 25, 1, 200),
        fetch_description=bool(d.get("fetch_description", True)),
        max_min_experience_years=num("max_min_experience_years", 3, 0, 30, float),
        accept_cities=[str(c).lower() for c in d.get("accept_cities", ["Bengaluru", "Bangalore"])],
        accept_remote=bool(d.get("accept_remote", True)), country_name=str(d.get("country_name", "India")),
        accept_states=[str(c).lower() for c in d.get("accept_states", ["KA", "Karnataka"])],
        title_include_words=_words(d, "title_include_words", DEFAULT_TITLE_INCLUDE_WORDS),
        title_exclude_words=_words(d, "title_exclude_words", DEFAULT_TITLE_EXCLUDE_WORDS),
        embedded_terms=_words(d, "embedded_terms", DEFAULT_EMBEDDED_TERMS),
        embedded_min_terms=num("embedded_min_terms", DEFAULT_EMBEDDED_MIN_TERMS, 1, 20),
        aggregators=_words(d, "aggregators", ["Uplers", "Ibrowsejobs", "Jobgether"]),
        aggregator_action=_choice(d, "aggregator_action", "cap", ("cap", "drop")),
        frontend_terms=_words(d, "frontend_terms", DEFAULT_FRONTEND_TERMS),
        backend_terms=_words(d, "backend_terms", DEFAULT_BACKEND_TERMS),
        delay_between_searches_s=(float(delay[0]), float(delay[1])),
        resolve_links=bool(d.get("resolve_links", True)), max_link_resolutions=num("max_link_resolutions", 60, 0, 1000))


# ─── experience asked ────────────────────────────────────────────────────────

@dataclass
class Ask:
    min_years: "float | None" = None          # the HIGHEST minimum mentioned anywhere (what the posting really asks for)
    max_years: "float | None" = None
    label: str = "not stated"
    mentions: list[str] = field(default_factory=list)       # every mention found, in order, e.g. ["0-2 yrs", "5+ yrs"]


_N = r"(\d+(?:\.\d+)?)"
_RANGE = re.compile(_N + r"\s*(?:-|–|—|to|\?)\s*" + _N + r"\s*\+?\s*(?:years?|yrs?)\b", re.I)
_PLUS = re.compile(_N + r"\s*\+\s*(?:years?|yrs?)\b", re.I)
_MINWORD = re.compile(r"(?:minimum|min\.?|at least|atleast|more than|over)\s*(?:of\s*)?" + _N + r"\s*\+?\s*(?:years?|yrs?)\b",
                      re.I)
_PLAIN = re.compile(_N + r"\s*(?:years?|yrs?)\b", re.I)
_EXP_WORD = re.compile(r"experience|\bexp\b|expertise|background|hands-on|worked", re.I)
# A requirement is often written without the word "experience" ("3+ years in backend development"). For those, only the
# forms that clearly ask for years (a range, "N+", "minimum N") count, never a bare "10 years" ("10 years in business").
_REQ_WORD = re.compile(r"develop|engineer|software|back-?end|python|django|programming|building|industry|professional|"
                       r"relevant|work(?:ing)?\b|candidates?|required|requirements?", re.I)
_FRESHER = re.compile(r"\b(fresher|freshers|entry[- ]level|new grads?|no experience|0 years)\b", re.I)
# The company talking about itself ("we have 10 years of experience in fintech") is not a requirement on the candidate.
_COMPANY_SELF = re.compile(r"\b(we|we've|our|ours|us)\b|since \d{4}|founded|established|in business|\bthe company\b", re.I)
_ABOUT_YOU = re.compile(r"\b(you|your|candidate|applicant|required|requirements?|must|should|looking for|ideal|minimum|"
                        r"preferred|qualifications?|eligib\w*|seeking|hiring|needs?|requires?|expects?|expected|bring|"
                        r"brings|wants?)\b", re.I)


def _fmt(n: float) -> str:
    return f"{n:g}"


def _all_matches(sentence: str, allow_plain: bool = True, allow_minword: bool = True) -> "list[Ask]":
    """EVERY years mention in the sentence (a range, "N+", "minimum N", and a bare "N years" when allowed), each once."""
    kinds = ((_RANGE, "range"), (_PLUS, "plus")) + (((_MINWORD, "min"),) if allow_minword else ()) + \
            (((_PLAIN, "plain"),) if allow_plain else ())
    taken: list[tuple[int, int]] = []
    found: list[tuple[int, Ask]] = []
    for pattern, kind in kinds:
        for m in pattern.finditer(sentence):
            if any(m.start() < e and s0 < m.end() for s0, e in taken):
                continue
            taken.append((m.start(), m.end()))
            if kind == "range":
                lo, hi = float(m.group(1)), float(m.group(2))
                found.append((m.start(), Ask(lo, hi, f"{_fmt(lo)}-{_fmt(hi)} yrs")))
            else:
                n = float(m.group(1))
                found.append((m.start(), Ask(n, None, f"{_fmt(n)}+ yrs" if kind in ("plus", "min") else
                                             f"{_fmt(n)} {'yr' if n == 1 else 'yrs'}")))
    return [a for _, a in sorted(found, key=lambda t: t[0])]


def years_asked(text: str, structured: "str | None" = None) -> Ask:
    """What a posting asks for, reading EVERY mention: Naukri's own field ("0-2 Yrs") and every sentence of the description
    that states years next to an experience word (or a clear "N+" / range / "minimum N" in a requirement sentence). The
    answer is the highest minimum found, so one "5+ years of backend experience" among softer lines still counts."""
    mentions: list[Ask] = []
    if structured:
        mentions += _all_matches(structured)
    for sentence in re.split(r"(?<=[.!?;])\s+|\n+", text or ""):
        if _COMPANY_SELF.search(sentence) and not _ABOUT_YOU.search(sentence):
            continue
        if _EXP_WORD.search(sentence):
            mentions += _all_matches(sentence)
        elif _REQ_WORD.search(sentence):
            mentions += _all_matches(sentence, allow_plain=False)
        else:                     # a list fragment ("5+ years;", or "5 to 10 Years" on the line after "Experience Range:")
            mentions += _all_matches(sentence, allow_plain=False, allow_minword=False)    # only the unmistakable forms
    if mentions:
        top = max(mentions, key=lambda m: m.min_years)
        labels = list(dict.fromkeys(m.label for m in mentions))
        return Ask(top.min_years, top.max_years, top.label, labels)
    if _FRESHER.search(text or ""):
        return Ask(0, 0, "fresher / entry level", ["fresher / entry level"])
    return Ask()


# A level number or numeral on an engineer title ("SDE III", "Engineer 4") is senior whatever the years say.
_LEVEL_TITLE = re.compile(r"\b(?:sde|swe|software (?:development )?engineer|engineer|developer)[ -]?(?:iii|iv|v|3|4|5)\b", re.I)


# ─── location, title, age ────────────────────────────────────────────────────

def location_ok(location: str, remote: "bool | None", s: Settings, city_search: bool = False,
                text: str = "") -> "tuple[bool, str]":
    """Bengaluru anywhere; otherwise only if the POSTING ITSELF says remote (JobSpy's is_remote flag, the word "remote" in
    its location, title or description) and it is in India. `text` is the title plus the description."""
    loc = (location or "").lower()
    if any(c in loc for c in s.accept_cities):
        return True, ""
    in_country = s.country_name.lower() in loc or bool(re.search(r"(^|,\s*)in$", loc.strip()))
    parts = [p.strip() for p in loc.split(",") if p.strip()]
    if city_search and in_country and parts and parts[0] in s.accept_states:      # "KA, IN" from the Bengaluru search
        return True, ""
    says_remote = bool(remote) or "remote" in loc or bool(re.search(r"\bremote\b", text or "", re.I))
    if s.accept_remote and says_remote and (in_country or not loc.strip()):
        return True, ""
    shown = location or "no location"
    note = "" if says_remote else " (and the posting does not say remote)"
    return False, (f"location '{shown}' is not {'/'.join(c.title() for c in s.accept_cities[:1])} or remote in "
                   f"{s.country_name}{note}")


def title_ok(title: str, s: Settings) -> "tuple[bool, str]":
    if not term_regex(s.title_include_words).search(title or ""):
        return False, f"title '{title}' is not a software role we look for"
    if m := term_regex(s.title_exclude_words).search(title or ""):
        return False, f"title '{title}' is excluded ('{m.group(0)}')"
    if m := _LEVEL_TITLE.search(title or ""):
        return False, f"title '{title}' is a senior level ('{m.group(0)}')"
    return True, ""


def description_ok(title: str, description: str, s: Settings) -> "tuple[bool, str]":
    """Drop a posting dominated by embedded/hardware work, or a frontend-only one with no backend/API work. A posting whose
    description could not be fetched passes (there is nothing to judge)."""
    text = f"{title}\n{description or ''}"
    if not (description or "").strip():
        return True, ""
    emb = terms_in(text, s.embedded_terms)
    if len(emb) >= s.embedded_min_terms:
        return False, f"embedded/hardware role (mentions {', '.join(emb[:5])})"
    fe, be = terms_in(text, s.frontend_terms), terms_in(text, s.backend_terms)
    if len(fe) >= 2 and not be:
        return False, f"frontend-only (mentions {', '.join(fe[:4])}) and no backend/API work"
    return True, ""


def fill_descriptions(tracker, fetch, *, limit: int = 60, sleep=lambda s: None, delay_s: float = 1.0, log=print,
                      min_chars: int = 200, save: bool = True) -> dict:
    """Fetch the description of Found postings that have none (HTTP only, no Claude), BEFORE they are scored: the scorer
    can only judge honestly from text, and the experience/embedded rules need it too. `fetch(url)` returns text or None."""
    rows = tracker.without_description(min_chars)
    tried = filled = 0
    for r in rows[:max(limit, 0)]:
        tried += 1
        text = None
        for url in dict.fromkeys(u for u in (r["source_url"], r["canonical_url"]) if u):
            try:
                text = fetch(url)
            except Exception:
                text = None
            if text and len(text.strip()) >= min_chars:
                break
            text = None
        if text:
            if save:
                tracker.set_description(r["id"], text.strip())
            filled += 1
        sleep(delay_s)
    left = len(tracker.without_description(min_chars)) - (0 if save else filled)
    log(f"  descriptions: {filled} {'fetched' if save else 'could be fetched'} of {tried} tried; {left} still have none")
    return {"tried": tried, "filled": filled, "still_missing": left}


def recheck_found(tracker, s: Settings, log=print) -> list[dict]:
    """Apply the CURRENT title, experience and description rules to postings that are only Found (no decision made), using
    the text already stored, and forget the ones that no longer pass. For when the rules or the parser changed: no new
    scrape, and anything already scored that survives keeps its score."""
    gone = []
    for r in tracker.found():
        why, kind = "", ""
        ok, w = title_ok(r["role"] or "", s)
        if not ok:
            why, kind = w, "title"
        else:
            ask = years_asked(r["description"] or "", r["experience_asked"])
            if ask.min_years is not None and ask.min_years > s.max_min_experience_years:
                why, kind = (f"asks {ask.label} (you keep a minimum up to {s.max_min_experience_years:g}); every mention: "
                             f"{', '.join(ask.mentions)}"), "experience"
            else:
                ok, w = description_ok(r["role"] or "", r["description"] or "", s)
                if not ok:
                    why, kind = w, "description"
                elif s.aggregator_action == "drop" and (agg := aggregator_of(r["company"] or "", s)):
                    why, kind = f"{agg} is a recruiting platform / aggregator (discovery.aggregators)", "aggregator"
        if kind:
            gone.append({"id": r["id"], "company": r["company"], "title": r["role"], "kind": kind, "why": why})
    tracker.delete_found_ids([g["id"] for g in gone])
    for g in gone:
        log(f"  rechecked and dropped #{g['id']} {g['title']} | {g['company']}: {g['why']}")
    return gone


# ─── normalising JobSpy's rows ───────────────────────────────────────────────

@dataclass
class Found:
    site: str
    site_id: str
    title: str
    company: str
    location: str
    remote: "bool | None"
    date_posted: "str | None"
    job_url: str
    direct_url: "str | None"
    description: str
    ask: Ask
    search: str = ""
    city_search: bool = False        # found by a non-remote (city) search
    company_hint: str = ""
    route: str = ""                  # greenhouse | lever | manual
    platform: str = ""
    canonical_url: str = ""
    apply_url: str = ""

    def as_row(self, row_id: str = "-") -> dict:
        """The same keys a tracker row has, so a dry run can be shown with the shortlist formatter."""
        return {"id": row_id, "role": self.title, "company": self.company, "location": self.location,
                "experience_asked": self.ask.label, "route": self.route, "canonical_url": self.canonical_url,
                "direct_url": self.apply_url, "source_url": self.job_url}

    def to_record(self) -> dict:
        return {"canonical_url": self.canonical_url, "platform": self.platform or self.site, "company": self.company,
                "role": self.title, "location": self.location, "route": self.route, "source": self.site,
                "source_url": self.job_url, "direct_url": self.apply_url or None, "experience_asked": self.ask.label,
                "date_posted": self.date_posted, "notes": f"found by search: {self.search}", "remote": self.remote,
                "description": (self.description or "")[:30000],
                "reason": f"found on {self.site} ({self.search}); routes to {self.route}"}


def _clean(v):
    if v is None:
        return None
    if isinstance(v, float) and v != v:                       # NaN
        return None
    if str(v) in ("NaT", "nan", "None", "<NA>"):
        return None
    return v


def records(result) -> list[dict]:
    """JobSpy returns a pandas DataFrame; tests pass a list of dicts."""
    if hasattr(result, "to_dict"):
        return [{k: _clean(v) for k, v in r.items()} for r in result.to_dict("records")]
    return [{k: _clean(v) for k, v in dict(r).items()} for r in (result or [])]


def _as_date(v) -> "date | None":
    v = _clean(v)
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    try:
        return date.fromisoformat(str(v)[:10])
    except ValueError:
        return None


def normalise(rec: dict, search: str = "") -> "Found | None":
    url = _clean(rec.get("job_url"))
    title = str(_clean(rec.get("title")) or "").strip()
    if not url or not title:
        return None
    posted = _as_date(rec.get("date_posted"))
    desc = str(_clean(rec.get("description")) or "")
    return Found(
        site=str(_clean(rec.get("site")) or "").lower(), site_id=str(_clean(rec.get("id")) or ""), title=title,
        company=str(_clean(rec.get("company")) or "").strip(), location=str(_clean(rec.get("location")) or "").strip(),
        remote=_clean(rec.get("is_remote")), date_posted=posted.isoformat() if posted else None, job_url=str(url),
        direct_url=_clean(rec.get("job_url_direct")), description=desc,
        ask=years_asked(f"{title}\n{desc}", _clean(rec.get("experience_range"))), search=search)


# ─── scraping ────────────────────────────────────────────────────────────────

def _jobspy():
    try:
        from jobspy import scrape_jobs
    except ImportError as e:
        raise DiscoveryError(f"python-jobspy is not installed. Run: {INSTALL_HINT}") from e
    return scrape_jobs


def run_searches(s: Settings, scrape_fn: "Callable | None" = None, sleep: Callable = time.sleep,
                 log: Callable = print, only_terms: "list[str] | None" = None, results: "int | None" = None,
                 rng: "random.Random | None" = None, should_stop: "Callable | None" = None) -> "tuple[list[tuple[str, dict, bool]], list[str]]":
    """[(search label, raw row, was it a city search)], [errors]. One scrape_jobs call per term x location; a failing call is recorded and the
    rest continue. A pause between calls keeps the sites from rate-limiting."""
    scrape_fn = scrape_fn or _jobspy()
    rng = rng or random.Random()
    rows, errors = [], []
    plan = [(t, loc) for t in (only_terms or s.terms) for loc in s.locations]
    for i, (term, loc) in enumerate(plan, 1):
        if should_stop and should_stop():
            log(f"  stop requested: {len(plan) - i + 1} search(es) skipped; what was found so far is kept")
            break
        label = f"{term} @ {loc.name}"
        log(f"  [{i}/{len(plan)}] {label}  ({', '.join(s.sites)})")
        try:
            got = records(scrape_fn(
                site_name=list(s.sites), search_term=term, location=loc.query, distance=s.distance_miles,
                is_remote=loc.remote, results_wanted=results or s.results_per_search, country_indeed=s.country,
                hours_old=s.max_age_days * 24, fetch_description=s.fetch_description, description_format="markdown"))
        except Exception as e:                                        # one blocked or failing call is not the run
            errors.append(f"{label}: {type(e).__name__}: {str(e)[:200]}")
            log(f"      failed: {errors[-1]}")
            got = []
        log(f"      {len(got)} postings")
        rows += [(label, r, not loc.remote) for r in got]
        if i < len(plan):
            sleep(rng.uniform(*s.delay_between_searches_s))
    return rows, errors


# ─── routing by the direct apply URL ─────────────────────────────────────────

def _is_board(url: "str | None") -> bool:
    host = urlparse(url or "").netloc.lower()
    return any(h in host for h in BOARD_HOSTS)


def default_fetch(url: str) -> "tuple[str, str]":
    """(final URL after redirects, page HTML). Failures return (url, "")."""
    import requests
    try:
        r = requests.get(url, timeout=12, headers={"User-Agent": "Mozilla/5.0"}, allow_redirects=True)
        return r.url, r.text[:400000]
    except Exception:
        return url, ""


_COMPANY_NOISE = re.compile(r"\b(pvt|private|ltd|limited|inc|llc|llp|corp|corporation|india|technologies|technology|"
                           r"software|solutions|services|systems|labs|group|global)\b")


def company_tokens(company: str) -> list[str]:
    """Plausible Greenhouse board tokens from a company NAME ("Cambridge Mobile Telematics" -> cambridgemobiletelematics),
    for company pages whose domain says nothing ("cmtelematics.com"). Only ever guesses: the Greenhouse API must confirm
    the job id on that board before one is used."""
    words = [w for w in re.sub(r"[^a-z0-9 ]+", " ", _COMPANY_NOISE.sub(" ", company.lower().replace("&", " "))).split() if w]
    full = [w for w in re.sub(r"[^a-z0-9 ]+", " ", company.lower()).split() if w]
    out: list[str] = []
    for cand in ("".join(full), "".join(words), "".join(words[:2]), words[0] if words else "", "-".join(words)):
        if cand and cand not in out and len(cand) > 2:
            out.append(cand)
    return out[:4]


def guess_by_company(url: str, company: str, http: "Callable | None" = None) -> "intake.Target | None":
    """A company page with `gh_jid=<id>`: try company-name board tokens until Greenhouse confirms that exact job id."""
    import json
    from urllib.parse import parse_qs
    http = http or intake.default_http
    jid = parse_qs(urlparse(url).query).get("gh_jid", [None])[0]
    if not jid or not str(jid).isdigit() or not company:
        return None
    for token in company_tokens(company):
        cand = intake.Target("greenhouse", token, jid)
        try:
            status, body = http(cand.api_url)
            if status == 200 and str(json.loads(body).get("id")) == jid:
                return cand
        except (ValueError, OSError):
            continue
    return None


def route_job(job: Found, s: Settings, fetch: Callable = default_fetch, budget: "list[int] | None" = None,
              http: "Callable | None" = None) -> None:
    """Sets route / platform / canonical_url / apply_url. `budget` is a one-item list: link resolutions left."""
    budget = budget if budget is not None else [s.max_link_resolutions]
    target, apply_url = None, job.direct_url if job.direct_url and not _is_board(job.direct_url) else None
    for candidate in (apply_url, job.job_url):
        if candidate and (target := intake.detect(candidate)):
            apply_url = candidate
            break
    if target is None and apply_url and s.resolve_links and budget[0] > 0:
        budget[0] -= 1
        final, html = fetch(apply_url)
        target = intake.detect(final) or (intake.resolve_embedded_greenhouse(final, html) if html else None)
        apply_url = final or apply_url
        if target is None and "gh_jid=" in (apply_url or ""):
            target = intake.guess_embedded_greenhouse(apply_url, http or intake.default_http) or \
                guess_by_company(apply_url, job.company, http)
    if target:
        job.route, job.platform, job.canonical_url, job.apply_url = target.platform, target.platform, \
            target.canonical_url, apply_url or target.canonical_url
    else:
        job.route, job.platform = "manual", job.site
        job.canonical_url, job.apply_url = job.job_url, apply_url or job.job_url


# ─── the whole run ───────────────────────────────────────────────────────────

@dataclass
class Report:
    scraped: int = 0
    errors: list[str] = field(default_factory=list)
    dropped: dict = field(default_factory=dict)         # reason kind -> [{"title","company","why"}]
    saved: dict = field(default_factory=dict)           # new / seen / replaced / duplicate -> count
    shortlist_ids: list[int] = field(default_factory=list)
    candidates: list = field(default_factory=list)       # Found records that passed every filter (not serialised)

    def drop(self, kind: str, job: Found, why: str) -> None:
        self.dropped.setdefault(kind, []).append({"title": job.title, "company": job.company, "site": job.site, "why": why})

    def summary(self) -> str:
        parts = [f"scraped {self.scraped}"] + [f"dropped {len(v)} ({k})" for k, v in self.dropped.items()]
        parts += [f"{n} {k}" for k, n in self.saved.items() if n]
        return "; ".join(parts)

    def to_dict(self) -> dict:
        return {"scraped": self.scraped, "errors": self.errors, "dropped": self.dropped, "saved": self.saved,
                "shortlist_ids": self.shortlist_ids}


def discover(s: Settings, tracker: "T.Tracker | None", *, scrape_fn: "Callable | None" = None,
             fetch: Callable = default_fetch, sleep: Callable = time.sleep, today: "date | None" = None,
             log: Callable = print, only_terms: "list[str] | None" = None, results: "int | None" = None,
             dry_run: bool = False, should_stop: "Callable | None" = None) -> Report:
    today = today or date.today()
    rep = Report()
    raw, rep.errors = run_searches(s, scrape_fn, sleep, log, only_terms, results, should_stop=should_stop)
    rep.scraped = len(raw)

    # 1. cheap filters, in order, each with its reason
    kept: list[Found] = []
    for label, r, city_search in raw:
        job = normalise(r, label)
        if job is None:
            continue
        job.city_search = city_search
        ok, why = title_ok(job.title, s)
        if not ok:
            rep.drop("title", job, why)
            continue
        if job.date_posted and (today - date.fromisoformat(job.date_posted)).days > s.max_age_days:
            rep.drop("too old", job, f"posted {job.date_posted}")
            continue
        ok, why = location_ok(job.location, job.remote, s, job.city_search, f"{job.title}\n{job.description}")
        if not ok:
            rep.drop("location", job, why)
            continue
        if job.ask.min_years is not None and job.ask.min_years > s.max_min_experience_years:
            more = f"; every mention: {', '.join(job.ask.mentions)}" if len(job.ask.mentions) > 1 else ""
            rep.drop("experience", job, f"asks {job.ask.label} (you keep a minimum up to "
                                        f"{s.max_min_experience_years:g}){more}")
            continue
        ok, why = description_ok(job.title, job.description, s)
        if not ok:
            rep.drop("description", job, why)
            continue
        if s.aggregator_action == "drop" and (agg := aggregator_of(job.company, s)):
            rep.drop("aggregator", job, f"{agg} is a recruiting platform / aggregator (discovery.aggregators)")
            continue
        kept.append(job)

    # 2. the same opening listed on several sites / searches: keep the one with a direct link, then the one with text
    best: dict[str, Found] = {}
    for job in kept:
        fp = T.fingerprint(job.company, job.title)
        cur = best.get(fp)
        score = (bool(job.direct_url and not _is_board(job.direct_url)), bool(job.description))
        if cur is None or score > (bool(cur.direct_url and not _is_board(cur.direct_url)), bool(cur.description)):
            if cur is not None:
                rep.drop("duplicate", cur, f"same opening as the better {job.site} listing")
            best[fp] = job
        else:
            rep.drop("duplicate", job, f"same opening already kept from {cur.site}" if cur.site != job.site else
                     "same opening already kept (found by another search)")
    kept = list(best.values())

    # 3. already in the tracker (applied, prepared, skipped, dry-run...): drop BEFORE spending network on its link
    fresh: list[Found] = []
    for job in kept:
        if tracker is not None and tracker.is_known(None, job.company, job.title):
            rep.drop("already tracked", job, "this company and title are already in the tracker")
        else:
            fresh.append(job)

    # 4. route (resolving external links costs a request each, capped), then save
    budget = [s.max_link_resolutions]
    rep.candidates = fresh
    for job in fresh:
        route_job(job, s, fetch, budget)
        if tracker is None or dry_run:
            continue
        outcome = tracker.add_found(job.to_record())
        rep.saved[outcome] = rep.saved.get(outcome, 0) + 1
        if outcome == "duplicate":
            rep.drop("already tracked", job, "this link is already in the tracker")
    if tracker is not None:
        rep.shortlist_ids = [r["id"] for r in tracker.found()]
    return rep


# ─── the shortlist ───────────────────────────────────────────────────────────

def link_of(row) -> str:
    if row["route"] != "manual":
        return row["canonical_url"]
    direct = row["direct_url"]
    return direct if direct and not _is_board(direct) else (row["source_url"] or row["canonical_url"])


def route_label(row, cfg: "dict | None" = None) -> str:
    route = row["route"] or "manual"
    if route == "manual":
        return "manual"
    if route == "lever":
        return "Lever -> prepare"
    mode = ((cfg or {}).get("platforms", {}).get(route, {}) or {}).get("mode") or (cfg or {}).get("default_mode") or "dry-run"
    return f"{route.title()} -> {mode}"


def format_shortlist(rows, cfg: "dict | None" = None, width: int = 30) -> str:
    if not rows:
        return "The shortlist is empty. (python discover.py runs a discovery.)"

    def cut(text, n):
        text = str(text or "")
        return text if len(text) <= n else text[:n - 1] + "~"
    out = [f"{'ID':>4}  {'TITLE':<{width}}  {'COMPANY':<22}  {'LOCATION':<20}  {'EXPERIENCE':<14}  {'ROUTE':<18}  LINK"]
    for r in rows:
        out.append(f"{r['id']:>4}  {cut(r['role'], width):<{width}}  {cut(r['company'], 22):<22}  "
                   f"{cut(r['location'], 20):<20}  {cut(r['experience_asked'], 14):<14}  "
                   f"{route_label(r, cfg):<18}  {link_of(r)}")
    out.append("")
    out.append(f"{len(rows)} posting(s). Approve with: python discover.py --approve 3,7,12   (or --approve all);   "
               "decline with --skip ...")
    return "\n".join(out)


def shortlist_records(rows, cfg: "dict | None" = None) -> list[dict]:
    """The shortlist as data (output/shortlist.json): one record per posting still waiting for a decision."""
    return [{"id": r["id"], "title": r["role"], "company": r["company"], "location": r["location"],
             "experience_asked": r["experience_asked"], "route": route_label(r, cfg), "link": link_of(r),
             "relevance_score": r["relevance"], "reason": r["relevance_reason"] or r["reason"]} for r in rows]


def parse_ids(text: str, available: "list[int]") -> list[int]:
    """'3,7,12' / '3 7 12' / 'all'. Raises DiscoveryError on anything else, so a typo approves nothing."""
    text = (text or "").strip().lower()
    if text == "all":
        return list(available)
    try:
        return sorted({int(p) for p in re.split(r"[,\s]+", text) if p})
    except ValueError:
        raise DiscoveryError(f"could not read the ids {text!r}: use numbers like 3,7,12 or the word all") from None
