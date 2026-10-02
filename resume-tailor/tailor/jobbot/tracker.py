"""M7: the local tracker. SQLite is the source of truth (output/tracker.sqlite3).

Two tables: `runs` (one row per job folder: what that run did) and `jobs` (one row per posting, derived from its runs).
A posting that has ever been submitted stays `submitted`: a later dry run of the same posting never downgrades it, and
`is_submitted` is what stops the bot from applying twice. Rows come from a job folder's own files
(`apply_result.json`, `answers.json`, `result.json`), so the database can always be rebuilt with `--import-existing`.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from jobbot import intake

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  job_dir TEXT PRIMARY KEY, canonical_url TEXT NOT NULL, status TEXT NOT NULL, mode TEXT, submitted_by TEXT,
  finished_at TEXT, platform TEXT, company TEXT, role TEXT, location TEXT, score INTEGER, gaps TEXT,
  resume_pdf TEXT, answers_path TEXT, screenshots TEXT, flagged_fields TEXT, notes TEXT);
CREATE INDEX IF NOT EXISTS runs_url ON runs (canonical_url);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, canonical_url TEXT UNIQUE NOT NULL, platform TEXT, company TEXT, role TEXT,
  location TEXT, status TEXT NOT NULL, mode TEXT, submitted_by TEXT, score INTEGER, gaps TEXT, resume_pdf TEXT,
  answers_path TEXT, screenshots TEXT, flagged_fields TEXT, created_at TEXT, submitted_at TEXT, notes TEXT,
  runs INTEGER DEFAULT 0);
"""
STATUSES = ("submitted", "needs_review", "failed", "prepared", "dry_run")


def key(url: str) -> str:
    t = intake.detect(url)
    return (t.canonical_url if t else url.strip()).rstrip("/").lower()


def _read(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError):
        return {}


class Tracker:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # ── writing ──
    def record_folder(self, job_dir: Path) -> "dict | None":
        """Read a job folder's result files into the database (idempotent). None if the folder has no run."""
        job_dir = Path(job_dir)
        res = _read(job_dir / "apply_result.json")
        if not res.get("url"):
            return None
        answers, tailored = _read(job_dir / "answers.json"), _read(job_dir / "result.json")
        meta, outcome = answers.get("meta", {}), answers.get("outcome", {})
        flagged = [a["label"] for a in answers.get("answers", []) if a.get("status") == "flagged"]
        finished = (res.get("submitted_on") or res.get("marked") or outcome.get("finished")
                    or datetime.fromtimestamp((job_dir / "apply_result.json").stat().st_mtime).isoformat(timespec="seconds"))
        row = {"job_dir": str(job_dir), "canonical_url": key(res["url"]), "status": res["status"], "mode": res.get("mode"),
               "submitted_by": res.get("submitted_by"), "finished_at": finished, "platform": meta.get("platform"),
               "company": res.get("company"), "role": res.get("role"), "location": meta.get("location"),
               "score": (tailored.get("scores") or {}).get("overall"), "gaps": json.dumps(tailored.get("gaps") or []),
               "resume_pdf": meta.get("resume_pdf") or tailored.get("resume_pdf"),
               "answers_path": str(job_dir / "answers.json") if answers else None,
               "screenshots": json.dumps(meta.get("screenshots") or []), "flagged_fields": json.dumps(flagged),
               "notes": res.get("note")}
        cols = ", ".join(row)
        self.db.execute(f"INSERT OR REPLACE INTO runs ({cols}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
        self.db.commit()
        self._refresh(row["canonical_url"])
        return row

    def _refresh(self, url: str) -> None:
        """Rebuild the postings row from its runs: a submitted run wins, otherwise the latest run."""
        runs = self.db.execute("SELECT * FROM runs WHERE canonical_url=? ORDER BY finished_at, rowid", (url,)).fetchall()
        if not runs:
            return
        submitted = [r for r in runs if r["status"] == "submitted"]
        best = submitted[-1] if submitted else runs[-1]
        values = {c: best[c] for c in ("platform", "company", "role", "location", "mode", "submitted_by", "score", "gaps",
                                       "resume_pdf", "answers_path", "screenshots", "flagged_fields", "notes")}
        values.update(status=best["status"], created_at=runs[0]["finished_at"],
                      submitted_at=best["finished_at"] if submitted else None, runs=len(runs))
        cols = list(values)
        self.db.execute(
            f"INSERT INTO jobs (canonical_url, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))}) "
            f"ON CONFLICT(canonical_url) DO UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in cols)}",
            [url] + [values[c] for c in cols])
        self.db.commit()

    def import_existing(self, output_dir: Path) -> int:
        n = 0
        for d in sorted(Path(output_dir).iterdir()):
            if d.is_dir() and self.record_folder(d):
                n += 1
        return n

    # ── reading ──
    def job(self, url: str) -> "sqlite3.Row | None":
        return self.db.execute("SELECT * FROM jobs WHERE canonical_url=?", (key(url),)).fetchone()

    def is_submitted(self, url: str) -> bool:
        r = self.job(url)
        return bool(r and r["status"] == "submitted")

    def recent(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM jobs ORDER BY COALESCE(submitted_at, created_at) DESC, id DESC LIMIT ?",
                               (limit,)).fetchall()

    def counts(self) -> dict:
        return {r["status"]: r["n"] for r in self.db.execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status")}


def format_status(rows: list, counts: dict) -> str:
    if not rows:
        return "No applications tracked yet. (python apply.py --import-existing reads the folders in output/.)"
    lines = [f"{'WHEN':<11} {'STATUS':<13} {'MODE':<8} {'COMPANY':<18} {'ROLE':<34} URL"]
    for r in rows:
        when = (r["submitted_at"] or r["created_at"] or "")[:10]
        by = "manual" if r["submitted_by"] == "manual" else (r["mode"] or "")
        lines.append(f"{when:<11} {r['status']:<13} {by:<8} {(r['company'] or '')[:17]:<18} "
                     f"{(r['role'] or '')[:33]:<34} {r['canonical_url']}")
    lines.append("")
    lines.append("  ".join(f"{s}: {counts[s]}" for s in STATUSES if s in counts))
    return "\n".join(lines)
