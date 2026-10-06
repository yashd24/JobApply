"""The `jobapply` command (bin/jobapply.cmd runs this): everything, in short words.

  Finding and choosing
    jobapply find                  discover and score new jobs only; nothing is approved or applied for
    jobapply list [min-score]      the Found jobs: ID, score, company, role, location, link (`list 7` shows 7 and up)
    jobapply approve <ids>         approve jobs: `approve 3,7,12`, `approve all`, or `approve 8+` (every job scoring 8+)
    jobapply skip <ids>            reject jobs so they never come back
    jobapply unapprove <ids>       move approved, unprocessed jobs back to Found (not rejected): `unapprove 3,7`,
                                   `unapprove all` or `unapprove below 7`; untick them in the sheet if they were ticked
  Doing the work
    jobapply process [N]           prepare or apply the approved jobs, best score first (the batch runner); N = only
                                   that many now. `process --list` shows the queue in that order, with scores
    jobapply                       the full run: find, approve the best (threshold), process, sync
    jobapply dry                   a dry run of the full run: nothing is approved, processed or submitted
    jobapply url <link>            tailor and prepare one job from a link (Greenhouse: applied per your config mode);
                                   other sites may need --company "X" --role "Y"
    jobapply retry <id>            redo a failed / needs-review job from its saved folder, without repeating paid work
  After that
    jobapply open <id>             open the job's link, its prepare_sheet.html and its folder
    jobapply done <ids>            mark jobs as applied (same as ticking "Mark applied" in the sheet)
    jobapply action                the jobs that need you (Ready for you, Needs review, Manual, Failed)
    jobapply status [N]            recent applications
    jobapply sync                  sync the Google Sheet (also reads the Approve ticks)
  Housekeeping
    jobapply check [--quick]       health check: LaTeX, Claude login, your profile, the sheet connection, the browser
    jobapply log                   open today's run log
    jobapply stop                  stop a run in progress cleanly, from a second terminal: the job in progress is finished
                                   and saved, the tracker is updated, the run exits, and the next run continues from there
    jobapply help                  this list

Ticking "Approve" on a Found row in the sheet's Applications tab approves it: `process`, the full run and every sync read
the ticks. Any other arguments go straight to run.py (`jobapply --skip-discovery`). Ctrl+C in a run's own window is the
same clean stop; a second Ctrl+C aborts at once. Nothing here is scheduled: it only runs when you type it.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

HELP = (__doc__ or "").strip()


@dataclass
class Plan:
    kind: str          # run | find | apply | list | approve | unapprove | skip | process | open | done | url | retry | check | action | log | stop | help
    args: list[str] = field(default_factory=list)


def parse(argv: list[str]) -> Plan:
    """argv (without the program name) -> what to do. Anything that is not a subcommand is passed to run.py."""
    if not argv:
        return Plan("run")
    word, rest = argv[0].lower(), list(argv[1:])
    if word in ("help", "-h", "--help", "/?", "?"):
        return Plan("help")
    if word == "dry":
        return Plan("run", ["--dry-run", *rest])
    if word == "status":
        return Plan("apply", ["--status", *rest])
    if word == "sync":
        return Plan("apply", ["--sync-sheet", *rest])
    if word == "action":
        return Plan("action")
    if word == "log":
        return Plan("log")
    if word == "stop":
        return Plan("stop")
    if word in ("find", "list", "approve", "unapprove", "skip", "process", "open", "done", "url", "retry", "check"):
        return Plan(word, rest)
    return Plan("run", list(argv))


# ─── subcommands ─────────────────────────────────────────────────────────────

def action_text(rows, link_of) -> str:
    """The jobs that need you, each with its link, folder and reason."""
    from jobbot import tracker as T
    if not rows:
        return "Nothing needs you right now."
    out = [f"{len(rows)} job(s) need you:"]
    for r in rows:
        out.append(f"\n#{r['id']}  {T.label(r['status'])}  |  {r['company'] or '?'}  |  {r['role'] or '?'}")
        out.append(f"    link:   {link_of(r)}")
        if r["job_folder"]:
            out.append(f"    folder: {r['job_folder']}")
        if r["reason"]:
            out.append(f"    why:    {r['reason']}")
    out.append("\nApplied by hand? `jobapply done <ids>`, or tick 'Mark applied' in the sheet.")
    return "\n".join(out)


def show_action() -> int:
    import tailor
    from jobbot import discovery
    from jobbot import tracker as T
    path = tailor.OUTPUT_DIR / "tracker.sqlite3"
    if not path.exists():
        print("No tracker yet: nothing has been run.")
        return 0
    with T.Tracker(path) as t:
        print(action_text(t.action_needed(), discovery.link_of))
    return 0


def todays_log() -> Path:
    import tailor
    return tailor.OUTPUT_DIR / "runs" / f"{date.today():%Y-%m-%d}.log"


def open_log(opener=None) -> int:
    path = todays_log()
    if not path.exists():
        print(f"No run log for today yet ({path}).")
        return 1
    print(f"Opening {path}")
    (opener or os.startfile)(str(path))                  # Windows: the default app for .log files
    return 0


def stop_run(alive=None) -> int:
    """Ask the run in progress (if any) to stop cleanly. The request is a file the run looks at between jobs."""
    from jobbot import stopflag
    pid = stopflag.running_pid(alive) if alive else stopflag.running_pid()
    if pid is None:
        print("No run is in progress, so there is nothing to stop.")
        return 0
    if stopflag.requested():
        print(f"A stop was already requested; the run (process {pid}) is finishing its current job.")
        return 0
    stopflag.request()
    print(f"Stop requested for the run in progress (process {pid}).\n"
          "It finishes and saves the job it is on, updates the tracker, then exits; that can take a few minutes.\n"
          "The next `jobapply` continues from there without repeating paid work.")
    return 0


def supervise(proc, request_stop, abort, kill, say=print, poll_s: float = 0.5) -> int:
    """Wait for the run. First Ctrl+C: ask for a clean stop. Second: abort at once. Third: kill it."""
    presses = 0
    while True:
        try:
            return proc.wait(timeout=poll_s)
        except subprocess.TimeoutExpired:
            continue
        except KeyboardInterrupt:
            presses += 1
            if presses == 1:
                request_stop()
                say("\nCtrl+C: stopping cleanly. The job in progress is finished and saved, then the run exits. "
                    "Press Ctrl+C again to abort at once (the job in progress may be repeated).")
            elif presses == 2:
                say("\nAborting at once...")
                abort()
            else:
                say("\nKilling the run.")
                kill()


def run_worker(args: list[str], popen=subprocess.Popen, script: str = "run.py") -> int:
    """Run run.py as a child in its own console process group, so a Ctrl+C in this window reaches only this launcher
    (and not the claude / LaTeX / browser processes mid-job); the launcher turns it into a clean-stop request."""
    import signal
    from jobbot import stopflag
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    proc = popen([sys.executable, str(ROOT / script), *args], cwd=str(ROOT), creationflags=flags)

    def abort():
        try:
            proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
        except (OSError, ValueError):
            proc.kill()

    return supervise(proc, lambda: stopflag.request("Ctrl+C"), abort, proc.kill)


def newest_summary(since: float) -> "dict | None":
    """The summary file a run wrote at or after `since` (a time.time() value), if any."""
    import tailor
    folder = tailor.OUTPUT_DIR / "runs"
    files = [p for p in folder.glob("*.json") if p.stat().st_mtime >= since - 1] if folder.exists() else []
    if not files:
        return None
    try:
        return json.loads(max(files, key=lambda p: p.stat().st_mtime).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def summary_block(s: "dict | None") -> str:
    """Five numbers, printed last so they are what stays on screen."""
    if not s:
        return "No run summary was written (the run did not finish; see the log above)."
    if s.get("find_only"):
        head = "FIND FINISHED (nothing approved or applied for)"
        nums = (f"found {s.get('found_new', 0)} new  |  scored {s.get('scored', 0)}  |  "
                f"{s.get('found_waiting', 0)} Found in all  |  {s.get('found_at_threshold', 0)} at the approval threshold "
                f"({s.get('threshold', '?')}+)")
    else:
        head = "DRY RUN (nothing approved or submitted)" if s.get("dry_run") else "RUN FINISHED"
        nums = (f"found {s.get('found_new', 0)}  |  scored {s.get('scored', 0)}  |  submitted {s.get('submitted', 0)}  |  "
                f"ready for you {s.get('ready_for_you', 0)}  |  needs review {s.get('needs_review', 0)}")
    lines = ["", "=" * 78, f" {head}  {s.get('date', '')}", " " + nums]
    if s.get("dry_run") and s.get("would_approve") is not None:
        lines.append(f" would approve: {len(s['would_approve'])}")
    if s.get("find_only"):
        lines.append(" next: `jobapply list`, then `jobapply approve ...`, then `jobapply process`")
    if s.get("approved_from_sheet"):
        lines.append(f" approved from the sheet's Approve ticks: {s['approved_from_sheet']}")
    if s.get("stopped"):
        lines.append(f" STOPPED: {s['stopped']}")
    lines.append("=" * 78)
    return "\n".join(lines)


def run_module(name: str, args: list[str]) -> int:
    mod = __import__(name)
    old = sys.argv
    sys.argv = [f"{name}.py", *args]
    try:
        code = mod.main()
    except SystemExit as e:
        code = e.code
    finally:
        sys.argv = old
    return code if isinstance(code, int) else 0


def tracker_file() -> Path:
    import tailor
    return tailor.OUTPUT_DIR / "tracker.sqlite3"


def sync_quietly(tr, say=print) -> None:
    """Bring the sheet up to date after a change here (and read the ticks). A sheet problem is a printed note."""
    try:
        import apply
        from jobbot import sheets
        client = apply._sheet_client()
        if client:
            say("Google Sheet:", sheets.sync(tr, client))
    except Exception as e:
        say(f"(Google Sheet not updated: {type(e).__name__}: {str(e)[:140]})")


def tracker_command(plan: Plan, say=print) -> int:
    """list / approve / skip / done / open / retry: all of them read or change the tracker and nothing else."""
    from jobbot import commands as C
    from jobbot import tracker as T
    try:
        with T.Tracker(tracker_file()) as tr:
            if plan.kind == "list":
                say(C.list_text(tr.found(), C.parse_min_score(plan.args)))
            elif plan.kind == "unapprove":
                ids, unscored = C.resolve_unapproval(tr, plan.args)
                say(C.unapprove_text(tr, ids, unscored))
                if ids:
                    sync_quietly(tr, say)
                    say(still_ticked_text(tr, ids))
            elif plan.kind in ("approve", "skip"):
                ids = C.resolve_approval(tr, plan.args) if plan.kind == "approve" else C.parse_ids(plan.args)
                say(C.decide_text(tr, ids, approve=plan.kind == "approve"))
                if ids:
                    sync_quietly(tr, say)
            elif plan.kind == "done":
                say(C.mark_done_text(tr, C.parse_ids(plan.args)))
                sync_quietly(tr, say)
            elif plan.kind == "open":
                ids = C.parse_ids(plan.args)
                if len(ids) != 1:
                    raise C.CommandError("open takes one id, like `jobapply open 12`")
                return C.open_job(tr, ids[0], say=say)
    except C.CommandError as e:
        say(f"jobapply {plan.kind}: {e}")
        return 2
    return 0


def still_ticked_text(tr, ids: list, client=None) -> str:
    """After `unapprove`: which of those jobs still have their Approve box ticked in the sheet. The bot never writes to
    that column, so it cannot untick them; it ignores a tick on an unapproved job until the box has been seen unticked."""
    try:
        from jobbot import sheets
        if client is None:
            import apply
            client = apply._sheet_client()
        if not client:
            return ""
        ticked = sheets.ticked_rows(client)
    except Exception as e:
        return f"(could not check the sheet's Approve ticks: {type(e).__name__}: {str(e)[:100]}; untick the moved jobs yourself)"
    hits = [(r, ticked[r["canonical_url"]]) for r in tr.by_ids(ids) if r["status"] == "found" and r["canonical_url"] in ticked]
    if not hits:
        return "None of them is ticked in the sheet."
    lines = ["Untick these in the sheet's Approve column (the bot never writes there; until you do, it ignores their ticks, "
             "so they stay Found):"]
    lines += [f"  row {n}: #{r['id']} {r['company'] or '?'} | {r['role'] or '?'}" for r, n in hits]
    return "\n".join(lines)


def split_url_args(args: list[str]) -> "tuple[str, str | None, str | None]":
    """`<link> [--company X] [--role Y]` -> (link, company, role)."""
    link, company, role, i = None, None, None, 0
    while i < len(args):
        a = args[i]
        if a in ("--company", "--role") and i + 1 < len(args):
            if a == "--company":
                company = args[i + 1]
            else:
                role = args[i + 1]
            i += 2
            continue
        if link is None and not a.startswith("--"):
            link = a
            i += 1
            continue
        raise ValueError(f"unexpected argument {a!r}")
    if not link:
        raise ValueError("give the link, like `jobapply url https://boards.greenhouse.io/acme/jobs/123`")
    return link, company, role


def queue_then_process(plan: Plan, say=print, worker=None) -> int:
    """`url` and `retry` change one job's state, then hand it to the batch runner (stoppable like any run)."""
    from jobbot import commands as C
    from jobbot import tracker as T
    worker = worker or (lambda args: run_worker(args, script="batch.py"))
    try:
        with T.Tracker(tracker_file()) as tr:
            if plan.kind == "url":
                link, company, role = split_url_args(plan.args)
                job_id = C.add_from_link(tr, link, company, role, say=say)
            else:
                ids = C.parse_ids(plan.args)
                if len(ids) != 1:
                    raise C.CommandError("retry takes one id, like `jobapply retry 12`")
                job_id = C.prepare_retry(tr, ids[0])
                say(f"#{job_id} is back in the queue with its saved folder.")
    except (C.CommandError, ValueError) as e:
        say(f"jobapply {plan.kind}: {e}")
        return 2
    code = worker(["--only", str(job_id)])
    finish_note(job_id, say)
    return code


def finish_note(job_id: int, say=print) -> None:
    """Where the job ended up, printed last."""
    from jobbot import tracker as T
    try:
        with T.Tracker(tracker_file()) as tr:
            row = (tr.by_ids([job_id]) or [None])[0]
    except Exception:
        return
    if row:
        say("\n" + "=" * 78)
        say(f" #{row['id']} {row['company']} | {row['role']}: {T.label(row['status'])}")
        if row["reason"]:
            say(f" {row['reason']}")
        say(f" `jobapply open {row['id']}` opens its link, sheet and folder.")
        say("=" * 78)


def process_command(plan: Plan, say=print, worker=None) -> int:
    worker = worker or (lambda args: run_worker(args, script="batch.py"))
    if plan.args == ["--list"]:                       # the queue, in the order it will be worked; touches nothing
        return run_module("batch", ["--list"])
    args = []
    if plan.args:
        if len(plan.args) != 1 or not plan.args[0].isdigit() or int(plan.args[0]) < 1:
            say("jobapply process: give a whole number, like `jobapply process 3`, nothing (all approved jobs) or --list")
            return 2
        args = ["--limit", plan.args[0]]
    code = worker(args)
    try:
        from jobbot import tracker as T
        with T.Tracker(tracker_file()) as tr:
            c = tr.counts()
        say("\n" + "=" * 78)
        say(f" PROCESS FINISHED  |  ready for you {c.get('ready_for_you', 0)}  |  needs review {c.get('needs_review', 0)}  |  "
            f"failed {c.get('failed', 0)}  |  approved, still waiting {c.get('approved', 0)}")
        say(" `jobapply action` lists what is waiting on you.")
        say("=" * 78)
    except Exception:
        pass
    return code


def main(argv: "list[str] | None" = None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    plan = parse(list(sys.argv[1:] if argv is None else argv))
    if plan.kind == "help":
        print(HELP)
        return 0
    if plan.kind == "action":
        return show_action()
    if plan.kind == "log":
        return open_log()
    if plan.kind == "stop":
        return stop_run()
    if plan.kind == "apply":
        return run_module("apply", plan.args)
    if plan.kind in ("list", "approve", "unapprove", "skip", "done", "open"):
        return tracker_command(plan)
    if plan.kind in ("url", "retry"):
        return queue_then_process(plan)
    if plan.kind == "process":
        return process_command(plan)
    if plan.kind == "check":
        from jobbot import commands as C
        return 0 if C.health_check(ROOT, quick="--quick" in plan.args) else 1
    args = ["--find-only", *plan.args] if plan.kind == "find" else plan.args
    started = time.time()
    code = run_worker(args)
    if not any(a in ("-h", "--help") for a in plan.args):
        print(summary_block(newest_summary(started) if code == 0 else None))
    return code


if __name__ == "__main__":
    sys.exit(main())
