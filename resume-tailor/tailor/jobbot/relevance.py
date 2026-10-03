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
{{"scores": [{{"id": <job id>, "score": <whole number 1-10>, "reason": "<one line, under 25 words>"}}, ...]}}
with exactly one entry per job below.

{summary}

HOW TO SCORE (be honest; a high score must be earned from the resume above, not assumed):
- 9-10: the work is what the resume shows (Python/Django backend, APIs, the stack and experience level asked match).
- 7-8: a solid fit: backend or general software work the candidate can do well, with a small gap or two.
- 5-6: partial: some of the work fits, or the main stack is one the resume only touches.
- 1-4: mainly needs skills or domain the resume does not show (for example mostly Java/Spring or .NET or C++ with no Python,
  data engineering, ML research, security, SAP/CRM, networking), or is not really a software engineering role, or asks
  clearly more than the candidate has. Roles mainly needing skills the candidate does not have score LOW.
- A posting with no description can only be judged on its title: never above {NO_TEXT_CAP}.
- Base every score on what the posting asks versus what the resume shows. The reason names the deciding fact.

JOBS:

{chr(10).join(jobs)}
"""


@dataclass
class Parsed:
    scores: dict = field(default_factory=dict)         # id -> (score, reason)
    rejected: list = field(default_factory=list)       # (id or None, why)


def parse_reply(reply: dict, rows: list) -> Parsed:
    """Validate Claude's answer against the batch it was asked about."""
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
        out.scores[jid] = (score, reason)
    return out


@dataclass
class ScoreReport:
    scored: int = 0
    calls: int = 0
    left_unscored: list = field(default_factory=list)
    problems: list = field(default_factory=list)


def score_unscored(tracker, llm, resume: dict, profile: dict, *, batch_size: int = BATCH_SIZE, log=print,
                   today: "date | None" = None) -> ScoreReport:
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
            parsed = parse_reply(llm(build_prompt(batch, summary)), batch)
        except Exception as e:                 # a bad reply loses one batch, not the run; a usage limit stops everything
            if type(e).__name__ == "UsageLimitError":
                raise
            rep.problems.append(f"batch {rep.calls}: {type(e).__name__}: {str(e)[:120]}")
            rep.left_unscored += [r["id"] for r in batch]
            continue
        for jid, (score, reason) in parsed.scores.items():
            tracker.set_relevance(jid, score, reason)
            rep.scored += 1
        rep.left_unscored += [r["id"] for r in batch if r["id"] not in parsed.scores]
        rep.problems += [f"batch {rep.calls}: job {jid if jid is not None else '?'}: {why}" for jid, why in parsed.rejected]
    return rep
