# Job Apply

Tailors a LaTeX resume to a job description, writes a guarded cover letter, and fills in the application on
Greenhouse and Lever, with a per-platform choice between **auto** (submit without you), **assist** (fill everything,
you click Submit) and **prepare** (you apply by hand from a copy-ready sheet). The LLM is **Claude Code in headless
mode** (`claude -p`) on a Claude Pro subscription; there is no API key.

The full design, rules and milestone checks live in [`plan.md`](plan.md). This file is the quick tour.

## Status

| Milestone | What | State |
|---|---|---|
| Phase 1 | Resume tailor: JD -> tailored one-page PDF + report | done |
| M0 | Verified on Windows (MiKTeX, real `claude -p`) | done |
| M1 | `profile.yaml` + fill-time answers (experience, notice, salary, EEO, education...) | done |
| M2 | Intake: Greenhouse / Lever URL -> job data + form questions | done |
| M3 | Browser session (Playwright, persistent profile, screenshots, pause/resume) | done |
| M4 | Field discovery, answer pipeline, cover letters, dry-run form filling | done |
| M5 | Greenhouse + Lever adapters, dry-run / prepare / assist / auto modes | done except one real **auto** submission (project44 was submitted in assist mode; Lever uses prepare) |
| M6 | Verification of the result page | done (tested on saved confirmation / error pages) |
| M7 | Tracker (SQLite) + optional Google Sheet | SQLite done; the Sheet sync is built and tested against a fake sheet, not yet run on a real one |
| M8 | Polish: `--resume-from`, clearer errors, this README | done |
| M9 | Job discovery with JobSpy: searches, cheap filters, routing, shortlist and approval | built and tested; batch runner not built yet |

`apply.py` is a dry run unless you explicitly approve a specific job (see below); nothing submits by default.

## Layout

All code is under `resume-tailor/tailor/`:

```
tailor.py            resume tailoring CLI and tailor_job() (importable)
guard.py             validates everything Claude proposes; enforces the honesty rules
render.py            renders resume.tex from resume_data.yaml
resume_data.yaml     the single source of truth for resume content (bullets have ids)
template/            the LaTeX class, fonts and original resume
contact.yaml         your real email + phone (gitignored); contact.example.yaml is the committed outline
profile.yaml         your real answers (gitignored); profile.example.yaml is the committed outline
config.yaml          modes per platform, optional Google Sheet (gitignored); config.example.yaml documents it
cover_letter.yaml    cover-letter length, style rules, closing styles and banned phrases (editable)
writing_samples.txt  your own past messages, for sentence length and plain wording only (gitignored)
cover_letter_model.txt  one cover letter you wrote, the main style model (gitignored; see the .example file)
jobbot/profile.py    load/validate the profile; fill-time answers; the "job has ended" date rule
jobbot/intake.py     job URL -> Job (platform, JD, questions)
jobbot/browser.py    Playwright browser session
jobbot/fields.py     discover a form's fields (labels, types, options) from the live page
jobbot/answers.py    field -> answer with a source; sensitive fields never reach the LLM
jobbot/filler.py     fill the form; blocks every submit (dry run)
jobbot/ats/          Greenhouse / Lever adapters (open, discover, fill, submit, confirm)
jobbot/coverletter.py  guarded cover letters (text + one-page PDF)
jobbot/tense.py      present-tense checks for a job that has ended
jobbot/prepare.py    prepare mode: the copy-ready answer sheet and manual-submission record
jobbot/verify.py     did the application really go through? (confirmation / error pages)
jobbot/discovery.py  JobSpy searches -> filters -> routing -> shortlist (python discover.py)
jobbot/tracker.py    SQLite tracker and dedupe
jobbot/sheets.py     optional Google Sheet sync
jobbot/config.py     config.yaml loading
apply.py             the application CLI (below)
discover.py          the discovery CLI (below)
tests/               unittest suites + saved fixtures (tests/fixtures/)
output/              per-job folders, screenshots, tracker.sqlite3 (gitignored)
browser_profile/     the automation browser's logins/cookies (gitignored)
```

## Setup (Windows)

1. **Python 3.10+** and **MiKTeX** (XeLaTeX; set "install missing packages on-the-fly" to *Always*).
2. **Claude Code**, installed and logged in (`claude` once in a terminal with your Pro account).
3. `xelatex` and `claude` must be on `PATH` (open a fresh terminal after installing them; check with
   `where.exe xelatex` and `where.exe claude`).
4. From `resume-tailor/tailor/`:
   ```
   python -m venv .venv
   .venv\Scripts\python -m pip install -r requirements.txt
   .venv\Scripts\python -m playwright install chromium
   copy contact.example.yaml contact.yaml      (then put your email and phone in it)
   copy profile.example.yaml profile.yaml      (then replace every TODO with your own answer)
   .venv\Scripts\python tailor.py --base
   ```
   `output\base\` holds the untailored PDF; it should match your current resume.
5. Optional, for the Google Sheet: the packages in `requirements.txt` cover it; the Google Cloud steps are in
   `config.example.yaml`.

Use the venv's Python for every command below.

## Tailor a resume

```
python tailor.py --company "Acme" --role "Backend Engineer" --jd-url https://jobs.lever.co/...
python tailor.py --company "Acme" --role "SDE" --jd-file jd.txt
```

Each job gets `output/<date>_<company>_<role>/` with the PDF, `report.md` (read it before applying:
scores, gaps, order changes, every keyword edit before/after), `result.json` and audit files.

**Locked selection (default).** The tailored resume has exactly the sections and bullets of the base
resume, and hidden (`default: false`) bullets are never used. Claude may only reorder bullets within a
role/project, reorder skills, and add JD keywords to existing bullets under `guard.py`:

- every number stays identical; LaTeX stays safe; at most 4 words removed and 8 added per bullet;
- an added word must appear in the JD; a technology name (known list, symbols, or capitalised mid-sentence
  in the JD) must also already be on your visible resume; up to 3 grammar words not in the JD are allowed;
- if the page overflows, keyword edits are undone (largest first), never bullets.

`--allow-selection` restores the old mode where Claude may add or drop bullets. `--base` renders the
untailored resume. `--prompt-only` writes the prompt without calling Claude.

## Profile

`profile.yaml` holds the facts that answer forms: contact, work authorisation, employment dates, notice period,
salary, EEO choices, education. Nothing is guessed: the code refuses to use a value that is still `TODO`, and
anything it cannot answer from the profile comes back as "flag it for the user". Edit the file directly; changes
apply on the next run. `jobbot/profile.py` turns the stored facts into answers at fill time (years of experience are
computed from dates, never stored as a number).

**When the job ends.** `employment.last_working_day` (with `serving_notice: true`) drives everything date-based. From
the day **after** that date, automatically: the resume shows the current job's dates as "Aug 2025 - Oct 2026"
instead of "Present"; cover letters describe that job in the past tense and never call it the current position (the
prompt says so and the guard rejects "currently", "my current role", "I work at ..." and present-tense verbs like
"I lead"); and `report.md` / the console warn about any present-tense bullet of that role. Nothing is hardcoded:
change the date in the profile and the switch moves with it. Notice-period answers stop being filled after that date
(they are flagged for you).

## Cover letters

Short, plain, confident, and checked by code, not by the model. Rules (in `cover_letter.yaml` and
`jobbot/coverletter.py`): 150-250 words in three paragraphs; the first paragraph is at most two sentences (the role,
connected to your work; no summary of the JD); one or two achievements with real numbers from your visible resume; no
quotation marks (the exact JD phrases the letter rests on are returned separately and checked); no enthusiasm words
("love to", "excited", "eager", "dream"), stock phrases, greetings in the body (the greeting is always "Hello,"), repeated
sentence openers, announced links ("This matters because..."), wording broader than the JD, or results the resume does
not state. A letter that fails is regenerated once; after that an optional field stays empty and a required one goes to
you. The guard checks words, not truth: read each letter before relying on it.

Tone comes from your own writing: `cover_letter_model.txt` (one letter you wrote) as the main model, and
`writing_samples.txt` for sentence length and plain wording only. Neither supplies facts, and the guard rejects any
letter that reuses eight words in a row from them.

## Intake

```
python -m jobbot.intake <greenhouse-or-lever-url> [--save-fixture tests/fixtures/real/<name>]
```

Supports `boards.greenhouse.io` / `job-boards.greenhouse.io` (including EU hosts), company career pages
that embed Greenhouse (`gh_jid`), and `jobs.lever.co` / `jobs.eu.lever.co`. Greenhouse comes from the public
Job Board API (JD and questions). Lever's API has the JD only, so its questions are read from the `/apply`
page. If an API fails it falls back to the HTML scraper (JD only).

## Browser

```
python -m jobbot.browser <url> [<url> ...]
```

Opens the automation browser (its own persistent profile, so logins survive), screenshots each page into the
job folder, and demonstrates pause/resume. Only one run can use the browser profile at a time. There is no
stealth, fingerprint spoofing or CAPTCHA solving: when a CAPTCHA, 2FA or anything else needs a human, it pauses.

## Apply (dry run by default)

```
python apply.py --url <greenhouse-or-lever-job-url>                       # dry run: fills, screenshots, never submits
python apply.py --url <job-url> --mode assist --allow-submit <job-url>    # fills, waits while YOU click Submit, verifies
python apply.py --url <job-url> --mode auto   --allow-submit <job-url>    # submits itself (see below)
python apply.py --url <lever-job-url>                                     # Lever defaults to prepare (see below)
python apply.py --resume-from <job folder> [--mode ...]                   # continue an earlier run (see below)
python apply.py --mark-submitted <job folder>                             # record a prepared job you submitted by hand
python apply.py --mark-applied 12,15                                        # you applied by hand: mark tracker ids as Submitted
python apply.py --status                                                  # recent applications (SQLite tracker)
python apply.py --import-existing                                         # rebuild the tracker from output/ folders
python apply.py --sync-sheet                                              # push everything to the Google Sheet
```

Reads the job, tailors the resume (`--resume-pdf` reuses one), opens the form, answers every field (profile first,
the LLM only for non-sensitive questions), fills it, and outlines what is left for you. The job folder gets
`screenshots/`, `answers.json` (every answer with its source and status) and `apply_result.json` (the outcome).

- **dry-run** is the default and what `--dry-run` forces. The browser also blocks every submit event and click.
- **prepare** (the default for Lever, whose anti-bot check rejects an automated submit) never drives the form. It
  tailors the resume, computes every answer and the cover letter, writes `prepare_sheet.html/.txt` (answers in form
  order, each with a Copy button, plus the PDF path) and opens the job and the sheet in your normal default browser.
  You fill and submit by hand, then type `yes` in the terminal: it is recorded as a manual submission
  (`submitted_by: manual`). No terminal? Run `--mark-submitted <job folder>` afterwards. Needs no `--allow-submit`.
- **assist** fills, highlights the fields in orange that are yours, then pauses; you click Submit; it verifies.
- **auto** fills, and submits only if every required field is filled and nothing is flagged; otherwise (or if a
  CAPTCHA / security-code challenge is actually visible) it switches to assist and says why. A challenge is handed
  to you, never solved. Statuses: `dry_run`, `prepared`, `submitted` (a confirmation was seen), `needs_review`, `failed`.
- **Approval is per job:** a real mode submits only if `--allow-submit` equals that job's canonical URL. Without
  it, or with a different URL, the run is downgraded to a dry run and says so. `config.yaml` (copy of
  `config.example.yaml`) sets the default mode per platform; with no config everything is a dry run except Lever
  (prepare).
- **Tracker and dedupe:** every run is recorded in `output/tracker.sqlite3`. A posting already tracked as submitted is
  refused in prepare / assist / auto (add `--reapply` to override); dry runs are always allowed.
- **Statuses:** Found, Approved, Ready for you (prepared: next step is yours), Needs review, Manual, Failed, Submitted,
  Skipped. Every posting also has a plain-English **Reason** and the **job folder** of its run.
- **Google Sheet (optional):** the main tab's columns A-N are written by the bot (including Reason and Job folder),
  O-R (Response, Interview stage, Follow-up date, My notes) are yours and are never written to; rows are matched by URL
  so you can sort freely. A second tab, **Action needed**, lists everything waiting on you (Ready for you, Needs review,
  Failed, Manual) with a **Mark applied** checkbox: tick it after you apply by hand and the next sync marks the posting
  Submitted. Setup is in `config.example.yaml`; the first `--sync-sheet` opens a Google sign-in.

### Resuming a run

Every job folder keeps the tailored resume, the cover letter and `job_info.json` (company, role, URL).
`--resume-from <folder>` reuses what is there instead of paying for it again: the resume PDF, and the saved cover letter
(only if it still passes the guard, for example after the job-ended date has passed it is rewritten). If the folder has
no resume yet it is tailored into that same folder. The URL is read from the folder, so `--url` is optional. The mode
rules are unchanged: a real submission still needs `--allow-submit`.

## When something goes wrong

| You see | What it means | What to do |
|---|---|---|
| "Claude Code's usage limit has been reached" | Your Pro limit is used up. Not a bug. | Wait for the reset, then run the printed `apply.py --resume-from "<folder>"` command with the same `--mode` / `--allow-submit` as before. Nothing already done is repeated. |
| "LaTeX compile failed: ! Undefined control sequence. (line 45: ...)" | XeLaTeX hit an error. | The message gives the first error, its line and the path of the full `resume.log`. The guard normally blocks unsafe edits, so please keep the log. "No LaTeX engine found": install MiKTeX. |
| "profile.yaml not found" / "has problems to fix first" / "still has TODO in: ..." | The profile is missing, malformed or incomplete. | Copy `profile.example.yaml`, replace every TODO; the message names the fields. |
| "... not a Greenhouse or Lever posting" | Another site (LinkedIn, Naukri, Indeed, Workday...). | Apply there by hand. Supported: Greenhouse (including career pages that embed it) and Lever. |
| "Already submitted (...)" | The tracker has this posting as submitted. | Nothing was done. `--reapply` overrides. |
| "The browser profile ... is already in use by another run" | Another run holds the automation browser. | Close it or wait. |

## Discover jobs

```
python discover.py                    # scrape the saved searches, filter, save as "found", print the shortlist
python discover.py --dry-run          # same, but save nothing
python discover.py --shortlist        # show the current shortlist (no scraping)
python discover.py --approve 3,7,12   # approve those ids (or: --approve all); --skip 2,5 declines
python discover.py --term "python developer" --results 10 --why     # a small trial run, with every drop explained
```

Needs `pip install -U python-jobspy` (it brings pandas and a few more). The searches live in `config.yaml` under
`discovery:` (terms, locations, sites, max age, experience limit; see `config.example.yaml`). No Claude calls: postings
are dropped for a stated reason when the title is not a software role, the posting is too old, the location is not
Bengaluru or remote in India, it asks more minimum experience than you allow, or it is a duplicate (of another site's
listing, or of anything already in the tracker). The rest are routed by their **direct apply link**: Greenhouse ->
apply (assist/auto per config), Lever -> prepare, anything else -> manual. LinkedIn never exposes a direct link, so a
LinkedIn-only posting is manual. You approve from the shortlist; approved manual postings go to the Google Sheet with
their link, approved Greenhouse/Lever ones wait for the batch runner (not built yet). Every dropped posting and its
reason is saved in `output/discovery/`.

## Run everything unattended (`run.py`)

```
python run.py --dry-run     # discover, filter and SCORE for real; show what would be approved; approve and submit nothing
python run.py               # the whole flow; you start it yourself (nothing is scheduled)
```

discover -> rules filter (title, experience, location, embedded/frontend-only, duplicates; no Claude) -> **relevance
score** (one Claude call per 10 postings, 1-10 against your resume, with a one-line reason; honest: roles needing skills
you do not have score low) -> auto-approve everything at or above `selection.relevance_threshold` (default 7), most
relevant first, up to the daily cap -> process each -> sync the sheet -> `output/runs/<date>.json` + `.log`.
**Greenhouse** is run in auto mode and submitted only if every required field is filled and nothing is flagged; if
anything is flagged, a captcha appears, or the result is unclear, nobody is waited for: the materials are prepared and
the job is marked **Needs review** with the reason. **Everything else** (Lever, LinkedIn, Naukri, company sites) is
prepared and marked **Ready for you** with the link. Anything already in the tracker is never applied for again. A
Claude usage limit stops the run cleanly and the next run resumes. See `plan.md` for the explicit authorisation this
rests on. The selection rules and term lists live in `config.yaml` under `discovery:`; the shortlist is also written
as data to `output/shortlist.json` (the Google Sheet is the main view).

**Descriptions and the board check.** Before scoring, a Found posting with no description gets one fetched (HTTP only).
After scoring, a posting with no employer link is looked up on the company's Greenhouse/Lever board and moved onto it
ONLY if the company, title (every word), location and a live job all match and exactly one job fits; the evidence is
logged and kept (`discover.py --board-check --dry-run` previews it). Anything weaker stays on its route.

## The `jobapply` command (any terminal, any folder)

`resume-tailor\tailor\bin\jobapply.cmd` is a short command for running the bot by hand. It uses the absolute paths of the
project's venv and folder, so it works from PowerShell or cmd in any folder. (If you move the project, edit the one
`JOBAPPLY_HOME` line at the top of the file.)

| Type | What it does |
|---|---|
| `jobapply` | the full run (`run.py`) |
| `jobapply dry` | a dry run: discover, filter and score; approve and submit nothing |
| `jobapply status [N]` | recent applications |
| `jobapply sync` | sync the Google Sheet |
| `jobapply action` | the jobs that need you (Ready for you, Needs review, Manual, Failed), each with its link, job folder and reason |
| `jobapply log` | open today's run log |
| `jobapply stop` | stop a run in progress cleanly, from a second terminal (see below) |
| `jobapply help` | list these |

Anything else is passed to `run.py`: `jobapply --skip-discovery`, `jobapply dry --skip-discovery`. A run ends with a
five-number summary (found, scored, submitted, ready for you, needs review) as the last thing on screen; the full
summary and log stay in `output\runs\`.

**Stopping a run cleanly.** `jobapply stop` (from a second terminal) or one Ctrl+C in the run's own window asks the run
to stop. It finishes and saves the job it is on, updates the tracker, syncs the sheet and exits. It looks for the request
between jobs, between searches and between scoring batches, so it can take a few minutes if a job is mid-way. Scores are
saved after every batch and approved jobs keep their folder, so the next `jobapply` continues without repeating paid
work. A second Ctrl+C aborts at once: what is saved is kept, but the job in progress may be repeated. `jobapply stop`
with no run in progress says so and does nothing.

**Add it to your PATH (once, for your user; nothing needs administrator rights):**

1. Press Win, type `environment variables`, open **Edit environment variables for your account**.
2. Under **User variables**, select **Path**, click **Edit**.
3. Click **New** and paste `D:\Projects\JobApply\resume-tailor\tailor\bin`. Click **OK** on all three windows.
4. Open a **new** terminal (already-open ones keep the old PATH) and test, from any folder:
   ```
   cd C:\
   jobapply help
   jobapply status 3
   where.exe jobapply          (should print ...\resume-tailor\tailor\bin\jobapply.cmd)
   ```
   Try it in both PowerShell and cmd. Nothing in the project changes your PATH for you.

## Prepare the approved jobs (batch runner)

```
python batch.py              # prepare every approved job, one at a time
python batch.py --list       # the queue, today's count and the daily cap; does nothing
python batch.py --only 12,15 --limit 3 --delay 30
```

Approving a job (`discover.py --approve`) is what spends Claude usage, not finding it. For each approved job:
**Greenhouse** is filled in the mode set for it in `config.yaml` (assist: you click Submit); **Lever** is prepared from
its real form (tailored resume, cover letter, answers); **anything else** (LinkedIn, Naukri, company pages) gets the
description fetched if missing, a tailored resume, a cover letter, and an answer sheet of the usual application
questions. Those become **Ready for you** with their link (the Action needed tab). A job whose description cannot be
read becomes **Manual**. Between jobs it waits `batch.delay_between_jobs_s`; it stops at `batch.daily_cap` jobs a day.
If Claude's usage limit is reached it stops at once; the next `python batch.py` resumes that job first and reuses its
folder. A failed job is recorded as Failed with the reason and the queue continues.

## Tests

```
python -m unittest discover -s tests
```

No network is used except the browser tests, which run a local server and headless Chromium. Fixtures:
`tests/fixtures/synthetic/` (hand-written), `tests/fixtures/real/` (public postings saved with `--save-fixture`) and
`tests/fixtures/verify/` (confirmation and error pages). Tests must never contain real profile values; use the
fictional "Test User" data. Date-driven tests use made-up dates read from a test profile.

## Rules that never bend

1. **No fabrication**: nothing goes into a resume or form that is not backed by `resume_data.yaml` or `profile.yaml`.
2. **Sensitive answers** (work authorization, sponsorship, salary, notice period, relocation, EEO, anything
   legal) come only from `profile.yaml`, never from the LLM.
3. **Submitted means verified**: a confirmation signal, not a click.
4. **No CAPTCHA/2FA bypass**; default to assist mode on LinkedIn and Naukri.
5. **Every run is auditable**: screenshots, answers and their sources are saved in the job folder.
6. **No real submission without your approval of that exact job.**

## Known limitations

- Windows is the only tested platform.
- v1 scope is Greenhouse and Lever, one job URL at a time; LinkedIn, Naukri, Workday and Indeed come later.
- Lever rejects an automated browser's submit, so Lever is prepare mode (you submit by hand).
- The real auto-mode submission and a live Google Sheet sync have not been run yet.
- The Greenhouse EU API host is assumed, not verified.
- Keyword-edit and cover-letter checks are word-based; they cannot judge whether a claim is *true*, so read
  `report.md` and each letter.
- A saved cover letter is reused as long as it passes the guard; if you want a fresh one, delete `cover_letter.txt`
  in the job folder.
