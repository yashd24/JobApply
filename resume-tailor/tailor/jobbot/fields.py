"""M4 field discovery: what does this application form ask?

`discover_fields(page)` reads the live DOM and resolves, for every input / select / textarea / radio group /
checkbox group / file input: its human label, type, whether it is required, and its options. Labels come from
accessible names first (aria-labelledby, aria-label, label[for], fieldset legend) and then from the nearest
text that precedes the control; no site-specific CSS classes are relied on.

`fields_from_questions(job.questions)` builds the same `Field` objects from what intake parsed, so the answer
pipeline can run (and be tested) without a browser.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_JS = r"""
() => {
  const txt = (el) => (el.innerText || el.textContent || '').replace(/\s+/g, ' ').trim();
  const isControl = (el) => el.matches && el.matches('input, select, textarea');
  const hasControl = (el) => isControl(el) || !!el.querySelector('input:not([type=hidden]), select, textarea');
  const optionText = (el) => {
    if (el.id) { const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l) return txt(l); }
    const w = el.closest('label'); if (w) return txt(w);
    const n = el.nextElementSibling; if (n && !hasControl(n)) { const t = txt(n); if (t) return t; }
    return el.value || '';
  };
  const labelOf = (el) => {
    const ids = (el.getAttribute('aria-labelledby') || '').split(/\s+/).filter(Boolean);
    if (ids.length) {
      const t = ids.map(i => document.getElementById(i)).filter(Boolean).map(txt).join(' ').trim();
      if (t) return t;
    }
    const al = el.getAttribute('aria-label'); if (al && al.trim()) return al.trim();
    const isOption = el.type === 'radio' || el.type === 'checkbox';
    if (!isOption && el.id) {
      const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]'); if (l && txt(l)) return txt(l);
    }
    const fs = el.closest('fieldset');
    if (fs) { const lg = fs.querySelector('legend'); if (lg && txt(lg)) return txt(lg); }
    let node = el;
    for (let depth = 0; depth < 8 && node && node !== document.body; depth++, node = node.parentElement) {
      let sib = node.previousElementSibling;
      for (let k = 0; k < 3 && sib; k++, sib = sib.previousElementSibling) {
        if (['SCRIPT', 'STYLE', 'NOSCRIPT'].includes(sib.tagName) || hasControl(sib)) continue;
        const t = txt(sib); if (t) return t;
      }
    }
    if (isOption) { const w = el.closest('label'); if (w) return txt(w); }
    return (el.placeholder || el.name || '').trim();
  };
  const shown = (el) => {
    if (el.type === 'file') return true;                       // file inputs are usually styled away
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && (r.width > 0 || r.height > 0);
  };
  const scope = document.querySelector('form') ? Array.from(document.querySelectorAll('form')) : [document];
  const seen = new Set(), out = [];
  for (const root of scope) for (const el of root.querySelectorAll('input, select, textarea')) {
    if (seen.has(el)) continue; seen.add(el);
    const type = (el.getAttribute('type') || el.tagName).toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'search'].includes(type) || !shown(el)) continue;
    if (/requiredInput/.test((el.className || '').toString())) continue;           // invisible validation helper (the phone box itself is iti__tel-input and stays)
    const rec = {tag: el.tagName.toLowerCase(), type, name: el.name || '', id: el.id || '', label: labelOf(el),
                 role: el.getAttribute('role') || '',
                 required: el.required || el.getAttribute('aria-required') === 'true', placeholder: el.placeholder || '',
                 multiple: !!el.multiple, value: el.value || '', options: []};
    if (el.tagName === 'SELECT') {
      rec.options = Array.from(el.options).filter(o => o.value !== '' && txt(o) !== '' && !/^(select|choose|please)/i.test(txt(o)))
                         .map(o => txt(o));
    } else if (type === 'radio' || type === 'checkbox') {
      rec.optionLabel = optionText(el);
    }
    out.push(rec);
  }
  return out;
}
"""

_STAR = re.compile(r"\s*[*✱]+\s*")


@dataclass
class Field:
    key: str                       # the input's name (or id); how the filler finds it again
    label: str
    type: str                      # text email tel url number date textarea select multiselect radio checkbox file
    required: bool
    options: list[str] = field(default_factory=list)
    group: str = "standard"        # standard | custom | demographic | consent | location
    multiple: bool = False         # checkbox group (several may be ticked) vs a single checkbox
    description: str = ""
    widget: str = ""               # "combobox" = a custom dropdown whose options are read by opening it

    def to_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "type": self.type, "required": self.required,
                "options": self.options, "group": self.group, "widget": self.widget}


def _group_for(name: str) -> str:
    if name.startswith("surveysResponses"):
        return "demographic"
    if name.startswith("consent["):
        return "consent"
    if name.startswith("cards["):
        return "custom"
    return "standard"


def clean_label(raw: str) -> tuple[str, bool]:
    """Strip required-markers from a label; returns (label, had_marker)."""
    marked = bool(re.search(r"[*✱]", raw))
    return re.sub(r"\s+", " ", _STAR.sub(" ", raw)).strip(), marked


def discover_fields(page) -> list[Field]:
    """Fields of the application form currently open in `page`."""
    raw = page.evaluate(_JS)
    fields: list[Field] = []
    by_group: dict[tuple[str, str], Field] = {}
    for i, r in enumerate(raw):
        label, marked = clean_label(r["label"])
        required = bool(r["required"]) or marked
        key = r["name"] or r["id"] or f"field{i}"
        t = r["type"]
        if t in ("radio", "checkbox"):
            gk = (t, key)
            f = by_group.get(gk)
            if f is None:
                f = by_group[gk] = Field(key=key, label=label, type=t, required=required,
                                         group=_group_for(r["name"]))
                fields.append(f)
            else:
                f.required = f.required or required
                f.multiple = True
            f.options.append(clean_label(r.get("optionLabel") or r["value"])[0])
            if t == "checkbox" and len(f.options) > 1:
                f.multiple = True
            if not f.label:
                f.label = label
            continue
        if r.get("role") == "combobox":              # a custom dropdown (React-Select): options appear when opened
            fields.append(Field(key=key, label=label, type="multiselect" if key.endswith("[]") else "select",
                                required=required, options=[], group=_group_for(r["name"]), widget="combobox"))
            continue
        if r["tag"] == "select":
            t = "multiselect" if r["multiple"] else "select"
        elif r["tag"] == "textarea":
            t = "textarea"
        elif t in ("tel", "email", "url", "number", "date", "file", "password"):
            pass
        else:
            t = "text"
        fields.append(Field(key=key, label=label, type=t, required=required, options=r["options"],
                            group=_group_for(r["name"])))
    return fields


_Q_TYPES = {"text": "text", "textarea": "textarea", "select": "select", "multiselect": "multiselect",
            "file": "file", "email": "email", "radio": "radio", "checkbox": "checkbox", "hidden": "hidden"}


def fields_from_questions(questions) -> list[Field]:
    """Field objects built from intake `Question`s (Greenhouse/Lever), for offline use and tests."""
    out: list[Field] = []
    for q in questions:
        usable = [f for f in q.fields if f.type != "hidden"]
        if not usable:
            continue
        f0 = usable[0]
        out.append(Field(key=f0.name, label=q.label, type=_Q_TYPES.get(f0.type, f0.type), required=q.required,
                         options=[o for f in usable for o in f.options], group=q.group,
                         multiple=f0.type in ("checkbox", "multiselect") and len(f0.options) > 1,
                         description=q.description))
    return out
