"""Locked mode: the selection never changes; overflow reverts edits, never bullets.
LaTeX is mocked, so these run without MiKTeX/XeLaTeX or network."""
import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import guard  # noqa: E402
import render  # noqa: E402
import tailor  # noqa: E402

DATA = yaml.safe_load((ROOT / "resume_data.yaml").read_text(encoding="utf-8"))
DATA.update(email="test.user@example.com", phone="+00 000 000 0000")   # fictional; real ones are in contact.yaml
JD = "Python Django Redis Docker PostgreSQL REST APIs backend developer. " * 6
TEXTS = render._bullet_lookup(DATA)


def base_plan():
    return render.default_plan(DATA)


def bullet_set(plan):
    return {b for e in plan["experience"] + plan["projects"] for b in e["bullets"]}


def raw_from_base():
    b = base_plan()
    return {"experience": copy.deepcopy(b["experience"]), "projects": copy.deepcopy(b["projects"]),
            "skills": copy.deepcopy(b["skills"]), "edits": {}}


def extra_bullet():
    """A real bullet from the bank that is NOT in the base resume."""
    base_ids = bullet_set(base_plan())
    for sec in ("experience", "projects"):
        for e in DATA[sec]:
            for b in e["bullets"]:
                if b["id"] not in base_ids:
                    return b["id"]
    raise AssertionError("bank has no non-base bullet")


def hidden_only_word():
    """A word that appears only in hidden (not-on-the-base-resume) bullets."""
    base_ids = bullet_set(base_plan())
    shown_text, hidden_text = [], []
    for sec in ("experience", "projects"):
        for e in DATA[sec]:
            for b in e["bullets"]:
                (shown_text if b["id"] in base_ids else hidden_text).append(b["text"])
    visible = guard.bank_vocabulary(DATA | {
        "experience": [{**e, "bullets": [b for b in e["bullets"] if b["id"] in base_ids]}
                       for e in DATA["experience"]],
        "projects": [{**e, "bullets": [b for b in e["bullets"] if b["id"] in base_ids]}
                     for e in DATA["projects"]]})
    hidden = set(guard.words(" ".join(hidden_text)))
    candidates = sorted(w for w in hidden - visible if w.isalpha() and len(w) > 4)
    if not candidates:
        raise unittest.SkipTest("no word is unique to hidden bullets")
    return candidates[0]


class LockedGuard(unittest.TestCase):
    def test_edit_cannot_use_a_term_that_only_exists_in_hidden_bullets(self):
        word = hidden_only_word()
        raw = raw_from_base()
        bid = raw["experience"][0]["bullets"][0]
        raw["edits"] = {bid: TEXTS[bid] + " " + word}
        jd = JD + f" Experience with {word.capitalize()} is needed."
        plan, warnings = guard.validate_locked_plan(DATA, base_plan(), raw, jd)
        self.assertNotIn(bid, plan["edits"])
        self.assertTrue(any("visible resume" in w for w in warnings), warnings)

    def test_plan_adding_a_bullet_is_reverted_to_base(self):
        raw = raw_from_base()
        extra = extra_bullet()
        raw["experience"][0]["bullets"].append(extra)
        plan, warnings = guard.validate_locked_plan(DATA, base_plan(), raw, JD)
        self.assertEqual(plan["experience"], base_plan()["experience"])
        self.assertEqual(plan["projects"], base_plan()["projects"])
        self.assertNotIn(extra, bullet_set(plan))
        self.assertTrue(any("permutation" in w for w in warnings))

    def test_plan_dropping_a_bullet_is_reverted_to_base(self):
        raw = raw_from_base()
        raw["experience"][0]["bullets"].pop()
        plan, warnings = guard.validate_locked_plan(DATA, base_plan(), raw, JD)
        self.assertEqual(bullet_set(plan), bullet_set(base_plan()))
        self.assertEqual(plan["experience"][0]["bullets"], base_plan()["experience"][0]["bullets"])
        self.assertTrue(any("permutation" in w for w in warnings))

    def test_duplicate_bullet_is_not_a_permutation(self):
        raw = raw_from_base()
        b = raw["experience"][0]["bullets"]
        b[-1] = b[0]
        plan, _ = guard.validate_locked_plan(DATA, base_plan(), raw, JD)
        self.assertEqual(plan["experience"], base_plan()["experience"])

    def test_dropping_or_adding_an_entry_is_reverted(self):
        raw = raw_from_base()
        raw["projects"] = raw["projects"][:-1]
        raw["experience"].append({"id": "made_up_entry", "bullets": ["z_partner_api"]})
        plan, warnings = guard.validate_locked_plan(DATA, base_plan(), raw, JD)
        self.assertEqual([e["id"] for e in plan["experience"]], [e["id"] for e in base_plan()["experience"]])
        self.assertEqual([e["id"] for e in plan["projects"]], [e["id"] for e in base_plan()["projects"]])
        self.assertTrue(any("made_up_entry" in w for w in warnings))

    def test_exact_permutation_is_accepted(self):
        raw = raw_from_base()
        entry = next(e for e in raw["experience"] if len(e["bullets"]) > 1)
        entry["bullets"].reverse()
        plan, warnings = guard.validate_locked_plan(DATA, base_plan(), raw, JD)
        got = next(e for e in plan["experience"] if e["id"] == entry["id"])
        self.assertEqual(got["bullets"], entry["bullets"])
        self.assertFalse([w for w in warnings if "permutation" in w])

    def test_skills_reorder_ok_but_added_skill_rejected(self):
        raw = raw_from_base()
        label = next(iter(raw["skills"]))
        raw["skills"][label] = list(reversed(raw["skills"][label]))
        plan, _ = guard.validate_locked_plan(DATA, base_plan(), raw, JD)
        self.assertEqual(plan["skills"][label], raw["skills"][label])
        raw["skills"][label] = raw["skills"][label] + ["Kubernetes"]
        plan, warnings = guard.validate_locked_plan(DATA, base_plan(), raw, JD)
        self.assertEqual(plan["skills"][label], base_plan()["skills"][label])
        self.assertTrue(any("reorder only" in w for w in warnings))


class LockedEdits(unittest.TestCase):
    """Keyword-edit rules in locked mode (guard.check_edit_locked)."""
    JD_EDITS = ("We build multi-tenant backends. Experience with Kubernetes is needed. "
                "Good knowledge of PostgreSQL is a must. Proficient with Python and Django. ") * 2

    def run_edit(self, bid, edited, jd=None):
        raw = raw_from_base()
        raw["edits"] = {bid: edited}
        plan, warnings = guard.validate_locked_plan(DATA, base_plan(), raw, jd or self.JD_EDITS)
        return plan, warnings

    def first_bullet(self):
        return raw_from_base()["experience"][0]["bullets"][0]

    def test_technology_not_on_resume_is_rejected(self):
        bid = self.first_bullet()
        plan, warnings = self.run_edit(bid, TEXTS[bid] + " using Kubernetes")
        self.assertNotIn(bid, plan["edits"])
        self.assertTrue(any("visible resume" in w and "kubernetes" in w for w in warnings), warnings)

    def test_technology_on_resume_skills_is_accepted(self):
        self.assertIn("postgresql", guard.bank_vocabulary(DATA))
        bid = self.first_bullet()
        plan, warnings = self.run_edit(bid, TEXTS[bid] + " using PostgreSQL")
        self.assertIn(bid, plan["edits"], warnings)

    def test_descriptive_lowercase_jd_word_needs_no_backing(self):
        bid = self.first_bullet()
        plan, warnings = self.run_edit(bid, TEXTS[bid] + " for multi-tenant")
        self.assertIn(bid, plan["edits"], warnings)

    def test_up_to_three_non_jd_linking_words_ok_fourth_rejected(self):
        bid = self.first_bullet()
        plan, warnings = self.run_edit(bid, TEXTS[bid] + " also while which")
        self.assertIn(bid, plan["edits"], warnings)
        plan, warnings = self.run_edit(bid, TEXTS[bid] + " also while which that")
        self.assertNotIn(bid, plan["edits"])
        self.assertTrue(any("more than 3" in w for w in warnings), warnings)

    def test_non_jd_word_that_is_not_a_linking_word_is_rejected(self):
        bid = self.first_bullet()
        plan, warnings = self.run_edit(bid, TEXTS[bid] + " with terraform")   # not in the JD
        self.assertNotIn(bid, plan["edits"])
        self.assertTrue(any("not linking words" in w for w in warnings), warnings)

    def test_changed_number_is_rejected(self):
        bid = next(b for e in raw_from_base()["experience"] for b in e["bullets"] if "80,000+" in TEXTS[b])
        plan, warnings = self.run_edit(bid, TEXTS[bid].replace("80,000+", "90,000+"))
        self.assertNotIn(bid, plan["edits"])
        self.assertTrue(any("numbers changed" in w for w in warnings), warnings)

    def test_tech_term_detection(self):
        terms = guard.jd_tech_terms("Strong Python skills. Proficient with Django and Node.js, C++ and "
                                    "Web3. Build multi-tenant apps.\nTesting is a plus.")
        self.assertTrue({"python", "django", "node.js", "c++", "web3"} <= terms)
        self.assertNotIn("strong", terms)          # sentence start
        self.assertNotIn("multi-tenant", terms)    # lowercase descriptive
        self.assertNotIn("testing", terms)         # line start counts as sentence start

    def test_known_technologies_are_technologies_whatever_their_capitalisation(self):
        terms = guard.jd_tech_terms("we use terraform and grpc, with postgres and k8s.\nDocker is a plus.")
        for w in ("terraform", "grpc", "postgres", "k8s", "docker"):   # lowercase, or at a line start
            self.assertIn(w, terms, w)
        self.assertNotIn("we", terms)
        self.assertNotIn("use", terms)

    def test_everyday_words_are_not_technologies(self):
        terms = guard.jd_tech_terms("we go fast with swift decisions, express ideas and keep the flow. "
                                    "Great chef of ownership.")
        for w in ("go", "swift", "express", "flow", "chef", "ideas"):
            self.assertNotIn(w, terms, w)

    def test_aliases(self):
        self.assertEqual(guard.canon("postgres"), "postgresql")
        self.assertEqual(guard.canon("K8S"), "kubernetes")
        self.assertEqual(guard.canon("python"), "python")
        for alias in ("postgres", "k8s", "mongo", "golang", "node", "reactjs"):
            self.assertTrue(guard.is_known_tech(alias), alias)

    def test_lowercase_jd_tools_not_on_the_resume_are_rejected(self):
        vocab = guard.bank_vocabulary(DATA)
        for tool in ("terraform", "grpc", "kubernetes"):
            self.assertNotIn(tool, vocab, f"{tool} is on the resume - pick another tool for this test")
        jd = ("we use terraform and grpc in production, with postgres and k8s on the platform. "
              "Build multi-tenant apps. " * 2)
        bid = self.first_bullet()
        for tool in ("terraform", "grpc"):
            plan, warnings = self.run_edit(bid, TEXTS[bid] + f" with {tool}", jd=jd)
            self.assertNotIn(bid, plan["edits"], tool)
            self.assertTrue(any("technology terms not on your visible resume" in w for w in warnings),
                            (tool, warnings))
        # "k8s" has a digit, so the number-lock also rejects it: rejected either way
        plan, _ = self.run_edit(bid, TEXTS[bid] + " with k8s", jd=jd)
        self.assertNotIn(bid, plan["edits"])
        ok, reason = guard.check_edit_locked("Built APIs.", "Built APIs with kubernetes.",
                                             {"kubernetes"}, guard.jd_tech_terms("kubernetes"), {"apis", "built"})
        self.assertFalse(ok)
        self.assertIn("technology terms", reason)

    def test_lowercase_postgres_is_accepted_because_postgresql_is_on_the_resume(self):
        self.assertIn("postgresql", guard.bank_vocabulary(DATA))
        jd = "we use terraform and grpc, with postgres in production. " * 3
        bid = self.first_bullet()
        plan, warnings = self.run_edit(bid, TEXTS[bid] + " with postgres", jd=jd)
        self.assertIn(bid, plan["edits"], warnings)

    def test_a_known_tech_absent_from_the_jd_is_still_rejected(self):
        bid = self.first_bullet()
        plan, warnings = self.run_edit(bid, TEXTS[bid] + " with terraform")      # JD_EDITS has no terraform
        self.assertNotIn(bid, plan["edits"])
        self.assertTrue(any("not linking words" in w for w in warnings), warnings)

    def test_selection_mode_check_edit_is_unchanged(self):
        ok, reason = guard.check_edit("Built APIs using Django.", "Built RESTful APIs using Django.",
                                      {"restful", "apis"}, {"restful", "apis", "django", "built", "using"})
        self.assertTrue(ok, reason)
        ok, _ = guard.check_edit("Built APIs.", "Built APIs also.", {"apis"}, {"apis", "also"})
        self.assertFalse(ok)       # selection mode still requires JD backing for every added word


def fake_compile(markers):
    """compile_pdf stand-in: 2 pages while any marker string is in the tex, else 1."""
    def _compile(tex, build_dir):
        return Path("fake.pdf"), 2 if any(m in tex for m in markers) else 1
    return _compile


def plan_with_edits(suffixes):
    """A locked plan whose first len(suffixes) bullets carry a (fake, pre-validated) edit."""
    plan = guard.validate_locked_plan(DATA, base_plan(), raw_from_base(), JD)[0]
    ids = [b for e in plan["experience"] for b in e["bullets"]][:len(suffixes)]
    plan["edits"], plan["edit_log"] = {}, []
    for bid, suffix in zip(ids, suffixes):
        text = TEXTS[bid] + suffix
        plan["edits"][bid] = text
        plan["edit_log"].append({"id": bid, "status": "applied", "before": TEXTS[bid], "after": text})
    return plan, ids


class LockedFit(unittest.TestCase):
    def test_overflow_reverts_only_the_offending_edit_and_never_bullets(self):
        plan, (small, big) = plan_with_edits([" SMALLMARK", " BIGMARK " + "x" * 200])
        with mock.patch.object(tailor, "compile_pdf", fake_compile(["BIGMARK"])):
            _, _, reverted = tailor.fit_locked(DATA, plan, Path("unused"))
        self.assertEqual(len(reverted), 1)
        self.assertIn(big, reverted[0])
        self.assertNotIn(big, plan["edits"])
        self.assertIn(small, plan["edits"])            # the smaller edit fits, so it stays
        self.assertEqual(bullet_set(plan), bullet_set(base_plan()))

    def test_overflow_reverts_edits_one_at_a_time_largest_first(self):
        plan, ids = plan_with_edits([" AAMARK", " BBMARK xx", " CCMARK xxxx"])
        with mock.patch.object(tailor, "compile_pdf", fake_compile(["AAMARK", "BBMARK", "CCMARK"])):
            _, _, reverted = tailor.fit_locked(DATA, plan, Path("unused"))
        self.assertEqual(plan["edits"], {})
        self.assertEqual([r.split(":")[0] for r in reverted], [ids[2], ids[1], ids[0]])
        self.assertEqual(bullet_set(plan), bullet_set(base_plan()))
        self.assertTrue(all(e["status"] == "reverted" for e in plan["edit_log"]))

    def test_no_edits_left_resets_order_then_errors_if_base_overflows(self):
        plan, _ = plan_with_edits([])
        next(e for e in plan["experience"] if len(e["bullets"]) > 1)["bullets"].reverse()
        base_tex = render.render_tex(DATA, base_plan())
        with mock.patch.object(tailor, "compile_pdf",
                               lambda tex, b: (Path("f.pdf"), 1 if tex == base_tex else 2)):
            _, _, reverted = tailor.fit_locked(DATA, plan, Path("unused"))
        self.assertIn("base order", reverted[0])
        self.assertEqual(plan["experience"], base_plan()["experience"])

        plan, _ = plan_with_edits([])
        with mock.patch.object(tailor, "compile_pdf", lambda tex, b: (Path("f.pdf"), 2)):
            with self.assertRaises(tailor.TailorError):
                tailor.fit_locked(DATA, plan, Path("unused"))
        self.assertEqual(bullet_set(plan), bullet_set(base_plan()))


class LockedEndToEnd(unittest.TestCase):
    def test_tailor_job_reverts_a_selection_plan_to_the_base_bullets(self):
        raw = raw_from_base()
        raw["experience"][0]["bullets"] = [extra_bullet()]      # tries to replace the bullets
        raw["projects"] = []                                     # tries to drop the projects
        raw.update({"scores": {"overall": 7}, "gaps": [], "jd_keywords_matched": []})
        with tempfile.TemporaryDirectory() as tmp:
            plan_file = Path(tmp) / "raw.json"
            plan_file.write_text(json.dumps(raw), encoding="utf-8")
            with mock.patch.object(tailor, "compile_pdf", lambda t, b: (Path(tmp) / "r.pdf", 1)), \
                    mock.patch.object(tailor, "verified_keywords", lambda r, p: []), \
                    mock.patch.object(tailor.shutil, "copy2", lambda *a, **k: None):
                result = tailor.tailor_job("Acme", "Backend", JD, plan_file=str(plan_file),
                                           output_dir=Path(tmp) / "out")
            final = json.loads((Path(result["job_dir"]) / "plan.json").read_text(encoding="utf-8"))
        self.assertEqual(result["mode"], "locked")
        self.assertEqual(bullet_set(final), bullet_set(base_plan()))
        self.assertEqual([e["id"] for e in final["projects"]], [e["id"] for e in base_plan()["projects"]])


if __name__ == "__main__":
    unittest.main()
