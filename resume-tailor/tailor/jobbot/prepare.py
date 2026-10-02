"""Prepare mode: the bot never drives the employer's form. It produces a copy-ready sheet of every answer, in form
order, plus the resume PDF path; the user opens the job in their normal browser, fills it in and submits by hand,
then confirms in the terminal. Used for Lever, whose anti-bot check rejects an automated browser's submit.
"""
from __future__ import annotations

import html
import webbrowser
from pathlib import Path

from jobbot import answers as A

FLAGGED_TEXT = "YOU ANSWER THIS"


def display_value(a: "A.Answer", resume_pdf: "Path | None" = None) -> str:
    """What the user should type or pick for this field ('' when it is theirs to answer)."""
    if a.status != A.FILLED or a.value in (None, "", []):
        return ""
    if a.type == "file":
        return str(a.value)
    return ", ".join(a.value) if isinstance(a.value, list) else str(a.value)


def _note(a: "A.Answer") -> str:
    if a.status == A.FLAGGED:
        return f"{FLAGGED_TEXT}: {a.note}"
    if a.status == A.BLANK:
        return f"leave empty{': ' + a.note if a.note else ''}"
    return ""


def sheet_text(answers: "list[A.Answer]", resume_pdf: Path, job) -> str:
    lines = [f"{job.company} | {job.role} | {job.location}", f"Apply at: {job.apply_url}",
             f"Resume PDF: {resume_pdf}", "", "Answers in form order (* = required):", ""]
    for i, a in enumerate(answers, 1):
        value = display_value(a)
        lines.append(f"{i:>2}. {'*' if a.required else ' '} {a.label}")
        if a.type == "file" and value:
            lines.append(f"      upload: {value}")
        elif value:
            lines.extend(f"      {part}" for part in value.splitlines() or [""])
        if _note(a):
            lines.append(f"      -> {_note(a)}")
    return "\n".join(lines) + "\n"


_CSS = """body{font:15px/1.5 system-ui,sans-serif;max-width:860px;margin:24px auto;padding:0 16px;color:#1a1a1a;background:#fff}
h1{font-size:20px;margin:0 0 4px}.meta{color:#555;margin-bottom:18px;word-break:break-all}
.row{border:1px solid #ddd;border-radius:8px;padding:10px 12px;margin:10px 0}.row.you{border-color:#e67e22;background:#fff6ec}
.label{font-weight:600}.req{color:#c0392b}.val{white-space:pre-wrap;background:#f5f5f5;border-radius:6px;padding:8px;margin:6px 0;
font-family:ui-monospace,Consolas,monospace;font-size:13px}.note{color:#b85c00;font-size:13px}
button{font:inherit;font-size:13px;padding:3px 10px;cursor:pointer}@media(prefers-color-scheme:dark){body{background:#161616;color:#eee}
.row{border-color:#444}.row.you{background:#2b2012}.val{background:#242424}.meta{color:#aaa}}"""
_JS = """document.querySelectorAll('button[data-copy]').forEach(b=>b.onclick=async()=>{
const t=document.getElementById(b.dataset.copy).textContent;
try{await navigator.clipboard.writeText(t)}catch(e){const r=document.createRange();r.selectNode(document.getElementById(b.dataset.copy));
getSelection().removeAllRanges();getSelection().addRange(r);document.execCommand('copy')}
b.textContent='Copied';setTimeout(()=>b.textContent='Copy',1200)});"""


def sheet_html(answers: "list[A.Answer]", resume_pdf: Path, job) -> str:
    e = html.escape
    rows = []
    for i, a in enumerate(answers, 1):
        value, note = display_value(a), _note(a)
        cls = "row you" if a.status == A.FLAGGED else "row"
        body = ""
        if value:
            body += (f'<div class="val" id="v{i}">{e(value)}</div><button data-copy="v{i}">Copy</button>'
                     + (" (upload this file)" if a.type == "file" else ""))
        if note:
            body += f'<div class="note">{e(note)}</div>'
        rows.append(f'<div class="{cls}"><span class="label">{i}. {e(a.label)}</span>'
                    f'{"<span class=req> *</span>" if a.required else ""}{body}</div>')
    return (f'<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">'
            f"<title>Answers for {e(job.company)}</title><style>{_CSS}</style></head><body>"
            f"<h1>{e(job.company)}: {e(job.role)}</h1>"
            f'<div class="meta">{e(job.location)}<br>Apply at: <a href="{e(job.apply_url)}">{e(job.apply_url)}</a></div>'
            f'<div class="row"><span class="label">Resume PDF (upload this)</span>'
            f'<div class="val" id="pdf">{e(str(resume_pdf))}</div><button data-copy="pdf">Copy</button></div>'
            f'{"".join(rows)}<script>{_JS}</script></body></html>')


def write_sheet(job_dir: Path, answers, resume_pdf: Path, job) -> "tuple[Path, Path]":
    txt, page = job_dir / "prepare_sheet.txt", job_dir / "prepare_sheet.html"
    txt.write_text(sheet_text(answers, resume_pdf, job), encoding="utf-8")
    page.write_text(sheet_html(answers, resume_pdf, job), encoding="utf-8")
    return txt, page


def open_in_default_browser(url: str, opener=None) -> bool:
    """The user's normal browser (their logins, their fingerprint): never the automation browser."""
    return bool((opener or webbrowser.open_new_tab)(url))


def confirm_manual(input_fn=input, output_fn=print) -> "bool | None":
    """True: the user says they submitted. False: they say they did not. None: no terminal to ask in."""
    output_fn("\nFill the form in your browser from the sheet, upload the PDF, and click Submit yourself.")
    try:
        while True:
            reply = input_fn("Did you submit it? Type 'yes' once the employer shows its confirmation, or 'no': ")
            reply = reply.strip().lower()
            if reply in ("y", "yes"):
                return True
            if reply in ("n", "no"):
                return False
    except (EOFError, OSError):
        return None
