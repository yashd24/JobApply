"""The `jobapply` command (bin/jobapply.cmd runs this): short words for the things done most.

    jobapply                  full run (run.py)
    jobapply dry              dry run: discover, filter and score; approve and submit nothing
    jobapply status [N]       recent applications
    jobapply sync             sync the Google Sheet
    jobapply action           the jobs that need you: Ready for you, Needs review, Manual, Failed
    jobapply log              open today's run log
    jobapply stop             disable the daily scheduled task
    jobapply start            enable the daily scheduled task
    jobapply help             this list

Any other arguments go straight to run.py, so `jobapply --skip-discovery` works.
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

TASK_NAME = "JobApply daily run"
ROOT = Path(__file__).resolve().parents[1]

HELP = (__doc__ or "").strip()


@dataclass
class Plan:
    kind: str                                   # run | apply | action | log | task | help
    args: list[str] = field(default_factory=list)
    enable: bool = False                        # kind == "task"


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
    if word in ("stop", "start"):
        return Plan("task", enable=(word == "start"))
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
    out.append("\nApplied by hand? Tick 'Mark applied' in the sheet, or: python apply.py --mark-applied <ids>")
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


def set_task(enable: bool, runner=subprocess.run) -> int:
    flag = "/ENABLE" if enable else "/DISABLE"
    res = runner(["schtasks", "/Change", "/TN", TASK_NAME, flag], capture_output=True, text=True)
    text = ((res.stdout or "") + (res.stderr or "")).strip()
    word = "enable" if enable else "disable"
    if res.returncode == 0:
        print(f"The daily task '{TASK_NAME}' is now {word}d.")
    else:
        print(f"Could not {word} '{TASK_NAME}': {text or 'schtasks failed'}\n"
              "(Is the task registered? See the README for how to create it.)")
    return res.returncode


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
    head = "DRY RUN (nothing approved or submitted)" if s.get("dry_run") else "RUN FINISHED"
    nums = (f"found {s.get('found_new', 0)}  |  scored {s.get('scored', 0)}  |  submitted {s.get('submitted', 0)}  |  "
            f"ready for you {s.get('ready_for_you', 0)}  |  needs review {s.get('needs_review', 0)}")
    lines = ["", "=" * 78, f" {head}  {s.get('date', '')}", " " + nums]
    if s.get("dry_run") and s.get("would_approve") is not None:
        lines.append(f" would approve: {len(s['would_approve'])}")
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
    if plan.kind == "task":
        return set_task(plan.enable)
    if plan.kind == "apply":
        return run_module("apply", plan.args)
    started = time.time()
    code = run_module("run", plan.args)
    if not any(a in ("-h", "--help") for a in plan.args):
        print(summary_block(newest_summary(started) if code == 0 else None))
    return code


if __name__ == "__main__":
    sys.exit(main())
