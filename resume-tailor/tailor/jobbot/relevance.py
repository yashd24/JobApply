"""Relevance scoring: for postings that passed the cheap rules, how well does each fit YOUR resume?

One Claude call per batch of 10 postings. Claude sees only the visible resume content, a few non-sensitive facts (where
you are based, about how many years you have) and the postings; never salary, notice period, work authorisation, EEO or
contact details. Code validates every score (a whole number 1-10, for a posting that was in the batch); anything else is
left unscored and tried again next run. Scores and reasons are stored in the tracker, so no posting is scored twice.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date

from jobbot import answers as A
from jobbot import profile as P

BATCH_SIZE = 10
DEFAULT_AGGREGATORS = ["Uplers", "Ibrowsejobs", "Jobgether"]
DESCRIPTION_CHARS = 1800
NO_TEXT_CAP = 6               # a posting with no description can be judged on its title only: never above this
REASON_CHARS = 200


def candidate_summary(resume: dict, profile: dict, today: "date | None" = None) -> str:
    """What the scorer may know about the candidate: the visible resume and three plain facts."""
    try:
        years = P.experience_months(profile, today) / 12
        experience = f"about {years:.1f} years of experience (full-time plus internship)"
    except Exception:
        experience = "about 1.5-2 years of experience"
    return (f"RESUME (the only source of what the candidate can do):\n{A.visible_text(resume)}\n\n"
            f"FACTS: based in Bengaluru, India; {experience}; looking for backend roles in any language (Python/Django "
            "preferred), backend-heavy full stack, or general Software Engineer / Software Developer / SDE 1 / Associate "
            "Software Engineer roles.")


def build_prompt(rows: list, summary: str) -> str:
    jobs = []
    for r in rows:
        text = re.sub(r"\n{3,}", "\n\n", (r["description"] or "").strip())[:DESCRIPTION_CHARS] or "(no description available)"
        jobs.append(f"### JOB {r['id']}\nTitle: {r['role']}\nCompany: {r['company']}\nLocation: {r['location']}\n"
                    f"Experience asked: {r['experience_asked'] or 'not stated'}\nDescription:\n<<<\n{text}\n>>>")
    return f"""You are scoring job postings for ONE candidate, honestly. Do not use any tools. Reply with ONE JSON object only,
no prose, no code fences:
{{"scores": [{{"id": <job id>, "score": <whole number 1-10>, "reason": "<one line, under 25 words>",
"exceptional": <true or false>}}, ...]}}
with exactly one entry per job below. "exceptional" is true ONLY when the posting's core stack is exactly the resume's with
no gap worth mentioning (it matters for rule 1).

{summary}

HOW TO SCORE (be honest; a high score must be earned from the resume above, not assumed):
- 9-10: the work is what the resume shows (Python/Django backend, APIs, the stack and experience level asked match).
- 7-8: a solid fit: backend or general software work the candidate can do well, with a small gap or two.
- 5-6: partial: some of the work fits, or the main stack is one the resume only touches.
- 1-4: mainly needs skills or domain the resume does not show (for example mostly Java/Spring or .NET or C++ with no Python,
  data engineering, ML research, security, SAP/CRM, networking), or is not really a software engineering role, or asks
  clearly more than the candidate has. Roles mainly needing skills the candidate does not have score LOW.
- A posting with no description can only be judged on its title: never above {NO_TEXT_CAP}.
- RULES THAT CAP THE SCORE (code enforces them too, so score by them honestly):
  1. A posting whose MINIMUM experience is 3 or more years scores at most 6, unless "exceptional" is true.
  2. A posting with no company name scores at most 6.
  3. A posting from a recruiting platform, staffing firm or job aggregator (not the employer itself) scores at most 6.
  4. The score must reflect every caveat in your own reason. If the reason says "below level", "overqualified", "major gap",
     "mismatch" or similar, the score is 6 or lower. A small or modest gap costs a point; do not write a serious caveat and
     still give 8.
  5. Java-centric roles (Java/Spring/Kotlin/Scala is the main stack) score at most 5: the candidate's experience is
     Python/Django, with Java only listed.
- Base every score on what the posting asks versus what the resume shows. The reason names the deciding fact.

JOBS:

{chr(10).join(jobs)}
"""


# ─── deterministic caps (applied in code to whatever Claude says) ────────────────────────────────────────

@dataclass
class Rules:
    """The scoring rules code enforces. `cap` is the ceiling for rules 1-4; `java_cap` for Java-centric roles."""
    cap: int = 6
    min_years_from: float = 3                    # a posting whose MINIMUM experience is this or more ...
    aggregators: list = field(default_factory=lambda: list(DEFAULT_AGGREGATORS))
    aggregator_action: str = "cap"               # "cap" here; "drop" is applied before scoring (discovery.recheck_found)
    java_cap: int = 5


def rules_from_config(cfg: dict) -> Rules:
    sel, disc = (cfg or {}).get("selection") or {}, (cfg or {}).get("discovery") or {}
    action = str(disc.get("aggregator_action", "cap"))
    if action not in ("cap", "drop"):
        raise ValueError("discovery.aggregator_action must be 'cap' or 'drop'")
    return Rules(cap=int(sel.get("score_cap", 6)), min_years_from=float(sel.get("cap_min_years_from", 3)),
                 aggregators=[str(x) for x in disc.get("aggregators", DEFAULT_AGGREGATORS)], aggregator_action=action,
                 java_cap=int(sel.get("java_cap", 5)))


_GAP = re.compile(r"\b(gap|gaps|lack|lacks|lacking|missing|not shown|unshown|stretch|absent|no exposure|without)\b", re.I)
# Words that mean "this is a real problem". A score of 7+ with one of these in its own reason contradicts itself.
_STRONG = re.compile(
    r"\b(major|significant|serious|large|big|substantial|critical|key)\s+(gap|gaps|mismatch|stretch)\b|"
    r"\bbelow\b[^.;]{0,30}\b(level|experience|seniority)\b|\b(level|seniority)\b[^.;]{0,25}\bbelow\b|"
    r"\b(fresher|entry|junior)[- ]level\b|\btoo junior\b|\bover-?qualified\b|"
    r"\b(far|well|much)\s+(above|below)\b|\bmismatch\b|\bnot really a software\b|"
    r"\b(mainly|mostly|primarily)\s+(needs|requires|centers|centres|focused|about|java|c\+\+|\.net|go\b|golang)", re.I)
_JAVA_TITLE = re.compile(r"\bjava\b|\bspring\s*boot\b|\bj2ee\b|\bkotlin\b|\bscala\b", re.I)
_PY_TITLE = re.compile(r"python|django|flask|fastapi", re.I)
_JAVA_BODY = re.compile(r"\bjava\b|spring(?:\s*boot)?|hibernate|j2ee|\bjsp\b|\bjpa\b|\bmaven\b|\bkotlin\b", re.I)
_PY_BODY = re.compile(r"\bpython\b|django|flask|fastapi|celery", re.I)


def min_years_of(row) -> "float | None":
    """The MINIMUM experience the posting asks for, from the label the selection rules stored ("3-5 yrs", "3+ yrs")."""
    m = re.match(r"\s*(\d+(?:\.\d+)?)", str(row["experience_asked"] or ""))
    return float(m.group(1)) if m else None


def java_centric(title: str, description: str) -> bool:
    """Java (Spring, Kotlin, Scala) is the main stack: in the title without Python/Django, or the text is mostly Java."""
    if _JAVA_TITLE.search(title or "") and not _PY_TITLE.search(title or ""):
        return True
    j, p = len(_JAVA_BODY.findall(description or "")), len(_PY_BODY.findall(description or ""))
    return j >= 4 and j > 2 * p


def is_aggregator(company: str, names: list) -> "str | None":
    """Which listed platform this company is: its name appears as WHOLE WORDS ("Ibrowsejobs Technologies" matches
    "Ibrowsejobs"; "Couplers Inc" does not match "Uplers"), ignoring case and punctuation."""
    words = " " + re.sub(r"[^a-z0-9]+", " ", (company or "").lower()).strip() + " "
    for n in names:
        key = re.sub(r"[^a-z0-9]+", " ", str(n).lower()).strip()
        if key and f" {key} " in words:
            return str(n)
    return None


def apply_rules(row, score: int, reason: str, exceptional: bool, rules: "Rules") -> "tuple[int, str]":
    """Cap Claude's score by the five rules. Returns the final score and the reason with a note of what was changed."""
    caps: list[tuple[int, str]] = []
    years = min_years_of(row)
    if years is not None and years >= rules.min_years_from:
        if not (exceptional and not _GAP.search(reason)):          # "exceptional" only counts if the reason names no gap
            caps.append((rules.cap, f"asks {row['experience_asked']} (minimum {years:g}+ years)"))
    if not (row["company"] or "").strip():
        caps.append((rules.cap, "no company name"))
    if rules.aggregator_action == "cap" and (hit := is_aggregator(row["company"], rules.aggregators)):
        caps.append((rules.cap, f"{hit} is a recruiting platform / aggregator"))
    if (m := _STRONG.search(reason)):
        caps.append((rules.cap, f"its own reason says '{m.group(0)}'"))
    if java_centric(row["role"] or "", row["description"] or ""):
        caps.append((rules.java_cap, "Java-centric role (your experience is Python/Django)"))
    if not caps:
        return score, reason
    ceiling = min(c for c, _ in caps)
    if score <= ceiling:
        return score, reason
    why = "; ".join(w for c, w in caps if c <= score)
    return ceiling, f"{reason} [model said {score}, capped at {ceiling}: {why}]"[:REASON_CHARS + 160]


@dataclass
class Parsed:
    scores: dict = field(default_factory=dict)         # id -> (score, reason)
    exceptional: dict = field(default_factory=dict)    # id -> the model's "exceptional" flag (only True counts)
    rejected: list = field(default_factory=list)       # (id or None, why)


def parse_reply(reply: dict, rows: list, rules: "Rules | None" = None) -> Parsed:
    """Validate Claude's answer against the batch it was asked about, then apply the caps in `rules` (if given)."""
    out = Parsed()
    ids = {r["id"]: r for r in rows}
    entries = reply.get("scores") if isinstance(reply, dict) else None
    if not isinstance(entries, list):
        out.rejected.append((None, "the reply has no 'scores' list"))
        return out
    for e in entries:
        try:
            jid, raw = int(e["id"]), e["score"]
        except (KeyError, TypeError, ValueError):
            out.rejected.append((None, f"unreadable entry {str(e)[:60]!r}"))
            continue
        if jid not in ids:
            out.rejected.append((jid, "not a job in this batch"))
            continue
        if jid in out.scores:
            out.rejected.append((jid, "scored twice"))
            continue
        if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw != int(raw) or not 1 <= int(raw) <= 10:
            out.rejected.append((jid, f"score {raw!r} is not a whole number from 1 to 10"))
            continue
        score, reason = int(raw), re.sub(r"\s+", " ", str(e.get("reason") or "")).strip()[:REASON_CHARS]
        if not reason:
            out.rejected.append((jid, "no reason given"))
            continue
        if not (ids[jid]["description"] or "").strip() and score > NO_TEXT_CAP:
            score, reason = NO_TEXT_CAP, f"{reason} (capped at {NO_TEXT_CAP}: no description to judge from)"[:REASON_CHARS + 40]
        if rules is not None:
            score, reason = apply_rules(ids[jid], score, reason, e.get("exceptional") is True, rules)
        out.scores[jid] = (score, reason)
        out.exceptional[jid] = e.get("exceptional") is True
    return out


@dataclass
class ScoreReport:
    scored: int = 0
    calls: int = 0
    left_unscored: list = field(default_factory=list)
    problems: list = field(default_factory=list)


def score_unscored(tracker, llm, resume: dict, profile: dict, *, batch_size: int = BATCH_SIZE, log=print,
                   today: "date | None" = None, rules: "Rules | None" = None) -> ScoreReport:
    """Score every Found posting that has no score, 10 per call. Progress is saved after each batch, so a usage limit
    (UsageLimitError propagates) loses nothing: the next run continues with what is still unscored."""
    rep = ScoreReport()
    rows = tracker.unscored()
    summary = candidate_summary(resume, profile, today)
    for i in range(0, len(rows), batch_size):
        batch = rows[i:i + batch_size]
        log(f"  scoring {i + 1}-{i + len(batch)} of {len(rows)}...")
        rep.calls += 1
        try:
            parsed = parse_reply(llm(build_prompt(batch, summary)), batch, rules)
        except Exception as e:                 # a bad reply loses one batch, not the run; a usage limit stops everything
            if type(e).__name__ == "UsageLimitError":
                raise
            rep.problems.append(f"batch {rep.calls}: {type(e).__name__}: {str(e)[:120]}")
            rep.left_unscored += [r["id"] for r in batch]
            continue
        for jid, (score, reason) in parsed.scores.items():
            tracker.set_relevance(jid, score, reason, parsed.exceptional.get(jid, False))
            rep.scored += 1
        rep.left_unscored += [r["id"] for r in batch if r["id"] not in parsed.scores]
        rep.problems += [f"batch {rep.calls}: job {jid if jid is not None else '?'}: {why}" for jid, why in parsed.rejected]
    return rep


def recap_found(tracker, rules: "Rules", log=print) -> list:
    """Apply the CURRENT caps to the scores already stored on Found postings (no Claude, no network), so a rule change takes
    effect at once. A score stored before the model's "exceptional" flag was kept counts as exceptional if it is 7 or more
    (that is the only way a 3+ year posting got past the cap), so rule 1 never removes a pass it once granted."""
    changed = []
    for r in tracker.found():
        if r["relevance"] is None:
            continue
        flag = r["relevance_exceptional"]
        exceptional = bool(flag) if flag is not None else r["relevance"] >= 7
        score, reason = apply_rules(r, r["relevance"], r["relevance_reason"] or "", exceptional, rules)
        if score != r["relevance"]:
            tracker.set_relevance(r["id"], score, reason, bool(exceptional))
            changed.append({"id": r["id"], "company": r["company"], "title": r["role"], "was": r["relevance"], "now": score})
            log(f"  rescored in place #{r['id']} {r['role']} | {r['company']}: {r['relevance']} -> {score}")
    return changed


def format_top(rows, n: int = 10) -> str:
    """The best-scored postings as a table: score, title, company, experience asked, reason (with any cap noted)."""
    top = sorted((r for r in rows if r["relevance"] is not None), key=lambda r: (-r["relevance"], r["id"]))[:n]
    esc = lambda x: str(x or "").replace("|", "/").replace("\n", " ")        # noqa: E731
    out = [f"TOP {len(top)} BY RELEVANCE", "| # | ID | Score | Title | Company | Experience asked | Reason |",
           "|--:|--:|--:|---|---|---|---|"]
    for i, r in enumerate(top, 1):
        out.append(f"| {i} | {r['id']} | {r['relevance']} | {esc(r['role'])} | {esc(r['company']) or '(blank)'} | "
                   f"{esc(r['experience_asked'])} | {esc(r['relevance_reason'])} |")
    return "\n".join(out)
