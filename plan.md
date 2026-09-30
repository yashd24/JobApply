# Job Application Bot — Build Plan (v1)

This plan is for Claude Code. Read it fully before writing any code.

## 0. Working agreement

- **Ask, don't assume.** If anything in this plan is ambiguous, or a real-world page behaves differently than described, stop and ask the user before choosing. This applies especially to anything that is submitted to an employer.
- **Build milestone by milestone.** Finish a milestone, run its acceptance checks, show the user the result, and commit before starting the next one.
- **Never weaken the honesty rules** in section 3 to make something "work." If a rule blocks progress, report it and ask.
- **Never submit a real application during development** unless the user explicitly approves that specific job. Use `--dry-run` everywhere else.
- **Target platform is Windows** (the user's laptop). Use `pathlib`, avoid shell-specific commands, test paths with spaces, and read/write all files as UTF-8.
- Keep secrets (cookies, browser profile, tokens, `profile.yaml`) out of git.

## 1. Goal

The user (a backend SDE with about 1-2 years of experience) wants to automate job applications:

1. Take a job posting.
2. Tailor his LaTeX resume to the job description.
3. Fill in and submit the application automatically, with a per-platform toggle between:
   - **auto**: fill and submit without him,
   - **assist**: fill everything, then stop so he can review and click submit himself.
4. Track every application.

He is targeting jobs anywhere in the world, on LinkedIn Easy Apply, company career pages (Greenhouse / Lever / Workday), Naukri, and Indeed. The LLM is **Claude Code in headless mode** (`claude -p`), which runs on his Claude Pro subscription. There is no Anthropic API key.

## 2. Current state (phase 1 — already built)

The project folder already contains a working resume tailor:

```
resume-tailor/tailor/
├── tailor.py            # CLI: JD -> tailored one-page PDF + report
├── guard.py             # validates Claude's plan; enforces honesty rules
├── render.py            # renders resume.tex from the content bank
├── resume_data.yaml     # single source of truth: all resume content, 33 bullets with ids
├── template/            # modern-deedy LaTeX class, .sty files, Lato/Raleway fonts
├── requirements.txt
└── README.md
```

How it works: `tailor.py` builds a prompt from `resume_data.yaml` and the JD, pipes it to `claude -p --output-format json` through stdin, parses the `result` field, runs the guard, renders the `.tex`, compiles with XeLaTeX (or Tectonic), and makes the PDF exactly one page. Output goes to `output/<date>_<company>_<role>/` with `<Name>_Resume.pdf`, `report.md`, `result.json`, `plan.json`, `claude_raw.json`, `jd.txt`, `resume.tex`.

**Tailoring mode: locked selection (default).** The tailored resume contains exactly the entries and bullets of `render.default_plan()` (everything marked `default: true` in `resume_data.yaml`) — the same sections and bullets as the base resume. Bullets and entries that are hidden (`default: false`, the previously commented-out ones) are never shown, never sent to Claude, and their wording never counts as backing for an edit. Claude may only:
- (a) reorder the bullets within each role/project, most relevant to the JD first;
- (b) reorder the items within each skills line;
- (c) make keyword edits to existing bullets, under `guard.check_edit_locked`: LaTeX safety, the number-lock, max 4 words removed and max 8 added are unchanged; every added word must appear in the JD; if it looks like a technology (digits or `/ . + #` in it, or capitalised mid-sentence in the JD) it must also appear in the visible resume (skills, visible bullets, visible project stacks/titles); plain lowercase descriptive JD words need no backing; up to 3 added words per bullet may be absent from the JD but only grammar linking words. (`--allow-selection` keeps the stricter original `check_edit`.)

The prompt sends only the base bullets. `guard.validate_locked_plan()` accepts a bullet order only if it is an exact permutation of that entry's base bullets; otherwise it keeps the base order and logs a warning (skills work the same way). Added, dropped or unknown entries/bullets are reverted to base.

**One page in locked mode:** bullets are never removed. If the page overflows, `fit_locked()` undoes keyword edits one at a time, largest addition first; if no edits are left it resets the order to the base order; the base resume fits, so this terminates (and it raises an error rather than removing content if it ever didn't). `report.md` has an "Order changes" section (per role: old order → new order), the keyword edits before/after (with reverted ones marked), and the gaps.

The old behaviour (Claude adds/drops bullets and entries from the whole bank, `guard.validate_plan()` + `fit_to_one_page()` trimming) exists only behind `--allow-selection` (`tailor_job(..., allow_selection=True)`), which is off by default.

Verified in a Linux sandbox: base render matches the original PDF; guard rejects changed numbers, unbacked keywords, skill additions, conflicting bullets and unsafe LaTeX; page fitting works.

**Not yet verified:** the real `claude -p` call (tested only with a stand-in returning the same JSON envelope) and the Windows toolchain (MiKTeX/XeLaTeX, font paths).

**Do not rewrite phase 1.** Extend it. Importable functions you should reuse rather than duplicate: `read_jd`/`fetch_jd`, `build_prompt`, `call_claude`, `parse_json_reply`, `compile_pdf`, `fit_to_one_page`. Refactor `tailor.py` into an importable `tailor_job(company, role, jd_text, jd_url) -> result dict` function so the applier can call it; keep the CLI working.

## 3. Hard rules (apply to every milestone)

1. **No fabrication.** Nothing is written into a resume or form that isn't backed by `resume_data.yaml` or `profile.yaml`. The resume guard rules in `guard.py` stay as they are unless the user says otherwise. **Resume selection is locked:** the tailored resume uses exactly the base resume's sections and bullets (`render.default_plan()`); tailoring may only reorder bullets and skills and make guarded keyword edits, and hidden/commented-out bullets are never un-hidden. Adding or dropping content requires the explicit `--allow-selection` flag.
2. **Sensitive answers are never generated by the LLM.** Work authorization, visa sponsorship, salary/CTC, notice period, relocation, EEO/diversity questions, criminal record, and anything legal are answered **only** from `profile.yaml`. If the profile has no answer for that country/case, the field is flagged, not guessed.
3. **"Submitted" means verified.** An application is marked `submitted` only after a confirmation signal is detected (see M6). Clicking a submit button is not success.
4. **CAPTCHAs and 2FA are never bypassed.** If one appears, pause and hand control to the user (assist behaviour), even in auto mode.
5. **Default to assist on LinkedIn and Naukri** (account-ban risk; automation is against their terms). Auto mode is intended mainly for Greenhouse and Lever. The user can override per platform in config.
6. **Every run is auditable**: screenshots before submit and after confirmation, the answers used, and which source each answer came from.

## 4. v1 scope

**In scope:** Greenhouse and Lever, one job URL at a time, end to end.

```
python apply.py --url <greenhouse-or-lever-job-url> [--mode auto|assist] [--dry-run]
```

1. Detect the ATS and fetch the JD and form questions.
2. Tailor the resume (phase 1).
3. Open the application form in a real browser.
4. Fill standard fields from the profile, upload the tailored PDF.
5. Answer custom questions (profile mapping first, then LLM for non-sensitive ones).
6. Auto: submit if every required field is filled with nothing flagged; otherwise fall back to assist.
   Assist: highlight flagged fields, wait for the user to submit in the browser.
7. Verify the submission.
8. Log it locally and to Google Sheets.

**Out of scope for v1** (see roadmap, section 8): LinkedIn, Naukri, Workday, Indeed, SmartRecruiters, job discovery/scraping searches, batch queues, email monitoring. (Cover letters are **in** scope: generated inside M4, see "Cover letters" there.)

## 5. Target layout

```
resume-tailor/tailor/            # (rename to jobbot/ only if the user agrees)
├── apply.py                 # v1 CLI entry point
├── tailor.py / guard.py / render.py / resume_data.yaml / template/   # phase 1
├── profile.yaml             # user's form answers (gitignored); profile.example.yaml committed
├── config.yaml              # modes per platform, paths, sheet id (gitignored); config.example.yaml committed
├── jobbot/
│   ├── profile.py           # load + validate profile, per-country lookups
│   ├── intake.py            # URL -> platform, company, job id, JD text, form questions
│   ├── browser.py           # Playwright persistent context, screenshots, pause/resume
│   ├── fields.py            # label discovery + standard field mapping
│   ├── answers.py           # question -> answer (profile rules, then LLM), with source + confidence
│   ├── coverletter.py       # cover letter text + one-page PDF, guard, regenerate-once (M4)
│   ├── ats/
│   │   ├── base.py          # ATS interface
│   │   ├── greenhouse.py
│   │   └── lever.py
│   ├── verify.py            # confirmation detection
│   └── tracker.py           # SQLite store + Google Sheets sync
├── tests/
│   ├── fixtures/            # saved HTML/JSON of real Greenhouse & Lever forms
│   └── test_*.py
└── output/                  # per-job folders (gitignored)
```

## 6. Milestones

### M0 — Verify phase 1 on Windows

- Walk the user through: install Python, MiKTeX (on-the-fly package install ON), Claude Code (logged in), `pip install -r requirements.txt`.
- Run `python tailor.py --base` and have the user confirm the PDF matches his current resume.
- Run one real tailoring with a JD the user provides. Show him `report.md`.
- Fix any Windows issues (font paths, encoding, `claude` not resolving; on Windows it may be `claude.cmd`, and `shutil.which` should find it).
- Tune the prompt only if the real output shows problems; show the user before/after.

**Done when:** base PDF matches, and one real tailored PDF + report is produced and approved by the user.

### M1 — Profile

Create `profile.example.yaml` (committed) and help the user fill `profile.yaml`. Ask the user for every value; **never invent them**. Suggested structure:

```yaml
# Outline only, all values are placeholders. The source of truth is profile.example.yaml
# (committed, every value TODO); the real profile.yaml is gitignored. Never copy real values into plan.md.
personal:      {first_name, last_name, email, phone, current_location{city,state,country}, linkedin, github, portfolio}
employment:    {current_company, current_title, fulltime_start, internship_start/end, serving_notice,
                notice_period_days, last_working_day, earliest_start_date}   # experience is computed at fill time
compensation:  {current_ctc{fixed_lpa, variable_lpa, stated_total_lpa, report_as}, expected_ctc_inr_lpa}
work_authorization:  {<Country>: {authorized, needs_sponsorship}, default_other: {...}}
preferences:   {willing_to_relocate, work_modes}
education:     {degree, institution, institution_location, affiliation, start_year, graduation, cgpa, percentage, twelfth{...}, tenth{...}}
eeo:           {gender, ethnicity, ethnicity_broad, veteran_status, disability_status, if_no_matching_option}
custom_answers: {}
```

`profile.py` must: validate required keys, refuse to run if any `TODO` remains in a field a form needs, and expose `work_auth_for(country)` which returns `None` (→ flag) when unknown.

**Done when:** the user has reviewed and approved `profile.yaml`; tests cover validation and per-country lookup.

### M2 — Intake (URL → job data)

- Detect platform from the URL: `boards.greenhouse.io`, `job-boards.greenhouse.io`, company sites with a `gh_jid` query parameter or an embedded Greenhouse iframe, and `jobs.lever.co`.
- Prefer the public job-board APIs over HTML scraping where available (verify current endpoints against Greenhouse's and Lever's own documentation before relying on them):
  - Greenhouse Job Board API (job by board token + id, with the application questions included).
  - Lever Postings API (posting by company + id). Verified: it does **not** expose custom application questions, so Lever questions are parsed from the server-rendered `/apply` page (`li.application-question`); the live form still has to be discovered in the browser in M4/M5 (hCaptcha is flagged as `captcha_possible`).
  - Verified endpoints (2026-09): Greenhouse `GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}?questions=true` (also returns `location_questions` and `demographic_questions`); Lever `GET https://api.lever.co/v0/postings/{site}/{id}` (EU: `api.eu.lever.co`). The Greenhouse EU API host (`boards-api.eu.greenhouse.io`) is assumed, not verified.
- Fall back to the HTML scraper in `tailor.fetch_jd` if the API route fails.
- Output a `Job` object: platform, company, role, location, canonical URL, apply URL, JD text, list of form questions (label, type, required, options) when available.
- Dedupe: if the canonical URL already exists in the tracker as `submitted`, stop and tell the user.

**Done when:** tests pass on saved fixtures for at least 3 Greenhouse and 3 Lever postings (the user supplies real URLs), including one embedded on a company career site.

### M3 — Browser session

- Playwright, Chromium, **headed**, **persistent context** (a dedicated user-data dir under the project, gitignored) so logins and cookies survive between runs.
- Helpers: `goto`, `screenshot(name)`, `pause_for_user(message)` (prints the reason, waits for Enter in the terminal while the user works in the browser), and a small human-like delay helper.
- No stealth hacks, no fingerprint spoofing, no CAPTCHA solving.

**Done when:** the browser opens a Greenhouse and a Lever posting, screenshots land in the job folder, and pause/resume works on Windows.

### M4 — Form filling and question answering

**Field discovery (`fields.py`):** for every input/select/textarea/radio group/checkbox/file input, resolve its human label (label[for], aria-label, aria-labelledby, fieldset legend, nearest text), its type, whether it's required, and its options. Do not rely on fragile CSS class names; prefer labels and accessible names.

**Answer pipeline (`answers.py`),** in this order, recording `source` and `confidence` for each answer:

1. **Standard fields** (name, email, phone, location, LinkedIn, GitHub, website, current company/title) → profile, deterministic mapping.
2. **Sensitive fields** (rule 3.2: work authorization, sponsorship, salary/CTC, notice period, start date, relocation, EEO) → profile only, matched by keyword rules plus the job's country. No profile answer → `flagged`.
3. **Resume upload** → the tailored PDF from phase 1.
3b. **Cover letter fields** (text box or file upload, required or optional) → generated per "Cover letters" below.
4. **Everything else** (e.g. "Why do you want to work here?", "Describe a project using Django", years with a specific tool) → one batched `claude -p` call with: the questions, the JD, `resume_data.yaml`, and `profile.yaml`. Claude must return JSON per question: `{answer, confidence: high|medium|low, based_on: [...]}`, and must return `null` if the answer would require facts not in the provided data. Low confidence or null → `flagged`.
   - For select/radio questions the answer must be one of the given options, validated in code.
   - Years-of-experience questions for a specific tool: answer only if derivable from dated experience in `resume_data.yaml`; otherwise flag.
5. Save every answer with its source to `answers.json` in the job folder.

**Cover letters** (decided by the user; implemented in M4 as `jobbot/coverletter.py`):
- **When:** fill every cover-letter field, required **and** optional. Text box → the letter as text. File upload → a one-page PDF rendered with the same LaTeX fonts/template as the resume (Lato/Raleway via the existing XeLaTeX pipeline).
- **Content:** 150–250 words, three short paragraphs. Connect 1–2 specific JD requirements to 1–2 concrete things from the **visible** resume bullets (the tailored resume's bullets), using his real numbers. Mention the company only with facts that appear in the JD. No invented facts. Never touches sensitive topics (salary, visa/work authorization, notice period, relocation, EEO): those come only from `profile.yaml`.
- **Style:** plain and specific. Stock phrases are banned ("I am writing to express", "passionate", "thrilled", "leverage", "dynamic team", "great fit" and similar); the banned list lives in an editable config file (`cover_letter.yaml`, also holding the word range), and the prompt includes a writing sample from the user (`writing_sample.txt`, gitignored; **the user will supply it**) to match his tone.
- **Guard (code, not the LLM):** every number in the letter must exist in the visible resume content (`resume_data.yaml` bullets/education); no banned phrase; word count in range; three paragraphs. If a check fails → regenerate once. If it still fails: an **optional** field is left empty; a **required** field switches that application to assist mode (the user writes it).
- **Review:** there is no separate cover-letter review step. In assist mode the user sees the letter in the form before clicking submit; in auto mode it is submitted as is (guard is the safeguard). Every letter (text and PDF) is saved in the job folder, and its guard result is recorded in `answers.json` (rule 3.6, auditable).

Notes from real forms (saved M2 fixtures):
- A question that states its own scope overrides the profile default. Gushwork asks "total years of experience **(excluding internship)**" → use full-time only (`profile.experience_years(..., include_internship=False)`), not the default full-time + internship.
- Other Gushwork/Portcast questions to handle: "Current/Expected CTC" (free text, India → `salary_answer`), "Notice Period", "From which college did you graduate?" (education.institution), "Were you referred by…?" (flag), "Expected **Fixed/Base** Salary" (profile stores a total; flag rather than guess the fixed part), "at least N yrs … experience" yes/no questions (answer only if derivable from dated experience, otherwise flag), tech-stack checkboxes (only tick what `resume_data.yaml` supports).
- Consent / acknowledgement checkboxes (Lever `consent[...]`, e.g. "Yes, X can contact me about future roles"; Greenhouse single-option "Privacy Policy" / "Confidential Information" multiselects) are never ticked silently: tick only what the user has explicitly approved, otherwise flag. Intake gives Lever consent boxes `group="consent"`.
- Greenhouse "How did you learn about this job?" can be a multiselect, and Greenhouse EEO (`demographic_questions`) mixes single and multi selects; `profile.eeo_answer` / `how_did_you_hear_answer` take option lists.
- Lever exposes hCaptcha on the apply page (`captcha_possible`) → assist behaviour.

**Done when:** on saved fixtures, every field is either filled or flagged, never silently skipped; a test proves sensitive fields never reach the LLM.

### M5 — ATS adapters and modes

Implement `ats/greenhouse.py` and `ats/lever.py` against a common interface:

```python
class ATS:
    def open_application(self, page, job): ...
    def discover_fields(self, page) -> list[Field]: ...
    def fill(self, page, answers): ...
    def submit(self, page): ...
    def is_confirmed(self, page) -> bool: ...
```

Mode handling in `apply.py`:

- **`--dry-run`**: fill everything, screenshot, never click submit. Default during development.
- **assist**: fill, visibly highlight flagged fields in the page (outline + tooltip), print a summary in the terminal, then `pause_for_user` until the user has submitted; then run verification.
- **auto**: fill; if **any** required field is empty or flagged, or a CAPTCHA/2FA appears, automatically switch to assist and say why. Otherwise screenshot, submit, verify.
- Mode comes from `config.yaml` per platform, overridable with `--mode`.

Check Greenhouse's newer React form (job-boards.greenhouse.io) and the classic board separately; they differ. Handle Lever's `/apply` page, including its optional hCaptcha (→ assist).

**Done when:** dry runs on at least 3 real Greenhouse and 3 real Lever postings fill all fields or flag them correctly, and the user has approved one real assist-mode submission and one real auto-mode submission.

### M6 — Verification

- `verify.py`: after submit, wait for a confirmation signal: URL change to a confirmation route, a thank-you/confirmation message, or the form disappearing without validation errors. Detect and report inline validation errors instead.
- Screenshot the confirmation.
- Status values: `submitted` (verified), `needs_review` (unclear result), `failed` (validation errors / crash), `dry_run`.

**Done when:** tests on saved confirmation and error-page fixtures classify correctly.

### M7 — Tracker

- Local **SQLite** is the source of truth (`jobs` table: id, canonical_url, platform, company, role, location, status, mode, score, gaps, resume_pdf, answers_path, screenshots, flagged_fields, created_at, submitted_at, notes).
- **Google Sheets sync** (optional; enabled when `config.yaml` has a sheet id): one row per job, updated on status change. Use OAuth desktop credentials owned by the user (service accounts cannot own Drive files, and the user's own sheet is simpler). Ask the user which columns he wants before building.
- `python apply.py --status` prints recent applications.

**Done when:** a dry run, an assist submission and an auto submission each appear correctly in SQLite and the sheet.

### M8 — Polish

- Update README: setup, `apply.py` usage, modes, where outputs go, how to edit the profile, known limitations.
- Clear error messages for: Claude Code usage limit reached (tell the user to wait and re-run; the job folder should allow resuming without re-tailoring), LaTeX errors, missing profile values, unsupported platform.
- `--resume-from <job folder>` to retry filling without re-tailoring.

## 7. Testing strategy

- Unit tests with **saved fixtures** (HTML/JSON captured from real postings with the user's consent) for field discovery, answer mapping, intake and verification. No network in unit tests.
- A guard test proving sensitive questions never go to the LLM, and that LLM answers for select fields are always one of the options.
- Manual end-to-end checks only in `--dry-run`, except the specific real submissions the user approves in M5.
- Keep phase 1 tests (add them if missing): guard cases (number change, unbacked keyword, skill addition, conflicts, unsafe LaTeX), one-page fitting, base render.

## 8. Roadmap after v1 (one feature at a time, each usable on its own)

1. **LinkedIn Easy Apply** adapter (assist by default; uses the persistent browser login; strict daily cap set by the user).
2. **Naukri** adapter (assist by default; profile-based apply + recruiter questionnaires; OTP → pause).
3. **Workday** adapter (assist; per-company account creation is manual).
4. **Indeed** adapter.
5. **Job discovery + queue**: collect postings from saved searches, dedupe, score against the resume, and let the user approve a batch before applying.
6. **Batch runner** that works through approved jobs, respects Claude Pro usage limits (pauses and resumes), and per-platform daily caps.
7. Response tracking. (Cover letters are already part of M4.)

Each item gets its own short plan and user approval before starting.

## 9. Open questions to ask the user before or during the build

- Profile values marked `TODO` in M1.
- Which currencies and countries matter for salary and work-authorization answers.
- How to count total experience (include the internship or not).
- ~~Whether he wants cover letters at all, and when.~~ Decided: always, required and optional (see M4). Still needed from him: a writing sample to match his tone.
- Which Google Sheet columns he wants.
- Real Greenhouse and Lever job URLs to use as test fixtures.
- Whether to rename the project folder.