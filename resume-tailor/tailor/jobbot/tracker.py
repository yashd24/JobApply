"""M7: the local tracker. SQLite is the source of truth (output/tracker.sqlite3).

Two tables: `runs` (one row per job folder: what that run did) and `jobs` (one row per posting, derived from its runs).
A posting that has ever been submitted stays `submitted`: a later dry run of the same posting never downgrades it, and
`is_submitted` is what stops the bot from applying twice. Rows come from a job folder's own files
(`apply_result.json`, `answers.json`, `result.json`), so the database can always be rebuilt with `--import-existing`.
"""
from __future__ import annotations

import json
import sqlite3
import re
from datetime import datetime, timedelta
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
CREATE TABLE IF NOT EXISTS batch_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_id INTEGER, started_at TEXT NOT NULL, finished_at TEXT, outcome TEXT);
"""
# The statuses a posting moves through. "ready_for_you" is what a run's own "prepared" becomes: the bot has done its part
# and the next step is yours. dry_run is internal (a rehearsal), never an application.
STATUS_LABELS = {"found": "Found", "approved": "Approved", "ready_for_you": "Ready for you", "needs_review": "Needs review",
                 "manual": "Manual", "failed": "Failed", "submitted": "Submitted", "skipped": "Skipped", "dry_run": "Dry run"}
STATUSES = ("submitted", "ready_for_you", "needs_review", "failed", "manual", "approved", "found", "skipped", "dry_run")
RUN_STATUS_MAP = {"prepared": "ready_for_you"}
ACTION_STATUSES = ("ready_for_you", "needs_review", "failed", "manual")        # waiting on you: the "Action needed" tab
_ACTION_ORDER = {s: i for i, s in enumerate(ACTION_STATUSES)}


def label(status: "str | None") -> str:
    return STATUS_LABELS.get(status or "", status or "")


def run_reason(status: str, res: dict, answers: dict) -> str:
    """One plain sentence on WHY a run ended in this status, for the Reason column."""
    verification = res.get("verification") or {}
    if res.get("reason") and status in ("needs_review", "failed"):
        return str(res["reason"])[:400]
    errors = [str(e) for e in (res.get("validation_errors") or []) if e]
    flagged = (answers.get("summary") or {}).get("flagged")
    if status == "submitted":
        if res.get("submitted_by") == "manual":
            return "submitted by you, by hand (recorded on your word)"
        return f"confirmation seen ({verification.get('signal') or 'confirmation page'})"
    if status == "ready_for_you":
        yours = f"; {flagged} field(s) are yours to answer" if flagged else ""
        return ("resume and answers are ready" + yours + ": open prepare_sheet.html in the job folder, apply by hand, "
                "then tick 'Mark applied'")
    if status == "needs_review":
        return verification.get("reason") or ("no confirmation was seen: check the employer's page and your email, then "
                                              "tick 'Mark applied' if it went through")
    if status == "failed":
        return "; ".join(errors[:3]) or verification.get("reason") or "the employer refused the submission"
    if status == "dry_run":
        return "dry run: nothing was submitted"
    return ""
# Columns added for job discovery (existing databases are migrated in place).
DISCOVERY_COLUMNS = {"route": "TEXT", "source": "TEXT", "source_url": "TEXT", "direct_url": "TEXT",
                     "experience_asked": "TEXT", "fingerprint": "TEXT", "date_posted": "TEXT", "last_seen": "TEXT",
                     "reason": "TEXT", "job_folder": "TEXT", "description": "TEXT",
                     "relevance": "INTEGER", "relevance_reason": "TEXT", "remote": "INTEGER",
                     "route_evidence": "TEXT", "board_checked": "TEXT", "relevance_exceptional": "INTEGER",
                     "tick_ignored": "INTEGER"}
_LEGAL_SUFFIX = re.compile(r"\b(pvt|private|ltd|limited|inc|llc|llp|corp|corporation|india|co)\b")


def norm_text(text: "str | None") -> str:
    """Lower case, parentheticals and punctuation removed, legal suffixes (Pvt, Ltd, Inc...) dropped, spaces squeezed."""
    t = re.sub(r"\([^)]*\)", " ", str(text or "").lower())
    t = _LEGAL_SUFFIX.sub(" ", re.sub(r"[^a-z0-9+#]+", " ", t))
    return re.sub(r"\s+", " ", t).strip()


def fingerprint(company: "str | None", role: "str | None") -> str:
    """The same opening listed on two sites: lower-case company and title without punctuation or legal suffixes."""
    return f"{norm_text(company)}|{norm_text(role)}"


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
        self._migrate()

    def _migrate(self) -> None:
        have = {r["name"] for r in self.db.execute("PRAGMA table_info(jobs)")}
        for name, kind in DISCOVERY_COLUMNS.items():
            if name not in have:
                self.db.execute(f"ALTER TABLE jobs ADD COLUMN {name} {kind}")
        if "reason" not in {r["name"] for r in self.db.execute("PRAGMA table_info(runs)")}:
            self.db.execute("ALTER TABLE runs ADD COLUMN reason TEXT")
        self.db.execute("UPDATE jobs SET status='ready_for_you' WHERE status='prepared'")
        self.db.execute("UPDATE runs SET status='ready_for_you' WHERE status='prepared'")
        for r in self.db.execute("SELECT id, company, role FROM jobs WHERE fingerprint IS NULL").fetchall():
            self.db.execute("UPDATE jobs SET fingerprint=? WHERE id=?", (fingerprint(r["company"], r["role"]), r["id"]))
        self.db.commit()

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
        status = RUN_STATUS_MAP.get(res["status"], res["status"])
        row = {"job_dir": str(job_dir), "canonical_url": key(res["url"]), "status": status, "mode": res.get("mode"),
               "submitted_by": res.get("submitted_by"), "finished_at": finished, "platform": meta.get("platform"),
               "company": res.get("company"), "role": res.get("role"), "location": meta.get("location"),
               "score": (tailored.get("scores") or {}).get("overall"), "gaps": json.dumps(tailored.get("gaps") or []),
               "resume_pdf": meta.get("resume_pdf") or tailored.get("resume_pdf"),
               "answers_path": str(job_dir / "answers.json") if answers else None,
               "screenshots": json.dumps(meta.get("screenshots") or []), "flagged_fields": json.dumps(flagged),
               "notes": res.get("note"), "reason": run_reason(status, res, answers)}
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
                                       "resume_pdf", "answers_path", "screenshots", "flagged_fields", "notes", "reason")}
        folders = [r["job_dir"] for r in runs if not str(r["job_dir"]).startswith("manual:")]
        values.update(fingerprint=fingerprint(best["company"], best["role"]),
                      job_folder=(best["job_dir"] if not str(best["job_dir"]).startswith("manual:")
                                  else (folders[-1] if folders else None)))
        values.update(status=best["status"], created_at=runs[0]["finished_at"],
                      submitted_at=best["finished_at"] if submitted else None, runs=len(runs))
        cols = list(values)
        self.db.execute(
            f"INSERT INTO jobs (canonical_url, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))}) "
            f"ON CONFLICT(canonical_url) DO UPDATE SET {', '.join(f'{c}=excluded.{c}' for c in cols)}",
            [url] + [values[c] for c in cols])
        self.db.commit()

    # ── discovery (M9): found -> approved / manual / skipped, then the normal run statuses ──
    def add_found(self, rec: dict) -> str:
        """Save a discovered posting as status "found". Returns "new", "seen" (already found, refreshed),
        "replaced" (a manual-route twin of this opening was swapped for this ATS link) or "duplicate" (this URL or
        this company+title is already in the tracker in any other state: applied, prepared, skipped, dry run...)."""
        url = key(rec["canonical_url"])
        fp = fingerprint(rec.get("company"), rec.get("role"))
        now = datetime.now().isoformat(timespec="seconds")
        cols = {"platform": rec.get("platform"), "company": rec.get("company"), "role": rec.get("role"),
                "location": rec.get("location"), "route": rec.get("route"), "source": rec.get("source"),
                "source_url": rec.get("source_url"), "direct_url": rec.get("direct_url"),
                "experience_asked": rec.get("experience_asked"), "date_posted": rec.get("date_posted"),
                "notes": rec.get("notes"), "fingerprint": fp, "description": rec.get("description"),
                "remote": None if rec.get("remote") is None else int(bool(rec.get("remote"))),
                "reason": rec.get("reason") or rec.get("notes") or f"found on {rec.get('source') or 'a job board'}"}
        existing = self.db.execute("SELECT * FROM jobs WHERE canonical_url=?", (url,)).fetchone()
        if existing:
            if existing["status"] != "found":
                return "duplicate"
            sets = ", ".join(f"{c}=?" for c in cols) + ", last_seen=?"
            self.db.execute(f"UPDATE jobs SET {sets} WHERE id=?", [*cols.values(), now, existing["id"]])
            self.db.commit()
            return "seen"
        twin = self.db.execute("SELECT * FROM jobs WHERE fingerprint=? AND canonical_url!=?", (fp, url)).fetchone()
        outcome = "new"
        if twin:
            if twin["status"] == "found" and twin["route"] == "manual" and rec.get("route") != "manual":
                self.db.execute("DELETE FROM jobs WHERE id=?", (twin["id"],))
                outcome = "replaced"
            else:
                return "duplicate"
        names = ["canonical_url", "status", "created_at", "last_seen", *cols]
        self.db.execute(f"INSERT INTO jobs ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                        [url, "found", now, now, *cols.values()])
        self.db.commit()
        return outcome

    def is_known(self, url: "str | None" = None, company: "str | None" = None, role: "str | None" = None) -> bool:
        """Is this URL, or this company+title, already in the tracker in any state other than found?"""
        if url and (r := self.job(url)) and r["status"] != "found":
            return True
        fp = fingerprint(company, role)
        return bool(company and self.db.execute("SELECT 1 FROM jobs WHERE fingerprint=? AND status!='found'", (fp,)).fetchone())

    def found(self) -> list[sqlite3.Row]:
        """The shortlist: discovered postings still waiting for a decision. Most relevant first (unscored last), then ATS
        routes, then newest."""
        return self.db.execute(
            "SELECT * FROM jobs WHERE status='found' ORDER BY COALESCE(relevance, 0) DESC, (route='manual'), "
            "COALESCE(date_posted, '') DESC, id").fetchall()

    def unscored(self) -> list[sqlite3.Row]:
        """Found postings that have no relevance score yet (they cost nothing until scored)."""
        return self.db.execute("SELECT * FROM jobs WHERE status='found' AND relevance IS NULL ORDER BY id").fetchall()

    def set_relevance(self, job_id: int, score: int, reason: str, exceptional: bool = False) -> None:
        self.db.execute("UPDATE jobs SET relevance=?, relevance_reason=?, relevance_exceptional=? WHERE id=?",
                        (int(score), reason, int(bool(exceptional)), job_id))
        self.db.commit()

    def clear_relevance(self) -> int:
        """Forget every score on postings that are still Found, so the next scoring pass redoes them (new scoring rules)."""
        n = self.db.execute("UPDATE jobs SET relevance=NULL, relevance_reason=NULL, relevance_exceptional=NULL WHERE status='found' "
                            "AND relevance IS NOT NULL").rowcount
        self.db.commit()
        return n

    def top_found(self, threshold: int, limit: int) -> list[sqlite3.Row]:
        """Found postings scored at or above the threshold: the most relevant first, then newest, up to `limit`."""
        return self.db.execute(
            "SELECT * FROM jobs WHERE status='found' AND relevance >= ? "
            "ORDER BY relevance DESC, COALESCE(date_posted, '') DESC, id LIMIT ?", (int(threshold), max(0, int(limit)))).fetchall()

    def set_description(self, job_id: int, text: str) -> None:
        self.db.execute("UPDATE jobs SET description=? WHERE id=?", (text[:30000], job_id))
        self.db.commit()

    def without_description(self, min_chars: int = 200) -> list[sqlite3.Row]:
        """Found postings whose stored description is missing or too short to judge."""
        return self.db.execute("SELECT * FROM jobs WHERE status='found' AND LENGTH(TRIM(COALESCE(description, ''))) < ? "
                               "ORDER BY id", (int(min_chars),)).fetchall()

    def board_candidates(self, min_relevance: int, recheck_days: int = 7, today: "datetime | None" = None) -> list[sqlite3.Row]:
        """Scored Found postings on the manual route that have not had the company-board check in `recheck_days`."""
        cutoff = ((today or datetime.now()) - timedelta(days=recheck_days)).strftime("%Y-%m-%d")
        return self.db.execute(
            "SELECT * FROM jobs WHERE status='found' AND route='manual' AND relevance >= ? "
            "AND (board_checked IS NULL OR board_checked < ?) ORDER BY relevance DESC, id", (int(min_relevance), cutoff)).fetchall()

    def set_board_checked(self, job_id: int, evidence_json: str, day: str) -> None:
        self.db.execute("UPDATE jobs SET route_evidence=?, board_checked=? WHERE id=?", (evidence_json, day, job_id))
        self.db.commit()

    def reroute(self, job_id: int, platform: str, canonical_url: str, reason: str) -> str:
        """Move a Found manual posting onto its Greenhouse/Lever posting (the board check confirmed it). Returns "rerouted",
        or "duplicate" when that exact posting is already in the tracker (then this one is skipped, never applied twice)."""
        url = key(canonical_url)
        other = self.db.execute("SELECT id, status FROM jobs WHERE canonical_url=? AND id!=?", (url, job_id)).fetchone()
        if other:
            self.db.execute("UPDATE jobs SET status='skipped', reason=? WHERE id=? AND status='found'",
                            (f"the same posting is already in the tracker as {label(other['status'])}", job_id))
            self.db.commit()
            return "duplicate"
        self.db.execute("UPDATE jobs SET canonical_url=?, platform=?, route=?, direct_url=?, reason=? "
                        "WHERE id=? AND status='found'", (url, platform, platform, url, reason, job_id))
        self.db.commit()
        return "rerouted"

    def delete_found_ids(self, ids: list[int]) -> int:
        """Forget these postings, but only while they are still just Found."""
        if not ids:
            return 0
        n = self.db.execute(f"DELETE FROM jobs WHERE status='found' AND id IN ({', '.join('?' * len(ids))})", list(ids)).rowcount
        self.db.commit()
        return n

    def delete_found(self) -> int:
        """Forget every posting that is still only "found" (never a decision): used to re-run discovery under new rules."""
        n = self.db.execute("DELETE FROM jobs WHERE status='found'").rowcount
        self.db.commit()
        return n

    def by_ids(self, ids: list[int]) -> list[sqlite3.Row]:
        marks = ", ".join("?" * len(ids))
        return self.db.execute(f"SELECT * FROM jobs WHERE id IN ({marks}) ORDER BY id", list(ids)).fetchall() if ids else []

    def decide(self, ids: list[int], approve: bool, why: "str | None" = None) -> dict:
        """Approve or skip shortlisted postings (only ones still in status found). EVERY approved posting waits for the
        batch runner ("approved"), which prepares it (Greenhouse is filled in assist mode; everything else gets a tailored
        resume, a cover letter and an answer sheet and becomes "Ready for you"). Nothing costs Claude usage before
        this approval."""
        out = {"approved": [], "skipped": [], "ignored": []}
        for r in self.by_ids(ids):
            if r["status"] != "found":
                out["ignored"].append(r["id"])
                continue
            new = "approved" if approve else "skipped"
            reason_text = why or ({"greenhouse": "approved: the batch runner will fill it in assist mode and you click Submit",
                    "lever": "approved: the batch runner prepares the resume, cover letter and answers; you apply by hand"}
                   .get(r["route"], "approved: the batch runner prepares a tailored resume, cover letter and likely answers; "
                                    "you apply by hand") if approve else "declined from the shortlist")
            self.db.execute("UPDATE jobs SET status=?, reason=? WHERE id=?", (new, reason_text, r["id"]))
            out[new].append(r["id"])
        out["ignored"] += [i for i in ids if i not in {r["id"] for r in self.by_ids(ids)}]
        self.db.commit()
        return out

    def action_needed(self) -> list[sqlite3.Row]:
        """Everything waiting on YOU: prepared and ready, needs a look, failed, or to be applied for by hand."""
        marks = ", ".join("?" * len(ACTION_STATUSES))
        rows = self.db.execute(f"SELECT * FROM jobs WHERE status IN ({marks}) ORDER BY id", list(ACTION_STATUSES)).fetchall()
        return sorted(rows, key=lambda r: (_ACTION_ORDER[r["status"]], r["id"]))

    def mark_applied(self, ref: "int | str") -> str:
        """You applied by hand (the sheet's 'Mark applied' tick, or --mark-applied). The posting becomes Submitted,
        submitted_by manual. Recorded as a run of its own, so later runs or refreshes can never undo it.
        Returns "marked", "already" (it was already Submitted) or "unknown"."""
        job = self.db.execute("SELECT * FROM jobs WHERE id=?", (int(ref),)).fetchone() if str(ref).isdigit() else self.job(str(ref))
        if job is None:
            return "unknown"
        if job["status"] == "submitted":
            return "already"
        now = datetime.now().isoformat(timespec="seconds")
        self.db.execute(
            "INSERT OR REPLACE INTO runs (job_dir, canonical_url, status, mode, submitted_by, finished_at, platform, company, "
            "role, location, score, gaps, resume_pdf, answers_path, screenshots, flagged_fields, notes, reason) "
            "VALUES (?, ?, 'submitted', ?, 'manual', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (f"manual:{job['id']}:{now}", job["canonical_url"], job["mode"], now, job["platform"], job["company"], job["role"],
             job["location"], job["score"], job["gaps"], job["resume_pdf"], job["answers_path"], job["screenshots"],
             job["flagged_fields"], job["notes"], "marked as applied by you"))
        self.db.commit()
        self._refresh(job["canonical_url"])
        return "marked"

    def approved(self) -> list[sqlite3.Row]:
        """Approved postings in the order the batch runner takes them: the highest relevance score first, ties by the newest
        posting (then the newest id); unscored ones last. The daily cap is spent from the top of this list."""
        return self.db.execute("SELECT * FROM jobs WHERE status='approved' ORDER BY (relevance IS NULL), relevance DESC, "
                               "COALESCE(date_posted, '') DESC, id DESC").fetchall()

    def unapprove(self, ids: list[int]) -> dict:
        """Move approved postings that nothing has been done for back to Found (NOT rejected: they can be approved again
        and a later search does not drop them). One with a saved job folder has paid work in it and is kept approved.
        The sheet's Approve tick is the user's: the posting is flagged so a tick that is still there does not approve it
        again until it has been seen unticked once. Returns {"moved": [...], "kept": {id: why}, "ignored": {id: status}}."""
        out: dict = {"moved": [], "kept": {}, "ignored": {}}
        for r in self.by_ids(ids):
            if r["status"] != "approved":
                out["ignored"][r["id"]] = r["status"]
            elif r["job_folder"]:
                out["kept"][r["id"]] = "work was already started (its folder is saved): `jobapply process` finishes it"
            else:
                self.db.execute("UPDATE jobs SET status='found', reason=?, tick_ignored=1 WHERE id=?",
                                ("moved back to Found with jobapply unapprove: not rejected", r["id"]))
                out["moved"].append(r["id"])
        self.db.commit()
        return out

    def clear_tick_ignored(self, job_id: int) -> None:
        self.db.execute("UPDATE jobs SET tick_ignored=NULL WHERE id=?", (job_id,))
        self.db.commit()

    def set_state(self, job_id: int, status: str, reason: str, job_folder: "str | None" = None) -> None:
        """Record a state that no run folder can express (a posting that could not be prepared, a batch that was stopped)."""
        if job_folder is None:
            self.db.execute("UPDATE jobs SET status=?, reason=? WHERE id=?", (status, reason, job_id))
        else:
            self.db.execute("UPDATE jobs SET status=?, reason=?, job_folder=? WHERE id=?", (status, reason, job_folder, job_id))
        self.db.commit()

    # ── the batch runner's log: the daily cap counts from here ──
    def batch_start(self, job_id: int) -> int:
        cur = self.db.execute("INSERT INTO batch_log (job_id, started_at) VALUES (?, ?)",
                              (job_id, datetime.now().isoformat(timespec="seconds")))
        self.db.commit()
        return cur.lastrowid

    def batch_finish(self, log_id: int, outcome: str) -> None:
        self.db.execute("UPDATE batch_log SET finished_at=?, outcome=? WHERE id=?",
                        (datetime.now().isoformat(timespec="seconds"), outcome, log_id))
        self.db.commit()

    def batch_count_today(self, today: "datetime | None" = None) -> int:
        """Jobs the batch runner started today. A job stopped by the usage limit does not count: it will be resumed."""
        day = (today or datetime.now()).strftime("%Y-%m-%d")
        return self.db.execute("SELECT COUNT(*) FROM batch_log WHERE substr(started_at, 1, 10)=? "
                               "AND COALESCE(outcome, '') != 'usage_limit'", (day,)).fetchone()[0]

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
        lines.append(f"{when:<11} {label(r['status']):<13} {by:<8} {(r['company'] or '')[:17]:<18} "
                     f"{(r['role'] or '')[:33]:<34} {r['canonical_url']}")
    lines.append("")
    lines.append("  ".join(f"{label(s)}: {counts[s]}" for s in STATUSES if s in counts))
    return "\n".join(lines)
