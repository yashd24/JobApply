"""M4 answer pipeline: Field -> Answer, with a source and a confidence for every answer.

Order (plan section M4):
  1. standard fields            -> profile, deterministic
  2. sensitive fields           -> profile only (work authorization, sponsorship, salary/CTC, notice period,
                                   start date, relocation, EEO). No profile answer -> flagged. NEVER the LLM.
  3. resume upload              -> the tailored PDF
  3b. cover letter              -> jobbot.coverletter (text or PDF), guarded
  4. everything else            -> one batched `claude -p` call with the question, the JD, the VISIBLE resume
                                   content and non-sensitive profile facts; `null`/low confidence -> flagged.
Anything we are not sure about is `flagged` (left empty, highlighted for the user), never guessed.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

import guard
from jobbot import profile as P
from jobbot.fields import Field

FILLED, FLAGGED, BLANK = "filled", "flagged", "blank"
Value = "str | list[str] | None"

SENSITIVE = {"salary_current", "salary_expected", "salary_flag", "notice_period", "serving_notice",
             "last_working_day", "start_date", "relocation", "work_auth", "sponsorship", "eeo_gender",
             "eeo_ethnicity", "eeo_veteran", "eeo_disability", "eeo_other", "history_flag", "legal_flag"}


@dataclass
class Answer:
    key: str
    label: str
    type: str
    required: bool
    category: str
    value: "str | list[str] | None"
    source: str
    confidence: str            # high | medium | low
    status: str                # filled | flagged | blank
    note: str = ""
    hints: list = field(default_factory=list)       # e.g. [state, country]: tiebreakers for a place autocomplete

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Context:
    profile: dict
    resume: dict                               # resume_data.yaml (+ contact) as a dict
    company: str = ""
    role: str = ""
    job_location: str = ""
    platform: str = "company"                  # greenhouse | lever | linkedin | naukri | indeed | company
    jd_text: str = ""
    resume_pdf: "Path | None" = None
    cover_letter: "Callable[[Field], tuple[str | None, str]] | None" = None   # field -> (value, note)
    llm: "Callable[[str], dict] | None" = None
    today: date = field(default_factory=date.today)
    origin: str = ""                           # where the job came from; defaults to the ATS platform

    @property
    def country(self) -> "str | None":
        return job_country(self.job_location)


# ─── helpers ─────────────────────────────────────────────────────────────────

_INDIA = re.compile(r"\b(india|bengaluru|bangalore|mumbai|delhi|new delhi|hyderabad|pune|chennai|gurgaon|gurugram|"
                    r"noida|kolkata|ahmedabad|kochi|jaipur|chandigarh|indore|coimbatore|thiruvananthapuram)\b", re.I)


def job_country(location: str) -> "str | None":
    """The country of the job: India (by country or Indian city name), else any country named in the location."""
    if not location:
        return None
    if _INDIA.search(location):
        return "India"
    return country_in(location)


_COUNTRIES = {"united states": "United States", "usa": "United States", "u.s.": "United States",
              "us": "United States", "united kingdom": "United Kingdom", "uk": "United Kingdom",
              "england": "United Kingdom", "scotland": "United Kingdom", "wales": "United Kingdom",
              "canada": "Canada", "india": "India", "germany": "Germany", "singapore": "Singapore",
              "australia": "Australia", "ireland": "Ireland", "netherlands": "Netherlands",
              "france": "France", "uae": "United Arab Emirates", "united arab emirates": "United Arab Emirates"}


def country_in(text: str) -> "str | None":
    t = text.lower()
    for alias, name in sorted(_COUNTRIES.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"(?<![a-z]){re.escape(alias)}(?![a-z])", t):
            return name
    return None


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().casefold())


def pick_option(options: list[str], wanted: str) -> "str | None":
    """The option equal to `wanted` (case-insensitive), else the one unique option containing it."""
    w = norm(wanted)
    for o in options:
        if norm(o) == w:
            return o
    hits = [o for o in options if w and w in norm(o)]
    return hits[0] if len(hits) == 1 else None


def yes_no_option(options: list[str], yes: bool) -> "str | None":
    pos = [o for o in options if re.match(r"^(yes|y|true)\b", norm(o))]
    neg = [o for o in options if re.match(r"^(no|n|false)\b", norm(o))]
    if len(pos) == 1 and len(neg) == 1:
        return (pos if yes else neg)[0]
    return None


def visible_resume(resume: dict) -> dict:
    """resume_data restricted to what is on the base resume (hidden/commented-out bullets excluded)."""
    import render
    base = render.default_plan(resume)
    ids = {b for s in ("experience", "projects") for e in base[s] for b in e["bullets"]} | set(base["cocurricular"])
    out = dict(resume)
    for s in ("experience", "projects"):
        out[s] = [{**e, "bullets": [b for b in e["bullets"] if b["id"] in ids]}
                  for e in resume[s] if any(b["id"] in ids for b in e["bullets"])]
    out["cocurricular"] = [c for c in resume.get("cocurricular", []) if c["id"] in ids]
    return out


def visible_text(resume: dict) -> str:
    """All wording on the visible resume: skills, roles, bullets, education."""
    vis = visible_resume(resume)
    parts = [" ".join(s["items"]) for s in vis["skills"]]
    for s in ("experience", "projects"):
        for e in vis[s]:
            parts += [e.get("company", ""), e.get("name", ""), e.get("position", ""), e.get("stack", ""),
                      e.get("dates", "")] + [b["text"] for b in e["bullets"]]
    parts += [c["text"] for c in vis["cocurricular"]]
    ed = vis.get("education", {})
    parts += [str(ed.get(k, "")) for k in ("degree", "institute", "location", "date", "cgpa")]
    return re.sub(r"\s+", " ", " ".join(parts))


# ─── classification ──────────────────────────────────────────────────────────

_RULES: list[tuple[str, re.Pattern]] = [(c, re.compile(p, re.I)) for c, p in [
    ("cover_letter", r"cover\s*letter|covering letter|motivation letter"),
    ("referral", r"\breferr"),
    ("history_flag", r"(work(ed)?|employed) (for|at) |family member|relative.*work|"
                     r"(previously|ever|before) applied|applied (to|for|at|with) (a |any |this )?(role|position|job|us|our)"),
    ("legal_flag", r"convict|criminal|felony|arrest|background check|legal proceeding|non-?compete|non-?solicit|restrictive covenant|security clearance|conflict of interest|government (official|employee)|politically exposed|sanction|"
                   r"nationality|citizenship|are you an? [\w ]{0,20}citizen|are you (an? )?[\w ]{0,20}national\b|"
                   r"working status|immigration|"
                   r"residen(cy|t of)|passport"),
    ("eeo_gender", r"\bgender\b|\bsex\b"),
    ("eeo_ethnicity", r"ethnic|\brace\b|racial"),
    ("eeo_veteran", r"veteran"),
    ("eeo_disability", r"disabilit|chronic condition"),
    ("eeo_other", r"age range|\bage\b.*range|sexual orientation|religio|transgender|marital status|caste"),
    ("pronouns", r"pronoun"),
    ("salary_expected", r"(expected|desired|expectation|anticipated).*(ctc|salary|compensation|pay)|"
                        r"(ctc|salary|compensation).*(expect|desired)"),
    ("salary_current", r"(current|present|last drawn|existing).*(ctc|salary|compensation|pay)|"
                       r"(ctc|salary|compensation).*(current|present)"),
    ("salary_flag", r"\b(ctc|salary|compensation|remuneration)\b"),
    ("serving_notice", r"serving (your |the |a )?notice"),
    ("last_working_day", r"last working day|last day (of|at) work"),
    ("notice_period", r"notice period|notice \(|period of notice"),
    ("start_date", r"earliest.*(start|join)|(start|joining|join) date|date of joining|how soon.*join|when can you (join|start)|"
                   r"availability to (join|start)|available to (join|start)"),
    ("relocation", r"relocat"),
    ("sponsorship", r"sponsor|(require|need)\b.*\b(visa|work permit)|visa (sponsorship|support)|work authori[sz]ation support"),
    ("work_auth", r"(legal|legally).*(right|authori[sz]ed|eligible|entitled)|authori[sz]ed to work|eligible to work|"
                  r"right to work|work authori[sz]ation|permission to work"),
    ("how_heard", r"how did you (hear|learn|find)|where did you (hear|find|learn)|how .* (hear|learn) about"),
    ("employment_status", r"currently employed|employment status|are you employed|presently working"),
    ("reason_leaving", r"why are you (looking|leaving|moving)|reason(s)? (for|of) (leaving|change|job change)|why .*(change|switch)"),
    ("tenth", r"\b(10th|tenth|ssc|matric|class x)\b"),
    ("twelfth", r"\b(12th|twelfth|hsc|class xii|higher secondary|intermediate)\b"),
    ("tool_years", r"(years?|yrs?)\s+(of\s+)?(\w+\s+){0,6}(with|in|using|on)\s+\w|\bat least\s+\d+\+?\s*(years?|yrs?)|\b\d+\+?\s*(years?|yrs?)"),
    ("experience_total", r"(total|overall).*(experience|years)|years of (total |overall |professional |work )?experience|"
                         r"how many years|experience \(in years\)"),
    ("linkedin", r"linked\s*in"),
    ("github", r"git\s*hub"),
    ("website", r"website|portfolio|personal site|blog|video link"),
    ("email", r"e-?mail"),
    ("phone", r"phone|mobile|contact number|whatsapp"),
    ("name_first", r"first name|given name|preferred (first )?name"),
    ("name_last", r"last name|surname|family name"),
    ("name_full", r"^(what('s| is) )?(your |candidate |applicant )?(full )?name\??$|^full name|legal name"),
    ("company", r"current (company|employer|organi[sz]ation)|present (company|employer)|employer"),
    ("title", r"current (job )?(title|role|designation|position)|job title|designation"),
    ("institution", r"college|universit|institut|school|alma mater"),
    ("degree", r"\bdegree\b|qualification|highest (level of )?education|education level"),
    ("grad_year", r"graduat.*(year|date)|year of (graduation|passing)|passing year|batch"),
    ("cgpa", r"cgpa|\bgpa\b|grade point"),
    ("percentage", r"percentage|\bmarks\b|aggregate"),
    ("country", r"^country|country of residence|country/region|country you (live|reside)"),
    ("city", r"^city|city of residence|current city|\bcity\b(?!.*state)"),
    ("location", r"location|where are you (currently )?(based|located|living)|primary residence|city and state|residing"),
    ("address", r"address|postcode|post code|zip|postal|street"),
    ("tech_stack", r"tech stack|technologies|tools|languages|frameworks|skills|stack"),
]]

_CONSENT = re.compile(r"\b(consent|agree|acknowledg|privacy (policy|notice)|terms|gdpr|confidential information|"
                      r"data (processing|protection)|contact me about|future (job )?opportunities)\b", re.I)


_IDENTITY_FIELDS = {"linkedin", "github", "website", "email", "phone", "name_first", "name_last", "name_full",
                    "company", "title", "institution", "degree", "grad_year", "cgpa", "percentage"}
_YES_NO_START = re.compile(r"^\s*(do|does|did|have|has|are|is|will|would|can|could|were|was|should)\b", re.I)
_FOLLOWUP = re.compile(r"^\s*(if|when)\s+(yes|so|applicable|you (answered|selected|chose|said))\b", re.I)


_FOLLOWUP_OPTION = re.compile(r"\s*if you (?:selected|chose|picked|answered)\s+[\"“']?([\w ]+?)[\"”']?(?:[,.]|\s+please|\s+then|\s+tell|$)", re.I)


def _is_no(value) -> bool:
    first = value[0] if isinstance(value, list) and value else value
    return isinstance(first, str) and bool(re.match(r"^\s*no\b", first, re.I))


def classify(f: Field) -> str:
    label = f.label
    if f.group == "consent":
        return "consent"
    if f.type != "file" and _FOLLOWUP.search(label):
        return "followup"
    if f.type == "file":
        named = label + " " + f.key.replace("_", " ")          # Greenhouse's label is just "Attach"; its key is "resume"
        if _RULES[0][1].search(named):
            return "cover_letter"
        return "resume" if re.search(r"resume|\bcv\b|curriculum", named, re.I) else "file_other"
    if f.type in ("checkbox", "multiselect", "select", "radio") and len(f.options) <= 2 \
            and _CONSENT.search(label + " " + " ".join(f.options)):         # the caption is often the only text
        return "consent"
    if re.search(r"\breferr", label, re.I):
        return "referral"
    if P.is_employment_status_question(label):
        return "employment_status"
    if P.is_reason_for_leaving_question(label):
        return "reason_leaving"
    is_question = bool(f.options) or bool(_YES_NO_START.match(label))
    for cat, rx in _RULES:
        if rx.search(label):
            if cat in _IDENTITY_FIELDS and is_question:
                continue              # "Do you have experience using GitHub Copilot?" is not the GitHub-profile field
            if cat in ("tool_years", "experience_total"):
                tech = any(guard.is_known_tech(w) for w in re.findall(r"[A-Za-z0-9+#.]+", label))
                if tech:
                    return "tool_years"
                if cat == "tool_years" and re.search(r"(total|overall)", label, re.I):
                    continue
            return cat
    return "llm"


# ─── deterministic handlers ──────────────────────────────────────────────────

def _ans(f: Field, cat: str, value, source: str, conf: str = "high", note: str = "") -> Answer:
    return Answer(f.key, f.label, f.type, f.required, cat, value, source, conf, FILLED, note)


def _flag(f: Field, cat: str, note: str, source: str = "none") -> Answer:
    return Answer(f.key, f.label, f.type, f.required, cat, None, source, "low", FLAGGED, note)


def _blank(f: Field, cat: str, note: str, source: str = "profile") -> Answer:
    if f.required:
        return _flag(f, cat, note + " (required)", source)
    return Answer(f.key, f.label, f.type, f.required, cat, None, source, "high", BLANK, note)


def _choice(f: Field, cat: str, wanted: "str | None", source: str, note: str = "") -> Answer:
    """Answer for a select/radio style field where `wanted` must become one of the options."""
    if wanted is None:
        return _flag(f, cat, note or "no option matches the profile answer", source)
    if f.options:
        opt = pick_option(f.options, wanted)
        if opt is None:
            return _flag(f, cat, f"profile answer '{wanted}' is not one of the options", source)
        return _ans(f, cat, [opt] if f.multiple or f.type == "multiselect" else opt, source)
    return _ans(f, cat, wanted, source)


def _bool_answer(f: Field, cat: str, flag: "bool | None", source: str, note: str = "") -> Answer:
    if flag is None:
        return _flag(f, cat, note or "no profile answer", source)
    if f.options:
        opt = yes_no_option(f.options, flag)
        if opt is None:
            return _flag(f, cat, "options are not plain Yes/No; answer it yourself", source)
        return _ans(f, cat, opt, source)
    return _ans(f, cat, "Yes" if flag else "No", source)


def _experience_text(f: Field, label: str, ctx: Context) -> Answer:
    include = not re.search(r"excluding|without|except|not (including|counting)|full[- ]?time only|"
                            r"post[- ]?(graduation|degree)", label, re.I)
    months = P.experience_months(ctx.profile, ctx.today, include_internship=include)
    scope = "full-time + internship (default)" if include else "full-time only (the question excludes internships)"
    if f.options:
        opt = P.experience_for_options(ctx.profile, f.options, ctx.today, include_internship=include)
        if opt is None:
            return _flag(f, "experience_total", "no dropdown range matches the computed experience",
                         "computed:profile.employment dates")
        return _ans(f, "experience_total", opt, f"computed:profile.employment dates, {scope}")
    years = round(months / 12, 1)
    text = f"{years:.1f}" if f.type == "number" else f"{years:.1f} years"
    return _ans(f, "experience_total", text, f"computed:profile.employment dates, {scope}",
                note=f"{months} months")


_THRESHOLD = re.compile(r"\bat least\b|\bminimum\b|\bmore than\b|\bover\b|\d+\s*\+?\s*(years?|yrs?)", re.I)


_TOOL_PHRASES = [(re.compile(p, re.I), name) for p, name in [
    (r"django[\s-]*rest[\s-]*framework|\brest framework\b", "drf"),     # not plain Django
    (r"\belk(\s+stack)?\b", "elasticsearch"),                            # Elasticsearch and ELK are one entry
]]


def _tools_in(label: str) -> list[str]:
    """Distinct canonical technologies named in a question, in order (Python and Python-based -> one)."""
    seen: list[str] = []
    for rx, name in _TOOL_PHRASES:                                       # multi-word names first
        if rx.search(label):
            if name not in seen:
                seen.append(name)
            label = rx.sub(" ", label)
    for w in re.findall(r"[A-Za-z0-9+#.]+", label):
        w = w.rstrip(".")
        if w and guard.is_known_tech(w) and guard.canon(w.lower()) not in seen:
            seen.append(guard.canon(w.lower()))
    return seen


def _tool_years(f: Field, ctx: Context) -> Answer:
    """'Years with <tool>' from the user's own skill_years figures. A tool not listed, several tools in one
    question, or a yes/no threshold question ('at least 4 yrs...') are flagged: never estimated."""
    src = "profile:skill_years"
    if _THRESHOLD.search(f.label) and f.options and yes_no_option(f.options, True):
        return _flag(f, "tool_years", "a yes/no threshold question about experience: the user answers",
                     "rule:tool-years")
    tools = _tools_in(f.label)
    if len(tools) != 1:
        return _flag(f, "tool_years", "no single tool is named, so skill_years cannot be used: the user answers"
                     if not tools else f"several tools named ({', '.join(tools)}): the user answers",
                     "rule:tool-years")
    years = P.skill_years_for(ctx.profile, tools[0])
    if years is None:
        return _flag(f, "tool_years", f"'{tools[0]}' is not in profile.yaml skill_years: the user answers", src)
    if f.options:
        opt = P.option_for_years(f.options, years)
        return _ans(f, "tool_years", opt, src, note=f"{tools[0]}: {years:g} years") if opt else \
            _flag(f, "tool_years", f"no dropdown range fits {years:g} years of {tools[0]}", src)
    text = f"{years:g}" if f.type == "number" else f"{years:g} year{'' if years == 1 else 's'}"
    return _ans(f, "tool_years", text, src, note=f"{tools[0]}: {years:g} years (the user's own figure)")


def _salary(f: Field, cat: str, ctx: Context) -> Answer:
    kind = "current" if cat == "salary_current" else "expected"
    label = f.label
    named_currency = re.search(r"\b(gbp|usd|eur|cad|aud|sgd|aed|chf|jpy)\b|[$£€]", label, re.I)
    local_currency_abroad = re.search(r"local currency", label, re.I) and ctx.country != "India"   # INR in India
    if (named_currency or local_currency_abroad) and not re.search(r"\binr\b|₹|rupee", label, re.I):
        return _flag(f, cat, "the field is in another currency; the profile holds INR only", "profile:compensation")
    fixed_part = bool(re.search(r"\b(fixed|base)\b", label, re.I))
    lpa = None
    if fixed_part:
        lpa = P.expected_fixed_ctc_lpa(ctx.profile) if kind == "expected" else P.current_fixed_ctc_lpa(ctx.profile)
        if lpa is None:
            return _flag(f, cat, "asks for the fixed/base part and profile.yaml has no "
                         + ("compensation.expected_fixed_ctc_lpa" if kind == "expected" else "fixed_lpa")
                         + ": the user answers", "profile:compensation")
    if f.options:
        return _flag(f, cat, "salary range dropdown; the user picks", "profile:compensation")
    free = f.type in ("text", "textarea")
    unit = "lpa"
    if not free:
        if re.search(r"inr|rs\.?|rupee|₹|per annum in", label, re.I) and not re.search(r"lpa|lakh", label, re.I):
            unit = "inr"
        elif not re.search(r"lpa|lakh", label, re.I):
            return _flag(f, cat, "numeric field with no stated unit (LPA or rupees)", "profile:compensation")
    action, value = P.salary_answer(ctx.profile, kind, ctx.country, required=f.required,
                                    free_text=free, unit=unit, lpa=lpa)
    src = ("profile:compensation." + ("expected_fixed_ctc_lpa" if kind == "expected" else "current_ctc.fixed_lpa")
           if fixed_part else "profile:compensation")
    if action == "fill":
        return _ans(f, cat, value, src)
    if action == "blank":
        return Answer(f.key, f.label, f.type, f.required, cat, None, src, "high", BLANK,
                      "non-Indian form and the field is optional")
    why = ("non-Indian or unknown country: salary questions are answered by the user"
           if kind == "expected" else "flagged")
    return _flag(f, cat, why, src)


def _work_auth(f: Field, cat: str, ctx: Context) -> Answer:
    label = f.label
    if cat == "sponsorship" and re.search(r"without|not (require|need)|no need", label, re.I):
        return _flag(f, cat, "negatively phrased sponsorship question; the user answers", "profile:work_authorization")
    country = country_in(label) or ctx.country
    if country is None:
        return _flag(f, cat, "job country unknown, so the work-authorization entry cannot be chosen",
                     "profile:work_authorization")
    wa = P.work_auth_for(ctx.profile, country)
    if wa is None:
        return _flag(f, cat, f"no work-authorization answer in the profile for {country}",
                     "profile:work_authorization")
    flag = wa["needs_sponsorship"] if cat == "sponsorship" else wa["authorized"]
    return _bool_answer(f, cat, flag, f"profile:work_authorization[{country}]")


def _eeo(f: Field, cat: str, ctx: Context) -> Answer:
    field_name = {"eeo_gender": "gender", "eeo_ethnicity": "ethnicity", "eeo_veteran": "veteran_status",
                  "eeo_disability": "disability_status"}[cat]
    if f.type in ("text", "textarea") and not f.options:
        return _ans(f, cat, P.eeo_answer(ctx.profile, field_name), f"profile:eeo.{field_name}")
    value = P.eeo_answer(ctx.profile, field_name, f.options)
    if value is None:
        return _flag(f, cat, "no matching option and no decline option", f"profile:eeo.{field_name}")
    multi = f.multiple or f.type == "multiselect"
    return _ans(f, cat, [value] if multi else value, f"profile:eeo.{field_name}",
                note="decline option" if re.search(r"decline|prefer not|do not wish|don'?t wish", value, re.I) else "")


def _tech_stack(f: Field, ctx: Context) -> Answer:
    vocab = {guard.canon(w) for w in guard.bank_vocabulary(visible_resume(ctx.resume))}
    chosen = []
    for o in f.options:
        words = [guard.canon(w) for w in guard.words(o)] or [guard.canon(norm(o))]
        if all(w in vocab for w in words):
            chosen.append(o)
    if not chosen:
        return _flag(f, "tech_stack", "none of the options appear on the visible resume", "resume:visible skills/bullets")
    return _ans(f, "tech_stack", chosen, "resume:visible skills/bullets", "medium",
                note=f"ticked only what the resume supports; left unticked: {[o for o in f.options if o not in chosen]}")


def _academic(f: Field, cat: str, ctx: Context) -> Answer:
    label = f.label
    if cat in ("tenth", "twelfth"):
        level = "10th" if cat == "tenth" else "12th"
        what = ("percentage" if re.search(r"percent|marks|%|score|cgpa", label, re.I)
                else "board" if re.search(r"board", label, re.I)
                else "year" if re.search(r"year|passing", label, re.I) else None)
        value = P.academic_answer(ctx.profile, level, what) if what else None
        return (_ans(f, cat, value, f"profile:education.{cat}.{what}") if value
                else _flag(f, cat, "value missing from the profile", f"profile:education.{cat}"))
    mapping = {"institution": ("institution", "institution"), "degree": ("degree", "degree"),
               "grad_year": ("graduation_year", "graduation"), "cgpa": ("cgpa", "cgpa"),
               "percentage": ("percentage", "percentage")}
    what, src = mapping[cat]
    if what == "institution" or what == "degree":
        value = ctx.profile["education"].get(what)
        value = None if value in (None, "", P.TODO) else str(value)
    else:
        value = P.academic_answer(ctx.profile, "degree", what)
    if value is None:
        return _flag(f, cat, "value missing from the profile", f"profile:education.{src}")
    return _choice(f, cat, value, f"profile:education.{src}") if f.options else _ans(f, cat, value, f"profile:education.{src}")


_CITY_ALIASES = [
    {"bangalore", "bengaluru", "bangalore urban", "bengaluru urban", "blr"},
    {"mumbai", "bombay"}, {"chennai", "madras"}, {"kolkata", "calcutta"}, {"gurgaon", "gurugram"},
    {"delhi", "new delhi"}, {"pune", "poona"}, {"kochi", "cochin"}, {"thiruvananthapuram", "trivandrum"},
    {"mysore", "mysuru"}, {"vadodara", "baroda"},
]


def _city_names(city: str) -> set[str]:
    c = norm(city)
    return next((a for a in _CITY_ALIASES if c in a), {c})


def _city_option(options: list[str], loc: dict) -> "str | None":
    """The option naming the profile's city (Bangalore = Bengaluru). One unambiguous hit, else None."""
    names = _city_names(str(loc.get("city", "")))
    def names_city(o: str) -> bool:
        return any(re.search(rf"(?<![a-z]){re.escape(n)}(?![a-z])", norm(o)) for n in names)
    hits = [o for o in options if names_city(o)]
    if len(hits) > 1:                                        # 'Bangalore' beats 'Bangalore (hybrid)' / 'Other'
        exact = [o for o in hits if norm(o) in names]
        hits = exact or [o for o in hits if norm(str(loc.get("state", ""))) in norm(o)] or hits
    return hits[0] if len(hits) == 1 else None


_DIAL = re.compile(r"\s*\(?\+\d[\d\s-]*\)?\s*$")
_COUNTRY_ALIASES = [{"india", "bharat", "ind", "in"}, {"united states", "usa", "us", "united states of america"},
                    {"united kingdom", "uk", "gb", "great britain"}]


def _country_option(options: list[str], country: str) -> "str | None":
    """The option naming the country, ignoring dial codes ('India +91' = 'India'). 'India' must never match
    'British Indian Ocean Territory', so the comparison is exact, not 'contains'."""
    c = norm(country)
    names = next((a for a in _COUNTRY_ALIASES if c in a), {c})
    hits = [o for o in options if norm(_DIAL.sub("", o)) in names]
    return hits[0] if len(hits) == 1 else None


def _location(f: Field, cat: str, ctx: Context) -> Answer:
    loc = ctx.profile["personal"]["current_location"]
    if f.options and cat == "country":
        hit = _country_option(f.options, str(loc.get("country", "")))
        src = "profile:personal.current_location"
        return _flag(f, cat, f"no option names {loc.get('country')}", src) if hit is None else \
            _ans(f, cat, [hit] if f.multiple or f.type == "multiselect" else hit, src, note="dial code ignored")
    if f.options and cat in ("location", "city"):
        hit = _city_option(f.options, loc)
        src = "profile:personal.current_location"
        if hit is None:
            return _flag(f, cat, f"none of the options names {loc.get('city')} (or its other spelling)", src)
        return _ans(f, cat, [hit] if f.multiple or f.type == "multiselect" else hit, src,
                    note="matched by city name (Bangalore = Bengaluru)")
    if cat == "city":
        value = loc["city"]
    elif cat == "country":
        value = loc["country"]
    else:
        value = ", ".join(str(loc[k]) for k in ("city", "state", "country") if loc.get(k))
    if f.options:
        return _choice(f, cat, value, "profile:personal.current_location")
    ans = _ans(f, cat, value, "profile:personal.current_location")
    if cat in ("city", "location"):          # an autocomplete must not pick "Bangalore, Oregon" for "Bangalore"
        ans.hints = [str(loc[k]) for k in ("state", "country") if loc.get(k)]
    return ans


_PRIVACY_ACK = re.compile(r"privacy (policy|notice|statement)|data (processing|protection|privacy)|gdpr|"
                          r"personal (data|information)|candidate privacy|processing of (my )?(personal )?data|"
                          r"processing (my )?(responses|personal|data|information)|collecting, storing", re.I)
_NOT_PRIVACY = re.compile(r"market|newsletter|future|alert|talent (community|pool|network)|contact me|updates?\b|"
                          r"promot|record|confidential|non-?compete|terms of (employment|service)|background|"
                          r"share (my|your)|third part", re.I)
_AFFIRM = re.compile(r"^(yes|i agree|agree|i accept|accept|acknowledge|i acknowledge|confirm|i confirm|i consent|"
                     r"i have read|i understand)\b", re.I)


def _consent(f: Field) -> Answer:
    """Option A (decided by the user): tick a REQUIRED privacy / data-processing acknowledgement, because the form
    cannot be submitted without it. Never tick marketing, 'contact me about future roles', confidentiality,
    recording or any other optional or non-privacy consent: those are flagged for the user."""
    text = f.label + " " + " ".join(f.options)
    if not f.required:
        return _flag(f, "consent", "optional consent box: never ticked silently", "rule:consent (Option A)")
    if _NOT_PRIVACY.search(text) or not _PRIVACY_ACK.search(text):
        return _flag(f, "consent", "required, but not a plain privacy / data-processing acknowledgement: the user "
                     "decides", "rule:consent (Option A)")
    if not f.options:
        return _flag(f, "consent", "no option text to tick", "rule:consent (Option A)")
    opt = f.options[0] if len(f.options) == 1 else next((o for o in f.options if _AFFIRM.match(o.strip())), None)
    if opt is None:
        return _flag(f, "consent", "no clear 'I agree / acknowledge' option", "rule:consent (Option A)")
    multi = f.multiple or f.type in ("multiselect", "checkbox")
    return _ans(f, "consent", [opt] if multi else opt, "rule:consent (Option A: required privacy acknowledgement)",
                "medium", note="ticked because the form requires it; marketing / optional consents are never ticked")


def _handle(f: Field, cat: str, ctx: Context) -> "Answer | None":
    """Deterministic answer, or None when the field must go to the LLM."""
    p = ctx.profile
    per = p["personal"]
    if cat == "resume":
        if ctx.resume_pdf is None:
            return _flag(f, cat, "no tailored resume PDF available")
        return _ans(f, cat, str(ctx.resume_pdf), "tailor:resume pdf")
    if cat == "cover_letter":
        if ctx.cover_letter is None:
            return _flag(f, cat, "cover-letter generation is not configured")
        value, note = ctx.cover_letter(f)
        if value:
            return _ans(f, cat, value, "coverletter:generated (guarded)", "medium", note)
        if f.required:
            return _flag(f, cat, f"required cover letter failed its checks twice: switch to assist ({note})",
                         "coverletter")
        return Answer(f.key, f.label, f.type, f.required, cat, None, "coverletter", "high", BLANK,
                      f"optional; left empty: {note}")
    if cat == "file_other":
        return _flag(f, cat, "an upload other than the resume/cover letter; the user provides it")
    if cat == "consent":
        return _consent(f)
    if cat == "referral":
        return _flag(f, cat, "only the user knows whether he was referred", "rule:referral")
    if cat == "legal_flag":
        return _flag(f, cat, "legal / criminal / conflict question: only the user answers", "rule:legal")
    if cat == "history_flag":
        return _flag(f, cat, "facts about past employment/relatives at this company; the user answers", "rule:history")
    if cat == "employment_status":
        return _flag(f, cat, "employment-status question: the user answers", "rule:employment-status")
    if cat == "reason_leaving":
        return _flag(f, cat, "'why are you leaving' questions are always the user's", "rule:reason-for-leaving")
    if cat == "pronouns":
        return _blank(f, cat, "no pronouns stored in the profile", "profile")
    if cat == "eeo_other":
        return _flag(f, cat, "no profile answer for this demographic question", "profile:eeo")
    if cat in ("eeo_gender", "eeo_ethnicity", "eeo_veteran", "eeo_disability"):
        return _eeo(f, cat, ctx)
    if cat in ("salary_current", "salary_expected"):
        return _salary(f, cat, ctx)
    if cat == "salary_flag":
        return _flag(f, cat, "salary question that is neither clearly current nor expected", "profile:compensation")
    if cat == "serving_notice":
        return _bool_answer(f, cat, {"Yes": True, "No": False}.get(P.serving_notice_answer(p, ctx.today)),
                            "profile:employment.serving_notice", "notice state unknown after the last working day")
    if cat == "last_working_day":
        v = P.last_working_day_answer(p, ctx.today, iso=f.type == "date")
        return _ans(f, cat, v, "profile:employment.last_working_day") if v else \
            _flag(f, cat, "not serving notice / last working day passed", "profile:employment")
    if cat == "notice_period":
        v = P.notice_period_answer(p, ctx.today, options=f.options or None)
        return _ans(f, cat, v, "profile:employment.notice_period_days / last_working_day") if v else \
            _flag(f, cat, "no notice-period answer fits (or notice already over)", "profile:employment")
    if cat == "start_date":
        v = P.earliest_start_answer(p, ctx.today, iso=f.type == "date")
        if f.options:
            return _choice(f, cat, v if v != "Immediately" else "Immediate", "profile:employment.earliest_start_date")
        if f.type == "date" and v == "Immediately":
            return _flag(f, cat, "date field but the earliest start date has passed ('Immediately')",
                         "profile:employment.earliest_start_date")
        return _ans(f, cat, v, "profile:employment.earliest_start_date")
    if cat == "relocation":
        return _bool_answer(f, cat, p["preferences"]["willing_to_relocate"], "profile:preferences.willing_to_relocate")
    if cat in ("work_auth", "sponsorship"):
        return _work_auth(f, cat, ctx)
    if cat == "how_heard":
        v = P.how_did_you_hear_answer(ctx.origin or ctx.platform, f.options or None, f.label)
        if v is None:
            return _flag(f, cat, "no matching source option (or it asks for a referrer)", "rule:how-did-you-hear")
        multi = f.multiple or f.type == "multiselect"
        return _ans(f, cat, [v] if multi and f.options else v, "rule:how-did-you-hear (platform the job came from)")
    if cat == "experience_total":
        return _experience_text(f, f.label, ctx)
    if cat == "tool_years":
        return _tool_years(f, ctx)
    if cat == "tech_stack":
        if f.type in ("checkbox", "multiselect") and f.options:
            return _tech_stack(f, ctx)
        return None
    # standard profile fields
    if cat == "name_full":
        return _ans(f, cat, f"{per['first_name']} {per['last_name']}", "profile:personal.name")
    if cat == "name_first":
        return _ans(f, cat, per["first_name"], "profile:personal.first_name")
    if cat == "name_last":
        return _ans(f, cat, per["last_name"], "profile:personal.last_name")
    if cat == "email":
        return _ans(f, cat, per["email"], "profile:personal.email")
    if cat == "phone":
        return _ans(f, cat, per["phone"], "profile:personal.phone")
    if cat == "linkedin":
        return _ans(f, cat, per["linkedin"], "profile:personal.linkedin")
    if cat == "github":
        return _ans(f, cat, per["github"], "profile:personal.github")
    if cat == "website":
        v = per.get("portfolio")
        if re.search(r"video", f.label, re.I) or v in (None, "", P.TODO):
            return _blank(f, cat, "nothing in the profile for this link", "profile:personal.portfolio")
        return _ans(f, cat, v, "profile:personal.portfolio")
    if cat == "company":
        return _ans(f, cat, P.current_company_answer(p), "profile:employment.current_company")
    if cat == "title":
        return _ans(f, cat, p["employment"]["current_title"], "profile:employment.current_title")
    if cat in ("institution", "degree", "grad_year", "cgpa", "percentage", "tenth", "twelfth"):
        return _academic(f, cat, ctx)
    if cat in ("location", "city", "country"):
        return _location(f, cat, ctx)
    if cat == "address":
        return _flag(f, cat, "no street address / postcode in the profile", "profile:personal")
    return None


# ─── the LLM step (non-sensitive questions only) ─────────────────────────────

def llm_profile_facts(profile: dict) -> dict:
    """The ONLY profile facts the LLM may see: no contact details, salary, notice, work authorization,
    relocation, EEO, marks or dates of leaving."""
    per, emp, edu = profile["personal"], profile["employment"], profile["education"]
    return {"name": f"{per['first_name']} {per['last_name']}", "current_company": emp["current_company"],
            "current_title": emp["current_title"], "degree": edu["degree"], "institution": edu["institution"]}


def build_llm_prompt(fields: list[Field], ctx: Context) -> str:
    qs = [{"key": f.key, "question": f.label, "type": f.type, "options": f.options or None,
           "required": f.required} for f in fields]
    return f"""You are answering job-application questions for a candidate. Do not use any tools. Reply with ONE
JSON object only, no prose, no code fences.

JOB: {ctx.role} at {ctx.company}

JOB DESCRIPTION:
<<<
{ctx.jd_text.strip()[:6000]}
>>>

THE CANDIDATE'S RESUME (this is everything you may rely on):
{visible_text(ctx.resume)}

CANDIDATE FACTS: {json.dumps(llm_profile_facts(ctx.profile), ensure_ascii=False)}

QUESTIONS:
{json.dumps(qs, ensure_ascii=False, indent=1)}

RULES
1. Answer ONLY from the resume and candidate facts above. If a question needs a fact that is not there, answer null.
   Never invent employers, tools, numbers, dates or achievements.
2. For select/radio questions, "answer" must be exactly one of the options; for checkbox groups a list of options.
3. Free-text answers: plain, specific, first person, 1-4 short sentences. Any number you write must come from the resume.
   Write any date as "13 Oct 2026" (day, three-letter month, year).
4. Give "confidence": "high", "medium" or "low". Use "low" if you are unsure.

OUTPUT JSON: {{"answers": {{"<key>": {{"answer": "..." | ["..."] | null, "confidence": "high|medium|low",
"based_on": ["resume bullet or fact used"]}}}}}}"""


def _numbers_ok(text: str, allowed: str) -> bool:
    return set(guard.numbers(text)) <= set(guard.numbers(allowed))


def apply_llm(fields: list[Field], ctx: Context) -> list[Answer]:
    if not fields:
        return []
    if ctx.llm is None:
        return [_flag(f, "llm", "no LLM configured") for f in fields]
    try:
        reply = ctx.llm(build_llm_prompt(fields, ctx)).get("answers", {})
    except Exception as e:                      # Claude unavailable / bad JSON: flag everything, never guess
        return [_flag(f, "llm", f"LLM call failed: {type(e).__name__}", "llm") for f in fields]
    allowed = visible_text(ctx.resume) + " " + json.dumps(llm_profile_facts(ctx.profile))
    out: list[Answer] = []
    for f in fields:
        r = reply.get(f.key) or {}
        ans, conf = r.get("answer"), str(r.get("confidence", "low")).lower()
        if ans in (None, "", []) or conf == "low":
            out.append(_flag(f, "llm", "the model could not answer from the resume" if ans in (None, "", [])
                             else "low confidence", "llm"))
            continue
        if f.options:
            opts = ans if isinstance(ans, list) else [ans]
            chosen = [pick_option(f.options, str(o)) for o in opts]
            if None in chosen or (len(chosen) > 1 and not (f.multiple or f.type == "multiselect")):
                out.append(_flag(f, "llm", f"answer is not one of the options: {ans!r}", "llm"))
                continue
            value = chosen if (f.multiple or f.type == "multiselect" or isinstance(ans, list)) else chosen[0]
            yes_no = bool(yes_no_option(f.options, True) and yes_no_option(f.options, False))
            if yes_no and conf != "high":
                # decided by the user: a factual yes/no claim must be high confidence, or the user answers
                out.append(_flag(f, "llm", f"factual yes/no answer with {conf} confidence: the user answers "
                                 f"(the model said {value if isinstance(value, str) else value[0]!r})", "llm"))
                continue
            if yes_no and not r.get("based_on"):
                out.append(_flag(f, "llm", "factual yes/no answer with no resume evidence cited: the user answers", "llm"))
                continue
        else:
            value = " ".join(str(ans).split())
            if not _numbers_ok(value, allowed + " " + f.label):
                out.append(_flag(f, "llm", "answer contains a number that is not on the resume", "llm"))
                continue
        out.append(_ans(f, "llm", value, "llm (from the visible resume + JD)", conf,
                        note="; ".join(map(str, r.get("based_on", [])))[:300]))
    return out


# ─── entry point ─────────────────────────────────────────────────────────────

def answer_fields(fields: list[Field], ctx: Context) -> list[Answer]:
    """One Answer per field, in form order. Sensitive categories never reach the LLM."""
    results: dict[str, Answer] = {}
    pending: list[Field] = []
    cats: dict[str, str] = {}
    followups: list[Field] = []
    for f in fields:
        cat = classify(f)
        cats[f.key] = cat
        if cat == "followup":                          # resolved below, from the answer to the question before it
            followups.append(f)
            continue
        ans = None if cat == "llm" else _handle(f, cat, ctx)
        if ans is None:
            if cat in SENSITIVE:
                ans = _flag(f, cat, "sensitive question with no profile rule")
            elif f.type == "file":
                ans = _flag(f, cat, "unrecognised upload")
            else:
                pending.append(f)
                continue
        results[f.key] = ans
    for ans in apply_llm(pending, ctx):
        results[ans.key] = ans
    index = {f.key: i for i, f in enumerate(fields)}
    for f in followups:                                # "If yes, please explain...": only matters after a Yes
        prev = results.get(fields[index[f.key] - 1].key) if index[f.key] else None
        named = _FOLLOWUP_OPTION.match(f.label)                 # "If you selected Other, please tell us..."
        prev_values = ([prev.value] if isinstance(prev.value, str) else list(prev.value or [])) if prev else []
        if named and prev is not None and prev.status == FILLED and prev_values and \
                not any(norm(named.group(1)) in norm(v) for v in prev_values):
            results[f.key] = Answer(f.key, f.label, f.type, f.required, "followup", None, "rule:follow-up", "high", BLANK,
                                    f"does not apply: the previous answer is not '{named.group(1).strip()}'")
        elif prev is not None and prev.status == FILLED and _is_no(prev.value):
            results[f.key] = Answer(f.key, f.label, f.type, f.required, "followup", None, "rule:follow-up", "high",
                                    BLANK, "follow-up to a question answered 'No', so it does not apply")
        else:
            results[f.key] = _flag(f, "followup", "follow-up to a question that was not a plain 'No': the user answers",
                                   "rule:follow-up")
    final = []
    for f in fields:
        a = results[f.key]
        if a.status == FILLED and (a.value in (None, "", [])):
            a = _flag(f, a.category, "empty answer", a.source)
        elif a.status == FILLED and f.options and f.type in ("select", "multiselect", "radio", "checkbox"):
            # safety net: a choice field is never filled with something that is not one of its options
            offered = {norm(o) for o in f.options}
            vals = a.value if isinstance(a.value, list) else [a.value]
            if any(norm(str(v)) not in offered for v in vals):
                a = _flag(f, a.category, f"the answer {a.value!r} is not one of this question's options: the user "
                          "answers", a.source)
        final.append(a)
    return final


def to_json(answers: list[Answer]) -> dict:
    flagged = [a for a in answers if a.status == FLAGGED]
    return {"summary": {"total": len(answers), "filled": sum(a.status == FILLED for a in answers),
                        "flagged": len(flagged), "blank": sum(a.status == BLANK for a in answers),
                        "required_flagged": [a.label for a in flagged if a.required],
                        "sent_to_llm": [a.label for a in answers if a.category == "llm"]},
            "answers": [a.to_dict() for a in answers]}
