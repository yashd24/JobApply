#!/usr/bin/env python3
"""
Tailor Yashdeep's resume to a job description.

  python tailor.py --company "Stripe" --role "Backend Engineer" --jd-file jd.txt
  python tailor.py --company "Razorpay" --role "SDE 1" --jd-url https://jobs.lever.co/...
  python tailor.py --company "X" --role "Y"            (paste JD, then Ctrl+Z Enter on Windows)
  python tailor.py --base                             (render the untailored resume)

Flow: JD -> Claude Code (headless, your Pro subscription) reorders the base resume's
      bullets/skills and inserts JD keywords -> guard.py validates every change ->
      renders the .tex -> compiles with XeLaTeX -> if over 1 page, reverts keyword
      edits (never bullets). The selection is locked to the base resume; the old
      add/drop-bullets behaviour is opt-in with --allow-selection.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import yaml
from pypdf import PdfReader

import guard
import render

ROOT = Path(__file__).resolve().parent
TEMPLATE_DIR = ROOT / "template"
DATA_FILE = ROOT / "resume_data.yaml"
CONTACT_FILE = ROOT / "contact.yaml"      # gitignored: the email and phone printed in the header
OUTPUT_DIR = ROOT / "output"
PDF_NAME = "Yashdeep_Sahu_Resume.pdf"

try:
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles
except Exception:
    pass


class TailorError(RuntimeError):
    """A recoverable failure in tailoring (missing tool, Claude error, LaTeX error...)."""


def load_resume_data() -> dict:
    """resume_data.yaml plus the private email/phone from contact.yaml."""
    data = yaml.safe_load(DATA_FILE.read_text(encoding="utf-8"))
    if not CONTACT_FILE.exists():
        raise TailorError(f"{CONTACT_FILE.name} not found. Copy contact.example.yaml to contact.yaml and "
                          "fill in your email and phone (it is gitignored).")
    contact = yaml.safe_load(CONTACT_FILE.read_text(encoding="utf-8")) or {}
    for key in ("email", "phone"):
        value = str(contact.get(key) or "").strip()
        if not value or value.upper() == "TODO":
            raise TailorError(f"{CONTACT_FILE.name}: '{key}' is missing or still TODO.")
        data[key] = value
    return data


# ─── Job description input ───────────────────────────────────────────────────

def fetch_jd(url: str) -> str | None:
    import requests
    from bs4 import BeautifulSoup
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
    except Exception as e:
        print(f"  Could not fetch URL: {e}")
        return None
    soup = BeautifulSoup(r.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()
    for sel in ["#content", ".posting-page", ".job-post", "#app_body", "main", "article"]:
        el = soup.select_one(sel)
        if el and len(el.get_text(strip=True)) > 300:
            return el.get_text("\n", strip=True)
    text = soup.get_text("\n", strip=True)
    return text if len(text) > 300 else None


def read_jd(args) -> str:
    if args.jd_file:
        return Path(args.jd_file).read_text(encoding="utf-8")
    if args.jd_url:
        text = fetch_jd(args.jd_url)
        if text:
            print(f"  Fetched JD ({len(text)} chars)")
            return text
        print("  The page needs a login or JavaScript (common for LinkedIn/Naukri/Workday).")
    print("Paste the job description, then press Ctrl+Z and Enter (Windows) / Ctrl+D (Mac/Linux):")
    return sys.stdin.read()


# ─── Claude Code (headless) ──────────────────────────────────────────────────

def build_prompt(data: dict, jd: str, company: str, role: str, *, allow_selection: bool = False) -> str:
    """Locked prompt by default (base bullets only; reorder + keyword edits).
    The old select-from-the-whole-bank prompt is opt-in via allow_selection."""
    if allow_selection:
        return _selection_prompt(data, jd, company, role)
    return _locked_prompt(data, jd, company, role)


def _locked_prompt(data: dict, jd: str, company: str, role: str) -> str:
    base = render.default_plan(data)
    texts = render._bullet_lookup(data)
    names = {e["id"]: f"{e['company']} | {e['position']} | {e['dates']}" for e in data["experience"]}
    names.update({p["id"]: f"{p['name']} | {p['stack']}" for p in data["projects"]})

    lines = []
    for section in ("experience", "projects"):
        for e in base[section]:
            label = "experience" if section == "experience" else "project"
            lines.append(f"\n[{label}:{e['id']}] {names[e['id']]}")
            for bid in e["bullets"]:
                lines.append(f"  - {bid}: {texts[bid]}")
    skills = {s["label"]: s["items"] for s in data["skills"]}

    return f"""You are tailoring a one-page software engineering resume to a job description.
Do not use any tools. Reply with ONE JSON object only, no prose, no code fences.

TARGET: {role} at {company}

JOB DESCRIPTION:
<<<
{jd.strip()[:12000]}
>>>

THE RESUME (LaTeX). Its sections, entries and bullets are FIXED. These are all the bullets there are:
{chr(10).join(lines)}

SKILLS (you may only reorder items within each line, never add or remove):
{json.dumps(skills, ensure_ascii=False)}

WHAT YOU MAY DO - only these three things:
 (a) Reorder the bullets within each role/project, most relevant to the JD first. Your list for each entry must
     contain exactly that entry's bullet ids above, each once. You cannot add, drop or move bullets between entries.
 (b) Reorder the items within each skills line.
 (c) Add JD keywords to existing bullets. Insert or swap in words that appear verbatim in the JD, ONLY where
     they truthfully describe that bullet's work (e.g. "APIs" -> "RESTful APIs", "notification workflow" ->
     "multi-tenant notification workflow" if the bullet really is that). Keep the original sentence, keep every
     number exactly, keep LaTeX (\\textbf{{}}, \\%, \\&). At most ~6 words changed per bullet.
     NEVER add a technology, tool, framework, cloud service or product name (e.g. Kubernetes, CloudFormation,
     Django Channels) unless it already appears on the resume above (skills line or a bullet); a JD requirement you
     cannot back with the resume belongs in "gaps", not in a bullet. Plain descriptive JD words need no such backing.
     You may also add up to 3 short grammar linking words that are not in the JD (e.g. "and", "that", "while").
     If no JD term fits honestly, do not edit that bullet.

RULES
1. Rank bullets by MEANING, not literal keywords: a bullet covers a JD requirement if it describes the same kind
   of work even in different words (e.g. an OAuth mailbox integration with Microsoft 365/Gmail counts as "third
   party integrations (mails)"; pushing data to HubSpot or calling OpenAI counts as third-party integration;
   compliance and security testing count as security).
2. Scores are 1-10 and must be honest. "gaps" = JD requirements that NO bullet above supports, by meaning.
   Before listing a gap, check every bullet: if one covers the requirement, it is not a gap.
   Do not list vague items (seniority, years of experience) as gaps unless the JD states a hard requirement.

OUTPUT JSON SCHEMA
{{
  "experience": [{{"id": "entry_id", "bullets": ["every bullet id of that entry, reordered"]}}],
  "projects": [{{"id": "entry_id", "bullets": ["every bullet id of that entry, reordered"]}}],
  "skills": {{"Languages:": ["..."]}},
  "edits": {{"bullet_id": "full edited LaTeX text of that bullet"}},
  "scores": {{"skills_match": 0, "experience_match": 0, "overall": 0}},
  "jd_keywords_matched": ["..."],
  "gaps": ["..."]
}}"""


def _selection_prompt(data: dict, jd: str, company: str, role: str) -> str:
    base = render.default_plan(data)
    base_count = sum(len(e["bullets"]) for e in base["experience"] + base["projects"])

    lines = []
    for e in data["experience"]:
        tag = "REQUIRED" if e.get("required") else "optional"
        lines.append(f"\n[experience:{e['id']}] ({tag}) {e['company']} | {e['position']} | {e['dates']}")
        for b in e["bullets"]:
            conf = f"  (never together with: {', '.join(b['conflicts_with'])})" if b.get("conflicts_with") else ""
            lines.append(f"  - {b['id']}: {b['text']}{conf}")
    for p in data["projects"]:
        lines.append(f"\n[project:{p['id']}] {p['name']} | {p['stack']}")
        for b in p["bullets"]:
            lines.append(f"  - {b['id']}: {b['text']}")
    lines.append("\n[cocurricular]")
    for c in data.get("cocurricular", []):
        lines.append(f"  - {c['id']}: {c['text']}")
    skills = {s["label"]: s["items"] for s in data["skills"]}

    return f"""You are tailoring a one-page software engineering resume to a job description.
Do not use any tools. Reply with ONE JSON object only, no prose, no code fences.

TARGET: {role} at {company}

JOB DESCRIPTION:
<<<
{jd.strip()[:12000]}
>>>

CONTENT BANK (LaTeX). You may only use these bullets, referenced by id:
{chr(10).join(lines)}

SKILLS (you may only reorder items within each line, never add or remove):
{json.dumps(skills, ensure_ascii=False)}

RULES
1. Choose bullets that best match the JD. Order bullets within each entry by relevance (most relevant first).
   Match on MEANING, not literal keywords: a bullet covers a JD requirement if it describes the same kind of work
   even in different words (e.g. an OAuth mailbox integration with Microsoft 365/Gmail counts as "third party
   integrations (mails)"; pushing data to HubSpot or calling OpenAI counts as third-party integration; blocking
   domains, compliance and security testing count as security). Judge every bullet in the bank against every JD
   requirement before deciding.
2. Both REQUIRED experience entries must appear. Optional entries/projects/cocurricular only if they strengthen the match.
   PREFER CURRENT PRODUCTION WORK: when a current, real-world role bullet and a side project or older internship
   bullet show the same skill, choose the current-role bullet. Use side projects and older internships only for
   JD requirements that no current-role bullet covers.
3. The page is full: keep the TOTAL number of bullets close to {base_count}. Prefer depth on the strongest matches.
4. Never select two bullets marked as "never together".
5. Bullet edits: you may ONLY insert or swap in terms that appear verbatim in the JD, where they truthfully
   describe the same work (e.g. "APIs" -> "RESTful APIs" if the JD says RESTful). Keep the original sentence,
   keep every number exactly, keep LaTeX (\\textbf{{}}, \\%, \\&). At most ~6 words changed per bullet.
   If no JD term fits honestly, do not edit that bullet. Never claim skills or tools not in the bank.
6. Scores are 1-10 and must be honest. "gaps" = JD requirements that NO bullet in the bank supports, by meaning.
   Before listing a gap, check the whole bank: if any bullet (selected or not) covers the requirement, it is not a
   gap. If an unselected bullet covers a requirement no selected bullet does, select it instead of reporting a gap.
   Do not list vague items (seniority, years of experience) as gaps unless the JD states a hard requirement.

OUTPUT JSON SCHEMA
{{
  "experience": [{{"id": "zintlr_asde", "bullets": ["bullet_id", "..."]}}],
  "projects": [{{"id": "project_id", "bullets": ["bullet_id"]}}],
  "cocurricular": ["cc_id"],
  "skills": {{"Languages:": ["..."]}},
  "edits": {{"bullet_id": "full edited LaTeX text of that bullet"}},
  "scores": {{"skills_match": 0, "experience_match": 0, "overall": 0}},
  "jd_keywords_matched": ["..."],
  "gaps": ["..."]
}}"""


def call_claude(prompt: str) -> dict:
    exe = shutil.which("claude") or shutil.which("claude.cmd")  # Windows npm installs use claude.cmd
    if not exe:
        raise TailorError("Claude Code CLI not found on PATH. Install it and run `claude` once to log in.")
    try:
        proc = subprocess.run([exe, "-p", "--output-format", "json"], input=prompt,
                              capture_output=True, text=True, encoding="utf-8", timeout=600)
    except subprocess.TimeoutExpired:
        raise TailorError("Claude Code timed out after 10 minutes.")
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        raise TailorError(f"Claude Code returned unexpected output (exit {proc.returncode}).\n"
                          f"{proc.stdout[-1500:]}\n{proc.stderr[-1500:]}\n"
                          "If this mentions a usage limit, wait for your Pro limit to reset and re-run.")
    if envelope.get("is_error"):
        raise TailorError(f"Claude Code reported an error: {envelope.get('result')}")
    return parse_json_reply(envelope.get("result", ""))


def parse_json_reply(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise TailorError(f"Could not find JSON in Claude's reply:\n{text[:1500]}")
    return json.loads(text[start:end + 1])


# ─── Compile & fit to one page ───────────────────────────────────────────────

def compile_pdf(tex: str, build_dir: Path) -> tuple[Path, int]:
    build_dir.mkdir(parents=True, exist_ok=True)
    for item in TEMPLATE_DIR.iterdir():
        dest = build_dir / item.name
        if item.is_dir() and not dest.exists():
            shutil.copytree(item, dest)
        elif item.is_file() and item.suffix in {".cls", ".sty"}:
            shutil.copy2(item, dest)
    (build_dir / "resume.tex").write_text(tex, encoding="utf-8")

    if shutil.which("xelatex"):
        cmd = ["xelatex", "-interaction=nonstopmode", "-halt-on-error", "resume.tex"]
    elif shutil.which("tectonic"):
        cmd = ["tectonic", "resume.tex"]
    else:
        raise TailorError("No LaTeX engine found. Install MiKTeX (with XeLaTeX) or Tectonic.")
    proc = subprocess.run(cmd, cwd=build_dir, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    pdf = build_dir / "resume.pdf"
    if proc.returncode != 0 or not pdf.exists():
        log = build_dir / "resume.log"
        tail = log.read_text(encoding="utf-8", errors="replace")[-2500:] if log.exists() else proc.stdout[-2500:]
        raise TailorError(f"LaTeX compile failed:\n{tail}")
    return pdf, len(PdfReader(str(pdf)).pages)


def _find(lst, eid):
    return next((e for e in lst if (e if isinstance(e, str) else e["id"]) == eid), None)


def trim_one(plan: dict, data: dict) -> dict | None:
    """Remove the least valuable item and return a record of what was removed.
    Claude orders bullets by relevance, so we always drop an entry's LAST bullet.
    Priority: shrink optional entries (older internships, projects) to one bullet,
    then drop optional sections, and only then shorten your required roles."""
    required = {e["id"] for e in data["experience"] if e.get("required")}

    def cut_bullet(section, entry):
        b = entry["bullets"].pop()
        return {"kind": "bullet", "section": section, "entry": entry["id"], "bullet": b,
                "desc": f"{entry['id']}: removed bullet '{b}'"}

    def cut_entry(section, i):
        item = plan[section].pop(i)
        eid = item if isinstance(item, str) else item["id"]
        return {"kind": "entry", "section": section, "item": item, "index": i,
                "desc": f"removed {section} entry '{eid}'"}

    optional = [(sec, e) for sec in ("experience", "projects") for e in plan[sec]
                if e["id"] not in required and len(e["bullets"]) > 1]
    if optional:
        sec, e = max(optional, key=lambda x: len(x[1]["bullets"]))
        return cut_bullet(sec, e)
    if plan["cocurricular"]:
        return cut_entry("cocurricular", len(plan["cocurricular"]) - 1)
    for i in range(len(plan["experience"]) - 1, -1, -1):
        if plan["experience"][i]["id"] not in required:
            return cut_entry("experience", i)
    if len(plan["projects"]) > 1:
        return cut_entry("projects", len(plan["projects"]) - 1)
    req = [e for e in plan["experience"] if len(e["bullets"]) > 1]
    if req:
        return cut_bullet("experience", max(req, key=lambda e: len(e["bullets"])))
    return None


def restore(plan: dict, rec: dict) -> bool:
    """Undo a removal. Returns False if it no longer applies (e.g. its entry is gone)."""
    if rec["kind"] == "bullet":
        entry = _find(plan[rec["section"]], rec["entry"])
        if entry is None:
            return False
        entry["bullets"].append(rec["bullet"])
    else:
        plan[rec["section"]].insert(min(rec["index"], len(plan[rec["section"]])), rec["item"])
    return True


def unrestore(plan: dict, rec: dict) -> None:
    if rec["kind"] == "bullet":
        _find(plan[rec["section"]], rec["entry"])["bullets"].remove(rec["bullet"])
    else:
        plan[rec["section"]].remove(rec["item"])


def fit_to_one_page(data: dict, plan: dict, build_dir: Path) -> tuple[str, Path, list[str]]:
    removed: list[dict] = []
    while True:
        tex = render.render_tex(data, plan)
        pdf, pages = compile_pdf(tex, build_dir)
        if pages <= 1:
            break
        rec = trim_one(plan, data)
        if not rec:
            raise TailorError("Cannot fit to one page even at minimum content.")
        removed.append(rec)
        print(f"  Over one page — {rec['desc']}")

    # Refill: the final cut may have freed more space than needed (e.g. a whole
    # entry went). Try restoring earlier cuts, most recent first; keep what fits.
    still_removed = removed[-1:]
    for rec in reversed(removed[:-1]):
        if not restore(plan, rec):
            still_removed.append(rec)
            continue
        _, pages = compile_pdf(render.render_tex(data, plan), build_dir)
        if pages <= 1:
            print(f"  Refilled — restored ({rec['desc'].split(': ')[-1]})")
        else:
            unrestore(plan, rec)
            still_removed.append(rec)

    tex = render.render_tex(data, plan)
    pdf, pages = compile_pdf(tex, build_dir)
    assert pages <= 1, "refill produced more than one page"
    return tex, pdf, [r["desc"] for r in reversed(still_removed)]


def _added_size(entry: dict) -> int:
    return len(guard.plain(entry["after"])) - len(guard.plain(entry["before"]))


def fit_locked(data: dict, plan: dict, build_dir: Path) -> tuple[str, Path, list[str]]:
    """Locked mode: bullets and entries are never removed. If the page overflows, undo
    keyword edits one at a time, largest addition first. If no edits are left and it
    still overflows, fall back to the base order (the base resume fits), and only then
    give up with an error."""
    base = render.default_plan(data)
    log = {e["id"]: e for e in plan.get("edit_log", []) if e["status"] == "applied"}
    reverted: list[str] = []
    while True:
        tex = render.render_tex(data, plan)
        pdf, pages = compile_pdf(tex, build_dir)
        if pages <= 1:
            return tex, pdf, reverted
        if plan["edits"]:
            bid = max(plan["edits"], key=lambda b: _added_size(log[b]))
            del plan["edits"][bid]
            log[bid]["status"] = "reverted"
            log[bid]["reason"] = "page overflow"
            reverted.append(f"{bid}: edit reverted (page overflow)")
            print(f"  Over one page - {reverted[-1]}")
        elif (plan["experience"], plan["projects"], plan["skills"]) != \
                (base["experience"], base["projects"], base["skills"]):
            plan["experience"], plan["projects"] = base["experience"], base["projects"]
            plan["skills"] = base["skills"]
            reverted.append("bullet and skill order reset to the base order (page overflow)")
            print(f"  Over one page - {reverted[-1]}")
        else:
            raise TailorError("The base resume with no edits does not fit on one page.")


def _order_changes(plan: dict, data: dict) -> list[str]:
    base = render.default_plan(data)
    new = {e["id"]: e["bullets"] for e in plan["experience"] + plan["projects"]}
    names = {e["id"]: e["company"] + " - " + e["position"] for e in data["experience"]}
    names.update({p["id"]: p["name"] for p in data["projects"]})
    out = []
    for e in base["experience"] + base["projects"]:
        if new.get(e["id"]) != e["bullets"]:
            out.append(f"**{names[e['id']]}**: {' → '.join(e['bullets'])}  ⟹  {' → '.join(new[e['id']])}")
    for label, items in base["skills"].items():
        if plan["skills"].get(label) != items:
            out.append(f"**Skills {label}** {', '.join(items)}  ⟹  {', '.join(plan['skills'][label])}")
    return out


# ─── Report ──────────────────────────────────────────────────────────────────

def verified_keywords(raw: dict, pdf: Path) -> list[str]:
    """Keep only the keywords Claude claimed that actually appear in the final PDF."""
    text = " ".join(p.extract_text() or "" for p in PdfReader(str(pdf)).pages).lower()
    text = " ".join(text.split())
    return [k for k in raw.get("jd_keywords_matched", []) if " ".join(k.lower().split()) in text]


def write_report(path: Path, company: str, role: str, raw: dict, plan: dict,
                 data: dict, warnings: list[str], trimmed: list[str], keywords: list[str],
                 locked: bool = True) -> None:
    s = raw.get("scores", {})
    out = [f"# {company} — {role}", "",
           f"**Scores:** overall {s.get('overall', '?')}/10 · skills {s.get('skills_match', '?')}/10 · "
           f"experience {s.get('experience_match', '?')}/10", "",
           "**JD keywords present in the final PDF:** " + (", ".join(keywords) or "—"), "",
           "**Gaps (not on your resume):** " + (", ".join(raw.get("gaps", [])) or "none"), ""]
    if locked:
        changes = _order_changes(plan, data)
        out += ["## Order changes (entries and bullets are locked to the base resume)", ""]
        out += [f"- {c}" for c in changes] if changes else ["Order unchanged from the base resume."]
        out.append("")
    else:
        base = render.default_plan(data)
        base_ids = {b for e in base["experience"] + base["projects"] for b in e["bullets"]}
        new_ids = {b for e in plan["experience"] + plan["projects"] for b in e["bullets"]}
        out += ["## Selection vs base resume", "",
                "Added: " + (", ".join(sorted(new_ids - base_ids)) or "—"), "",
                "Removed: " + (", ".join(sorted(base_ids - new_ids)) or "—"), ""]
    out += ["## Keyword edits", ""]
    for e in plan.get("edit_log", []):
        out += [f"**{e['id']}** — {e['status']}" + (f" ({e['reason']})" if e.get("reason") else ""),
                f"- before: {guard.plain(e['before'])}",
                f"- after:  {guard.plain(e['after'])}", ""]
    if not plan.get("edit_log"):
        out.append("No bullet edits.\n")
    if trimmed:
        title = "Reverted to fit one page" if locked else "Trimmed to fit one page"
        out += [f"## {title}", ""] + [f"- {t}" for t in trimmed] + [""]
    if warnings:
        out += ["## Guard warnings", ""] + [f"- {w}" for w in warnings] + [""]
    path.write_text("\n".join(out), encoding="utf-8")


# ─── Main ────────────────────────────────────────────────────────────────────

def slug(s: str) -> str:
    return re.sub(r"[^\w-]+", "_", s.strip()).strip("_")[:40]


def tailor_job(company: str, role: str, jd_text: str, jd_url: str = "", *,
               plan_file: str | None = None, prompt_only: bool = False,
               output_dir: Path | None = None, allow_selection: bool = False) -> dict:
    """JD -> tailored one-page PDF + report. Returns the result dict (also saved as result.json).
    Default is LOCKED mode: exactly the base resume's entries and bullets; only bullet order,
    skill order and guarded keyword edits change. allow_selection=True restores the old
    behaviour where Claude may add/drop bullets. Raises TailorError on recoverable failures."""
    output_dir = output_dir or OUTPUT_DIR
    data = load_resume_data()
    output_dir.mkdir(exist_ok=True)
    if len(jd_text.strip()) < 200:
        raise TailorError("Job description is too short — paste the full text.")

    job_dir = output_dir / f"{date.today():%Y-%m-%d}_{slug(company)}_{slug(role)}"
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "jd.txt").write_text(jd_text, encoding="utf-8")

    prompt = build_prompt(data, jd_text, company, role, allow_selection=allow_selection)
    if prompt_only:
        (job_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        return {"job_dir": str(job_dir), "prompt_only": True}

    if plan_file:
        raw = json.loads(Path(plan_file).read_text(encoding="utf-8"))
    else:
        print("Asking Claude Code to tailor (this uses your Pro usage)...")
        raw = call_claude(prompt)
    (job_dir / "claude_raw.json").write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")

    if allow_selection:
        plan, warnings = guard.validate_plan(data, raw, jd_text)
    else:
        base = render.default_plan(data)
        plan, warnings = guard.validate_locked_plan(data, base, raw, jd_text)
    for w in warnings:
        print(f"  guard: {w}")

    if allow_selection:
        tex, pdf, trimmed = fit_to_one_page(data, plan, output_dir / "_build")
    else:
        tex, pdf, trimmed = fit_locked(data, plan, output_dir / "_build")
        shown = {b for e in plan["experience"] + plan["projects"] for b in e["bullets"]}
        if shown != {b for e in base["experience"] + base["projects"] for b in e["bullets"]}:
            raise TailorError("Locked mode violated: the final bullets differ from the base resume.")
    shutil.copy2(pdf, job_dir / PDF_NAME)
    (job_dir / "resume.tex").write_text(tex, encoding="utf-8")
    (job_dir / "plan.json").write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")
    keywords = verified_keywords(raw, job_dir / PDF_NAME)
    write_report(job_dir / "report.md", company, role, raw, plan, data, warnings, trimmed, keywords,
                 locked=not allow_selection)

    # Machine-readable summary for the tracker / applier
    result = {
        "company": company, "role": role, "jd_url": jd_url or "",
        "job_dir": str(job_dir), "resume_pdf": str(job_dir / PDF_NAME),
        "report": str(job_dir / "report.md"), "scores": raw.get("scores", {}),
        "gaps": raw.get("gaps", []), "keywords": keywords,
        "mode": "selection" if allow_selection else "locked",
        "edits_applied": len(plan["edits"]), "edits_rejected":
            sum(1 for e in plan["edit_log"] if e["status"] == "rejected"),
        "trimmed": trimmed,
    }
    (job_dir / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def main() -> None:
    ap = argparse.ArgumentParser(description="Tailor resume to a job description.")
    ap.add_argument("--company")
    ap.add_argument("--role")
    ap.add_argument("--jd-file")
    ap.add_argument("--jd-url")
    ap.add_argument("--plan", help="Use this plan JSON instead of calling Claude (testing)")
    ap.add_argument("--prompt-only", action="store_true", help="Write the prompt to a file and stop")
    ap.add_argument("--base", action="store_true", help="Render the untailored base resume")
    ap.add_argument("--allow-selection", action="store_true",
                    help="Let Claude add/drop bullets and entries (default: locked to the base resume)")
    args = ap.parse_args()

    try:
        if args.base:
            data = load_resume_data()
            OUTPUT_DIR.mkdir(exist_ok=True)
            tex, pdf, _ = fit_to_one_page(data, render.default_plan(data), OUTPUT_DIR / "_build")
            dest = OUTPUT_DIR / "base"
            dest.mkdir(exist_ok=True)
            shutil.copy2(pdf, dest / PDF_NAME)
            (dest / "resume.tex").write_text(tex, encoding="utf-8")
            print(f"Base resume: {dest / PDF_NAME}")
            return

        if not (args.company and args.role):
            ap.error("--company and --role are required (or use --base)")
        result = tailor_job(args.company, args.role, read_jd(args), args.jd_url or "",
                            plan_file=args.plan, prompt_only=args.prompt_only,
                            allow_selection=args.allow_selection)
    except TailorError as e:
        sys.exit(str(e))

    if result.get("prompt_only"):
        print(f"Prompt written to {Path(result['job_dir']) / 'prompt.txt'}")
        return
    s = result["scores"]
    print(f"\nDone: {result['resume_pdf']}")
    print(f"  Score {s.get('overall', '?')}/10 | edits applied {result['edits_applied']} | "
          f"gaps: {', '.join(result['gaps']) or 'none'}")
    print(f"  Review: {result['report']}")


if __name__ == "__main__":
    main()
