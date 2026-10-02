"""M7: optional Google Sheets sync of the tracker.

The sheet has two kinds of columns:
  A-L  written by the bot: Date, Status, Mode, Company, Role, Location, URL, Score, Flagged fields,
       Resume file used, Gaps, Notes
  M-P  the user's own: Response, Interview stage, Follow-up date, My notes

The sync NEVER writes to M-P. Every write goes through `_AutoOnly`, which refuses any range outside A-L (the only
exception: the header row of a completely empty sheet). Rows are matched by the URL in column G, not by position, so
sorting, filtering or inserting rows in the sheet cannot make the bot update the wrong row or touch the wrong notes.
Values are written RAW (a note starting with "=" can never become a formula).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from jobbot import tracker as T

AUTO_COLUMNS = ["Date", "Status", "Mode", "Company", "Role", "Location", "URL", "Score", "Flagged fields",
                "Resume file used", "Gaps", "Notes"]
MANUAL_COLUMNS = ["Response", "Interview stage", "Follow-up date", "My notes"]
HEADER = AUTO_COLUMNS + MANUAL_COLUMNS
URL_INDEX = AUTO_COLUMNS.index("URL")
LAST_AUTO_COLUMN = "L"
DEFAULT_SHEET_NAME = "Applications"
INSTALL_HINT = "pip install google-api-python-client google-auth-oauthlib"


class SheetsError(Exception):
    pass


def _col_number(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


_RANGE = re.compile(r"^([A-Z]{1,3})(\d*)(?::([A-Z]{1,3})(\d*))?$")


def columns_of(a1: str) -> tuple[int, int]:
    m = _RANGE.match(a1)
    if not m:
        raise SheetsError(f"unsupported range {a1!r}")
    first = _col_number(m.group(1))
    return first, _col_number(m.group(3)) if m.group(3) else first


class _AutoOnly:
    """Wraps a sheet client; refuses to write outside the bot's columns."""

    def __init__(self, client, header_allowed: bool = False):
        self.client, self.header_allowed = client, header_allowed

    def read(self, a1):
        return self.client.read(a1)

    def _check(self, a1: str) -> None:
        first, last = columns_of(a1)
        last_allowed = _col_number(LAST_AUTO_COLUMN)
        if first < 1 or last > last_allowed:
            if self.header_allowed and a1 == f"A1:{chr(64 + len(HEADER))}1":
                return
            raise SheetsError(f"refusing to write {a1}: only columns A-{LAST_AUTO_COLUMN} belong to the bot")

    def write(self, a1, values):
        self._check(a1)
        self.client.write(a1, values)

    def append(self, a1, values):
        self._check(a1)
        if any(len(row) > _col_number(LAST_AUTO_COLUMN) for row in values):
            raise SheetsError("refusing to append more than the bot's columns")
        self.client.append(a1, values)


def job_row(job) -> list:
    """The twelve bot-owned cells for one tracked posting (a sqlite3.Row or dict)."""
    def jl(field):
        try:
            return "; ".join(str(x) for x in json.loads(job[field] or "[]"))
        except ValueError:
            return str(job[field] or "")
    pdf = job["resume_pdf"]
    return [(job["submitted_at"] or job["created_at"] or "")[:10], job["status"] or "",
            "manual" if job["submitted_by"] == "manual" else (job["mode"] or ""), job["company"] or "",
            job["role"] or "", job["location"] or "", job["canonical_url"], "" if job["score"] is None else job["score"],
            jl("flagged_fields"), f"{Path(pdf).parent.name}/{Path(pdf).name}" if pdf else "", jl("gaps"),
            job["notes"] or ""]


def _same(a: list, b: list) -> bool:
    pad = lambda row: [str(x) for x in list(row) + [""] * (len(AUTO_COLUMNS) - len(row))][:len(AUTO_COLUMNS)]  # noqa: E731
    return pad(a) == pad(b)


@dataclass
class SyncResult:
    added: int = 0
    updated: int = 0
    unchanged: int = 0

    def __str__(self) -> str:
        return f"{self.added} added, {self.updated} updated, {self.unchanged} unchanged"


def sync(tracker: "T.Tracker", client, urls: "list[str] | None" = None) -> SyncResult:
    """Push tracker rows to the sheet. `urls` limits it to those postings (after a run); None syncs everything."""
    rows = client.read(f"A1:{chr(64 + len(HEADER))}")
    result = SyncResult()
    if not any(any(str(c).strip() for c in r) for r in rows):                    # an empty sheet: write the header
        _AutoOnly(client, header_allowed=True).write(f"A1:{chr(64 + len(HEADER))}1", [HEADER])
        rows = [HEADER]
    elif [str(c) for c in rows[0][:len(AUTO_COLUMNS)]] != AUTO_COLUMNS:           # our headers only; M1:P1 is the user's
        _AutoOnly(client).write(f"A1:{LAST_AUTO_COLUMN}1", [AUTO_COLUMNS])
    out = _AutoOnly(client)
    where: dict[str, int] = {}
    for n, r in enumerate(rows[1:], start=2):
        cell = str(r[URL_INDEX]).strip() if len(r) > URL_INDEX else ""
        if cell:
            where.setdefault(T.key(cell), n)                                      # the first row with that URL
    wanted = {T.key(u) for u in urls} if urls is not None else None
    jobs = [j for j in tracker.db.execute("SELECT * FROM jobs ORDER BY id").fetchall()
            if wanted is None or j["canonical_url"] in wanted]
    for job in jobs:
        values = job_row(job)
        n = where.get(job["canonical_url"])
        if n is None:
            out.append(f"A:{LAST_AUTO_COLUMN}", [values])
            result.added += 1
        elif _same(rows[n - 1], values):
            result.unchanged += 1
        else:
            out.write(f"A{n}:{LAST_AUTO_COLUMN}{n}", [values])
            result.updated += 1
    return result


# ── the real Google Sheet ────────────────────────────────────────────────────

class GoogleSheet:
    """Values API over OAuth desktop credentials the user owns (credentials.json -> token.json, both gitignored)."""
    SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

    def __init__(self, spreadsheet_id: str, sheet_name: str, credentials_file: Path, token_file: Path):
        self.id, self.name = spreadsheet_id, sheet_name
        self.credentials_file, self.token_file = Path(credentials_file), Path(token_file)
        self._svc = None

    def _service(self):
        if self._svc is None:
            try:
                from google.auth.transport.requests import Request
                from google.oauth2.credentials import Credentials
                from google_auth_oauthlib.flow import InstalledAppFlow
                from googleapiclient.discovery import build
            except ImportError as e:
                raise SheetsError(f"the Google libraries are not installed. Run: {INSTALL_HINT}") from e
            creds = None
            if self.token_file.exists():
                creds = Credentials.from_authorized_user_file(str(self.token_file), self.SCOPES)
            if not creds or not creds.valid:
                if creds and creds.expired and creds.refresh_token:
                    creds.refresh(Request())
                else:
                    if not self.credentials_file.exists():
                        raise SheetsError(f"{self.credentials_file.name} not found: create OAuth desktop credentials in "
                                          "Google Cloud Console and save them there (see README)")
                    creds = InstalledAppFlow.from_client_secrets_file(str(self.credentials_file), self.SCOPES) \
                        .run_local_server(port=0)
                self.token_file.write_text(creds.to_json(), encoding="utf-8")
            self._svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
        return self._svc

    def _range(self, a1: str) -> str:
        return "'" + self.name.replace("'", "''") + "'!" + a1

    def _call(self, request):
        try:
            return request.execute()
        except Exception as e:                  # googleapiclient.errors.HttpError and network errors
            raise SheetsError(f"Google Sheets said: {e}. Check the spreadsheet id, that the tab "
                              f"'{self.name}' exists, and that you may edit the sheet.") from e

    def read(self, a1):
        return self._call(self._service().spreadsheets().values().get(
            spreadsheetId=self.id, range=self._range(a1))).get("values", [])

    def write(self, a1, values):
        self._call(self._service().spreadsheets().values().update(
            spreadsheetId=self.id, range=self._range(a1), valueInputOption="RAW", body={"values": values}))

    def append(self, a1, values):
        self._call(self._service().spreadsheets().values().append(
            spreadsheetId=self.id, range=self._range(a1), valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": values}))


def configured(cfg: dict) -> bool:
    return bool((cfg.get("sheets") or {}).get("spreadsheet_id"))


def client_from_config(cfg: dict, root: Path) -> GoogleSheet:
    s = cfg.get("sheets") or {}
    if not s.get("spreadsheet_id"):
        raise SheetsError("config.yaml has no sheets.spreadsheet_id")
    return GoogleSheet(s["spreadsheet_id"], s.get("sheet_name") or DEFAULT_SHEET_NAME,
                       root / s.get("credentials_file", "credentials.json"), root / s.get("token_file", "token.json"))
