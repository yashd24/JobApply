"""A clean stop for a run in progress: `jobapply stop` (from any terminal) or the first Ctrl+C in the run's window.

The request is a file, output/run.stop. The run looks at it at the safe points: between searches, between scoring
batches (each batch is saved as it finishes) and before each job. The job in progress is finished and saved first, the
tracker is updated, and the run exits; the next run resumes from what is saved, so nothing paid for is repeated.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from datetime import datetime
from pathlib import Path

LOCK_MAX_AGE_S = 8 * 3600


def _output_dir() -> Path:
    import tailor
    return tailor.OUTPUT_DIR


def stop_file() -> Path:
    return _output_dir() / "run.stop"


def lock_file() -> Path:
    return _output_dir() / "run.lock"


def request(reason: str = "jobapply stop") -> Path:
    path = stop_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{reason} {datetime.now().isoformat(timespec='seconds')}\n", encoding="utf-8")
    return path


def requested() -> bool:
    return stop_file().exists()


def clear() -> None:
    try:
        stop_file().unlink()
    except OSError:
        pass


def pid_alive(pid: int) -> bool:
    """Is a process with this id running? (Windows: tasklist; elsewhere: signal 0.)"""
    if os.name == "nt":
        res = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
        return str(pid) in (res.stdout or "").split()
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def running_pid(alive=pid_alive) -> "int | None":
    """The process id of the run that holds output/run.lock, if that process is really still running."""
    try:
        text = lock_file().read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.match(r"\s*pid\s+(\d+)", text)
    return int(m.group(1)) if m and alive(int(m.group(1))) else None


STOP_WORDS = "stopped at your request (jobapply stop / Ctrl+C)"


class AlreadyRunning(Exception):
    pass


class Lock:
    """output/run.lock: two runs must not overlap (they would share the browser profile and the daily cap)."""

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


def install_stop_signals():
    """First Ctrl+C asks for a clean stop (same as `jobapply stop`); a second one aborts at once. Ctrl+Break aborts at
    once too (jobapply sends it on a second Ctrl+C). Returns a function that puts the old handlers back."""
    import signal
    import threading
    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    asked = []

    def on_interrupt(signum, frame):
        if asked:
            raise KeyboardInterrupt
        asked.append(1)
        request("Ctrl+C")
        print("\nCtrl+C: stopping cleanly. The job in progress is finished and saved, then the run exits and the next run "
              "continues from there. Press Ctrl+C again to abort at once (the job in progress may be repeated).")

    old = {signal.SIGINT: signal.signal(signal.SIGINT, on_interrupt)}
    if hasattr(signal, "SIGBREAK"):
        old[signal.SIGBREAK] = signal.signal(signal.SIGBREAK, signal.default_int_handler)

    def restore():
        for sig, handler in old.items():
            signal.signal(sig, handler)
    return restore
