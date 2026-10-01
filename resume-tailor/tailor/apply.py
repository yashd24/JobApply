#!/usr/bin/env python3
"""Fill (and, only when explicitly approved for that job, submit) a Greenhouse / Lever application.

    python apply.py --url <job url>                          dry run (the default): fills, screenshots, NEVER submits
    python apply.py --url <job url> --mode assist --allow-submit <job url>
    python apply.py --url <job url> --mode auto   --allow-submit <job url>

Modes (config.yaml sets the default per platform; --mode overrides; --dry-run overrides everything):
  dry-run  fill, highlight, screenshot. No submit code runs; the browser also blocks every submit.
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
from jobbot import coverletter, filler, intake
from jobbot import config as cfgmod
from jobbot import profile as P
from jobbot.ats import adapter_for
from jobbot.browser import BrowserError, BrowserSession

ROOT = Path(__file__).resolve().parent
PROFILE_FILE = ROOT / "profile.yaml"
CONFIG_FILE = ROOT / "config.yaml"
RESULT_WAIT_S = 90          # how long auto mode waits for a confirmation after clicking Submit
ASSIST_VERIFY_WAIT_S = 20   # how long assist mode keeps looking for a confirmation after the user presses Enter


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
    if requested == "dry-run":
        return "dry-run", ""
    if not allow_submit:
        return "dry-run", (f"{requested} was requested but --allow-submit was not given, so this is a dry run. "
                           f"To submit for real add: --allow-submit {canonical_url}")
    if _canonical(allow_submit) != _canonical(canonical_url):
        return "dry-run", f"--allow-submit does not match this job ({canonical_url}), so this is a dry run"
    return requested, "real submission approved for this job"


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
            session.pause_for_user(f"A {challenge.replace('-', ' ')} needs you. Complete it in the browser window "
                                   "(it is never bypassed), then press Enter.")
            deadline = time.time() + timeout_s
            page = session.page
            continue
        verdict = adapter.is_confirmed(page)
        if verdict is not None:
            return verdict
        page.wait_for_timeout(int(poll_s * 1000))
    return adapter.is_confirmed(page)


def assist(session, adapter, answers, note: str = "") -> "bool | None":
    flagged = [a for a in answers if a.status == A.FLAGGED]
    print("\n" + "=" * 70)
    print("ASSIST: the form is filled. Fields outlined in orange are yours to answer:")
    for a in flagged:
        print(f"   {'*' if a.required else ' '} {a.label[:70]}  ({a.note})")
    session.pause_for_user((note + " " if note else "") + "Check the form, fix the highlighted fields, click Submit "
                           "yourself, then press Enter here so I can verify the result.")
    page = session.page
    deadline = time.time() + ASSIST_VERIFY_WAIT_S
    verdict = adapter.is_confirmed(page)
    while verdict is None and time.time() < deadline:
        page.wait_for_timeout(1000)
        verdict = adapter.is_confirmed(page)
    return verdict


def run(url: str, *, mode: "str | None" = None, allow_submit: "str | None" = None, dry_run: bool = False,
        resume_pdf: "Path | None" = None, headless: bool = False, keep_open: bool = False,
        session_factory=None) -> dict:
    """`session_factory(job_dir) -> BrowserSession` lets tests drive the run with a scripted user."""
    profile = P.load_profile(PROFILE_FILE)
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
    if used != "dry-run" and headless:
        raise SystemExit("assist / auto need a visible browser window (do not use --headless).")
    adapter = adapter_for(job.platform)

    if resume_pdf is None:
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

    cover = coverletter.make_provider(job_dir, job.company, job.role, job.jd_text, resume, llm=tailor.call_claude)
    meta: dict = {"mode": used, "mode_requested": requested, "mode_note": mode_note, "url": url,
                  "canonical_url": job.canonical_url, "platform": job.platform, "company": job.company,
                  "role": job.role, "location": job.location, "resume_pdf": str(resume_pdf),
                  "captcha_possible": job.captcha_possible, "intake_questions": len(job.questions),
                  "warnings": job.warnings}
    outcome: dict = {"status": "dry_run", "confirmation": None}

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
                if outcome["status"] != "submitted":
                    outcome["validation_errors"] = adapter.validation_errors(b.page)
                shots.append(b.screenshot("after_submit"))
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
                if outcome["status"] != "submitted":
                    outcome["validation_errors"] = adapter.validation_errors(b.page)
                shots.append(b.screenshot("after_submit"))
            meta["screenshots"] = [str(s) for s in shots]
            if keep_open and used == "dry-run":
                b.pause_for_user("Dry run finished. Look at the form; NOTHING was submitted. Press Enter to close.")
    except BrowserError as e:
        raise SystemExit(str(e))

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
    ap.add_argument("--url", required=True)
    ap.add_argument("--mode", choices=cfgmod.MODES, help="dry-run | assist | auto (default: config.yaml, else dry-run)")
    ap.add_argument("--allow-submit", metavar="JOB_URL",
                    help="the canonical URL of THIS job; required for assist/auto to really submit")
    ap.add_argument("--dry-run", action="store_true", help="force a dry run whatever else is set")
    ap.add_argument("--resume-pdf", type=Path, help="use this PDF instead of tailoring a resume")
    ap.add_argument("--headless", action="store_true", help="dry runs only")
    ap.add_argument("--keep-open", action="store_true", help="dry run: wait for Enter before closing the browser")
    args = ap.parse_args()
    try:
        run(args.url, mode=args.mode, allow_submit=args.allow_submit, dry_run=args.dry_run,
            resume_pdf=args.resume_pdf, headless=args.headless, keep_open=args.keep_open)
    except (tailor.TailorError, intake.IntakeError, cfgmod.ConfigError) as e:
        sys.exit(f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
