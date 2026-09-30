"""M3 browser session: Playwright + Chromium, headed, persistent profile.

- One dedicated user-data dir under the project (gitignored: browser_profile/) so logins and cookies
  survive between runs.
- Helpers: goto, screenshot(name), pause_for_user(message), human_delay.
- No stealth, no fingerprint spoofing, no CAPTCHA solving: when something needs a human, we pause.

    python -m jobbot.browser <url> [<url> ...] [--job-dir DIR] [--headless] [--no-pause]
"""
from __future__ import annotations

import random
import re
import sys
import time
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROFILE_DIR = ROOT / "browser_profile"


class BrowserError(Exception):
    pass


def _safe(name: str) -> str:
    return re.sub(r"[^\w.-]+", "_", name).strip("_") or "shot"


class _ProfileLock:
    """Exclusive OS lock on the browser profile, so two runs can't share (and corrupt) it.
    Chromium itself did not refuse a second instance in testing. The OS drops the lock if the
    process dies, so it can never go stale."""

    def __init__(self, profile_dir: Path):
        self.path = profile_dir / ".jobbot.lock"
        self._fh = None

    def acquire(self) -> None:
        self._fh = open(self.path, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            self._fh = None
            raise BrowserError(f"The browser profile {self.path.parent} is already in use by another run. "
                               "Close the other automation browser (or wait for that run to finish) "
                               "and retry.") from None

    def release(self) -> None:
        if self._fh:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    self._fh.seek(0)
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
            self._fh.close()
            self._fh = None


class BrowserSession:
    """Context manager around a persistent Chromium context.

        with BrowserSession(job_dir) as b:
            b.goto(url)
            b.screenshot("01_form")
            b.pause_for_user("A CAPTCHA appeared - solve it, then press Enter")
    """

    def __init__(self, job_dir: str | Path, *, profile_dir: str | Path | None = None,
                 headless: bool = False, input_fn: Callable[[str], str] = input,
                 output_fn: Callable[[str], None] = print, slow_mo_ms: int = 0):
        self.job_dir = Path(job_dir)
        self.screenshot_dir = self.job_dir / "screenshots"
        self.profile_dir = Path(profile_dir) if profile_dir else DEFAULT_PROFILE_DIR
        self.headless = headless
        self._input, self._out = input_fn, output_fn
        self._slow_mo = slow_mo_ms
        self._pw = None
        self.context = None
        self.page = None
        self._shots = 0

    # ── lifecycle ──
    def __enter__(self) -> "BrowserSession":
        from playwright.sync_api import sync_playwright
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._lock = _ProfileLock(self.profile_dir)
        self._lock.acquire()
        try:
            self._pw = sync_playwright().start()
        except Exception:
            self._lock.release()
            raise
        try:
            opts = ({"viewport": {"width": 1280, "height": 900}} if self.headless
                    else {"no_viewport": True, "args": ["--start-maximized"]})
            self.context = self._pw.chromium.launch_persistent_context(
                str(self.profile_dir), headless=self.headless, slow_mo=self._slow_mo, **opts)
        except Exception as e:
            self._pw.stop()
            self._lock.release()
            msg = str(e)
            raise BrowserError(f"Could not start Chromium: {msg.splitlines()[0]}. "
                               "Run `python -m playwright install chromium`.") from e
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        try:
            if self.context:
                self.context.close()
        finally:
            try:
                if self._pw:
                    self._pw.stop()
            finally:
                if getattr(self, "_lock", None):
                    self._lock.release()
                self.context = self._pw = self.page = None

    # ── helpers ──
    def _ensure_page(self):
        """The active page; if the user closed the tab during a pause, open a fresh one."""
        if self.context is None:
            raise BrowserError("The browser session is closed.")
        if self.page is None or self.page.is_closed():
            live = [p for p in self.context.pages if not p.is_closed()]
            self.page = live[-1] if live else self.context.new_page()
        return self.page

    def goto(self, url: str, *, wait: str = "domcontentloaded", timeout_ms: int = 45000,
             retries: int = 1) -> int:
        """Open a URL and return its HTTP status. A 5xx is retried `retries` times (Greenhouse
        occasionally answers 502/503); a page that still reports >= 400 is returned, not hidden,
        so the caller must check it before screenshotting or filling."""
        page = self._ensure_page()
        status = 0
        for attempt in range(retries + 1):
            try:
                resp = page.goto(url, wait_until=wait, timeout=timeout_ms)
            except Exception as e:
                raise BrowserError(f"Could not open {url}: {str(e).splitlines()[0]}") from e
            status = resp.status if resp else 0
            if status < 500 or attempt == retries:
                break
            self._out(f"  {url} answered HTTP {status}; retrying in 3s...")
            time.sleep(3)
        self.human_delay(0.4, 1.0)
        return status

    def screenshot(self, name: str, *, full_page: bool = True) -> Path:
        """Save into <job_dir>/screenshots/NN_name.png and return the path."""
        self._shots += 1
        path = self.screenshot_dir / f"{self._shots:02d}_{_safe(name)}.png"
        self._ensure_page().screenshot(path=str(path), full_page=full_page)
        return path

    def pause_for_user(self, message: str) -> None:
        """Print why we stopped, then block until the user presses Enter in the terminal.
        The browser window stays live, so the user can work in it meanwhile."""
        self._out("\n" + "=" * 70)
        self._out(f"PAUSED: {message}")
        self._out("Work in the browser window, then come back here.")
        self._out("=" * 70)
        try:
            self._input("Press Enter to continue... ")
        except EOFError:
            raise BrowserError("Needed the user to continue but there is no interactive terminal "
                               "(stdin closed).") from None
        self._ensure_page()     # survives the user closing a tab; raises if the whole window is gone
        self._out("Resuming.\n")

    @staticmethod
    def human_delay(low: float = 0.3, high: float = 0.9) -> None:
        """A short random pause between actions; not an evasion technique, just less frantic."""
        time.sleep(random.uniform(low, high))


# ─── demo / manual check:  python -m jobbot.browser <url> ... ─────────────────

def _main() -> None:
    import argparse
    from datetime import datetime
    ap = argparse.ArgumentParser(description="Open job pages in the automation browser and screenshot them.")
    ap.add_argument("urls", nargs="+")
    ap.add_argument("--job-dir", help="where screenshots/ goes (default: output/_browser_demo/<time>)")
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--no-pause", action="store_true", help="skip the pause/resume step")
    args = ap.parse_args()
    job_dir = Path(args.job_dir) if args.job_dir else ROOT / "output" / "_browser_demo" / f"{datetime.now():%Y%m%d_%H%M%S}"
    try:
        with BrowserSession(job_dir, headless=args.headless) as b:
            for i, url in enumerate(args.urls, 1):
                status = b.goto(url)
                if status >= 400:
                    print(f"WARNING: {url} returned HTTP {status}")
                print("screenshot:", b.screenshot(f"page{i}"), f"(HTTP {status})")
            if not args.no_pause:
                b.pause_for_user("pause/resume check - move around in the browser if you like")
                print("after pause:", b._ensure_page().url)
                print("screenshot:", b.screenshot("after_pause"))
    except BrowserError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    _main()
