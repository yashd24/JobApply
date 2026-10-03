#!/usr/bin/env python3
"""Fill (and, only when explicitly approved for that job, submit) a Greenhouse / Lever application.

    python apply.py --url <job url>                          dry run (the default): fills, screenshots, NEVER submits
    python apply.py --url <job url> --mode assist --allow-submit <job url>
    python apply.py --url <job url> --mode auto   --allow-submit <job url>

Modes (config.yaml sets the default per platform; --mode overrides; --dry-run overrides everything):
  dry-run  fill, highlight, screenshot. No submit code runs; the browser also blocks every submit.
  prepare  tailor, compute every answer and the cover letter, write a copy-ready sheet, open the job and the sheet in
           your normal default browser. YOU fill and submit by hand, then confirm in the terminal (recorded as a manual
           submission). The automation browser is not used. The default for Lever. No --allow-submit needed.
           (No terminal? `python apply.py --mark-submitted <job folder>` records it afterwards.)
  assist   fill, highlight what is left for you, then wait while YOU click Submit; then verify.
  auto     fill; if any required field is empty/flagged or a CAPTCHA / security-code challenge is visible, switch to
           assist and say why; otherwise screenshot, click Submit, hand over any visible challenge, verify.

A real submission needs BOTH a real mode and `--allow-submit` equal to this job's canonical URL. Anything else is
downgraded to a dry run with the reason printed, so a forgotten flag or a wrong URL can never submit.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import tailor
from jobbot import answers as A
from jobbot import coverletter, filler, intake, prepare, sheets, tracker
from jobbot import config as cfgmod
from jobbot import fields as F
from jobbot import profile as P
from jobbot.ats import adapter_for
from jobbot.browser import BrowserError, BrowserSession

ROOT = Path(__file__).resolve().parent
PROFILE_FILE = ROOT / "profile.yaml"
CONFIG_FILE = ROOT / "config.yaml"
RESULT_WAIT_S = 90          # how long auto mode waits for a confirmation after clicking Submit
ASSIST_VERIFY_WAIT_S = 20   # how long assist mode keeps looking for a confirmation after the user presses Enter
ASSIST_WAIT_S = 900         # without a terminal: how long assist mode watches the page for the user's submit (15 min)


def would_need_assist(answers: list[A.Answer], captcha_challenge_visible: bool) -> list[str]:
    """Why auto mode falls back to assist for this form. A CAPTCHA that might appear does not count;
    only a challenge that is actually visible does."""
    reasons = [f"required field not answered: {a.label}" for a in answers if a.required and a.status != A.FILLED]
    if captcha_challenge_visible:
        reasons.append("a CAPTCHA challenge is visible")
    return reasons


def _canonical(url: str) -> str:
    t = intake.detect(url)
    return (t.canonical_url if t else url.strip()).rstrip("/").lower()


def decide_mode(requested: str, canonical_url: str, allow_submit: "str | None") -> tuple[str, str]:
    """(mode actually used, note). Real modes need --allow-submit to name THIS job."""
    if requested in ("dry-run", "prepare"):          # neither ever submits, so neither needs an approval
        return requested, ""
    if not allow_submit:
        return "dry-run", (f"{requested} was requested but --allow-submit was not given, so this is a dry run. "
                           f"To submit for real add: --allow-submit {canonical_url}")
    if _canonical(allow_submit) != _canonical(canonical_url):
        return "dry-run", f"--allow-submit does not match this job ({canonical_url}), so this is a dry run"
    return requested, "real submission approved for this job"


def ended_job(profile: dict, today=None) -> "tuple[str, object] | None":
    """(company, last working day) from the DAY AFTER the last working day in profile.yaml, else None. Resume dates,
    cover-letter tense and the tense checks all follow this one rule."""
    end = P.employment_end(profile, today)
    return (profile["employment"]["current_company"], end) if end else None


def _tracker_file() -> Path:
    return tailor.OUTPUT_DIR / "tracker.sqlite3"


def _sheet_client():
    """The Google Sheet from config.yaml, or None when it is not configured or not yet authorised."""
    return sheets.authorised_client(cfgmod.load_config(CONFIG_FILE), ROOT)


def _track(job_dir: Path) -> None:
    """Record this folder's run in the tracker (and the Google Sheet, if configured). A tracker or sheet problem
    never fails an application run."""
    try:
        with tracker.Tracker(_tracker_file()) as t:
            row = t.record_folder(job_dir)
            client = _sheet_client() if row else None
            if client:
                print(f"    Google Sheet: {sheets.sync(t, client, urls=[row['canonical_url']])}")
    except Exception as e:
        print(f"    warning: could not update the tracker or sheet ({type(e).__name__}: {e})")


def _already_submitted(url: str):
    try:
        with tracker.Tracker(_tracker_file()) as t:
            row = t.job(url)
            return dict(row) if row and row["status"] == "submitted" else None
    except Exception:
        return None


def _status_from(confirmed: "bool | None") -> str:
    return {True: "submitted", False: "failed", None: "needs_review"}[confirmed]


def wait_for_result(session, adapter, *, timeout_s: "float | None" = None, poll_s: float = 1.5) -> "bool | None":
    """After a submit click: hand over any visible challenge, then wait for a verdict.
    True = confirmed, False = validation errors, None = unclear."""
    timeout_s = RESULT_WAIT_S if timeout_s is None else timeout_s          # read at call time, so it can be tuned
    deadline = time.time() + timeout_s
    page = session.page
    while time.time() < deadline:
        challenge = adapter.challenge(page)
        if challenge:
            session.hand_over(f"A {challenge.replace('-', ' ')} needs you. Complete it in the browser window "
                              "(it is never bypassed), then press Enter.",
                              lambda: adapter.challenge(session.page) is not None, timeout_s=ASSIST_WAIT_S)
            deadline = time.time() + timeout_s
            page = session.page
            continue
        verdict = adapter.is_confirmed(page)
        if verdict is not None:
            return verdict
        page.wait_for_timeout(int(poll_s * 1000))
    return adapter.is_confirmed(page)


def _snap(session, name: str):
    """A screenshot, or None if the page is gone (the user may have closed the window)."""
    try:
        return session.screenshot(name)
    except Exception:
        return None


def assist(session, adapter, answers, note: str = "") -> "bool | None":
    flagged = [a for a in answers if a.status == A.FLAGGED]
    print("\n" + "=" * 70)
    print("ASSIST: the form is filled. Fields outlined in orange are yours to answer:")
    for a in flagged:
        print(f"   {'*' if a.required else ' '} {a.label[:70]}  ({a.note})")
    lead = (note + " " if note else "") + "Check the form, fix the highlighted fields, click Submit yourself"
    seen: dict = {"verdict": None}

    def waiting() -> bool:                       # only used when there is no terminal to press Enter in
        seen["verdict"] = adapter.is_confirmed(session.page)
        return seen["verdict"] is not True

    # With a terminal: wait for Enter. Without one (or if the Enter prompt fails because stdin is closed): watch
    # the page until the confirmation appears, the time is up, or the window is closed. A CAPTCHA the user solves
    # along the way is just part of the wait.
    outcome = session.hand_over(lead + ", then press Enter here (or just submit: I will detect the confirmation).",
                                waiting, timeout_s=ASSIST_WAIT_S)
    verdict = seen["verdict"]
    if outcome == "done" and verdict is not True:        # Enter was pressed: give the page a moment to show the result
        page = session.page
        verdict = adapter.is_confirmed(page)
        deadline = time.time() + ASSIST_VERIFY_WAIT_S
        while verdict is None and time.time() < deadline:
            page.wait_for_timeout(1000)
            verdict = adapter.is_confirmed(page)
    else:
        print(f"    assist wait ended: {outcome}")
    return verdict


def _save_prepare(job_dir: Path, out: dict, job, status: str, requested: str, **extra) -> None:
    out["outcome"] = {"status": status, "confirmation": status == "submitted" or None, "submitted_by": extra.get("submitted_by"),
                      "finished": datetime.now().isoformat(timespec="seconds")}
    (job_dir / "answers.json").write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    (job_dir / "apply_result.json").write_text(json.dumps(
        {"status": status, "mode": "prepare", "mode_requested": requested, "url": job.canonical_url,
         "company": job.company, "role": job.role, "confirmation": out["outcome"]["confirmation"], **extra},
        indent=2, ensure_ascii=False), encoding="utf-8")


def _prepare(job, job_dir: Path, resume_pdf: Path, resume: dict, profile: dict, cover, meta: dict, requested: str,
             *, opener=None, input_fn=input, interactive: bool = True) -> dict:
    """Prepare mode: answers computed from the intake questions, no automation browser at all. The user opens the job
    in their normal browser, submits by hand and confirms here; it is recorded as a manual submission."""
    fields, note = F.fields_from_questions(job.questions), ""
    if not fields and not interactive:                 # the batch runner: still prepare, with the usual questions
        fields, note = prepare.generic_fields(), prepare.GENERIC_NOTE
    if not fields:
        raise SystemExit("No questions could be read for this posting, so there is nothing to prepare. "
                         "Try --mode dry-run to look at the live form.")
    print("3/6 Computing every answer from the posting's questions (no browser is driven)...")
    ctx = A.Context(profile=profile, resume=resume, company=job.company, role=job.role, job_location=job.location,
                    platform=job.platform, jd_text=job.jd_text, resume_pdf=resume_pdf, cover_letter=cover,
                    llm=tailor.call_claude)
    answers = A.answer_fields(fields, ctx)
    meta["fields_found"] = len(fields)
    out = A.to_json(answers)
    out["meta"] = meta
    txt, page = prepare.write_sheet(job_dir, answers, resume_pdf, job, note)
    _save_prepare(job_dir, out, job, "prepared", requested)
    _track(job_dir)
    if not interactive:                                # batch: nothing to open or ask; it is now "Ready for you"
        print(f"    prepared: {page}")
        return out

    print("4/6 The sheet:\n")
    print(prepare.sheet_text(answers, resume_pdf, job))
    mine = [a for a in answers if a.status == A.FLAGGED]
    if mine:
        print(f"{len(mine)} field(s) are yours to answer ({sum(a.required for a in mine)} required):")
        for a in mine:
            print(f"   {'*' if a.required else ' '} {a.label[:70]}  ({a.note})")
    print(f"5/6 Opening the job in your default browser, with the sheet next to it.\n"
          f"    Sheet: {page}\n    Resume PDF: {resume_pdf}")
    for target in (job.apply_url, page.as_uri()):
        if not prepare.open_in_default_browser(target, opener):
            print(f"    could not open a browser; open this yourself: {target}")

    submitted = prepare.confirm_manual(input_fn)
    if submitted:
        status, extra = "submitted", {"submitted_by": "manual"}
    elif submitted is None:
        status, extra = "prepared", {"note": "no terminal to confirm in; run: python apply.py --mark-submitted "
                                             f"\"{job_dir}\" once you have submitted"}
        print("\nNo terminal to ask in. After you submit, record it with:\n    " + extra["note"].split("run: ")[1])
    else:
        status, extra = "prepared", {"note": "the user said they did not submit"}
    _save_prepare(job_dir, out, job, status, requested, **extra)
    _track(job_dir)
    print(f"6/6 Done. Status: {status.upper()}" + (" (manual submission)" if status == "submitted" else
                                                    " (nothing was submitted)"))
    return out


def url_of_folder(folder: Path) -> "str | None":
    """The posting URL a job folder was made for (job_info.json from tailoring, else the run's own result file)."""
    for name, key in (("job_info.json", "url"), ("apply_result.json", "url")):
        try:
            value = json.loads((Path(folder) / name).read_text(encoding="utf-8")).get(key)
        except (OSError, ValueError):
            continue
        if value:
            return value
    return None


def mark_submitted(job_dir: Path) -> dict:
    """Record a prepared application as submitted by hand (for runs that had no terminal to confirm in)."""
    path = Path(job_dir) / "apply_result.json"
    if not path.exists():
        raise SystemExit(f"{path} does not exist: that folder has no prepared application.")
    result = json.loads(path.read_text(encoding="utf-8"))
    if result.get("mode") != "prepare":
        raise SystemExit(f"This folder is a {result.get('mode')} run, not a prepared one; nothing changed.")
    result.update({"status": "submitted", "confirmation": True, "submitted_by": "manual", "note": None,
                   "marked": datetime.now().isoformat(timespec="seconds")})
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    _track(Path(job_dir))
    print(f"Recorded as a manual submission: {result.get('company')} | {result.get('role')}")
    return result


def run(url: str, *, mode: "str | None" = None, allow_submit: "str | None" = None, dry_run: bool = False,
        resume_pdf: "Path | None" = None, headless: bool = False, keep_open: bool = False,
        session_factory=None, opener=None, input_fn=input, reapply: bool = False,
        resume_from: "Path | None" = None, prepare_interactive: bool = True) -> dict:
    """`session_factory(job_dir) -> BrowserSession` lets tests drive the run with a scripted user; `opener(url)` and
    `input_fn(prompt)` stand in for the default browser and the terminal in prepare mode. `resume_from` is a job
    folder from an earlier run: its tailored resume and cover letter are reused instead of being made again."""
    if not PROFILE_FILE.exists():
        raise SystemExit(f"{PROFILE_FILE.name} not found. Copy profile.example.yaml to {PROFILE_FILE.name} and replace "
                         "every TODO with your own answer (it is gitignored, so it stays on this machine).")
    try:
        profile = P.load_profile(PROFILE_FILE)
    except P.ProfileError as e:
        raise SystemExit(f"{PROFILE_FILE.name} has problems to fix first: {e}")
    todos = P.find_todos(profile)
    if todos:
        raise SystemExit(f"profile.yaml still has TODO in: {', '.join(todos)}")
    resume = tailor.load_resume_data()
    cfg = cfgmod.load_config(CONFIG_FILE)

    print(f"1/6 Reading the job: {url}")
    job = intake.fetch_job(url)
    print(f"    {job.platform} | {job.company} | {job.role} | {job.location} | {len(job.questions)} questions parsed")
    for w in job.warnings:
        print("    warning:", w)

    requested = cfgmod.requested_mode(cfg, job.platform, mode, dry_run)
    used, mode_note = decide_mode(requested, job.canonical_url, allow_submit)
    print(f"    mode: {used}" + (f"  ({mode_note})" if mode_note else ""))
    if used != "dry-run" and not reapply and (prev := _already_submitted(job.canonical_url)):
        raise SystemExit(f"Already submitted ({prev['submitted_by'] or prev['mode']}, {(prev['submitted_at'] or '')[:10]}): "
                         f"{prev['company']} | {prev['role']}. Nothing was done. Add --reapply to apply again anyway.")
    if used in ("assist", "auto") and headless:
        raise SystemExit("assist / auto need a visible browser window (do not use --headless).")
    try:
        adapter = adapter_for(job.platform)
    except KeyError as e:
        raise SystemExit(f"{e.args[0]}. Only Greenhouse and Lever postings can be filled so far.")

    if resume_from is not None:
        job_dir = Path(resume_from)
        if not job_dir.is_dir():
            raise SystemExit(f"--resume-from: {job_dir} is not a folder.")
        resume_pdf = job_dir / tailor.PDF_NAME
        if resume_pdf.exists():
            print(f"2/6 Reusing the tailored resume (and cover letter, if one was saved) from {job_dir.name}")
        else:
            print(f"2/6 {job_dir.name} has no tailored resume yet: tailoring now (uses your Claude Pro usage)...")
            result = tailor.tailor_job(job.company, job.role, job.jd_text, job.canonical_url, job_dir=job_dir)
            resume_pdf = Path(result["resume_pdf"])
    elif resume_pdf is None:
        print("2/6 Tailoring the resume (uses your Claude Pro usage)...")
        result = tailor.tailor_job(job.company, job.role, job.jd_text, job.canonical_url)
        job_dir, resume_pdf = Path(result["job_dir"]), Path(result["resume_pdf"])
        print(f"    {resume_pdf}")
    else:
        resume_pdf = Path(resume_pdf).resolve()
        # reuse the PDF's own folder only if it was tailored for THIS job; otherwise give the run a folder of its own
        in_job_folder = (resume_pdf.parent.parent == tailor.OUTPUT_DIR.resolve()
                         and resume_pdf.parent.name.endswith(f"_{tailor.slug(job.company)}_{tailor.slug(job.role)}"))
        job_dir = resume_pdf.parent if in_job_folder else \
            tailor.OUTPUT_DIR / f"dryrun_{tailor.slug(job.company)}_{tailor.slug(job.role)}"
        job_dir.mkdir(parents=True, exist_ok=True)
        print(f"2/6 Using the given resume: {resume_pdf}")

    ended = ended_job(profile)
    if ended:
        print(f"    note: the job at {ended[0]} ended on {P.human_date(ended[1])}; resume dates and cover letters use the past tense")
    cover = coverletter.make_provider(job_dir, job.company, job.role, job.jd_text, resume, llm=tailor.call_claude,
                                      ended=ended)
    meta: dict = {"mode": used, "mode_requested": requested, "mode_note": mode_note, "url": url,
                  "canonical_url": job.canonical_url, "platform": job.platform, "company": job.company,
                  "role": job.role, "location": job.location, "resume_pdf": str(resume_pdf),
                  "captcha_possible": job.captcha_possible, "intake_questions": len(job.questions),
                  "warnings": job.warnings}
    outcome: dict = {"status": "dry_run", "confirmation": None}

    if used == "prepare":
        try:
            return _prepare(job, job_dir, resume_pdf, resume, profile, cover, meta, requested,
                            opener=opener, input_fn=input_fn, interactive=prepare_interactive)
        except tailor.UsageLimitError as e:
            e.job_dir = e.job_dir or job_dir
            raise

    print(f"3/6 Opening the form ({'submitting is disabled for the whole browser' if used == 'dry-run' else 'REAL MODE'})...")
    try:
        with (session_factory or (lambda d: BrowserSession(d, headless=headless)))(job_dir) as b:
            if used == "dry-run":
                filler.install_submit_guard(b.context)       # not in assist/auto: there a submit is allowed
            status = adapter.open_application(b, job)
            meta["http_status"] = status
            if status >= 400:
                b.screenshot("error_page")
                raise SystemExit(f"{job.apply_url} answered HTTP {status}; the posting may be closed. Nothing was filled.")
            shots = [b.screenshot("form_loaded")]

            print("4/6 Discovering fields and answering...")
            fields = adapter.discover_fields(b.page)
            ctx = A.Context(profile=profile, resume=resume, company=job.company, role=job.role,
                            job_location=job.location, platform=job.platform, jd_text=job.jd_text,
                            resume_pdf=resume_pdf, cover_letter=cover, llm=tailor.call_claude)
            answers = A.answer_fields(fields, ctx)
            meta["fields_found"] = len(fields)

            print("5/6 Filling (resume first, then a pause for any resume auto-parse, then the rest)...")
            results = adapter.fill(b.page, fields, answers)
            recheck = adapter.read_back(b.page, fields, answers)
            highlighted = adapter.highlight(b.page, answers)
            shots.append(b.screenshot("filled_and_flagged"))
            meta["blocked_submit_attempts"] = filler.blocked_submits(b.page)
            meta["blocked_submit_details"] = filler.blocked_details(b.page)
            challenge = adapter.challenge(b.page)
            meta["captcha_challenge_visible"] = challenge == "captcha"
            meta["fill_results"] = [{"label": r.label, "ok": r.ok, "detail": r.detail} for r in results]
            meta["read_back_after_fill"] = [{"label": r.label, "ok": r.ok, "detail": r.detail} for r in recheck]
            meta["highlighted_flagged_fields"] = highlighted
            meta["would_need_assist"] = would_need_assist(answers, challenge == "captcha")
            if challenge == "security-code":
                meta["would_need_assist"].append("a security-code step is showing")
            meta["fill_problems"] = [r.label for r in results + recheck if not r.ok]

            if used == "assist":
                verdict = assist(b, adapter, answers)
                outcome = {"status": _status_from(verdict), "confirmation": verdict}
                outcome["verification"] = adapter.verify(b.page).to_dict()
                if outcome["status"] != "submitted":
                    outcome["validation_errors"] = adapter.validation_errors(b.page)
                shots.extend(filter(None, [_snap(b, "after_submit")]))
            elif used == "auto":
                reasons = list(meta["would_need_assist"]) + [f"could not fill: {x}" for x in meta["fill_problems"]]
                if reasons:
                    print("\nAUTO -> ASSIST. Not submitting automatically because:")
                    for r in reasons:
                        print("   -", r)
                    meta["switched_to_assist_because"] = reasons
                    verdict = assist(b, adapter, answers, "Auto mode switched to assist (see the list above).")
                else:
                    shots.append(b.screenshot("before_submit"))
                    print("\nAUTO: every required field is filled and nothing is flagged. Submitting.")
                    adapter.submit(b.page)
                    verdict = wait_for_result(b, adapter)
                outcome = {"status": _status_from(verdict), "confirmation": verdict}
                outcome["verification"] = adapter.verify(b.page).to_dict()
                if outcome["status"] != "submitted":
                    outcome["validation_errors"] = adapter.validation_errors(b.page)
                shots.extend(filter(None, [_snap(b, "after_submit")]))
            meta["screenshots"] = [str(s) for s in shots]
            if keep_open and used == "dry-run":
                b.pause_for_user("Dry run finished. Look at the form; NOTHING was submitted. Press Enter to close.")
    except BrowserError as e:
        raise SystemExit(str(e))
    except tailor.UsageLimitError as e:
        e.job_dir = e.job_dir or job_dir
        raise

    out = A.to_json(answers)
    out["meta"] = meta
    out["outcome"] = {**outcome, "finished": datetime.now().isoformat(timespec="seconds")}
    path = job_dir / "answers.json"
    path.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    (job_dir / "apply_result.json").write_text(json.dumps(
        {"status": outcome["status"], "mode": used, "mode_requested": requested, "url": job.canonical_url,
         "company": job.company, "role": job.role, **{k: v for k, v in outcome.items() if k != "status"},
         "switched_to_assist_because": meta.get("switched_to_assist_because")}, indent=2, ensure_ascii=False),
        encoding="utf-8")

    _track(job_dir)
    print(f"6/6 Done. Status: {outcome['status'].upper()}" + (" (nothing was submitted)" if used == "dry-run" else ""))
    print(f"\n{'STATUS':<8} {'CATEGORY':<17} {'REQ':<4} LABEL -> ANSWER   [source]")
    for a in answers:
        shown = "" if a.value is None else (", ".join(a.value) if isinstance(a.value, list) else str(a.value))
        print(f"{a.status.upper():<8} {a.category:<17} {'*' if a.required else ' ':<4} {a.label[:48]} -> "
              f"{shown[:60]}   [{a.source}]")
        if a.status == A.FLAGGED:
            print(f"{'':<31}you: {a.note}")
    s = out["summary"]
    print(f"\n{s['filled']} filled, {s['flagged']} flagged, {s['blank']} blank of {s['total']} fields | "
          f"fill problems: {len(meta['fill_problems'])} | submit attempts blocked: {meta['blocked_submit_attempts']}")
    for d in meta["blocked_submit_details"]:
        print(f"    blocked: {d}")
    print(f"auto mode would fall back to assist: {meta['would_need_assist'] or 'no'}")
    print(f"answers.json: {path}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Fill an application form; submits only with --mode and --allow-submit.")
    ap.add_argument("--url")
    ap.add_argument("--resume-from", type=Path, metavar="JOB_FOLDER",
                    help="continue an earlier run: reuse that folder's tailored resume and cover letter "
                         "(the url is read from the folder if --url is not given)")
    ap.add_argument("--mark-submitted", type=Path, metavar="JOB_FOLDER",
                    help="record a prepared application (that folder) as submitted by hand, then exit")
    ap.add_argument("--status", nargs="?", const=20, type=int, metavar="N",
                    help="show the N most recent tracked applications (default 20), then exit")
    ap.add_argument("--mark-applied", metavar="IDS",
                    help="you applied by hand: mark these tracker ids (or URLs), comma-separated, as Submitted, then exit")
    ap.add_argument("--sync-sheet", action="store_true",
                    help="push every tracked application to the Google Sheet (first run opens a Google sign-in), then exit")
    ap.add_argument("--import-existing", action="store_true",
                    help="read every job folder in output/ into the tracker, then exit")
    ap.add_argument("--reapply", action="store_true", help="apply even though this posting is already tracked as submitted")
    ap.add_argument("--mode", choices=cfgmod.MODES,
                    help="dry-run | prepare | assist | auto (default: config.yaml; Lever defaults to prepare, else dry-run)")
    ap.add_argument("--allow-submit", metavar="JOB_URL",
                    help="the canonical URL of THIS job; required for assist/auto to really submit")
    ap.add_argument("--dry-run", action="store_true", help="force a dry run whatever else is set")
    ap.add_argument("--resume-pdf", type=Path, help="use this PDF instead of tailoring a resume")
    ap.add_argument("--headless", action="store_true", help="dry runs only")
    ap.add_argument("--keep-open", action="store_true", help="dry run: wait for Enter before closing the browser")
    args = ap.parse_args()
    if args.mark_submitted:
        mark_submitted(args.mark_submitted)
        return
    if args.mark_applied:
        with tracker.Tracker(_tracker_file()) as t:
            for ref in [x.strip() for x in args.mark_applied.split(",") if x.strip()]:
                print(f"{ref}: {t.mark_applied(ref)}")
            try:
                if client := _sheet_client():
                    print("Google Sheet:", sheets.sync(t, client))
            except sheets.SheetsError as e:
                print(f"(Google Sheet not updated: {e})")
        return
    if args.sync_sheet:
        cfg = cfgmod.load_config(CONFIG_FILE)
        if not sheets.configured(cfg):
            sys.exit("config.yaml has no `sheets: {spreadsheet_id: ...}`; see config.example.yaml.")
        try:
            with tracker.Tracker(_tracker_file()) as t:
                print("Google Sheet:", sheets.sync(t, sheets.client_from_config(cfg, ROOT)))
        except sheets.SheetsError as e:
            sys.exit(f"SheetsError: {e}")
        return
    if args.import_existing or args.status is not None:
        with tracker.Tracker(_tracker_file()) as t:
            if args.import_existing:
                print(f"Read {t.import_existing(tailor.OUTPUT_DIR)} job folder(s) into {_tracker_file()}")
            if args.status is not None or args.import_existing:
                print(tracker.format_status(t.recent(args.status or 20), t.counts()))
        return
    url = args.url or (url_of_folder(args.resume_from) if args.resume_from else None)
    if not url:
        ap.error("--url is required (or --resume-from a folder from an earlier run)")
    try:
        run(url, mode=args.mode, allow_submit=args.allow_submit, dry_run=args.dry_run,
            resume_pdf=args.resume_pdf, headless=args.headless, keep_open=args.keep_open, reapply=args.reapply,
            resume_from=args.resume_from)
    except tailor.UsageLimitError as e:
        again = (f"\nContinue with:  python apply.py --resume-from \"{e.job_dir}\"  (add the same --mode / --allow-submit "
                 "flags as before; what is already in that folder is reused)") if e.job_dir else ""
        sys.exit(f"{e}{again}")
    except (tailor.TailorError, intake.IntakeError, cfgmod.ConfigError, P.ProfileError, sheets.SheetsError) as e:
        sys.exit(f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
