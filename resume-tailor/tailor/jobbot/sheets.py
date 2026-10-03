"""M7: optional Google Sheets sync of the tracker.

Main tab ("Applications"), two kinds of columns:
  A-N  written by the bot: Date, Status, Mode, Company, Role, Location, URL, Score, Flagged fields, Resume file used,
       Gaps, Notes, Reason, Job folder
  O-R  the user's own: Response, Interview stage, Follow-up date, My notes

The sync NEVER writes to O-R. Every write to the main tab goes through `_AutoOnly`, which refuses any range outside A-N
(the only exception: the header row of a completely empty sheet). Rows are matched by the URL in column G, not by
position, so sorting, filtering or inserting rows cannot make the bot update the wrong row or touch the wrong notes.
Values are written RAW (a note starting with "=" can never become a formula).

Second tab ("Action needed"): everything waiting on YOU (Ready for you, Needs review, Failed, Manual), with a "Mark
applied" checkbox in column A. The sync reads the ticks FIRST (a ticked posting becomes Submitted, by hand), and only
then rebuilds the tab, so a tick can never end up on a different posting's row.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from jobbot import tracker as T

AUTO_COLUMNS = ["Date", "Status", "Mode", "Company", "Role", "Location", "URL", "Score", "Flagged fields",
                "Resume file used", "Gaps", "Notes", "Reason", "Job folder", "Relevance", "Relevance reason"]
SHOW_FOUND_FROM = 5          # a Found posting appears in the main tab only if its relevance score is at least this
MANUAL_COLUMNS = ["Response", "Interview stage", "Follow-up date", "My notes"]
HEADER = AUTO_COLUMNS + MANUAL_COLUMNS
URL_INDEX = AUTO_COLUMNS.index("URL")
LAST_AUTO_COLUMN = chr(64 + len(AUTO_COLUMNS))              # N
HEADER_RANGE = f"A1:{chr(64 + len(HEADER))}1"               # A1:R1
DEFAULT_SHEET_NAME = "Applications"
ACTION_TAB = "Action needed"
ACTION_HEADER = ["Mark applied", "ID", "Status", "Company", "Role", "Link", "Reason", "Job folder", "Date"]
ACTION_LAST_COLUMN = chr(64 + len(ACTION_HEADER))           # I
ACTION_MAX_ROW = 1000
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
    """Wraps a sheet client (main tab only); refuses to write outside the bot's columns."""

    def __init__(self, client, header_allowed: bool = False):
        self.client, self.header_allowed = client, header_allowed

    def read(self, a1):
        return self.client.read(a1)

    def _check(self, a1: str) -> None:
        first, last = columns_of(a1)
        last_allowed = _col_number(LAST_AUTO_COLUMN)
        if first < 1 or last > last_allowed:
            if self.header_allowed and a1 == HEADER_RANGE:
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


def _list_text(job, field) -> str:
    try:
        return "; ".join(str(x) for x in json.loads(job[field] or "[]"))
    except ValueError:
        return str(job[field] or "")


def job_row(job) -> list:
    """The sixteen bot-owned cells for one tracked posting (a sqlite3.Row or dict)."""
    pdf = job["resume_pdf"]
    return [(job["submitted_at"] or job["created_at"] or "")[:10], T.label(job["status"]),
            "manual" if job["submitted_by"] == "manual" else (job["mode"] or ""), job["company"] or "",
            job["role"] or "", job["location"] or "", job["canonical_url"], "" if job["score"] is None else job["score"],
            _list_text(job, "flagged_fields"), f"{Path(pdf).parent.name}/{Path(pdf).name}" if pdf else "",
            _list_text(job, "gaps"), job["notes"] or "", job["reason"] or "", job["job_folder"] or "",
            "" if job["relevance"] is None else job["relevance"], job["relevance_reason"] or ""]


def action_row(job) -> list:
    """One row of the "Action needed" tab. The checkbox starts unticked."""
    from jobbot import discovery
    return [False, job["id"], T.label(job["status"]), job["company"] or "", job["role"] or "", discovery.link_of(job),
            job["reason"] or "", job["job_folder"] or "", (job["submitted_at"] or job["created_at"] or "")[:10]]


def _same(a: list, b: list) -> bool:
    pad = lambda row: [str(x) for x in list(row) + [""] * (len(AUTO_COLUMNS) - len(row))][:len(AUTO_COLUMNS)]  # noqa: E731
    return pad(a) == pad(b)


@dataclass
class SyncResult:
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    marked_applied: int = 0
    action_rows: int = 0

    def __str__(self) -> str:
        text = f"{self.added} added, {self.updated} updated, {self.unchanged} unchanged"
        if self.marked_applied:
            text += f"; {self.marked_applied} marked applied from the sheet"
        return text + (f"; {self.action_rows} waiting on you" if self.action_rows else "")


def _is_ticked(cell) -> bool:
    return str(cell).strip().upper() == "TRUE"


def consume_ticks(tracker: "T.Tracker", client, tab: str = ACTION_TAB) -> int:
    """Mark every posting whose 'Mark applied' box is ticked as applied by hand. Always reads the tab at least twice and
    keeps going while new ticks keep appearing, so a tick made while the sync is running is caught before the tab is
    rebuilt (the only gap left is the instant between the last read and the rebuild)."""
    handled: set[str] = set()
    marked = 0
    for attempt in range(5):
        rows = client.read(f"A1:{ACTION_LAST_COLUMN}{ACTION_MAX_ROW}", tab=tab)
        fresh = []
        for row in rows[1:]:
            if row and _is_ticked(row[0]):
                ref = str(row[1]).strip() if len(row) > 1 and str(row[1]).strip() else (str(row[5]).strip() if len(row) > 5 else "")
                if ref and ref not in handled:
                    fresh.append(ref)
        if not fresh and attempt >= 1:
            break
        for ref in fresh:
            handled.add(ref)
            if tracker.mark_applied(ref) == "marked":
                marked += 1
    return marked


def write_action_tab(tracker: "T.Tracker", client, tab: str = ACTION_TAB) -> int:
    rows = [action_row(j) for j in tracker.action_needed()]
    values = [ACTION_HEADER] + rows
    client.write(f"A1:{ACTION_LAST_COLUMN}{len(values)}", values, tab=tab)
    if len(values) < ACTION_MAX_ROW:
        client.clear(f"A{len(values) + 1}:{ACTION_LAST_COLUMN}{ACTION_MAX_ROW}", tab=tab)
    return len(rows)


def show_found_from(cfg: "dict | None") -> int:
    try:
        return int(((cfg or {}).get("sheets") or {}).get("show_found_from", SHOW_FOUND_FROM))
    except (TypeError, ValueError):
        return SHOW_FOUND_FROM


def sync(tracker: "T.Tracker", client, urls: "list[str] | None" = None, action_tab: "str | None" = ACTION_TAB,
         found_from: int = SHOW_FOUND_FROM) -> SyncResult:
    """Push tracker rows to the sheet. `urls` limits the main tab to those postings (after a run); None syncs everything.
    The Action needed tab is always read (for ticks) and rebuilt."""
    result = SyncResult()
    if action_tab:
        client.ensure_action_tab(action_tab, ACTION_HEADER)
        result.marked_applied = consume_ticks(tracker, client, action_tab)
    rows = client.read(f"A1:{chr(64 + len(HEADER))}")
    if not any(any(str(c).strip() for c in r) for r in rows):                    # an empty sheet: write the header
        _AutoOnly(client, header_allowed=True).write(HEADER_RANGE, [HEADER])
        rows = [HEADER]
    elif [str(c) for c in rows[0][:len(AUTO_COLUMNS)]] != AUTO_COLUMNS:           # our headers only; the user's stay
        _AutoOnly(client).write(f"A1:{LAST_AUTO_COLUMN}1", [AUTO_COLUMNS])
    out = _AutoOnly(client)
    where: dict[str, int] = {}
    for n, r in enumerate(rows[1:], start=2):
        cell = str(r[URL_INDEX]).strip() if len(r) > URL_INDEX else ""
        if cell:
            where.setdefault(T.key(cell), n)                                      # the first row with that URL
    wanted = {T.key(u) for u in urls} if urls is not None else None
    # skipped postings and dry runs are not in the sheet; a Found posting only once it is scored high enough to be worth
    # a look (the rest stay in the tracker and output/shortlist.json)
    jobs = [j for j in tracker.db.execute(
        "SELECT * FROM jobs WHERE status NOT IN ('skipped', 'dry_run') "
        "AND NOT (status='found' AND COALESCE(relevance, 0) < ?) ORDER BY id", (int(found_from),)).fetchall()
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
    if action_tab:
        result.action_rows = write_action_tab(tracker, client, action_tab)
    return result


# ── the real Google Sheet ────────────────────────────────────────────────────

class GoogleSheet:
    """Values API over OAuth desktop credentials the user owns (credentials.json -> token.json, both gitignored)."""
    SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

    def __init__(self, spreadsheet_id: str, sheet_name: str, credentials_file: Path, token_file: Path):
        self.id, self.name = spreadsheet_id, sheet_name
        self.credentials_file, self.token_file = Path(credentials_file), Path(token_file)
        self._svc = None
        self._action_ready: set = set()

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

    def _range(self, a1: str, tab: "str | None" = None) -> str:
        return "'" + (tab or self.name).replace("'", "''") + "'!" + a1

    def _call(self, request):
        try:
            return request.execute()
        except Exception as e:                  # googleapiclient.errors.HttpError and network errors
            raise SheetsError(f"Google Sheets said: {e}. Check the spreadsheet id, that the tab "
                              f"'{self.name}' exists, and that you may edit the sheet.") from e

    def read(self, a1, tab=None):
        return self._call(self._service().spreadsheets().values().get(
            spreadsheetId=self.id, range=self._range(a1, tab))).get("values", [])

    def write(self, a1, values, tab=None):
        self._call(self._service().spreadsheets().values().update(
            spreadsheetId=self.id, range=self._range(a1, tab), valueInputOption="RAW", body={"values": values}))

    def append(self, a1, values, tab=None):
        self._call(self._service().spreadsheets().values().append(
            spreadsheetId=self.id, range=self._range(a1, tab), valueInputOption="RAW", insertDataOption="INSERT_ROWS",
            body={"values": values}))

    def clear(self, a1, tab=None):
        self._call(self._service().spreadsheets().values().clear(
            spreadsheetId=self.id, range=self._range(a1, tab), body={}))

    def ensure_action_tab(self, tab, header):
        """Create the tab if it is missing and make column A (rows 2+) real checkboxes. Done once per run."""
        if tab in self._action_ready:
            return
        svc = self._service()
        meta = self._call(svc.spreadsheets().get(spreadsheetId=self.id, fields="sheets.properties(sheetId,title)"))
        found = next((s["properties"] for s in meta.get("sheets", []) if s["properties"]["title"] == tab), None)
        if found is None:
            reply = self._call(svc.spreadsheets().batchUpdate(spreadsheetId=self.id, body={
                "requests": [{"addSheet": {"properties": {"title": tab}}}]}))
            found = reply["replies"][0]["addSheet"]["properties"]
        self._call(svc.spreadsheets().batchUpdate(spreadsheetId=self.id, body={"requests": [{"setDataValidation": {
            "range": {"sheetId": found["sheetId"], "startRowIndex": 1, "endRowIndex": ACTION_MAX_ROW,
                      "startColumnIndex": 0, "endColumnIndex": 1},
            "rule": {"condition": {"type": "BOOLEAN"}, "strict": True, "showCustomUi": True}}}]}))
        self._action_ready.add(tab)


def configured(cfg: dict) -> bool:
    return bool((cfg.get("sheets") or {}).get("spreadsheet_id"))


def authorised_client(cfg: dict, root: Path, log=print) -> "GoogleSheet | None":
    """The sheet client, or None when no sheet is configured or Google has not been authorised yet. A normal run never
    opens a Google sign-in by itself: authorise once with `python apply.py --sync-sheet`."""
    if not configured(cfg):
        return None
    client = client_from_config(cfg, root)
    if not client.token_file.exists():
        log("    (Google Sheet not synced: run `python apply.py --sync-sheet` once to authorise it)")
        return None
    return client


def client_from_config(cfg: dict, root: Path) -> GoogleSheet:
    s = cfg.get("sheets") or {}
    if not s.get("spreadsheet_id"):
        raise SheetsError("config.yaml has no sheets.spreadsheet_id")
    return GoogleSheet(s["spreadsheet_id"], s.get("sheet_name") or DEFAULT_SHEET_NAME,
                       root / s.get("credentials_file", "credentials.json"), root / s.get("token_file", "token.json"))
