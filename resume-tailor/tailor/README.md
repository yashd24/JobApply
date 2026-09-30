# Resume Tailor (phase 1)

Turns a job description into a tailored, one-page PDF of your resume, using
your exact LaTeX template and your Claude Code Pro subscription (no API key).

## One-time setup (Windows)

1. **Python 3.10+** from python.org (tick "Add python.exe to PATH").
2. **MiKTeX** from miktex.org. During install, set "Install missing packages
   on-the-fly" to **Yes** (your template needs `fontawesome` and `ulem`).
   Your template uses custom fonts, so it must compile with **XeLaTeX**,
   which MiKTeX includes. (Tectonic also works if you prefer it.)
3. **Claude Code**: install per https://docs.claude.com/en/docs/claude-code/overview,
   then run `claude` once in a terminal and log in with your Pro account.
4. In this folder:
   ```
   pip install -r requirements.txt
   python tailor.py --base
   ```
   Open `output\base\Yashdeep_Sahu_Resume.pdf`. It should match your current resume.

## Tailor to a job

```
python tailor.py --company "Razorpay" --role "SDE 1" --jd-file jd.txt
python tailor.py --company "Stripe" --role "Backend Engineer" --jd-url https://boards.greenhouse.io/...
python tailor.py --company "Acme" --role "SDE"      (paste the JD, then Ctrl+Z, Enter)
```

`--jd-url` works for Greenhouse/Lever/most career pages. LinkedIn, Naukri and
Workday need a login or JavaScript, so it falls back to asking you to paste.

Each job gets a folder in `output\` containing:

| File | What it is |
|---|---|
| `Yashdeep_Sahu_Resume.pdf` | the tailored resume to upload |
| `report.md` | **read this before applying**: scores, gaps, every edit before/after, what was trimmed |
| `result.json` | summary for the tracker / auto-apply (phase 2+) |
| `resume.tex`, `plan.json`, `claude_raw.json`, `jd.txt` | for debugging and audit |

## What Claude can and cannot change

Claude only picks which bullets to show, orders them, and inserts JD keywords.
`guard.py` checks every change and reverts anything that breaks a rule:

- Company names, titles, dates, education and contact details are never editable.
- Every number must stay identical (80,000+, 250+, 90%...).
- At most 4 original words removed and 8 added per bullet.
- Every added word must appear in the JD **and** somewhere in your own content,
  so a JD term like "Kubernetes" can never be inserted into a bullet.
- Skills can only be reordered, never added.
- Overlapping bullets (`conflicts_with`) are never shown together.

If the result runs over one page, the least relevant bullets are trimmed
(older/optional entries first, your current role last), then anything that
fits is refilled.

## Updating your resume

Edit `resume_data.yaml`, not the `.tex`. Add a bullet with a new `id`;
`default: true` puts it on your base resume. Run `python tailor.py --base` to check.

## Other options

- `--prompt-only`: writes the prompt to a file without calling Claude.
- `--plan plan.json`: uses a plan you provide instead of calling Claude.
