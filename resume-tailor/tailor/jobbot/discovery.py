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

DEFAULT_TITLE_INCLUDE = (r"engineer|developer|\bsde\b|\bswe\b|programmer|software|back-?end|python|django|full.?stack|"
                         r"architect")
DEFAULT_TITLE_EXCLUDE = (r"\b(intern|internship|trainee|training|apprentice|manager|recruiter|sales|sdet|qa|test engineer|"
                         r"data scientist|front[- ]?end|ios|android|react native|flutter|mobile|wordpress|unpaid|"
                         r"game|dev ?ops|support engineer|solutions? engineer|hr)\b|(?<!\w)\.net\b")


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
    title_include: str = DEFAULT_TITLE_INCLUDE
    title_exclude: str = DEFAULT_TITLE_EXCLUDE
    delay_between_searches_s: tuple[float, float] = (5.0, 10.0)
    resolve_links: bool = True
    max_link_resolutions: int = 60


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
        title_include=str(d.get("title_include", DEFAULT_TITLE_INCLUDE)),
        title_exclude=str(d.get("title_exclude", DEFAULT_TITLE_EXCLUDE)),
        delay_between_searches_s=(float(delay[0]), float(delay[1])),
        resolve_links=bool(d.get("resolve_links", True)), max_link_resolutions=num("max_link_resolutions", 60, 0, 1000))


# ─── experience asked ────────────────────────────────────────────────────────

@dataclass
class Ask:
    min_years: "float | None" = None
    max_years: "float | None" = None
    label: str = "not stated"


_N = r"(\d+(?:\.\d+)?)"
_RANGE = re.compile(_N + r"\s*(?:-|–|—|to)\s*" + _N + r"\s*\+?\s*(?:years?|yrs?)\b", re.I)
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


def _num(text: str) -> float:
    return float(text)


def _fmt(n: float) -> str:
    return f"{n:g}"


def _first_match(sentence: str, allow_plain: bool = True) -> "Ask | None":
    best = None
    kinds = ((_RANGE, "range"), (_PLUS, "plus"), (_MINWORD, "min")) + (((_PLAIN, "plain"),) if allow_plain else ())
    for pattern, kind in kinds:
        m = pattern.search(sentence)
        if m and (best is None or m.start() < best[0].start()):
            best = (m, kind)
    if not best:
        return None
    m, kind = best
    if kind == "range":
        lo, hi = _num(m.group(1)), _num(m.group(2))
        return Ask(lo, hi, f"{_fmt(lo)}-{_fmt(hi)} yrs")
    n = _num(m.group(1))
    return Ask(n, None, f"{_fmt(n)}+ yrs" if kind in ("plus", "min") else f"{_fmt(n)} yrs")


def years_asked(text: str, structured: "str | None" = None) -> Ask:
    """The experience a posting asks for: Naukri's own field ("0-2 Yrs") if given, else the FIRST sentence of the
    description that states years next to an experience word (JDs state the overall requirement first; later
    "2 years of X" lines are about one skill)."""
    if structured and (a := _first_match(structured)):
        return a
    for sentence in re.split(r"(?<=[.!?;])\s+|\n+", text or ""):
        if _EXP_WORD.search(sentence) and (a := _first_match(sentence)):
            return a
        if _REQ_WORD.search(sentence) and (a := _first_match(sentence, allow_plain=False)):
            return a
    if _FRESHER.search(text or ""):
        return Ask(0, 0, "fresher / entry level")
    return Ask()


_SENIOR_TITLE = re.compile(
    r"\b(senior|sr\.?|lead|principal|staff|architect|head|director|vp|distinguished|fellow)\b|"
    r"\b(?:sde|swe|software (?:development )?engineer|engineer|developer)[ -]?(?:iii|iv|v|3|4|5)\b", re.I)


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
    if not re.search(s.title_include, title or "", re.I):
        return False, f"title '{title}' is not a software role"
    if m := re.search(s.title_exclude, title or "", re.I):
        return False, f"title '{title}' is excluded ('{m.group(0)}')"
    return True, ""


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
                "date_posted": self.date_posted, "notes": f"found by search: {self.search}",
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
                 rng: "random.Random | None" = None) -> "tuple[list[tuple[str, dict, bool]], list[str]]":
    """[(search label, raw row, was it a city search)], [errors]. One scrape_jobs call per term x location; a failing call is recorded and the
    rest continue. A pause between calls keeps the sites from rate-limiting."""
    scrape_fn = scrape_fn or _jobspy()
    rng = rng or random.Random()
    rows, errors = [], []
    plan = [(t, loc) for t in (only_terms or s.terms) for loc in s.locations]
    for i, (term, loc) in enumerate(plan, 1):
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
             dry_run: bool = False) -> Report:
    today = today or date.today()
    rep = Report()
    raw, rep.errors = run_searches(s, scrape_fn, sleep, log, only_terms, results)
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
            rep.drop("experience", job, f"asks {job.ask.label} (you keep a minimum up to {s.max_min_experience_years:g})")
            continue
        if job.ask.min_years is None and _SENIOR_TITLE.search(job.title):
            rep.drop("experience", job, "senior-level title and no years stated")
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


def shortlist_html(rows, cfg: "dict | None" = None) -> str:
    import html as H
    e = H.escape
    body = "".join(
        f"<tr><td>{r['id']}</td><td>{e(r['role'] or '')}</td><td>{e(r['company'] or '')}</td><td>{e(r['location'] or '')}</td>"
        f"<td>{e(r['experience_asked'] or '')}</td><td>{e(route_label(r, cfg))}</td>"
        f"<td><a href=\"{e(link_of(r))}\">{e(link_of(r)[:70])}</a></td></tr>" for r in rows)
    return ("<!doctype html><html><head><meta charset=\"utf-8\"><title>Shortlist</title><style>"
            "body{font:14px system-ui;margin:20px}table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:4px 8px;"
            "text-align:left}th{background:#f3f3f3}@media(prefers-color-scheme:dark){body{background:#161616;color:#eee}"
            "td,th{border-color:#444}th{background:#242424}a{color:#8ab4f8}}</style></head><body><h1>Shortlist</h1><table>"
            "<tr><th>ID</th><th>Title</th><th>Company</th><th>Location</th><th>Experience</th><th>Route</th><th>Link</th></tr>"
            f"{body}</table></body></html>")


def parse_ids(text: str, available: "list[int]") -> list[int]:
    """'3,7,12' / '3 7 12' / 'all'. Raises DiscoveryError on anything else, so a typo approves nothing."""
    text = (text or "").strip().lower()
    if text == "all":
        return list(available)
    try:
        return sorted({int(p) for p in re.split(r"[,\s]+", text) if p})
    except ValueError:
        raise DiscoveryError(f"could not read the ids {text!r}: use numbers like 3,7,12 or the word all") from None
