"""Company-board check: is this LinkedIn/Naukri/Indeed posting really a Greenhouse or Lever posting?

Most postings found on job boards have no employer link, so they would be "manual" even when the company hires through
Greenhouse or Lever. This looks the company's public board up and re-routes a posting ONLY when EVERYTHING matches:

  company    Greenhouse: the board's registered name equals the posting's company (after dropping Pvt/Ltd/Inc...).
             Lever: the posting page's own title names the same company (the Lever list has no company name).
  title      the same title, word for word (case and punctuation aside). Nothing is dropped: "Backend Engineer, India" and
             "Backend Engineer (Python)" are NOT "Backend Engineer", so a suffix means no match.
  location   the same place: the posting's city (Bengaluru = Bangalore) is the board job's city, or both are remote in
             India. A state-only location ("KA, IN") is not enough.
  live       the board's API still serves that exact job (HTTP 200 on the single-job endpoint).
  unique     exactly one job on one board fits; two fits, or a fit on both Greenhouse and Lever, is ambiguous.

Anything weaker stays on its original route. Every check (match or not) leaves evidence in the tracker
(`route_evidence`), the run log and the run summary. Read-only: it only GETs public board APIs.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from jobbot import discovery as D
from jobbot import intake
from jobbot import tracker as T


@dataclass
class Check:
    matched: bool
    evidence: dict
    platform: str = ""
    canonical_url: str = ""


@dataclass
class BoardReport:
    checked: int = 0
    rerouted: list = field(default_factory=list)       # evidence dicts of postings moved onto Greenhouse/Lever
    unmatched: int = 0
    errors: list = field(default_factory=list)


def _same_place(ours: str, ours_remote: "bool | None", theirs: list, s: "D.Settings") -> "tuple[bool, str]":
    """The posting's location against the board job's location(s). Strict: a city, or remote in India."""
    o = (ours or "").lower()
    ours_city = next((c for c in s.accept_cities if c in o), None)
    theirs_l = [t.lower() for t in theirs if t]
    if ours_city:
        for t in theirs_l:
            if any(c in t for c in s.accept_cities):
                return True, f"city: {ours!r} and {t!r} are both {s.accept_cities[0].title()}"
    if ours_remote or "remote" in o:
        for t in theirs_l:
            if "remote" in t and s.country_name.lower() in t:
                return True, f"remote in {s.country_name}: {ours!r} and {t!r}"
    return False, f"location differs: {ours!r} vs {theirs_l!r}"


def title_key(text: "str | None") -> str:
    """A title compared strictly: lower case, punctuation and spacing aside, but every word kept."""
    return re.sub(r"[^a-z0-9+#]+", " ", str(text or "").lower()).strip()


class _Http:
    """GETs with a per-run cache and a polite pause between real requests."""

    def __init__(self, http: Callable, sleep: Callable, pause: float):
        self.http, self.sleep, self.pause, self.cache, self.requests = http, sleep, pause, {}, 0

    def get(self, url: str) -> "tuple[int, str]":
        if url in self.cache:
            return self.cache[url]
        self.requests += 1
        try:
            result = self.http(url)
        except Exception:
            result = (0, "")
        self.cache[url] = result
        self.sleep(self.pause)
        return result

    def json(self, url: str):
        status, body = self.get(url)
        if status != 200:
            return None
        try:
            return json.loads(body)
        except ValueError:
            return None


def check_row(row, s: "D.Settings", io: _Http, now: "datetime | None" = None) -> Check:
    """Strictly decide whether `row` (a Found manual posting) is one specific Greenhouse/Lever posting."""
    company, title = row["company"] or "", row["role"] or ""
    ev: dict = {"checked": (now or datetime.now()).strftime("%Y-%m-%d"), "matched": False,
                "ours": {"company": company, "title": title, "location": row["location"], "remote": row["remote"]},
                "tried": []}
    if not company.strip():
        ev["tried"].append("the posting has no company name: nothing to match a board against")
        return Check(False, ev)
    want_company, want_title = T.norm_text(company), title_key(title)
    fits: list[dict] = []
    for token in D.company_tokens(company):
        # ---- Greenhouse ----
        info = io.json(f"https://boards-api.greenhouse.io/v1/boards/{token}")
        if info and info.get("name"):
            if T.norm_text(info["name"]) != want_company:
                ev["tried"].append(f"greenhouse:{token}: board is {info['name']!r}, not {company!r}")
            else:
                jobs = (io.json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs") or {}).get("jobs") or []
                same = [j for j in jobs if title_key(j.get("title")) == want_title]
                if not same:
                    ev["tried"].append(f"greenhouse:{token}: company matches, {len(jobs)} jobs, none titled {title!r}")
                for j in same:
                    loc = (j.get("location") or {}).get("name") or ""
                    ok, how = _same_place(row["location"], row["remote"], [loc], s)
                    if not ok:
                        ev["tried"].append(f"greenhouse:{token}: job {j.get('id')} has the title but {how}")
                        continue
                    live = io.json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{j['id']}")
                    if not live or str(live.get("id")) != str(j["id"]):
                        ev["tried"].append(f"greenhouse:{token}: job {j.get('id')} is not served by the single-job API (not live)")
                        continue
                    fits.append({"platform": "greenhouse", "board": token, "job_id": str(j["id"]),
                                 "url": intake.Target("greenhouse", token, str(j["id"])).canonical_url,
                                 "company": {"ours": company, "theirs": info["name"], "equal": True,
                                             "how": "Greenhouse board name"},
                                 "title": {"ours": title, "theirs": j.get("title"), "equal": True},
                                 "location": {"ours": row["location"], "theirs": loc, "rule": how},
                                 "live": {"endpoint": "boards-api single job", "status": 200}})
        # ---- Lever ----
        postings = io.json(f"https://api.lever.co/v0/postings/{token}?mode=json")
        if isinstance(postings, list) and postings:
            same = [p for p in postings if title_key(p.get("text")) == want_title]
            if not same:
                ev["tried"].append(f"lever:{token}: {len(postings)} postings, none titled {title!r}")
            for p in same:
                cats = p.get("categories") or {}
                places = [cats.get("location")] + list(cats.get("allLocations") or []) + list(p.get("allLocations") or [])
                ok, how = _same_place(row["location"], row["remote"], [x for x in places if x], s)
                if not ok:
                    ev["tried"].append(f"lever:{token}: posting {p.get('id')} has the title but {how}")
                    continue
                page_status, page = io.get(p.get("hostedUrl") or f"https://jobs.lever.co/{token}/{p.get('id')}")
                their_company = ""
                if page_status == 200:
                    m = re.search(r"<title>(.*?)</title>", page, re.I | re.S)
                    their_company = intake.lever_company_from_title(re.sub(r"\s+", " ", m.group(1)).strip() if m else "",
                                                                    p.get("text") or "")
                if not their_company or T.norm_text(their_company) != want_company:
                    ev["tried"].append(f"lever:{token}: posting {p.get('id')} fits, but its page names {their_company!r}, "
                                       f"not {company!r}")
                    continue
                live = io.json(f"https://api.lever.co/v0/postings/{token}/{p['id']}")
                if not live or str(live.get("id")) != str(p["id"]):
                    ev["tried"].append(f"lever:{token}: posting {p.get('id')} is not served by the single-posting API (not live)")
                    continue
                fits.append({"platform": "lever", "board": token, "job_id": str(p["id"]),
                             "url": intake.Target("lever", token, str(p["id"])).canonical_url,
                             "company": {"ours": company, "theirs": their_company, "equal": True, "how": "Lever page title"},
                             "title": {"ours": title, "theirs": p.get("text"), "equal": True},
                             "location": {"ours": row["location"], "theirs": [x for x in places if x], "rule": how},
                             "live": {"endpoint": "api.lever.co single posting", "status": 200}})
    if len(fits) == 1:
        ev.update(matched=True, **{k: fits[0][k] for k in ("platform", "board", "job_id", "url", "company", "title", "location", "live")})
        return Check(True, ev, fits[0]["platform"], fits[0]["url"])
    if len(fits) > 1:
        ev["tried"].append(f"ambiguous: {len(fits)} postings fit ({', '.join(f['url'] for f in fits)}): left on its route")
    return Check(False, ev)


def check_found(tracker, s: "D.Settings", *, min_relevance: int = 5, max_companies: int = 150, recheck_days: int = 7,
                http: "Callable | None" = None, sleep: Callable = time.sleep, pause_s: float = 0.2, now=datetime.now,
                dry_run: bool = False, log=print) -> BoardReport:
    """Check scored Found postings on the manual route. A match is re-routed (unless dry_run) and logged with its evidence;
    everything else keeps its route. Each check is remembered for `recheck_days` so a daily run does not repeat it."""
    rep = BoardReport()
    io = _Http(http or intake.default_http, sleep, pause_s)
    seen_companies: set = set()
    for row in tracker.board_candidates(min_relevance, recheck_days, now()):
        key = T.norm_text(row["company"])
        if key not in seen_companies:
            if len(seen_companies) >= max_companies:
                break
            seen_companies.add(key)
        chk = check_row(row, s, io, now())
        rep.checked += 1
        if not dry_run:       # a preview must not use up the weekly check
            tracker.set_board_checked(row["id"], json.dumps(chk.evidence, ensure_ascii=False), chk.evidence["checked"])
        if not chk.matched:
            rep.unmatched += 1
            continue
        e = chk.evidence
        log(f"  BOARD MATCH #{row['id']} {row['company']} | {row['role']} -> {chk.platform} {chk.canonical_url}\n"
            f"      company  {e['company']['ours']!r} = {e['company']['theirs']!r} ({e['company']['how']})\n"
            f"      title    {e['title']['ours']!r} = {e['title']['theirs']!r}\n"
            f"      location {e['location']['rule']}\n"
            f"      live     {e['live']['endpoint']} -> HTTP {e['live']['status']}")
        if dry_run:
            rep.rerouted.append({"id": row["id"], "dry_run": True, **e})
            continue
        why = (f"board check: the same company, title and location are live on {chk.platform} ({chk.canonical_url}); "
               f"was {row['source']} only")
        outcome = tracker.reroute(row["id"], chk.platform, chk.canonical_url, why)
        rep.rerouted.append({"id": row["id"], "outcome": outcome, **e})
        if outcome == "duplicate":
            log(f"      (already in the tracker: skipped, never applied for twice)")
    log(f"  board check: {rep.checked} checked ({io.requests} requests), {len([r for r in rep.rerouted])} matched, "
        f"{rep.unmatched} left on their route")
    return rep
