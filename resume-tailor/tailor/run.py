#!/usr/bin/env python3
"""python run.py: find, filter, score, approve the best and process them, with nobody at the keyboard.

    python run.py                  the whole unattended flow (this is what the daily scheduled task runs)
    python run.py --dry-run        discover, filter and score for real, then show what WOULD be approved; nothing is
                                   approved, processed or submitted
    python run.py --skip-discovery use the postings already found (score, approve, process)

The steps: discover -> rules filter -> relevance score -> auto-approve the postings at or above the threshold (most relevant
first, up to the daily cap) -> process each -> sync the sheet -> write output/runs/<date>.json and print it.

  Greenhouse                auto mode: submitted without review ONLY if every required field is filled and nothing is flagged.
                            Anything flagged, a captcha, or an unclear result: nobody is waited for; the materials are
                            prepared and the job is marked Needs review with the reason.
  Lever, LinkedIn, Naukri,  prepared (tailored resume, cover letter, answer sheet) and marked Ready for you with the link.
  company sites
  Anything already in the tracker is never applied for again. A Claude usage limit stops the run cleanly; the next run
  resumes where it stopped.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import apply
import batch
import tailor
from jobbot import config as cfgmod
from jobbot import discovery as D
from jobbot import profile as P
from jobbot import boardcheck, relevance, sheets
from jobbot import tracker as T

ROOT = Path(__file__).resolve().parent
LOCK_MAX_AGE_S = 8 * 3600
DEFAULT_THRESHOLD = 7


class AlreadyRunning(Exception):
    pass


def runs_dir() -> Path:
    return tailor.OUTPUT_DIR / "runs"


class Lock:
    """output/run.lock: two scheduled runs must not overlap (they would share the browser profile and the daily cap)."""

    def __init__(self, path: Path, now=time.time):
        self.path, self.now = path, now

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and self.now() - self.path.stat().st_mtime < LOCK_MAX_AGE_S:
            raise AlreadyRunning(f"another run started {int((self.now() - self.path.stat().st_mtime) / 60)} minutes ago and "
                                 f"holds {self.path.name}. If it crashed, delete {self.path}.")
        self.path.write_text(f"pid {os.getpid()} started {datetime.now().isoformat(timespec='seconds')}\n", encoding="utf-8")
        return self

    def __exit__(self, *exc):
        try:
            self.path.unlink()
        except OSError:
            pass


def threshold_of(cfg: dict) -> int:
    try:
        return int(((cfg or {}).get("selection") or {}).get("relevance_threshold", DEFAULT_THRESHOLD))
    except (TypeError, ValueError):
        raise cfgmod.ConfigError("selection.relevance_threshold must be a whole number") from None


def selection_of(cfg: dict) -> dict:
    """The selection knobs, with their defaults."""
    sel = (cfg or {}).get("selection") or {}
    try:
        return {"threshold": threshold_of(cfg),
                "fetch_missing_descriptions": bool(sel.get("fetch_missing_descriptions", True)),
                "max_description_fetches": int(sel.get("max_description_fetches", 60)),
                "board_check": bool(sel.get("board_check", True)),
                "board_check_min_relevance": int(sel.get("board_check_min_relevance", 5)),
                "board_check_max_companies": int(sel.get("board_check_max_companies", 150))}
    except (TypeError, ValueError) as e:
        raise cfgmod.ConfigError(f"selection: a number is not a number ({e})") from None


def greenhouse_mode_of(cfg: dict) -> str:
    mode = ((cfg or {}).get("run") or {}).get("greenhouse_mode", "auto")
    if mode not in cfgmod.MODES:
        raise cfgmod.ConfigError(f"run.greenhouse_mode must be one of {cfgmod.MODES}, got {mode!r}")
    return mode


def already_in_tracker(tr: "T.Tracker", row) -> "str | None":
    """Another posting with the same company and title that is not just 'found': we have applied, prepared, queued or
    declined it. Returns that posting's status, or None."""
    other = tr.db.execute("SELECT status FROM jobs WHERE fingerprint=? AND id!=? AND status NOT IN ('found') LIMIT 1",
                          (T.fingerprint(row["company"], row["role"]), row["id"])).fetchone()
    return other["status"] if other else None


def auto_approve(tr: "T.Tracker", threshold: int, daily_cap: int, log=print) -> list:
    """Approve the postings scored at or above the threshold, most relevant first, as many as today's cap still allows
    (the cap counts jobs already started today and jobs already approved and waiting)."""
    room = daily_cap - tr.batch_count_today() - len(tr.approved())
    chosen = []
    for row in tr.top_found(threshold, max(room, 0) + 50):          # a few spares for ones already in the tracker
        if len(chosen) >= max(room, 0):
            break
        if (status := already_in_tracker(tr, row)):
            tr.set_state(row["id"], "skipped", f"already in the tracker as {T.label(status)}: never applied for twice")
            log(f"  skipped #{row['id']} {row['company']} | {row['role']}: already in the tracker as {T.label(status)}")
            continue
        chosen.append(row)
    for row in chosen:
        tr.decide([row["id"]], approve=True,
                  why=f"auto-approved: relevance {row['relevance']}/10 (threshold {threshold}): {row['relevance_reason']}")
    return chosen


def format_scored(rows, threshold: int, cfg: "dict | None" = None) -> str:
    """The scored list, best first: for checking that the scorer agrees with you."""
    if not rows:
        return "(no scored postings)"

    def cut(x, n):
        x = str(x or "")
        return x if len(x) <= n else x[:n - 1] + "~"
    out = [f"{'ID':>4} {'SC':>3}  {'TITLE':<34} {'COMPANY':<22} {'EXP':<9} {'ROUTE':<20} WHY"]
    for r in sorted(rows, key=lambda r: (-(r["relevance"] or 0), r["id"])):
        mark = "*" if (r["relevance"] or 0) >= threshold else " "
        out.append(f"{r['id']:>4} {r['relevance'] if r['relevance'] is not None else '-':>3}{mark} {cut(r['role'], 34):<34} "
                   f"{cut(r['company'], 22):<22} {cut(r['experience_asked'], 9):<9} {cut(D.route_label(r, cfg), 20):<20} "
                   f"{r['relevance_reason'] or '(not scored)'}")
    out.append(f"\n* = at or above the threshold ({threshold}): auto-approved on a real run, most relevant first, up to the daily cap.")
    return "\n".join(out)


def run_all(cfg: dict, tr: "T.Tracker", *, dry_run: bool = False, skip_discovery: bool = False, discover_fn=None, llm=None,
            profile_loader=None, resume_loader=None, process=None, sync_fn=None, sleep=time.sleep,
            rng: "random.Random | None" = None, now=datetime.now, log=print, fetch_text=None, board_http=None) -> dict:
    """The whole flow. Returns the run summary. A Claude usage limit ends the Claude steps cleanly (the rest still
    happens: sheet sync, summary); a problem in one step is recorded and does not abort the others."""
    started = now()
    llm = llm or tailor.call_claude
    profile_loader = profile_loader or (lambda: P.load_profile(apply.PROFILE_FILE))
    resume_loader = resume_loader or tailor.load_resume_data
    settings, sel = batch.load_settings(cfg), selection_of(cfg)
    threshold = sel["threshold"]
    s: dict = {"date": started.strftime("%Y-%m-%d"), "started": started.isoformat(timespec="seconds"), "dry_run": dry_run,
               "scraped": 0, "dropped": {}, "found_new": 0, "already_tracked": 0, "scored": 0, "unscored_left": 0,
               "approved": 0, "submitted": 0, "ready_for_you": 0, "needs_review": 0, "manual": 0, "failed": 0,
               "stopped": "", "errors": [], "jobs": [], "threshold": threshold}

    def usage_limit(e) -> None:
        s["stopped"] = (f"Claude's usage limit was reached ({str(e)[:120]}): the Claude steps stopped; run again after the "
                        "reset and it continues (approved jobs resume first, unscored postings are scored next)")
        log(f"\n{s['stopped']}")

    # 1-2. discover + rules filter (no Claude)
    if not skip_discovery:
        log("1/5 Discovering and filtering (no Claude usage)...")
        try:
            disc = (discover_fn or (lambda: D.discover(D.load_settings(cfg), tr, log=log)))()
            s["scraped"] = disc.scraped
            s["dropped"] = {k: len(v) for k, v in disc.dropped.items()}
            s["found_new"] = disc.saved.get("new", 0) + disc.saved.get("replaced", 0)
            s["already_tracked"] = len(disc.dropped.get("already tracked", []))
            s["errors"] += disc.errors
        except D.DiscoveryError as e:
            s["errors"].append(f"discovery: {e}")
            log(f"  discovery could not run: {e}")
    else:
        log("1/5 Discovery skipped (--skip-discovery).")

    # 2b. postings found earlier are re-judged by TODAY's rules (no network, no Claude): rule fixes apply at once
    try:
        gone = D.recheck_found(tr, D.load_settings(cfg), log=log)
        for g in gone:
            key = f"rechecked: {g['kind']}"
            s["dropped"][key] = s["dropped"].get(key, 0) + 1
    except D.DiscoveryError as e:
        s["errors"].append(f"recheck: {e}")

    # 2c. postings with no description get one (HTTP only) BEFORE scoring: the scorer and the rules judge from text
    s["descriptions"] = {"tried": 0, "filled": 0, "still_missing": 0}
    if sel["fetch_missing_descriptions"]:
        log("2/6 Fetching missing descriptions (HTTP only, no Claude)...")
        try:
            s["descriptions"] = D.fill_descriptions(tr, fetch_text or batch.fetch_description, sleep=sleep, log=log,
                                                    limit=sel["max_description_fetches"])
            if s["descriptions"]["filled"]:                     # new text can reveal "5+ years" or firmware work
                for g in D.recheck_found(tr, D.load_settings(cfg), log=log):
                    key = f"rechecked: {g['kind']}"
                    s["dropped"][key] = s["dropped"].get(key, 0) + 1
        except Exception as e:
            s["errors"].append(f"descriptions: {type(e).__name__}: {str(e)[:160]}")

    # 3. relevance score (one Claude call per 10 postings)
    todo = len(tr.unscored())
    log(f"3/6 Scoring {todo} posting(s) that have no relevance score yet (one Claude call per 10)...")
    if todo:
        try:
            rep = relevance.score_unscored(tr, llm, resume_loader(), profile_loader(), log=log)
            s["scored"] = rep.scored
            s["errors"] += rep.problems
        except tailor.UsageLimitError as e:
            usage_limit(e)
        except Exception as e:
            s["errors"].append(f"scoring: {type(e).__name__}: {str(e)[:160]}")
            log(f"  scoring failed: {s['errors'][-1]}")
    s["unscored_left"] = len(tr.unscored())

    # 3b. the strict company-board check: a manual posting that is really a Greenhouse/Lever one moves onto that route
    s["board_check"] = {"checked": 0, "matched": 0, "rerouted": []}
    if sel["board_check"]:
        log("4/6 Company-board check (Greenhouse/Lever public APIs, read-only; same company + title + location, confirmed live)...")
        try:
            bc = boardcheck.check_found(tr, D.load_settings(cfg), min_relevance=sel["board_check_min_relevance"],
                                        max_companies=sel["board_check_max_companies"], http=board_http, sleep=sleep, log=log,
                                        now=now)
            s["board_check"] = {"checked": bc.checked, "matched": len(bc.rerouted), "rerouted": bc.rerouted}
        except Exception as e:
            s["errors"].append(f"board check: {type(e).__name__}: {str(e)[:160]}")
    found_rows = tr.found()
    log("\n" + format_scored([r for r in found_rows if r["relevance"] is not None], threshold, cfg) + "\n")

    # 4. approve + process
    if dry_run:
        would = tr.top_found(threshold, max(settings.daily_cap - tr.batch_count_today() - len(tr.approved()), 0))
        log(f"DRY RUN: would auto-approve {len(would)} posting(s) now (daily cap {settings.daily_cap}); nothing is approved, "
            "processed or submitted.")
        s["would_approve"] = [{"id": r["id"], "company": r["company"], "role": r["role"], "relevance": r["relevance"],
                               "route": r["route"], "reason": r["relevance_reason"]} for r in would]
    elif not s["stopped"]:
        log("5/6 Approving the most relevant postings (up to the daily cap)...")
        chosen = auto_approve(tr, threshold, settings.daily_cap, log)
        s["approved"] = len(chosen)
        for r in chosen:
            log(f"  approved #{r['id']} [{r['relevance']}] {r['company']} | {r['role']}  ({D.route_label(r, cfg)})")
        log(f"6/6 Processing {len(tr.approved())} approved job(s), one at a time...")
        process = process or batch.make_processor(tr, cfg, unattended=True, greenhouse_mode=greenhouse_mode_of(cfg),
                                                  profile_loader=profile_loader, resume_loader=resume_loader)
        rep = batch.run_batch(tr, cfg, settings, process=process, sleep=sleep, rng=rng, log=log)
        for jid, company, role, outcome in rep.done:
            row = tr.by_ids([jid])
            s[outcome if outcome in ("submitted", "ready_for_you", "needs_review", "manual", "failed") else "failed"] += 1
            s["jobs"].append({"id": jid, "company": company, "role": role, "outcome": outcome,
                              "relevance": row[0]["relevance"] if row else None, "reason": row[0]["reason"] if row else ""})
        if rep.stopped:
            s["stopped"] = rep.stopped
            log(f"\n{rep.stopped}")
    else:
        log("5/6 Approval and processing skipped: " + s["stopped"])

    # 5. sync the sheet
    log("Syncing the Google Sheet...")
    try:
        if sync_fn:
            note = sync_fn(tr)
        else:
            client = apply._sheet_client()
            note = (f"Google Sheet: {sheets.sync(tr, client, found_from=sheets.show_found_from(cfg))}" if client
                    else "(no Google Sheet configured or authorised)")
        log(f"  {note}")
        s["sheet"] = str(note)
    except Exception as e:
        s["errors"].append(f"sheet: {type(e).__name__}: {str(e)[:160]}")
        log(f"  sheet not updated: {s['errors'][-1]}")

    s["finished"] = now().isoformat(timespec="seconds")
    try:
        discover_json = tailor.OUTPUT_DIR / "shortlist.json"
        discover_json.parent.mkdir(parents=True, exist_ok=True)
        discover_json.write_text(json.dumps(D.shortlist_records(tr.found(), cfg), indent=2, ensure_ascii=False),
                                 encoding="utf-8")
    except Exception as e:
        s["errors"].append(f"shortlist.json: {e}")
    return s


def write_summary(summary: dict) -> Path:
    d = runs_dir()
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{summary['date']}.json"
    if path.exists():                                        # a second run on the same day keeps the first
        path = d / f"{summary['date']}_{summary['started'][11:19].replace(':', '')}.json"
    path.write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def format_summary(s: dict) -> str:
    dropped = ", ".join(f"{k} {n}" for k, n in s["dropped"].items()) or "none"
    lines = [f"RUN SUMMARY {s['date']}" + (" (DRY RUN: nothing approved, processed or submitted)" if s["dry_run"] else ""),
             f"  found {s['found_new']} new (scraped {s['scraped']}; already tracked {s['already_tracked']})",
             f"  dropped by reason: {dropped}",
             f"  scored {s['scored']}" + (f" ({s['unscored_left']} still unscored)" if s["unscored_left"] else "") +
             f"; threshold {s['threshold']}",
             f"  approved {s['approved']}; submitted {s['submitted']}; ready for you {s['ready_for_you']}; "
             f"needs review {s['needs_review']}" + (f"; manual {s['manual']}" if s["manual"] else "") +
             (f"; failed {s['failed']}" if s["failed"] else "")]
    d = s.get("descriptions") or {}
    if d.get("tried"):
        lines.append(f"  descriptions fetched {d['filled']} of {d['tried']} missing ({d['still_missing']} still without)")
    bc = s.get("board_check") or {}
    if bc.get("checked"):
        lines.append(f"  board check: {bc['checked']} checked, {bc['matched']} re-routed onto Greenhouse/Lever")
        for m in bc.get("rerouted", []):
            lines.append(f"    #{m['id']} -> {m.get('platform')} {m.get('url')}  (company {m['company']['theirs']!r}, "
                         f"title {m['title']['theirs']!r}, {m['location']['rule']})")
    if s.get("would_approve") is not None:
        lines.append(f"  would approve now: {len(s['would_approve'])}")
    for j in s["jobs"]:
        lines.append(f"    #{j['id']} {j['company']} | {j['role']}: {T.label(j['outcome'])}")
    if s["stopped"]:
        lines.append(f"  STOPPED: {s['stopped']}")
    for e in s["errors"][:6]:
        lines.append(f"  problem: {e}")
    return "\n".join(lines)


class Tee:
    """Print to the console and to the day's log file, so a scheduled run leaves a record."""

    def __init__(self, stream, path: Path):
        self.stream, self.file = stream, open(path, "a", encoding="utf-8")

    def write(self, text):
        self.stream.write(text)
        self.file.write(text)
        self.file.flush()

    def flush(self):
        self.stream.flush()

    def close(self):
        self.file.close()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="The whole unattended flow: discover, score, approve, process, sync.")
    ap.add_argument("--dry-run", action="store_true", help="discover, filter and score, then show what would be approved")
    ap.add_argument("--skip-discovery", action="store_true", help="work from the postings already found")
    args = ap.parse_args(argv)
    runs_dir().mkdir(parents=True, exist_ok=True)
    tee = Tee(sys.stdout, runs_dir() / f"{datetime.now():%Y-%m-%d}.log")
    real_stdout, sys.stdout = sys.stdout, tee
    try:
        print(f"\n===== run.py {datetime.now():%Y-%m-%d %H:%M:%S}{' (dry run)' if args.dry_run else ''} =====")
        cfg = cfgmod.load_config(apply.CONFIG_FILE)
        with Lock(tailor.OUTPUT_DIR / "run.lock"), T.Tracker(apply._tracker_file()) as tr:
            summary = run_all(cfg, tr, dry_run=args.dry_run, skip_discovery=args.skip_discovery)
            path = write_summary(summary)
            print("\n" + format_summary(summary))
            print(f"\n(run summary: {path})")
        return 0
    except AlreadyRunning as e:
        print(f"Not started: {e}")
        return 3
    except (cfgmod.ConfigError, D.DiscoveryError, P.ProfileError) as e:
        print(f"{type(e).__name__}: {e}")
        return 2
    finally:
        sys.stdout = real_stdout
        tee.close()


if __name__ == "__main__":
    sys.exit(main())
