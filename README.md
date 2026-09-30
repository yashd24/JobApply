# Job Apply

Tailors a LaTeX resume to a job description and (in progress) fills in the application, with a
per-platform choice between **auto** (submit without you) and **assist** (fill everything, you click
submit). The LLM is **Claude Code in headless mode** (`claude -p`) on a Claude Pro subscription; there is
no API key.

The full design, rules and milestone checks live in [`plan.md`](plan.md). This file is the quick tour.

## Status

| Milestone | What | State |
|---|---|---|
| Phase 1 | Resume tailor: JD -> tailored one-page PDF + report | done |
| M0 | Verified on Windows (MiKTeX, real `claude -p`) | done |
| M1 | `profile.yaml` + fill-time answers (experience, notice, salary, EEO, education...) | done |
| M2 | Intake: Greenhouse / Lever URL -> job data + form questions | done |
| M3 | Browser session (Playwright, persistent profile, screenshots, pause/resume) | built; pause/resume check on your terminal pending |
| M4-M8 | Field discovery + answering, cover letters, ATS adapters + modes, verification, tracker, polish | not started |

Nothing in this repo submits an application yet.

## Layout

All code is under `resume-tailor/tailor/`:

```
tailor.py            resume tailoring CLI and tailor_job() (importable)
guard.py             validates everything Claude proposes; enforces the honesty rules
render.py            renders resume.tex from resume_data.yaml
resume_data.yaml     the single source of truth for resume content (bullets have ids)
template/            the LaTeX class, fonts and original resume
contact.example.yaml the email + phone printed in the resume header, values TODO (committed)
contact.yaml         your real email + phone (gitignored; kept out of resume_data.yaml)
profile.example.yaml the profile outline, every value TODO (committed)
profile.yaml         your real answers (gitignored, never committed)
jobbot/profile.py    load/validate the profile; fill-time answers
jobbot/intake.py     job URL -> Job (platform, JD, questions)
jobbot/browser.py    Playwright browser session
tests/               unittest suites + saved fixtures (tests/fixtures/)
output/              per-job folders, screenshots (gitignored)
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
   .venv\Scripts\python tailor.py --base
   ```
   `output\base\` holds the untailored PDF; it should match your current resume.

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

Copy `profile.example.yaml` to `profile.yaml` and fill every value yourself (nothing is guessed). The code
refuses to use a value that is still `TODO`. `jobbot/profile.py` turns the stored facts into form answers at
fill time: years of experience (computed from dates), notice period and start date, salary (Indian forms
only; non-Indian forms are flagged), EEO options, education, and "how did you hear about us". Anything it
cannot answer from the profile comes back as `None`, meaning **flag it for the user**.

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

## Tests

```
python -m unittest discover -s tests
```

No network is used except the browser tests, which run a local server and headless Chromium. Fixtures:
`tests/fixtures/synthetic/` (hand-written) and `tests/fixtures/real/` (public postings saved with
`--save-fixture`). Tests must never contain real profile values; use the fictional "Test User" data.

## Rules that never bend

1. **No fabrication**: nothing goes into a resume or form that is not backed by `resume_data.yaml` or `profile.yaml`.
2. **Sensitive answers** (work authorization, sponsorship, salary, notice period, relocation, EEO, anything
   legal) come only from `profile.yaml`, never from the LLM.
3. **Submitted means verified**: a confirmation signal, not a click.
4. **No CAPTCHA/2FA bypass**; default to assist mode on LinkedIn and Naukri.
5. **Every run is auditable**: screenshots, answers and their sources are saved in the job folder.

## Known limitations

- Windows is the only tested platform.
- v1 scope is Greenhouse and Lever, one job URL at a time; LinkedIn, Naukri, Workday and Indeed come later.
- The Greenhouse EU API host is assumed, not verified.
- Keyword-edit checks are word-based; they cannot judge whether an added word is *true* for that bullet, so
  read `report.md`.
