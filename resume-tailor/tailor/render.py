"""
Renders resume.tex from resume_data.yaml + a selection plan, using the
exact commands from the original modern-deedy template.

A "plan" says which entries/bullets to show and in what order:
{
  "experience":   [{"id": "zintlr_asde", "bullets": ["z_partner_api", ...]}, ...],
  "projects":     [{"id": "zquencer", "bullets": ["zq_main"]}, ...],
  "cocurricular": ["cc_quillz"],
  "skills":       {"Languages:": ["Python", "Java", "SQL"], ...},
  "edits":        {"z_vapt": "<bullet text with JD keywords inserted>"}
}
"""

from __future__ import annotations

PREAMBLE = r"""\documentclass[]{resume-openfont}

\pagestyle{fancy}
\resetHeaderAndFooter

% Create job position command. Parameters: company, position, location, when
\newcommand{\resumeHeading}[4]{\runsubsection{\uppercase{#1}}\descript{ | #2}\hfill\location{#3 | #4}\fakeNewLine}

% Create education heading. Parameters: Name, degree, location, when
\newcommand{\educationHeading}[4]{\runsubsection{#1}\hspace*{\fill}  \location{#3 | #4}\\
\descript{#2}\fakeNewLine}

% Create project heading. Parameters: Name, link, Tech stack
\newcommand{\projectHeading}[3]{\Project{#1}{#2}
\descript{#3}\\}

% Project heading without a link (no external-link icon)
\newcommand{\projectHeadingNoLink}[2]{\runsubsection{\uppercase{#1}}\hfill
\descript{#2}\\}

\newcommand{\cgpa}[1]{\textbf{CGPA:} #1}
"""


def default_plan(data: dict) -> dict:
    """The untailored resume: everything marked default: true."""
    def pick(entries, section_default_key="default"):
        out = []
        for e in entries:
            if e.get("required") or e.get(section_default_key, False) is True:
                out.append({"id": e["id"],
                            "bullets": [b["id"] for b in e["bullets"] if b.get("default")]})
        return out

    exp = [{"id": e["id"], "bullets": [b["id"] for b in e["bullets"] if b.get("default")]}
           for e in data["experience"] if e.get("required")]
    return {
        "experience": exp,
        "projects": pick(data["projects"]),
        "cocurricular": [c["id"] for c in data.get("cocurricular", []) if c.get("default")],
        "skills": {s["label"]: list(s["items"]) for s in data["skills"]},
        "edits": {},
    }


def _bullet_lookup(data: dict) -> dict[str, str]:
    lookup = {}
    for section in ("experience", "projects"):
        for e in data[section]:
            for b in e["bullets"]:
                lookup[b["id"]] = b["text"]
    for c in data.get("cocurricular", []):
        lookup[c["id"]] = c["text"]
    return lookup


def render_tex(data: dict, plan: dict) -> str:
    texts = _bullet_lookup(data)
    texts.update(plan.get("edits", {}))
    exp_by_id = {e["id"]: e for e in data["experience"]}
    proj_by_id = {p["id"]: p for p in data["projects"]}

    L: list[str] = [PREAMBLE, r"\begin{document}", ""]

    # ── Header (fixed; mailto link now points to the real address) ──
    L += [
        r"\begin{center}",
        rf"    \Huge \scshape \latoRegular{{{data['name']}}} \\ \vspace{{1pt}}",
        rf"    \small \href{{mailto:{data['email']}}}{{\underline{{{data['email']}}}}}  $|$  {data['phone']} $|$ ",
        rf"    \href{{https://www.linkedin.com/in/{data['linkedin']}}}{{\underline{{linkedIn/{data['linkedin']}}}}} $|$",
        rf"    \href{{https://github.com/{data['github']}}}{{\underline{{github/{data['github']}}}}}",
        r"\end{center}",
        "",
    ]

    # ── Education (fixed) ──
    ed = data["education"]
    L += [
        r"\section{Education}",
        rf"\educationHeading{{{ed['degree']}}}{{{ed['institute']}}}{{{ed['location']}}}{{{ed['date']}}}",
        "",
        rf"\cgpa{{{ed['cgpa']}}}",
        r"\sectionsep",
        "",
    ]

    # ── Skills (reorder only) ──
    L += [r"\section{Skills}", r"\begin{skillList}"]
    skill_lines = []
    for s in data["skills"]:
        items = plan.get("skills", {}).get(s["label"], s["items"])
        skill_lines.append(rf"    \singleItem{{{s['label']}}}{{{', '.join(items)}}}")
    L.append("\n    \\\\\n".join(skill_lines))
    L += [r"\end{skillList}", r"\sectionsep", ""]

    # ── Experience (always chronological, in bank order) ──
    chosen = {e["id"]: e["bullets"] for e in plan["experience"]}
    exp_ids = [e["id"] for e in data["experience"] if e["id"] in chosen]
    L.append(r"\section{Professional Experience}")
    for i, eid in enumerate(exp_ids):
        e = exp_by_id[eid]
        L.append(rf"\resumeHeading{{{e['company']}}}{{{e['position']}}}{{{e['location']}}}{{{e['dates']}}}")
        L.append(r"\begin{bullets}")
        for bid in chosen[eid]:
            L.append(rf"    \item {texts[bid]}")
        L.append(r"\end{bullets}")
        if i < len(exp_ids) - 1:
            L.append(r"\sectionsep")
        L.append("")

    # ── Projects (order as planned) ──
    if plan["projects"]:
        L += ["", r"\section{Projects}", ""]
        for p in plan["projects"]:
            proj = proj_by_id[p["id"]]
            if proj.get("link"):
                L.append(rf"\projectHeading{{{proj['name']}}}{{{proj['link']}}}{{{proj['stack']}}}")
            else:
                L.append(rf"\projectHeadingNoLink{{{proj['name']}}}{{{proj['stack']}}}")
            L.append(r"\begin{bullets}")
            for bid in p["bullets"]:
                L.append(rf"\item {texts[bid]}\\")
            L.append(r"\end{bullets}")
            L.append("")
        L.append(r"\sectionsep")

    # ── Co-curricular (optional) ──
    if plan.get("cocurricular"):
        L += ["", r"\section{Co-Curricular}", r"\begin{bullets}"]
        for cid in plan["cocurricular"]:
            L.append(rf"    \item {texts[cid]}")
        L += [r"\end{bullets}", ""]

    L.append(r"\end{document}")
    return "\n".join(L) + "\n"
