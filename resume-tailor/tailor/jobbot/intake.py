"""M2 intake: job URL -> Job (platform, company, role, JD text, form questions).

Greenhouse: public Job Board API, `GET boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}?questions=true`
            (JD + application questions).
Lever:      public Postings API, `GET api.lever.co/v0/postings/{site}/{id}` (JD only - Lever's API does
            not expose custom questions), so the questions are parsed from the server-rendered
            `/apply` page.
If the API route fails we fall back to the HTML scraper `tailor.fetch_jd` (JD only, no questions).

No network happens unless `fetch_job` is called; all fetching goes through an injectable `Http`
so tests run on saved fixtures.
"""
from __future__ import annotations

import html as htmllib
import json
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

UA = {"User-Agent": "Mozilla/5.0"}
GH_HOSTS = {"boards.greenhouse.io", "job-boards.greenhouse.io",
            "boards.eu.greenhouse.io", "job-boards.eu.greenhouse.io"}
LEVER_HOSTS = {"jobs.lever.co", "jobs.eu.lever.co"}


class IntakeError(Exception):
    pass


class UnsupportedPlatform(IntakeError):
    pass


class AlreadySubmitted(IntakeError):
    def __init__(self, url: str):
        self.url = url
        super().__init__(f"Already submitted: {url}")


class Tracker(Protocol):
    """The M7 tracker implements this; intake only needs the one question."""
    def is_submitted(self, canonical_url: str) -> bool: ...


@dataclass
class FieldSpec:
    name: str
    type: str                     # text | textarea | file | hidden | select | multiselect | radio | checkbox | ...
    options: list[str] = field(default_factory=list)


@dataclass
class Question:
    label: str
    required: bool
    fields: list[FieldSpec]
    group: str = "standard"       # standard | location | demographic | custom
    description: str = ""

    @property
    def type(self) -> str:
        return self.fields[0].type if self.fields else "unknown"

    @property
    def options(self) -> list[str]:
        return [o for f in self.fields for o in f.options]


@dataclass
class Job:
    platform: str                 # greenhouse | lever
    company: str
    role: str
    location: str
    canonical_url: str
    apply_url: str
    jd_text: str
    questions: list[Question]
    job_id: str
    board: str                    # greenhouse board token / lever site
    source: str                   # api | html
    source_url: str = ""          # the URL the user gave (e.g. a company career page)
    captcha_possible: bool = False
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Target:
    platform: str
    board: str
    job_id: str
    eu: bool = False

    @property
    def canonical_url(self) -> str:
        if self.platform == "greenhouse":
            return f"https://job-boards{'.eu' if self.eu else ''}.greenhouse.io/{self.board}/jobs/{self.job_id}"
        return f"https://jobs{'.eu' if self.eu else ''}.lever.co/{self.board}/{self.job_id}"

    @property
    def api_url(self) -> str:
        if self.platform == "greenhouse":
            host = "boards-api.eu.greenhouse.io" if self.eu else "boards-api.greenhouse.io"
            return f"https://{host}/v1/boards/{self.board}/jobs/{self.job_id}?questions=true"
        host = "api.eu.lever.co" if self.eu else "api.lever.co"
        return f"https://{host}/v0/postings/{self.board}/{self.job_id}"

    @property
    def apply_page_url(self) -> str:
        return self.canonical_url + "/apply" if self.platform == "lever" else self.canonical_url


# ─── HTTP ────────────────────────────────────────────────────────────────────

Http = Callable[[str], tuple[int, str]]


def default_http(url: str) -> tuple[int, str]:
    import requests
    try:
        r = requests.get(url, headers=UA, timeout=20)
    except requests.RequestException as e:
        raise IntakeError(f"Network error fetching {url}: {type(e).__name__}") from e
    return r.status_code, r.text


# ─── Platform detection ──────────────────────────────────────────────────────

_UUID = r"[0-9a-fA-F-]{8,}"


def detect(url: str) -> Target | None:
    """Platform/board/job id straight from the URL, or None when the URL alone isn't enough
    (a company career page: see resolve_embedded_greenhouse)."""
    u = urlparse(url.strip())
    host, path, q = u.netloc.lower(), u.path, parse_qs(u.query)
    if host in GH_HOSTS:
        eu = ".eu." in host
        if m := re.match(r"^/([^/]+)/jobs/(\d+)", path):
            return Target("greenhouse", m.group(1), m.group(2), eu)
        if path.startswith("/embed/job_app") and q.get("for") and q.get("token"):
            return Target("greenhouse", q["for"][0], q["token"][0], eu)
        if (m := re.match(r"^/([^/]+)/?$", path)) and q.get("gh_jid"):
            return Target("greenhouse", m.group(1), q["gh_jid"][0], eu)
        return None
    if host in LEVER_HOSTS:
        if m := re.match(rf"^/([^/]+)/({_UUID})(?:/apply)?/?$", path):
            return Target("lever", m.group(1), m.group(2), ".eu." in host)
    return None


_GH_EMBED = re.compile(r"greenhouse\.io/embed/job_(?:app|board)[^\"'\s<>]*", re.I)


def resolve_embedded_greenhouse(url: str, page_html: str) -> Target | None:
    """A company career page that embeds Greenhouse (gh_jid query parameter and/or an
    embed iframe/script) -> the Greenhouse target."""
    jid = parse_qs(urlparse(url).query).get("gh_jid", [None])[0]
    board = None
    eu = False
    for m in _GH_EMBED.finditer(htmllib.unescape(page_html)):
        ref = m.group(0)
        q = parse_qs(urlparse("https://x/" + ref.split("greenhouse.io/", 1)[1]).query)
        board = board or (q.get("for") or [None])[0]
        jid = jid or (q.get("token") or [None])[0]
    if not board:   # some sites only link to the board: boards.greenhouse.io/<token>/jobs/<id>
        m = re.search(r"(?:job-)?boards(\.eu)?\.greenhouse\.io/([\w-]+)/jobs/(\d+)", page_html)
        if m:
            eu, board, jid = bool(m.group(1)), m.group(2), jid or m.group(3)
    if board and jid and board != "embed":
        return Target("greenhouse", board, str(jid), eu)
    return None


_GENERIC_LABELS = {"www", "www2", "careers", "career", "jobs", "job", "join", "work", "apply", "boards", "app",
                   "com", "org", "net", "io", "co", "in", "uk", "us", "de", "eu", "ai", "app", "hq", "inc"}


def board_token_candidates(url: str) -> list[str]:
    """Plausible Greenhouse board tokens for a company site, from its hostname
    (careers.airbnb.com -> airbnb, www.okta.com -> okta, stripe.com -> stripe).
    Candidates are only ever *guesses*: guess_embedded_greenhouse accepts one only after the
    Greenhouse API confirms the job id exists on that board."""
    labels = [re.sub(r"[^a-z0-9-]", "", p) for p in urlparse(url).netloc.lower().split(":")[0].split(".")]
    out: list[str] = []
    for lab in labels:
        for cand in (lab, lab[:-2] if lab.endswith("hq") else ""):
            if cand and cand not in _GENERIC_LABELS and cand not in out:
                out.append(cand)
    return out


def guess_embedded_greenhouse(url: str, http: "Http") -> Target | None:
    """Company site with gh_jid but no visible embed (often JavaScript-rendered): try board tokens
    derived from the domain; accept the first whose Greenhouse API has this exact job id."""
    jid = parse_qs(urlparse(url).query).get("gh_jid", [None])[0]
    if not jid or not jid.isdigit():
        return None
    for token in board_token_candidates(url):
        cand = Target("greenhouse", token, jid)
        try:
            status, body = http(cand.api_url)
            if status == 200 and str(json.loads(body).get("id")) == jid:
                return cand
        except ValueError:
            continue
    return None


# ─── Parsing helpers ─────────────────────────────────────────────────────────

_BLOCKS = ["p", "div", "ul", "ol", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "blockquote"]


def html_to_text(markup: str) -> str:
    """Readable text: line breaks at block elements and <br>, '- ' for list items, inline
    tags (strong, a, em...) stay inside their sentence."""
    soup = BeautifulSoup(markup or "", "html.parser")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for li in soup.find_all("li"):
        li.insert(0, "- ")
    for tag in soup.find_all(_BLOCKS):
        tag.insert(0, "\n")
        tag.append("\n")
    lines = (re.sub(r"[ \t]+", " ", ln).strip() for ln in soup.get_text().splitlines())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


_GH_TYPES = {"input_text": "text", "textarea": "textarea", "input_file": "file", "input_hidden": "hidden",
             "multi_value_single_select": "select", "multi_value_multi_select": "multiselect"}


def _gh_question(q: dict, group: str) -> Question:
    fields = [FieldSpec(name=f.get("name", ""), type=_GH_TYPES.get(f.get("type"), f.get("type") or "unknown"),
                        options=[str(v.get("label", "")) for v in f.get("values") or []])
              for f in q.get("fields") or []]
    return Question(label=(q.get("label") or "").strip(), required=bool(q.get("required")), fields=fields,
                    group=group, description=html_to_text(htmllib.unescape(q.get("description") or "")))


def _gh_demographic(dq) -> list[Question]:
    """demographic_questions is null on most jobs; when present it is an object with a `questions`
    list of {label, required, type, answer_options: [{label}]}. Parsed defensively."""
    items = dq.get("questions") if isinstance(dq, dict) else dq
    out = []
    for q in items or []:
        opts = [str(o.get("label", "")) for o in q.get("answer_options") or []]
        t = {"multi_value_single_select": "select", "multi_value_multi_select": "multiselect",
             "short_text": "text", "long_text": "textarea"}.get(q.get("type"), q.get("type") or "unknown")
        out.append(Question(label=(q.get("label") or "").strip(), required=bool(q.get("required")),
                            fields=[FieldSpec(name=str(q.get("id", "")), type=t, options=opts)],
                            group="demographic"))
    return out


def parse_greenhouse(data: dict, target: Target, source_url: str = "") -> Job:
    questions = [_gh_question(q, "standard") for q in data.get("questions") or []]
    questions += [_gh_question(q, "location") for q in data.get("location_questions") or []]
    questions += _gh_demographic(data.get("demographic_questions"))
    return Job(platform="greenhouse", company=data.get("company_name") or target.board,
               role=data.get("title", ""), location=(data.get("location") or {}).get("name", ""),
               canonical_url=target.canonical_url, apply_url=target.apply_page_url,
               jd_text=html_to_text(htmllib.unescape(data.get("content") or "")), questions=questions,
               job_id=str(data.get("id", target.job_id)), board=target.board, source="api",
               source_url=source_url)


def parse_lever_posting(data: dict, target: Target, company: str = "", source_url: str = "") -> Job:
    cats = data.get("categories") or {}
    parts = [data.get("descriptionPlain") or html_to_text(data.get("description", ""))]
    for lst in data.get("lists") or []:
        parts += [str(lst.get("text", "")), html_to_text(lst.get("content", ""))]
    parts.append(data.get("additionalPlain") or html_to_text(data.get("additional", "")))
    return Job(platform="lever", company=company or target.board.replace("-", " ").title(),
               role=data.get("text", ""), location=cats.get("location") or ", ".join(cats.get("allLocations") or []),
               canonical_url=target.canonical_url, apply_url=data.get("applyUrl") or target.apply_page_url,
               jd_text="\n\n".join(p.strip() for p in parts if p and p.strip()), questions=[],
               job_id=str(data.get("id", target.job_id)), board=target.board, source="api",
               source_url=source_url)


def lever_company_from_title(title: str, role: str) -> str:
    """Lever page titles read '<Company> - <Role>'."""
    title = title.strip()
    if role and title.endswith(" - " + role):
        return title[: -len(role) - 3].strip()
    return ""


def parse_lever_apply_form(page_html: str) -> tuple[list[Question], bool, str]:
    """(questions, captcha_possible, company) from a Lever /apply page."""
    soup = BeautifulSoup(page_html, "html.parser")
    questions: list[Question] = []
    for li in soup.select("li.application-question"):
        label_el = li.select_one(".application-label .text") or li.select_one(".application-label")
        raw = label_el.get_text(" ", strip=True) if label_el else ""
        required = "✱" in raw or li.select_one(".required") is not None
        label = re.sub(r"\s+", " ", raw.replace("✱", "")).strip()
        controls = li.select("input, select, textarea")
        by_name: dict[str, FieldSpec] = {}
        for c in controls:
            name, ctype = c.get("name", ""), (c.get("type") or c.name)
            if not name or ctype in ("submit", "button"):
                continue
            spec = by_name.setdefault(name, FieldSpec(name=name, type=ctype))
            if spec.type == "hidden" and ctype != "hidden":    # checkboxes ship a hidden "0" companion
                spec.type = ctype
            if c.name == "select":
                spec.type = "select"
                spec.options = [o.get_text(strip=True) for o in c.select("option") if o.get("value", o.text)]
            elif ctype in ("radio", "checkbox"):
                alt = c.find_parent("label")
                text = alt.select_one(".application-answer-alternative") if alt else None
                spec.options.append(text.get_text(strip=True) if text else c.get("value", ""))
            elif c.name == "textarea":
                spec.type = "textarea"
        if not by_name:
            continue
        names = " ".join(by_name)
        classes = li.get("class", [])
        group = ("demographic" if "surveysResponses" in names
                 else "consent" if "consent[" in names
                 else "custom" if "custom-question" in classes else "standard")
        if not label and group == "consent":      # the consent text is the checkbox's own caption
            label = next((o for f in by_name.values() for o in f.options if o), "")
        questions.append(Question(label=label, required=required, fields=list(by_name.values()), group=group))
    captcha = bool(soup.select_one("div.h-captcha, [data-sitekey], script[src*='hcaptcha']")
                   or "hcaptcha.com" in page_html)
    title = soup.title.get_text(strip=True) if soup.title else ""
    return questions, captcha, title


# ─── Fetch ───────────────────────────────────────────────────────────────────

def fetch_job(url: str, *, http: Http | None = None, tracker: Tracker | None = None,
              jd_fallback: Callable[[str], str | None] | None = None) -> Job:
    """URL -> Job. Raises UnsupportedPlatform, AlreadySubmitted or IntakeError."""
    http = http or default_http
    target = detect(url)
    page_html = None
    if target is None:
        status, page_html = http(url)
        target = resolve_embedded_greenhouse(url, page_html) if status == 200 else None
        target = target or guess_embedded_greenhouse(url, http)
        if target is None:
            raise UnsupportedPlatform(
                f"{url}: not a Greenhouse or Lever posting (v1 supports boards.greenhouse.io, "
                "job-boards.greenhouse.io, company pages with gh_jid or an embedded Greenhouse form, "
                "and jobs.lever.co). LinkedIn, Naukri and Indeed are not supported yet; apply there by hand, and for "
                "a company career page that uses another system (Workday and so on) do the same.")

    if tracker is not None and tracker.is_submitted(target.canonical_url):
        raise AlreadySubmitted(target.canonical_url)

    source_url = url if url.strip().rstrip("/") != target.canonical_url else ""
    warnings: list[str] = []
    try:
        status, body = http(target.api_url)
        if status != 200:
            raise IntakeError(f"{target.api_url} returned HTTP {status}")
        data = json.loads(body)
        job = (parse_greenhouse(data, target, source_url) if target.platform == "greenhouse"
               else parse_lever_posting(data, target, source_url=source_url))
    except (IntakeError, ValueError) as e:
        job = _html_fallback(target, url, source_url, jd_fallback, e)
        warnings.append(f"API route failed ({e}); fell back to the HTML scraper: JD only, no questions.")
        job.warnings = warnings
        return job

    if target.platform == "lever":
        status, form_html = http(target.apply_page_url)
        if status == 200:
            job.questions, job.captcha_possible, title = parse_lever_apply_form(form_html)
            job.company = lever_company_from_title(title, job.role) or job.company
            if not job.questions:
                warnings.append("Lever apply page parsed but no questions were found.")
        else:
            warnings.append(f"Lever apply page returned HTTP {status}; questions unavailable"
                            + (" - the posting may be closed." if status == 404 else "."))
    job.warnings = warnings
    return job


def _html_fallback(target: Target, url: str, source_url: str, jd_fallback, err) -> Job:
    if jd_fallback is None:
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        import tailor   # phase-1 scraper
        jd_fallback = tailor.fetch_jd
    text = jd_fallback(target.canonical_url) or (jd_fallback(url) if source_url else None)
    if not text:
        raise IntakeError(f"Could not read the job from the API or the page: {err}")
    return Job(platform=target.platform, company=target.board, role="", location="",
               canonical_url=target.canonical_url, apply_url=target.apply_page_url, jd_text=text,
               questions=[], job_id=target.job_id, board=target.board, source="html", source_url=source_url)


# ─── CLI: python -m jobbot.intake <url> [--save-fixture DIR] ──────────────────

def _main() -> None:
    import argparse
    ap = argparse.ArgumentParser(description="Fetch a Greenhouse/Lever posting and show what intake sees.")
    ap.add_argument("url")
    ap.add_argument("--save-fixture", metavar="DIR",
                    help="also save the raw responses (API JSON, apply page, company page) into DIR")
    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    saved: dict[str, str] = {}

    def recording_http(u: str) -> tuple[int, str]:
        status, text = default_http(u)
        saved[u] = text if status == 200 else ""
        return status, text

    try:
        job = fetch_job(args.url, http=recording_http)
    except IntakeError as e:
        sys.exit(f"{type(e).__name__}: {e}")
    # A page that was only fetched to look for an embed, and did not contain one (the board was found
    # by the API check instead), is not needed to replay the fixture.
    page = saved.get(args.url)
    if page and detect(args.url) is None and resolve_embedded_greenhouse(args.url, page) is None:
        saved[args.url] = ""
    print(f"{job.platform} | {job.company} | {job.role} | {job.location} | source={job.source}")
    print(f"canonical: {job.canonical_url}\napply:     {job.apply_url}")
    print(f"JD: {len(job.jd_text)} chars | captcha possible: {job.captcha_possible}")
    for q in job.questions:
        print(f"  [{q.group}] {'*' if q.required else ' '} {q.type:<11} {q.label[:70]}"
              + (f"  ({len(q.options)} options)" if q.options else ""))
    for w in job.warnings:
        print("  warning:", w)
    if args.save_fixture:
        out = Path(args.save_fixture)
        out.mkdir(parents=True, exist_ok=True)
        (out / "urls.json").write_text(json.dumps({"input": args.url, "responses": saved}, indent=2),
                                       encoding="utf-8")
        print(f"raw responses saved to {out / 'urls.json'}")


if __name__ == "__main__":
    _main()
