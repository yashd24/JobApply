"""The small `jobapply` commands that work on the tracker: list, approve, skip, done, open, retry, url and check.

Each one is a plain function that takes what it needs (a tracker, an opener, a runner), so tests can drive it with fakes.
Nothing here spends Claude usage or submits anything: `approve` and `url` only change a posting's state; the paid work
happens in `jobapply process`.
"""
from __future__ import annotations

import html
import os
import re
import shutil
import subprocess
import webbrowser
from pathlib import Path
from urllib.parse import urlparse

from jobbot import discovery
from jobbot import tracker as T

RETRYABLE = ("failed", "needs_review", "manual")


class CommandError(Exception):
    """A usage mistake or a refusal, with a plain-English message."""


# ─── choosing postings ───────────────────────────────────────────────────────

def _cut(text, n: int) -> str:
    text = str(text or "").replace("\n", " ")
    return text if len(text) <= n else text[:n - 1] + "~"


def parse_min_score(args: list) -> "int | None":
    """`7` or `7+` -> 7; nothing -> None."""
    text = " ".join(args).strip().rstrip("+").strip()
    if not text:
        return None
    if not text.isdigit() or not 1 <= int(text) <= 10:
        raise CommandError(f"the minimum score must be a number from 1 to 10 (like 7 or 7+), not {' '.join(args)!r}")
    return int(text)


def list_text(rows, min_score: "int | None" = None) -> str:
    """The Found postings (best first) as a table with the full link, so it can be clicked or copied."""
    shown = [r for r in rows if min_score is None or (r["relevance"] is not None and r["relevance"] >= min_score)]
    if not shown:
        what = f"scored {min_score}+" if min_score else "found"
        return f"No Found jobs {what}. (`jobapply find` looks for new ones.)"
    out = [f"{'ID':>5}  {'SCORE':>5}  {'COMPANY':<24} {'ROLE':<40} {'LOCATION':<22} LINK"]
    for r in shown:
        score = "-" if r["relevance"] is None else r["relevance"]
        out.append(f"{r['id']:>5}  {score!s:>5}  {_cut(r['company'], 24):<24} {_cut(r['role'], 40):<40} "
                   f"{_cut(r['location'], 22):<22} {discovery.link_of(r)}")
    label = f"scoring {min_score}+" if min_score else "Found"
    out.append(f"\n{len(shown)} job(s) {label}. Approve with `jobapply approve <ids>`, `approve {min_score or 8}+` or "
               "`approve all`; reject with `jobapply skip <ids>`.")
    return "\n".join(out)


def parse_ids(args: list) -> list[int]:
    """'3,7,12' / '3 7 12'. Anything else is an error, so a typo changes nothing."""
    text = " ".join(args).strip()
    if not text:
        raise CommandError("give at least one id, like 3 or 3,7,12 (see `jobapply list`)")
    try:
        return sorted({int(p) for p in re.split(r"[,\s]+", text) if p})
    except ValueError:
        raise CommandError(f"could not read the ids {text!r}: use numbers like 3,7,12") from None


def resolve_approval(tr: "T.Tracker", args: list) -> list[int]:
    """What `jobapply approve` was asked for: ids, `all` (every Found job) or `8+` (every Found job scoring 8 or more)."""
    text = " ".join(args).strip().lower()
    if not text:
        raise CommandError("say what to approve: ids (3,7,12), `8+` (every job scoring 8 or more) or `all`")
    if text == "all":
        return [r["id"] for r in tr.found()]
    m = re.fullmatch(r"(\d+)\s*\+", text)
    if m:
        n = int(m.group(1))
        if not 1 <= n <= 10:
            raise CommandError("a minimum score is from 1 to 10, like `approve 8+`")
        return [r["id"] for r in tr.found() if r["relevance"] is not None and r["relevance"] >= n]
    return parse_ids(args)


def resolve_unapproval(tr: "T.Tracker", args: list) -> "tuple[list[int], int]":
    """What `jobapply unapprove` was asked for among the approved jobs: ids, `all`, or `below 7` (every approved job scoring
    under 7). Also returns how many approved jobs have no score yet (they are never counted as 'below')."""
    text = " ".join(args).strip().lower()
    approved = tr.approved()
    if not text:
        raise CommandError("say what to unapprove: ids (3,7,12), `all` or `below 7`")
    if text == "all":
        return [r["id"] for r in approved], 0
    m = re.fullmatch(r"(?:below|<)\s*(\d+)", text)
    if m:
        n = int(m.group(1))
        if not 1 <= n <= 10:
            raise CommandError("a score is from 1 to 10, like `unapprove below 7`")
        return ([r["id"] for r in approved if r["relevance"] is not None and r["relevance"] < n],
                sum(1 for r in approved if r["relevance"] is None))
    return parse_ids(args), 0


def unapprove_text(tr: "T.Tracker", ids: list, unscored: int = 0) -> str:
    """Move those approved jobs back to Found and say what happened to each."""
    if not ids:
        extra = f" ({unscored} approved job(s) have no score yet and are left alone)" if unscored else ""
        return "Nothing matched, so nothing changed." + extra
    known = {r["id"]: r for r in tr.by_ids(ids)}
    out = tr.unapprove(ids)
    lines = []
    if out["moved"]:
        lines.append(f"Moved back to Found {len(out['moved'])} (not rejected: they can be approved again):")
        lines += [f"  {_describe(known[i])}" + ("" if known[i]["relevance"] is None else f"  [score {known[i]['relevance']}]")
                  for i in out["moved"]]
    for i, why in out["kept"].items():
        lines.append(f"  not changed: {_describe(known[i])}: {why}")
    for i, status in out["ignored"].items():
        lines.append(f"  not changed: {_describe(known[i])} is {T.label(status)}, not Approved")
    for i in ids:
        if i not in known:
            lines.append(f"  not changed: no job with id {i}")
    if unscored:
        lines.append(f"  ({unscored} approved job(s) have no score yet and were left alone)")
    return "\n".join(lines)


def _describe(row) -> str:
    return f"#{row['id']} {row['company'] or '?'} | {row['role'] or '?'}"


def decide_text(tr: "T.Tracker", ids: list, approve: bool) -> str:
    """Approve or skip those ids, and say exactly what happened to each."""
    if not ids:
        return "Nothing matched, so nothing changed."
    known = {r["id"]: r for r in tr.by_ids(ids)}
    out = tr.decide(ids, approve=approve)
    verb = "approved" if approve else "skipped"
    lines = []
    done = out["approved" if approve else "skipped"]
    if done:
        lines.append(f"{verb.capitalize()} {len(done)}:")
        lines += [f"  {_describe(known[i])}" for i in done]
    for i in out["ignored"]:
        if i in known:
            lines.append(f"  not changed: {_describe(known[i])} is {T.label(known[i]['status'])}, not Found")
    for i in ids:
        if i not in known:
            lines.append(f"  not changed: no job with id {i}")
    if approve and done:
        lines.append("Next: `jobapply process` prepares the approved jobs (or `jobapply process 3` for just three).")
    if not approve and done:
        lines.append("Skipped jobs never come back.")
    return "\n".join(lines)


def mark_done_text(tr: "T.Tracker", ids: list) -> str:
    out = []
    for i in ids:
        res = tr.mark_applied(i)
        row = (tr.by_ids([i]) or [None])[0]
        who = _describe(row) if row else f"#{i}"
        out.append(f"{who}: " + {"marked": "marked as applied", "already": "was already Submitted",
                                 "unknown": "no such job"}[res])
    return "\n".join(out)


# ─── open ────────────────────────────────────────────────────────────────────

def open_job(tr: "T.Tracker", job_id: int, open_link=None, open_path=None, say=print) -> int:
    """The job's link, its prepare_sheet.html and its folder, all at once."""
    open_link = open_link or webbrowser.open
    open_path = open_path or os.startfile
    row = (tr.by_ids([job_id]) or [None])[0]
    if row is None:
        say(f"No job with id {job_id}. (`jobapply list` shows Found jobs, `jobapply action` the ones waiting on you.)")
        return 1
    say(_describe(row) + f"  [{T.label(row['status'])}]")
    link = discovery.link_of(row)
    open_link(link)
    say(f"  link:    {link}")
    folder = Path(row["job_folder"]) if row["job_folder"] else None
    if not folder or not folder.is_dir():
        say("  folder:  none yet (nothing has been prepared for this job)")
        return 0
    sheet = folder / "prepare_sheet.html"
    if sheet.exists():
        open_path(str(sheet))
        say(f"  sheet:   {sheet}")
    else:
        say("  sheet:   no prepare_sheet.html in this folder")
    open_path(str(folder))
    say(f"  folder:  {folder}")
    return 0


# ─── retry ───────────────────────────────────────────────────────────────────

def prepare_retry(tr: "T.Tracker", job_id: int) -> int:
    """Put a failed / needs-review / manual job back in the queue with its saved folder, so `process` continues from what
    is already there (the tailored resume, the cover letter) instead of paying for it again. Returns the id."""
    row = (tr.by_ids([job_id]) or [None])[0]
    if row is None:
        raise CommandError(f"no job with id {job_id}")
    if row["status"] not in RETRYABLE:
        hint = {"found": "approve it first: `jobapply approve %d`" % job_id,
                "approved": "it is already queued: `jobapply process`",
                "submitted": "it was already applied for",
                "ready_for_you": "it is prepared and waiting on you: `jobapply open %d`" % job_id,
                "skipped": "you rejected it"}.get(row["status"], "only failed, needs-review and manual jobs can be retried")
        raise CommandError(f"{_describe(row)} is {T.label(row['status'])}: {hint}")
    folder = row["job_folder"] if row["job_folder"] and Path(row["job_folder"]).is_dir() else None
    tr.set_state(job_id, "approved", "retry requested: continuing from the saved folder" if folder else "retry requested", folder)
    return job_id


# ─── url ─────────────────────────────────────────────────────────────────────

_TITLE_PATTERNS = (
    re.compile(r"^(?P<company>.+?) hiring (?P<role>.+?)(?: in [^|]+)?\s*\|\s*LinkedIn\s*$", re.I),
    re.compile(r"^(?P<role>.+?) at (?P<company>[^|]+?)(?:\s*\|.*)?$", re.I),
    re.compile(r"^(?P<role>.+?)\s+[-–|]\s+(?P<company>[^-–|]+?)(?:\s+[-–|].*)?$"),
)


def company_role_from_title(title: str) -> "tuple[str, str] | None":
    """Best-effort company and role from a page title like 'Acme hiring Backend Engineer in Bengaluru | LinkedIn'."""
    title = html.unescape(re.sub(r"\s+", " ", title or "")).strip()
    for pat in _TITLE_PATTERNS:
        m = pat.match(title)
        if m and m.group("company").strip() and m.group("role").strip():
            return m.group("company").strip(), m.group("role").strip()
    return None


def page_title(link: str, get=None) -> str:
    import requests
    from bs4 import BeautifulSoup
    get = get or (lambda u: requests.get(u, timeout=20, headers={"User-Agent": "Mozilla/5.0"}).text)
    try:
        soup = BeautifulSoup(get(link), "html.parser")
    except Exception:
        return ""
    og = soup.find("meta", property="og:title")
    return (og.get("content") if og and og.get("content") else (soup.title.string if soup.title and soup.title.string else "")) or ""


def add_from_link(tr: "T.Tracker", link: str, company: "str | None" = None, role: "str | None" = None, *, fetch_job=None,
                  fetch_text=None, title_of=None, say=print) -> int:
    """Make sure this link is a tracked job and approved for the next `process`; returns its id.
    Greenhouse/Lever links (and company pages that embed Greenhouse) are read through the ATS APIs; any other link is a
    manual job whose company and role come from the page title or from --company/--role."""
    from jobbot import intake
    link = link.strip()
    if not re.match(r"^https?://", link, re.I):
        raise CommandError("give a full link starting with http:// or https://")
    fetch_job = fetch_job or intake.fetch_job
    existing = tr.job(link)
    if existing is None:
        try:
            job = fetch_job(link)
        except intake.UnsupportedPlatform:
            job = None
        except intake.IntakeError as e:
            raise CommandError(f"could not read that posting: {e}") from None
        if job is not None:
            existing = tr.job(job.canonical_url)
            if existing is None:
                outcome = tr.add_found({"canonical_url": job.canonical_url, "company": job.company, "role": job.role,
                                        "location": job.location, "route": job.platform, "platform": job.platform,
                                        "source": "url", "source_url": link, "description": job.jd_text,
                                        "reason": "added with jobapply url"})
                existing = tr.job(job.canonical_url)
                if existing is None:
                    raise CommandError(f"{job.company} | {job.role} is already tracked under another link ({outcome}); "
                                       "use `jobapply list` or `jobapply action` to find it")
        else:
            if not (company and role):
                guess = company_role_from_title((title_of or page_title)(link))
                company, role = (company or (guess[0] if guess else None)), (role or (guess[1] if guess else None))
            if not (company and role):
                raise CommandError("could not tell the company and role from that page: add them, like "
                                   "`jobapply url <link> --company \"Acme\" --role \"Backend Engineer\"`")
            host = (urlparse(link).netloc or "web").removeprefix("www.")
            outcome = tr.add_found({"canonical_url": link, "company": company, "role": role, "route": "manual",
                                    "source": host, "source_url": link, "reason": "added with jobapply url"})
            existing = tr.job(link)
            if existing is None:
                raise CommandError(f"{company} | {role} is already tracked under another link ({outcome}); "
                                   "use `jobapply action` to find it")
    status = existing["status"]
    if status == "submitted":
        raise CommandError(f"{_describe(existing)} was already applied for")
    if status != "approved":
        note = "approved: you gave this link to `jobapply url`"
        if status == "found":
            tr.decide([existing["id"]], approve=True, why=note)
        else:                                             # it was failed / skipped / prepared before: you asked for it again
            keep = existing["job_folder"] if existing["job_folder"] and Path(existing["job_folder"]).is_dir() else None
            tr.set_state(existing["id"], "approved", note, keep)
    say(f"{_describe(existing)}: queued (the {existing['route'] or 'manual'} route)")
    return existing["id"]


# ─── check ───────────────────────────────────────────────────────────────────

OK, WARN, BAD = "ok", "!!", "XX"


def _line(state: str, what: str, detail: str = "") -> str:
    return f"  [{state}] {what}" + (f": {detail}" if detail else "")


def check_latex(which=shutil.which, run=subprocess.run) -> tuple[str, str]:
    exe = which("xelatex")
    if exe:
        try:
            out = run([exe, "--version"], capture_output=True, text=True, timeout=30).stdout.splitlines()
            return OK, f"XeLaTeX found ({(out[0] if out else exe)[:70]})"
        except Exception as e:
            return WARN, f"XeLaTeX at {exe} would not run ({type(e).__name__})"
    if which("tectonic"):
        return OK, "Tectonic found (XeLaTeX is not, but Tectonic also compiles the resume)"
    return BAD, "no XeLaTeX or Tectonic on PATH: the resume cannot be built (install MiKTeX)"


def check_claude(which=shutil.which, run=subprocess.run, ask: bool = True) -> tuple[str, str]:
    exe = which("claude") or which("claude.cmd")
    if not exe:
        return BAD, "Claude Code is not on PATH (install it, then run `claude` once to log in)"
    try:
        version = run([exe, "--version"], capture_output=True, text=True, timeout=60).stdout.strip().splitlines()
    except Exception as e:
        return BAD, f"`claude --version` failed ({type(e).__name__})"
    ver = version[0] if version else "unknown version"
    if not ask:
        return OK, f"Claude Code found ({ver}); login not tested (--quick)"
    import tailor
    try:
        proc = run([exe, "-p", "--output-format", "json"], input="Reply with the single word: ok", capture_output=True,
                   text=True, encoding="utf-8", timeout=120)
    except subprocess.TimeoutExpired:
        return WARN, f"Claude Code found ({ver}) but did not answer within 2 minutes"
    except Exception as e:
        return BAD, f"Claude Code found ({ver}) but would not run ({type(e).__name__})"
    text = (proc.stdout or "") + "\n" + (proc.stderr or "")
    if tailor.usage_limit_message(text):
        return WARN, f"logged in ({ver}), but the usage limit is reached right now: wait for the reset"
    if proc.returncode == 0 and '"result"' in (proc.stdout or ""):
        return OK, f"logged in and answering ({ver}); that test used one tiny request"
    return BAD, f"Claude Code ({ver}) did not answer: log in by running `claude` once. It said: {text.strip()[:160]}"


def check_profile(root: Path) -> list[tuple[str, str]]:
    from jobbot import profile as P
    out = []
    pfile = root / "profile.yaml"
    if not pfile.exists():
        return [(BAD, "profile.yaml not found (copy profile.example.yaml and fill it in)")]
    try:
        profile = P.load_profile(pfile)
    except Exception as e:
        return [(BAD, f"profile.yaml has problems: {str(e)[:200]}")]
    todos = P.find_todos(profile)
    out.append((OK if not todos else WARN, "profile.yaml loads" + (f"; still TODO: {', '.join(todos[:8])}" if todos else
                                                                    " and has no TODO left")))
    for name, hint in (("contact.yaml", "copy contact.example.yaml"), ("resume_data.yaml", "your resume content")):
        out.append((OK, f"{name} present") if (root / name).exists() else (BAD, f"{name} missing ({hint})"))
    try:
        from jobbot import config as cfgmod
        cfgmod.load_config(root / "config.yaml")
        out.append((OK, "config.yaml loads"))
    except Exception as e:
        out.append((BAD, f"config.yaml has problems: {str(e)[:160]}"))
    return out


def check_sheet(cfg: dict, root: Path) -> tuple[str, str]:
    from jobbot import sheets
    if not sheets.configured(cfg):
        return WARN, "no Google Sheet configured in config.yaml (optional)"
    s = cfg.get("sheets") or {}
    creds, token = root / s.get("credentials_file", "credentials.json"), root / s.get("token_file", "token.json")
    if not creds.exists() and not token.exists():
        return BAD, f"{creds.name} not found in {root}: create OAuth desktop credentials (see config.example.yaml)"
    if not token.exists():
        return WARN, "credentials.json is there but you have not signed in yet: run `jobapply sync` once"
    try:
        client = sheets.client_from_config(cfg, root)
        head = client.read(f"A1:{sheets.APPROVE_LETTER}1")
    except Exception as e:
        return BAD, f"could not read the sheet: {str(e)[:200]}"
    cells = [str(c) for c in (head[0] if head else [])]
    missing = [h for h in (sheets.ID_COLUMN, sheets.APPROVE_COLUMN) if h not in cells]
    detail = f"connected to tab '{client.name}'"
    if missing:
        detail += f"; the {' and '.join(missing)} column{'s' if len(missing) > 1 else ''} will be added by the next sync"
    return OK, detail


def check_browser() -> tuple[str, str]:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            p.chromium.launch(headless=True).close()
        return OK, "Playwright Chromium starts"
    except Exception as e:
        return WARN, f"Playwright Chromium would not start ({type(e).__name__}): run `python -m playwright install chromium`"


def health_check(root: Path, cfg: "dict | None" = None, quick: bool = False, say=print, *, latex=check_latex,
                 claude=check_claude, sheet=check_sheet, browser=check_browser, profile=check_profile) -> bool:
    """Print one line per thing the bot needs; True when nothing is broken (warnings are fine)."""
    results: list[tuple[str, str, str]] = []

    def add(label, pair):
        results.append((pair[0], label, pair[1]))
        say(_line(pair[0], label, pair[1]))
    say("jobapply check")
    add("LaTeX", latex())
    add("Claude", claude(ask=not quick))
    for state, text in profile(root):
        results.append((state, "Files", text))
        say(_line(state, text))
    if cfg is None:
        try:
            from jobbot import config as cfgmod
            cfg = cfgmod.load_config(root / "config.yaml")
        except Exception:
            cfg = {}
    add("Google Sheet", sheet(cfg, root))
    add("Browser", browser())
    bad = [r for r in results if r[0] == BAD]
    warns = [r for r in results if r[0] == WARN]
    say(f"\n{'Everything the bot needs is working.' if not bad and not warns else ''}"
        f"{f'{len(bad)} problem(s) to fix. ' if bad else ''}{f'{len(warns)} warning(s).' if warns else ''}".rstrip())
    return not bad
