"""Tense checks for a job that has ended (profile.employment.last_working_day has passed).

Two checks, both pure text rules, neither rewrites anything:
  resume bullets    present-tense wording in the bullets of the ended role ("Owns", "which handles", "currently")
  cover-letter text present-tense or "current position" wording about that job (used by coverletter.check_letter)
"""
from __future__ import annotations

import re

_BASE = ("own lead manage build maintain drive deliver handle develop design write work run coordinate oversee support "
         "act serve implement create optimi[sz]e integrate monitor deploy automate collaborate architect ship wire "
         "delegate mentor review test document analy[sz]e configure migrate resolve").split()
_FIRST_WORD = re.compile(r"^(?:%s)(?:s|es)?$" % "|".join(_BASE), re.I)
_PHRASES = re.compile(
    r"\b(currently|presently|at present|ongoing)\b|"
    r"\b(?:is|are) (?:responsible|owning|leading|managing|building|handling|driving|maintaining)\b|"
    r"\b(?:which|that|who) (?:handles|serves|processes|runs|manages|supports|powers|owns|drives|delivers|"
    r"enables|allows)\b", re.I)

# ── cover letters ──
CURRENT_JOB = re.compile(
    r"\b(currently|presently|at present|these days)\b|"
    r"\b(?:current|present) (?:position|role|job|employer|company|title|organi[sz]ation)\b|\bmy (?:current|present)\b|"
    r"\bI(?: am|'m|\u2019m) (?:now |still )?(?:working|employed)\b|\bI (?:now |still )?works? (?:at|for|as)\b", re.I)
PRESENT_I = re.compile(
    r"\bI(?: am|'m|\u2019m) (?:responsible|leading|owning|managing|building|running|handling|driving|maintaining)\b|"
    r"\bI (?:own|lead|manage|run|handle|drive|deliver|oversee|coordinate|act|delegate|maintain|serve|build|wire|develop|"
    r"design|write|deploy|ship|support|implement|create|monitor|automate|collaborate)\b", re.I)


def letter_problems(text: str, company: str, ended_on: str) -> list[str]:
    """Reasons a letter must not be used once the job at `company` has ended."""
    hits = [m.group(0) for m in (CURRENT_JOB.search(text), PRESENT_I.search(text)) if m]
    if not hits:
        return []
    return [f"describes the {company} job in the present tense or as the current position ({'; '.join(hits)}), but it "
            f"ended on {ended_on}: use the past tense (\"At {company}, I owned...\") and never call it current"]


# ── resume bullets ──

def _plain(latex: str) -> str:
    text = re.sub(r"\\textbf\{([^{}]*)\}", r"\1", latex)
    text = re.sub(r"\\[A-Za-z]+\*?(\{[^{}]*\})?", " ", text)
    return re.sub(r"\s+", " ", text.replace("{", "").replace("}", "").replace("---", " - ")).strip()


def bullet_problems(text: str) -> list[str]:
    """Present-tense wording in one bullet (empty list: reads as past tense)."""
    plain = _plain(text)
    out = []
    first = re.match(r"[A-Za-z]+", plain)
    if first and _FIRST_WORD.match(first.group(0)):
        out.append(f"starts with the present-tense verb '{first.group(0)}'")
    for m in _PHRASES.finditer(plain):
        out.append(f"contains present-tense wording '{m.group(0)}'")
    return out


def resume_problems(resume: dict, company: str, edits: "dict | None" = None) -> list[str]:
    """Every bullet of the ended role (shown or hidden) that reads as present tense. `edits` maps bullet id -> the
    keyword-edited text that is actually on the tailored resume."""
    out = []
    for entry in resume.get("experience", []):
        if entry.get("company") != company:
            continue
        for b in entry.get("bullets", []):
            text = (edits or {}).get(b["id"], b["text"])
            for problem in bullet_problems(text):
                out.append(f"{company} bullet {b['id']}: {problem}: \"{_plain(text)[:70]}...\"")
    return out
