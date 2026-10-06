"""M7: optional Google Sheets sync of the tracker.

Main tab ("Applications"), three kinds of columns:
  A-P  written by the bot: Date, Status, Mode, Company, Role, Location, URL, Score, Flagged fields, Resume file used,
       Gaps, Notes, Reason, Job folder, Relevance, Relevance reason
  Q-T  the user's own: Response, Interview stage, Follow-up date, My notes
  U    ID, written by the bot (the tracker id, for `jobapply open / done / retry`)
  V    Approve: the user's checkbox on Found jobs. The bot NEVER writes a value there (only the header label, once, when
       it is blank, and the checkbox formatting on Found rows). Ticks are read FIRST on every sync and every
       `jobapply process`: a ticked Found posting becomes Approved in the tracker.

The sync NEVER writes to Q-T or to the Approve ticks. Every write to the main tab goes through `_AutoOnly`, which refuses
any range outside A-P and U (the exceptions: the whole header row of a completely empty sheet, and the V1 label). Rows are
matched by the URL in column G, not by position, so sorting, filtering or inserting rows cannot make the bot update the
wrong row, touch the wrong notes or approve the wrong posting.
Values are written RAW (a note starting with "=" can never become a formula).

Second tab ("Action needed"): everything waiting on YOU (Ready for you, Needs review, Failed, Manual), with a "Mark
applied" checkbox in column A. The sync reads the ticks FIRST (a ticked posting becomes Submitted, by hand), and only
then rebuilds the tab, so a tick can never end up on a different posting's row.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from jobbot import tracker as T

AUTO_COLUMNS = ["Date", "Status", "Mode", "Company", "Role", "Location", "URL", "Score", "Flagged fields",
                "Resume file used", "Gaps", "Notes", "Reason", "Job folder", "Relevance", "Relevance reason"]
SHOW_FOUND_FROM = 5          # a Found posting appears in the main tab only if its relevance score is at least this
MANUAL_COLUMNS = ["Response", "Interview stage", "Follow-up date", "My notes"]
HEADER = AUTO_COLUMNS + MANUAL_COLUMNS
ID_COLUMN, APPROVE_COLUMN = "ID", "Approve"
ID_INDEX = len(HEADER)                                      # 20: column U, after the user's columns
APPROVE_INDEX = ID_INDEX + 1                                # 21: column V
ID_LETTER, APPROVE_LETTER = chr(65 + ID_INDEX), chr(65 + APPROVE_INDEX)
FULL_HEADER = HEADER + [ID_COLUMN, APPROVE_COLUMN]
URL_INDEX = AUTO_COLUMNS.index("URL")
LAST_AUTO_COLUMN = chr(64 + len(AUTO_COLUMNS))              # P
HEADER_RANGE = f"A1:{APPROVE_LETTER}1"                      # A1:V1
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
        if first >= 1 and last <= last_allowed:
            return
        if first == last == ID_INDEX + 1:                                          # the ID column
            return
        if a1 == f"{APPROVE_LETTER}1":                                             # the Approve header label, nothing else
            return
        if self.header_allowed and a1 == HEADER_RANGE:
            return
        raise SheetsError(f"refusing to write {a1}: only columns A-{LAST_AUTO_COLUMN} and {ID_LETTER} belong to the bot")

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
    approved: int = 0
    still_ticked: int = 0

    def __str__(self) -> str:
        text = f"{self.added} added, {self.updated} updated, {self.unchanged} unchanged"
        if self.marked_applied:
            text += f"; {self.marked_applied} marked applied from the sheet"
        if self.approved:
            text += f"; {self.approved} approved from the sheet (run `jobapply process`)"
        if self.still_ticked:
            text += f"; {self.still_ticked} unapproved job(s) are still ticked in the sheet: untick them"
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


def ticked_rows(client) -> dict:
    """{job url key: sheet row number} for every row whose Approve box is ticked. Read only."""
    out = {}
    for n, row in enumerate(client.read(f"A1:{APPROVE_LETTER}")[1:], start=2):
        if len(row) > APPROVE_INDEX and _is_ticked(row[APPROVE_INDEX]) and len(row) > URL_INDEX and str(row[URL_INDEX]).strip():
            out.setdefault(T.key(str(row[URL_INDEX])), n)
    return out


def consume_approvals(tracker: "T.Tracker", client, report: "dict | None" = None) -> list:
    """Approve every Found posting whose 'Approve' box is ticked in the main tab; returns the approved ids. Like 'Mark
    applied' it reads the ticks BEFORE the sheet is touched, keeps reading while new ticks keep appearing (one made while the
    sync runs is caught), and finds the posting by the URL in its own row (the ID cell if the URL is blank), never by the
    row's position. It only reads: the bot never writes a value into the Approve column.

    A posting moved back to Found with `jobapply unapprove` is flagged: while its box is still ticked the tick is ignored
    (it is listed in report["ignored"]), and once the box has been seen unticked the flag is cleared, so ticking it again
    later approves it as usual. `report`, if given, is filled with {"ignored": [ids]}."""
    handled: set = set()
    approved: list = []
    ignored: list = []
    unticked: set = set()
    for attempt in range(5):
        rows = client.read(f"A1:{APPROVE_LETTER}")
        fresh = []
        unticked = set()
        for row in rows[1:]:
            url = str(row[URL_INDEX]).strip() if len(row) > URL_INDEX else ""
            ident = str(row[ID_INDEX]).strip() if len(row) > ID_INDEX else ""
            ref = ("url", T.key(url)) if url else (("id", ident) if ident.isdigit() else None)
            if ref is None:
                continue
            if len(row) > APPROVE_INDEX and _is_ticked(row[APPROVE_INDEX]):
                if ref not in handled:
                    fresh.append(ref)
            else:
                unticked.add(ref)
        if not fresh and attempt >= 1:
            break
        for ref in fresh:
            handled.add(ref)
            job = _job_of(tracker, ref)
            if job is not None and job["status"] == "found":
                if job["tick_ignored"]:
                    ignored.append(job["id"])
                    continue
                tracker.decide([job["id"]], approve=True, why="approved by ticking Approve in the sheet")
                approved.append(job["id"])
    for ref in unticked - handled:                        # seen unticked: a later tick counts again
        job = _job_of(tracker, ref)
        if job is not None and job["tick_ignored"]:
            tracker.clear_tick_ignored(job["id"])
    if report is not None:
        report["ignored"] = ignored
    return approved


def _job_of(tracker: "T.Tracker", ref: tuple):
    return tracker.job(ref[1]) if ref[0] == "url" else (tracker.by_ids([int(ref[1])]) or [None])[0]


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
    report: dict = {}
    result.approved = len(consume_approvals(tracker, client, report))
    result.still_ticked = len(report.get("ignored", []))
    rows = client.read(f"A1:{APPROVE_LETTER}")
    if not any(any(str(c).strip() for c in r) for r in rows):                    # an empty sheet: write the header
        _AutoOnly(client, header_allowed=True).write(HEADER_RANGE, [FULL_HEADER])
        rows = [FULL_HEADER]
    else:
        if [str(c) for c in rows[0][:len(AUTO_COLUMNS)]] != AUTO_COLUMNS:         # our headers only; the user's stay
            _AutoOnly(client).write(f"A1:{LAST_AUTO_COLUMN}1", [AUTO_COLUMNS])
        head = [str(c) for c in rows[0]] + [""] * (len(FULL_HEADER) - len(rows[0]))
        if head[ID_INDEX] != ID_COLUMN:
            _AutoOnly(client).write(f"{ID_LETTER}1:{ID_LETTER}1", [[ID_COLUMN]])
        if not head[APPROVE_INDEX].strip():                                       # the label only, and only if blank
            _AutoOnly(client).write(f"{APPROVE_LETTER}1", [[APPROVE_COLUMN]])
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
    sync_ids_and_boxes(tracker, client, rows, result)
    if action_tab:
        result.action_rows = write_action_tab(tracker, client, action_tab)
    return result


def sync_ids_and_boxes(tracker: "T.Tracker", client, before: list, result: SyncResult) -> None:
    """Fill the ID column (U) from each row's own URL, in one write that touches only column U, and put the Approve
    checkbox on the rows of Found postings. Rows are found by URL, so a sorted sheet gets the right ids."""
    rows = client.read(f"A1:{APPROVE_LETTER}") if (result.added or result.updated) else before
    info = {r["canonical_url"]: (r["id"], r["status"]) for r in tracker.db.execute("SELECT id, canonical_url, status FROM jobs")}
    wanted, have, found_rows = [], [], []
    for n, r in enumerate(rows[1:], start=2):
        url = str(r[URL_INDEX]).strip() if len(r) > URL_INDEX else ""
        here = str(r[ID_INDEX]).strip() if len(r) > ID_INDEX else ""
        known = info.get(T.key(url)) if url else None
        wanted.append([known[0] if known else here])
        have.append([here])
        if known and known[1] == "found":
            found_rows.append(n)
    if wanted and [[str(c[0])] for c in wanted] != have:
        _AutoOnly(client).write(f"{ID_LETTER}2:{ID_LETTER}{len(wanted) + 1}", wanted)
    if found_rows and hasattr(client, "show_approve_boxes"):
        client.show_approve_boxes(found_rows)


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

    def show_approve_boxes(self, rows: "list[int]", tab=None) -> None:
        """Make the Approve cell (column V) a real checkbox on these rows. Formatting only: no value is written."""
        svc = self._service()
        meta = self._call(svc.spreadsheets().get(spreadsheetId=self.id, fields="sheets.properties(sheetId,title)"))
        found = next((s["properties"] for s in meta.get("sheets", []) if s["properties"]["title"] == (tab or self.name)), None)
        if found is None or not rows:
            return
        spans, start, prev = [], rows[0], rows[0]
        for n in rows[1:]:
            if n != prev + 1:
                spans.append((start, prev))
                start = n
            prev = n
        spans.append((start, prev))
        requests = [{"setDataValidation": {
            "range": {"sheetId": found["sheetId"], "startRowIndex": a - 1, "endRowIndex": b,
                      "startColumnIndex": APPROVE_INDEX, "endColumnIndex": APPROVE_INDEX + 1},
            "rule": {"condition": {"type": "BOOLEAN"}, "strict": True, "showCustomUi": True}}} for a, b in spans]
        for i in range(0, len(requests), 100):
            self._call(svc.spreadsheets().batchUpdate(spreadsheetId=self.id, body={"requests": requests[i:i + 100]}))

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
    if not configured(cfg) or os.environ.get("JOBBOT_NO_SHEET"):      # the test suite sets it: tests never reach a real sheet
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
