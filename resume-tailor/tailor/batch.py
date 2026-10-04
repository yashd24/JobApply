#!/usr/bin/env python3
"""Batch runner: prepare the jobs you approved, one at a time.

    python batch.py                   process the approved jobs (see config.yaml: batch)
    python batch.py --list            show the queue, today's count and the daily cap; do nothing
    python batch.py --only 12,15      just these ids
    python batch.py --limit 3         at most 3 jobs this time

What happens to each APPROVED job (nothing costs Claude usage before you approve):
  Greenhouse   apply.py in the mode set for Greenhouse in config.yaml (assist: the form is filled and you click Submit)
  Lever        prepared: tailored resume, cover letter and answers from the real form; no browser is opened
  everything   fetch the description if it is missing, tailor the resume, write the cover letter, build the answer sheet
  else         with the usual application questions; the job becomes "Ready for you" with its link
Between jobs it waits (batch.delay_between_jobs_s), it stops at the daily cap (batch.daily_cap), and when Claude's usage
limit is reached it stops at once, keeps that job's folder, and the next run resumes it where it stopped.
"""
from __future__ import annotations

import argparse
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import apply
import tailor
from jobbot import answers as A
from jobbot import config as cfgmod
from jobbot import coverletter, discovery, intake, prepare
from jobbot import profile as P
from jobbot import tracker as T

ROOT = Path(__file__).resolve().parent
MIN_DESCRIPTION_CHARS = 200


# ─── settings ────────────────────────────────────────────────────────────────

@dataclass
class Settings:
    delay_s: tuple[float, float] = (60.0, 180.0)
    daily_cap: int = 10


def load_settings(cfg: dict) -> Settings:
    b = (cfg or {}).get("batch") or {}
    delay = b.get("delay_between_jobs_s", [60, 180])
    if not (isinstance(delay, (list, tuple)) and len(delay) == 2 and 0 <= float(delay[0]) <= float(delay[1])):
        raise cfgmod.ConfigError("batch.delay_between_jobs_s must be [min, max] seconds")
    try:
        cap = int(b.get("daily_cap", 10))
    except (TypeError, ValueError):
        raise cfgmod.ConfigError("batch.daily_cap must be a whole number") from None
    if cap < 1:
        raise cfgmod.ConfigError("batch.daily_cap must be at least 1")
    return Settings((float(delay[0]), float(delay[1])), cap)


# ─── the description of a posting whose text is missing ──────────────────────

def fetch_description(url: str, get=None) -> "str | None":
    """The job description from the posting's page, or None. LinkedIn's public posting endpoint is tried first for
    LinkedIn links; anything else goes through the same page reader the resume tailor uses."""
    if m := re.search(r"linkedin\.com/jobs/view/(?:[^/?]*-)?(\d{6,})", url or ""):
        import requests
        from bs4 import BeautifulSoup
        get = get or (lambda u: requests.get(u, timeout=15, headers={"User-Agent": "Mozilla/5.0"}).text)
        try:
            soup = BeautifulSoup(get(f"https://www.linkedin.com/jobs-guest/jobs/api/jobPosting/{m.group(1)}"), "html.parser")
        except Exception:
            soup = None
        node = soup and (soup.select_one(".show-more-less-html__markup") or soup.select_one(".description__text"))
        text = node.get_text("\n", strip=True) if node else ""
        if len(text) >= MIN_DESCRIPTION_CHARS:
            return text
    try:
        text = tailor.fetch_jd(url)
    except Exception:
        text = None
    return text if text and len(text) >= MIN_DESCRIPTION_CHARS else None


# ─── preparing a posting whose form cannot be read ───────────────────────────

class CouldNotPrepare(Exception):
    """The posting cannot be prepared (no readable description); it becomes Manual with this reason."""


def prepare_manual(row, *, profile: dict, resume: dict, llm=None, tailor_job=None, fetch=None) -> Path:
    """Fetch the description if missing, tailor the resume, write the cover letter and build an answer sheet of the
    usual questions. Writes the same files a prepare run does, so the tracker shows it as Ready for you."""
    llm, tailor_job, fetch = llm or tailor.call_claude, tailor_job or tailor.tailor_job, fetch or fetch_description
    url, link = row["canonical_url"], discovery.link_of(row)
    jd = (row["description"] or "").strip()
    if len(jd) < MIN_DESCRIPTION_CHARS:
        jd = (fetch(row["source_url"] or link) or fetch(link) or "").strip()
    if len(jd) < MIN_DESCRIPTION_CHARS:
        raise CouldNotPrepare("could not read the job description, so nothing was tailored: apply by hand from the link")
    folder = Path(row["job_folder"]) if row["job_folder"] and Path(row["job_folder"]).is_dir() else None
    pdf = folder / tailor.PDF_NAME if folder else None
    try:
        if not (pdf and pdf.exists()):
            result = tailor_job(row["company"], row["role"], jd, url, job_dir=folder)
            folder, pdf = Path(result["job_dir"]), Path(result["resume_pdf"])
        end = apply.ended_job(profile)
        cover = coverletter.make_provider(folder, row["company"], row["role"], jd, resume, llm=llm, ended=end)
        job = SimpleNamespace(company=row["company"], role=row["role"], location=row["location"] or "", apply_url=link,
                              canonical_url=url, platform="manual", jd_text=jd, questions=[], captcha_possible=False)
        ctx = A.Context(profile=profile, resume=resume, company=job.company, role=job.role, job_location=job.location,
                        platform="manual", jd_text=jd, resume_pdf=pdf, cover_letter=cover, llm=llm)
        answers = A.answer_fields(prepare.generic_fields(), ctx)
    except tailor.UsageLimitError as e:
        e.job_dir = e.job_dir or folder
        raise
    out = A.to_json(answers)
    out["meta"] = {"mode": "prepare", "platform": row["source"] or "manual", "canonical_url": url, "company": job.company,
                   "role": job.role, "location": job.location, "resume_pdf": str(pdf), "generic_questions": True,
                   "apply_link": link}
    prepare.write_sheet(folder, answers, pdf, job, prepare.GENERIC_NOTE)
    apply._save_prepare(folder, out, job, "prepared", "prepare",
                        note="likely answers only: the real form was not read")
    apply._track(folder)
    return folder


# ─── one job ─────────────────────────────────────────────────────────────────

class StopBatch(Exception):
    """Something that would make every remaining job fail the same way."""


def _folder_of(row) -> "Path | None":
    f = row["job_folder"]
    return Path(f) if f and Path(f).is_dir() else None


def make_processor(tr: "T.Tracker", cfg: dict, *, apply_run=None, manual_prepare=None, profile_loader=None,
                   resume_loader=None, unattended: bool = False, greenhouse_mode: "str | None" = None):
    """A function row -> what happened (a tracker status), doing the right thing for the posting's route. Unattended
    (python run.py): Greenhouse uses `greenhouse_mode` (auto) and never waits for a person."""
    apply_run = apply_run or apply.run
    manual_prepare = manual_prepare or prepare_manual
    profile_loader = profile_loader or (lambda: P.load_profile(apply.PROFILE_FILE))
    resume_loader = resume_loader or tailor.load_resume_data

    def process(row) -> str:
        route, url = row["route"] or "manual", row["canonical_url"]
        try:
            if route == "greenhouse":
                mode = greenhouse_mode or cfgmod.requested_mode(cfg, "greenhouse")
                kw = {"unattended": True} if unattended else {}
                apply_run(url, mode=mode, allow_submit=url, resume_from=_folder_of(row), **kw)   # approved for THIS url
            elif route == "lever":
                apply_run(url, mode="prepare", resume_from=_folder_of(row), prepare_interactive=False)
            else:
                manual_prepare(row, profile=profile_loader(), resume=resume_loader())
        except CouldNotPrepare as e:
            tr.set_state(row["id"], "manual", str(e))
            return "manual"
        except tailor.UsageLimitError:
            raise
        except SystemExit as e:                       # apply.run explains itself with SystemExit
            message = str(e.code if e.code is not None else "stopped")
            if "profile.yaml" in message or "config.yaml" in message:
                raise StopBatch(message) from None
            tr.set_state(row["id"], "failed", message[:300])
            return "failed"
        except (tailor.TailorError, intake.IntakeError, cfgmod.ConfigError) as e:
            tr.set_state(row["id"], "failed", f"{type(e).__name__}: {str(e)[:260]}")
            return "failed"
        except Exception as e:                        # one job's bug must not take the queue down
            tr.set_state(row["id"], "failed", f"{type(e).__name__}: {str(e)[:260]}")
            return "failed"
        after = tr.db.execute("SELECT status FROM jobs WHERE id=?", (row["id"],)).fetchone()
        return after["status"] if after else "done"

    return process


# ─── the queue ───────────────────────────────────────────────────────────────

@dataclass
class Report:
    done: list = field(default_factory=list)            # (id, company, role, outcome)
    stopped: str = ""
    left: int = 0

    def lines(self) -> list[str]:
        out = [f"  #{i}  {c} | {r}: {T.label(o)}" for i, c, r, o in self.done]
        return out or ["  (nothing was processed)"]


STOPPED_BY_YOU = ("stopped at your request: the job in progress was finished and saved; the rest stay approved and the "
                  "next run continues with them")


def run_batch(tr: "T.Tracker", cfg: dict, settings: Settings, *, process=None, only: "list[int] | None" = None,
              limit: "int | None" = None, sleep=time.sleep, rng: "random.Random | None" = None, now=datetime.now,
              log=print, should_stop=None) -> Report:
    rng = rng or random.Random()
    process = process or make_processor(tr, cfg)
    queue = [r for r in tr.approved() if only is None or r["id"] in only]
    rep = Report(left=len(queue))
    started = 0
    for row in queue:
        if should_stop and should_stop():
            rep.stopped = STOPPED_BY_YOU
            break
        if limit is not None and started >= limit:
            rep.stopped = f"--limit {limit} reached"
            break
        used = tr.batch_count_today(now())
        if used >= settings.daily_cap:
            rep.stopped = (f"daily cap of {settings.daily_cap} jobs reached ({used} started today): the rest stay approved "
                           "for tomorrow")
            break
        if started:
            wait = rng.uniform(*settings.delay_s)
            log(f"  waiting {wait:.0f}s before the next job...")
            if should_stop:                                   # wait in short steps so a stop request is not left waiting
                left = wait
                while left > 0 and not should_stop():
                    sleep(min(3.0, left))
                    left -= 3.0
                if should_stop():
                    rep.stopped = STOPPED_BY_YOU
                    break
            else:
                sleep(wait)
        log(f"\n[{started + 1}/{len(queue)}] #{row['id']} {row['company']} | {row['role']}  "
            f"({discovery.route_label(row, cfg)})")
        lid = tr.batch_start(row["id"])
        try:
            outcome = process(row)
        except tailor.UsageLimitError as e:
            folder = str(e.job_dir) if getattr(e, "job_dir", None) else None
            tr.set_state(row["id"], "approved", "stopped by Claude's usage limit: run batch.py again after the reset and it "
                         "continues from the saved folder" if folder else "stopped by Claude's usage limit: run batch.py "
                         "again after the reset", folder)
            tr.batch_finish(lid, "usage_limit")
            rep.stopped = (f"Claude's usage limit was reached while working on #{row['id']}. Nothing is lost: wait for the "
                           "reset, then run `python batch.py` again; it resumes this job first.")
            break
        except StopBatch as e:
            tr.batch_finish(lid, "stopped")
            rep.stopped = f"stopped: {e}"
            break
        except KeyboardInterrupt:
            tr.batch_finish(lid, "interrupted")
            rep.stopped = "interrupted"
            raise
        tr.batch_finish(lid, outcome)
        started += 1
        rep.done.append((row["id"], row["company"], row["role"], outcome))
        log(f"  -> {T.label(outcome)}")
    rep.left = len([r for r in tr.approved() if only is None or r["id"] in only])
    return rep


def format_queue(tr: "T.Tracker", cfg: dict, settings: Settings) -> str:
    rows = tr.approved()
    used = tr.batch_count_today()
    head = (f"{len(rows)} approved job(s) waiting; {used}/{settings.daily_cap} started today; "
            f"{settings.delay_s[0]:.0f}-{settings.delay_s[1]:.0f}s between jobs.")
    lines = [head]
    for r in rows:
        resume = "  [resumes: folder kept]" if r["job_folder"] else ""
        lines.append(f"  #{r['id']:<4} {(r['company'] or '')[:24]:<24} {(r['role'] or '')[:36]:<36} "
                     f"{discovery.route_label(r, cfg)}{resume}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Prepare the approved jobs, one at a time.")
    ap.add_argument("--list", action="store_true", help="show the queue and today's count, then exit")
    ap.add_argument("--only", metavar="IDS", help="process only these ids (comma-separated)")
    ap.add_argument("--limit", type=int, help="process at most this many jobs now")
    ap.add_argument("--delay", type=float, metavar="SECONDS", help="wait exactly this long between jobs (overrides config)")
    args = ap.parse_args()
    try:
        cfg = cfgmod.load_config(apply.CONFIG_FILE)
        settings = load_settings(cfg)
        if args.delay is not None:
            settings.delay_s = (args.delay, args.delay)
        with T.Tracker(apply._tracker_file()) as tr:
            if args.list:
                print(format_queue(tr, cfg, settings))
                return 0
            only = [int(x) for x in re.split(r"[,\s]+", args.only.strip()) if x] if args.only else None
            print(format_queue(tr, cfg, settings))
            rep = run_batch(tr, cfg, settings, only=only, limit=args.limit)
            print("\nDone:")
            print("\n".join(rep.lines()))
            if rep.stopped:
                print(f"\n{rep.stopped}")
            print(f"{rep.left} approved job(s) still waiting.")
            ready = len(tr.action_needed())
            if ready:
                print(f"{ready} posting(s) are waiting on you (python apply.py --status, or the Action needed tab).")
    except (cfgmod.ConfigError, ValueError) as e:
        sys.exit(f"{type(e).__name__}: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
