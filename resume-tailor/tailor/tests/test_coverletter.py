"""M4 cover letters: generation, the code-enforced guard, regenerate-once, and the one-page PDF."""
import json
import shutil
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("JOBBOT_NO_SHEET", "1")      # a test must never reach the real Google Sheet
sys.path.insert(0, str(ROOT / "tests"))

import persona  # noqa: E402
from jobbot import answers as A  # noqa: E402
from jobbot import coverletter as C  # noqa: E402
from jobbot.fields import Field  # noqa: E402

RESUME = persona.resume()
CFG = C.load_config()
JD = "We need a backend engineer to build reliable payment APIs in Python with PostgreSQL. " * 3

P1 = ("I'm applying to join Acme as a Backend Engineer. The role asks for reliable payment APIs, and that is the work I do now at Acme Test Co, where small, careful changes matter more than big rewrites.")
P2 = ("Your job description asks for reliable Python services. At Acme Test Co I built a payments API serving "
      "50,000+ requests per week using Python and PostgreSQL. I also cut a nightly report from 4 hours to "
      "20 minutes with Celery workers, so I know how to find the slow step and fix it without breaking "
      "anything around it.")
P3 = ("Above all, I'd be glad to talk more about how I could help the team. I can share more detail about either project "
      "whenever it is useful. I am also happy to answer any questions by email or on a short call, at a time "
      "that suits you. I would also be glad to explain how the payments API was monitored and how the nightly report was split into smaller jobs. Thank you for your time and for reading my application.")
GOOD = "\n\n".join([P1, P2, P3])


QUOTES = ["reliable payment APIs in Python", "backend engineer to build"]      # verbatim from JD above


def reply(text, quotes=None):
    return {"letter": text, "jd_quotes": QUOTES if quotes is None else quotes}


class Guard(unittest.TestCase):
    def check(self, text):
        return C.check_letter(text, RESUME, CFG)

    def test_the_good_letter_passes(self):
        self.assertTrue(150 <= len(C.words(GOOD)) <= 250, len(C.words(GOOD)))
        self.assertEqual(self.check(GOOD), [])

    def assertFails(self, text, fragment):
        problems = self.check(text)
        self.assertTrue(any(fragment in p for p in problems), (fragment, problems))

    def test_length_and_paragraphs(self):
        self.assertFails(P1 + "\n\n" + P3, "paragraphs")
        self.assertFails(GOOD.replace("\n\n", "\n"), "paragraphs")
        self.assertFails("\n\n".join(["Short.", "Also short.", "Thanks."]), "words")
        self.assertFails(GOOD + "\n\n" + " ".join(["filler"] * 80), "words")

    def test_banned_phrases_including_curly_apostrophes(self):
        for phrase in ("I am writing to express my interest", "I’m passionate about APIs", "I am thrilled to apply",
                       "I hope you’re doing well", "I will leverage my skills", "a dynamic team", "a great fit"):
            self.assertFails(P1 + " " + phrase + ".\n\n" + P2 + "\n\n" + P3, "banned phrase")

    def test_numbers_must_exist_on_the_resume(self):
        bad = GOOD.replace("50,000+", "900,000+")
        self.assertFails(bad, "numbers that are not on the visible resume")
        self.assertFails(GOOD.replace("Thank you", "After 7 years, thank you"), "numbers")

    def test_technologies_must_be_on_the_visible_resume(self):
        self.assertFails(GOOD.replace("Celery workers", "Kubernetes clusters"), "technologies")
        self.assertFails(GOOD.replace("Celery workers", "Terraform modules"), "Terraform".lower())   # hidden bullet
        self.assertFails(GOOD.replace("PostgreSQL.", "MySQL."), "mysql")                  # a tech at a sentence end
        self.assertEqual(self.check(GOOD.replace("PostgreSQL", "Postgres")), [])           # alias of a visible skill

    def test_sensitive_topics_placeholders_and_markdown(self):
        self.assertFails(GOOD.replace("Thank you", "My salary expectation is flexible. Thank you"), "sensitive")
        self.assertFails(GOOD.replace("Acme", "[Company]"), "placeholder")
        self.assertFails(GOOD.replace("Above all, I'd be glad to talk", "- Above all, I'd be glad to talk"), "markdown")
        self.assertFails(GOOD.replace("helpful", "**helpful**").replace("useful", "**useful**"), "markdown")


class NewGuards(unittest.TestCase):
    """Added after reading real generated letters: claims the resume does not make, repetition, JD anchoring."""

    def check(self, text, quotes=None):
        return C.check_letter(text, RESUME, CFG, JD, QUOTES if quotes is None else quotes)

    def assertFails(self, problems, fragment):
        self.assertTrue(any(fragment in p for p in problems), (fragment, problems))

    def test_the_good_letter_passes_with_quotes(self):
        self.assertEqual(self.check(GOOD), [])

    def test_personal_claims_lessons_and_unstated_outcomes_are_rejected(self):
        for claim in ("This taught me to care about reliability.", "I enjoy work like this.", "I like to chase root causes.",
                      "I learned a lot there.", "It made debugging much faster.", "I am someone who ships carefully."):
            self.assertFails(self.check(GOOD.replace("where small, careful changes matter more than big rewrites.", claim)),
                             "claim about the candidate")

    def test_company_statements_must_be_anchored_by_verbatim_jd_quotes(self):
        self.assertFails(self.check(GOOD, quotes=[]), "fewer than 2")
        self.assertFails(self.check(GOOD, quotes=["reliable payment APIs in Python"]), "fewer than 2")
        fake = self.check(GOOD, quotes=["reliable payment APIs in Python", "real-time access to their earnings"])
        self.assertFails(fake, "not in the job description")
        unused = self.check(GOOD.replace("Backend Engineer", "role"), quotes=["reliable payment APIs in Python",
                                                                             "need a backend engineer"])
        self.assertFails(unused, "fewer than 2")                    # an unused quote does not count towards the two
        spare = self.check(GOOD, quotes=["reliable payment APIs in Python", "backend engineer to build", "zebra crossing"])
        self.assertFails(spare, "not in the job description")       # a quote that is not in the JD is still an error
        self.assertEqual(self.check(GOOD, quotes=["reliable payment APIs in Python", "backend engineer to build",
                                                  "build reliable payment APIs in Python with PostgreSQL"]), [])

    def test_the_letter_may_not_contain_quotation_marks(self):
        for q in ('"reliable payment APIs in Python"', "“reliable payment APIs in Python”"):
            quoted = GOOD.replace("reliable payment APIs", q, 1)
            self.assertFails(self.check(quoted), "quotation marks")
        self.assertEqual(self.check(GOOD.replace("I would like", "I'd like", 1)), [])         # apostrophes are fine

    def test_a_paraphrase_is_enough_when_it_keeps_the_quotes_key_nouns(self):
        text = GOOD.replace("reliable payment APIs", "dependable payment APIs", 1)
        self.assertEqual(self.check(text, quotes=["reliable payment APIs in Python", "backend engineer to build"]), [])

    def test_no_more_than_two_achievements_with_numbers(self):
        self.assertEqual(len([x for x in GOOD.split(". ") if any(c.isdigit() for c in x)]), 2)
        three = GOOD.replace("anything around it.", "anything around it. It ran for 20 minutes each night.")
        self.assertFails(self.check(three), "describe at most 2 achievements")
        self.assertEqual(C.check_letter(three, RESUME, {**CFG, "achievements": {"max_sentences_with_numbers": 3}}), [])

    def test_digits_in_the_role_name_are_not_an_achievement(self):
        text = GOOD.replace("as a Backend Engineer.", "as a Software Engineer 2.", 1)
        self.assertFails(self.check(text), "describe at most 2 achievements")
        self.assertEqual(C.check_letter(text, RESUME, CFG, names="Acme Software Engineer 2"), [])

    def test_the_interview_closing_is_banned_and_the_prompt_asks_for_a_simple_one(self):
        bad = GOOD.replace("Above all, I'd be glad to talk more about how I could help the team.",
                           "In an interview, I would like to discuss how the team works.")
        self.assertFails(self.check(bad), "banned phrase")
        prompt = C.build_prompt("Acme", "Backend", JD, RESUME, CFG, ["Hi,\n\nHello."], closing=CFG["closing_styles"][0])
        self.assertIn("NEVER put words in quotation marks", prompt)
        self.assertIn("ONE or TWO achievements", prompt)
        self.assertIn("simple, polite close", prompt)
        self.assertFalse(any("interview" in x.lower() for x in CFG["closing_styles"]))

    def test_quotes_ignore_case_and_spacing(self):
        q = ["Reliable   payment APIs in PYTHON", "BACKEND engineer to build"]
        self.assertEqual(self.check(GOOD, quotes=q), [])

    def test_full_letter_has_one_sign_off(self):
        full = C.full_letter(RESUME, GOOD)
        self.assertEqual(full.splitlines()[0], "Hello,")
        self.assertEqual(full.splitlines()[-2:], ["Best regards,", "Test User"])
        self.assertEqual(full.count("Best regards,"), 1)


class Closings(unittest.TestCase):
    """The user asked for a different closing in each letter."""

    def test_styles_exist_in_the_config(self):
        self.assertGreaterEqual(len(CFG["closing_styles"]), 6)
        self.assertEqual(len(set(CFG["closing_styles"])), len(CFG["closing_styles"]))

    def test_choice_is_deterministic_and_skips_recent_styles(self):
        a = C.choose_closing(CFG, "Acme", "Backend", [])
        self.assertEqual(a, C.choose_closing(CFG, "Acme", "Backend", []))
        self.assertIn(a, CFG["closing_styles"])
        used = []
        for company in ("A", "B", "C", "D", "E", "F"):
            nxt = C.choose_closing(CFG, company, "Backend", used)
            self.assertNotIn(nxt, used[-5:])                                  # never one of the last five
            used.append(nxt)
        self.assertGreaterEqual(len(set(used)), 5)

    def test_prompt_carries_the_chosen_style_and_no_longer_forces_a_fixed_ending(self):
        style = CFG["closing_styles"][2]
        p = C.build_prompt("Acme", "Backend", JD, RESUME, CFG, ["Hi,\n\nHello."], closing=style)
        self.assertIn("HOW TO END", p)
        self.assertIn(style, p)

    def test_the_guard_rejects_a_closing_sentence_used_before(self):
        prev = [C.closing_line(GOOD)]
        problems = C.check_letter(GOOD, RESUME, CFG, JD, QUOTES, prev)
        self.assertTrue(any("closing sentence is identical" in x for x in problems), problems)
        self.assertEqual(C.check_letter(GOOD, RESUME, CFG, JD, QUOTES, ["Something else entirely."]), [])
        self.assertEqual(C.last_sentence("One.\n\nTwo. Three here."), "Three here.")

    def test_a_bare_thank_you_is_not_what_makes_a_closing_repeat(self):
        """Regression (project44, 2026-10-02): the optional cover letter was left empty because both attempts ended
        with "Thank you ..." and earlier letters had too. Only the sentence before the thanks has to differ."""
        self.assertEqual(C.closing_line(GOOD), "I would also be glad to explain how the payments API was monitored and how the "
                         "nightly report was split into smaller jobs.")
        self.assertEqual(C.closing_line("One.\n\nTwo here. Thank you."), "Two here.")
        self.assertEqual(C.closing_line("One.\n\nThank you for reading my application."),
                         "Thank you for reading my application.")                       # only thanks: falls back
        history = ["Thank you.", "Thank you for reading my application.", "Thank you for your time."]
        self.assertEqual(C.check_letter(GOOD, RESUME, CFG, JD, QUOTES, history), [])

    def test_the_same_closing_sentence_before_the_thanks_is_still_rejected(self):
        again = GOOD.replace("Thank you for your time and for reading my application.", "Thank you.")
        problems = C.check_letter(again, RESUME, CFG, JD, QUOTES, [C.closing_line(GOOD)])
        self.assertTrue(any("closing sentence is identical" in x for x in problems), problems)

    def test_digits_in_the_company_or_role_name_are_not_unbacked_numbers(self):
        text = GOOD.replace("join Acme as", "join project44 as")
        self.assertTrue(any("numbers that are not" in x for x in C.check_letter(text, RESUME, CFG)))
        self.assertEqual(C.check_letter(text, RESUME, CFG, JD, QUOTES, names="project44 Software Engineer 2"), [])
        invented = text.replace("50,000+", "90,000+")                                  # a real claim is still caught
        self.assertTrue(any("90" in x for x in C.check_letter(invented, RESUME, CFG, names="project44")))

    def test_an_optional_cover_letter_is_written_even_when_every_earlier_letter_ended_with_thanks(self):
        with tempfile.TemporaryDirectory() as tmp:
            hist = Path(tmp) / "h.json"
            hist.write_text(json.dumps([{"company": c, "style": "s%d" % i, "closing": t} for i, (c, t) in enumerate(
                [("A", "Thank you."), ("B", "Thank you for reading my application."),
                 ("C", "Thank you for your time.")])]), encoding="utf-8")
            text = GOOD.replace("Thank you for your time and for reading my application.", "Thank you.")
            prov = C.make_provider(Path(tmp) / "job", "project44", "Software Engineer 2", JD, RESUME,
                                   lambda prompt: reply(text.replace("join Acme as", "join project44 as")), cfg=CFG,
                                   samples=["Hi,\n\nHello."], history_path=hist)
            value, note = prov(Field("cover_letter", "Cover Letter", "textarea", False))
            self.assertIsNotNone(value, note)
            self.assertIn("project44", value)

    def test_overused_template_closings_are_banned(self):
        for phrase in ("Thank you for your time and consideration.", "how this experience could help your team"):
            bad = GOOD.replace("Thank you for your time and for reading my application.", phrase)
            self.assertTrue(any("banned phrase" in x for x in C.check_letter(bad, RESUME, CFG)), phrase)

    def test_provider_remembers_closings_so_the_next_letter_differs(self):
        with tempfile.TemporaryDirectory() as tmp:
            hist = Path(tmp) / "h.json"
            prompts = []

            def llm(prompt):
                prompts.append(prompt)
                return reply(GOOD.replace("Thank you for your time and for reading my application.",
                                          "Thank you for reading, " + ("first" if len(prompts) == 1 else "second") + " letter."))
            f = Field("cl", "Cover Letter", "textarea", False)
            for company in ("Acme", "Beta"):
                prov = C.make_provider(Path(tmp) / company, company, "Backend", JD, RESUME, llm, cfg=CFG,
                                       samples=["Hi,\n\nHello."], history_path=hist)
                self.assertIsNotNone(prov(f)[0])
            saved = json.loads(hist.read_text(encoding="utf-8"))
            self.assertEqual([h["company"] for h in saved], ["Acme", "Beta"])
            self.assertNotEqual(saved[0]["style"], saved[1]["style"])
            self.assertNotEqual(saved[0]["closing"], saved[1]["closing"])
            self.assertIn(saved[0]["style"], prompts[0])
            self.assertIn(saved[1]["style"], prompts[1])
            self.assertNotIn(saved[0]["style"], prompts[1])                  # the second prompt skips the first style


class Generation(unittest.TestCase):
    def gen(self, replies):
        prompts, it = [], iter(replies)

        def llm(prompt):
            prompts.append(prompt)
            r = next(it)
            if isinstance(r, Exception):
                raise r
            return r
        res = C.generate("Acme", "Backend Engineer", JD, RESUME, CFG, ["Hi Sam,\n\nI would love to apply.\n\nThank you!"], llm)
        return res, prompts

    def test_first_attempt_ok(self):
        res, prompts = self.gen([reply(GOOD)])
        self.assertEqual((res.ok, res.text, len(prompts)), (True, GOOD, 1))

    def test_a_failing_letter_is_regenerated_once_with_the_reasons(self):
        res, prompts = self.gen([reply(GOOD.replace("50,000+", "900,000+")), reply(GOOD)])
        self.assertTrue(res.ok)
        self.assertEqual(len(prompts), 2)
        self.assertIn("PREVIOUS ATTEMPT WAS REJECTED", prompts[1])
        self.assertIn("numbers that are not on the visible resume", prompts[1])
        self.assertNotIn("PREVIOUS ATTEMPT", prompts[0])
        self.assertEqual([bool(a["problems"]) for a in res.attempts], [True, False])

    def test_two_failures_produce_no_letter_and_exactly_two_calls(self):
        res, prompts = self.gen([reply("too short"), reply("still too short"), reply(GOOD)])
        self.assertEqual((res.ok, res.text, len(prompts)), (False, None, 2))        # the third reply is never requested
        self.assertTrue(res.problems)

    def test_llm_failure_means_no_letter(self):
        res, _ = self.gen([RuntimeError("usage limit reached")])
        self.assertEqual((res.ok, res.text), (False, None))
        self.assertIn("LLM call failed", res.problems[0])

    def test_prompt_uses_samples_for_tone_only_and_only_visible_resume_facts(self):
        _, prompts = self.gen([reply(GOOD)])
        p = prompts[0]
        self.assertIn("TONE ONLY", p)
        self.assertIn("NO\nfacts", p.replace("NO facts", "NO\nfacts"))
        self.assertIn("I would love to apply.", p)                     # the sample text is there
        self.assertIn("payments API", p)
        self.assertNotIn("Kubernetes", p)                               # the hidden bullet a3
        self.assertIn("Never use these phrases", p)
        for phrase in ("passionate", "thrilled", "great fit", "dynamic team"):
            self.assertIn(phrase, p)
        self.assertNotIn("test.user@example.com", p)                   # no contact details, no profile at all


class Samples(unittest.TestCase):
    def test_falls_back_to_the_committed_example(self):
        with tempfile.TemporaryDirectory() as tmp:
            samples, which = C.load_samples(Path(tmp) / "missing.txt")
        self.assertEqual(which, "writing_samples.example.txt")
        self.assertGreaterEqual(len(samples), 2)

    def test_reads_a_private_file_and_ignores_comments(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "s.txt"
            f.write_text("# note\n=== Sample 1 (x) ===\nHi A,\n\nHello there.\n\n=== Sample 2 ===\nHi B,\n", encoding="utf-8")
            samples, which = C.load_samples(f)
        self.assertEqual((which, len(samples)), ("s.txt", 2))
        self.assertNotIn("note", samples[0])


class Provider(unittest.TestCase):
    def provider(self, replies, tmp, renderer=None, **kw):
        it = iter(replies)
        calls = []

        def llm(prompt):
            calls.append(prompt)
            return next(it)
        kwargs = dict(cfg=CFG, samples=["Hi,\n\nI would love to apply.\n\nThank you!"],
                      history_path=Path(tmp) / "closings.json")          # never the real history file
        if renderer:
            kwargs["renderer"] = renderer
        kwargs.update(kw)
        return C.make_provider(Path(tmp) / "job", "Acme", "Backend Engineer", JD, RESUME, llm, **kwargs), calls

    def test_text_field_gets_the_text_and_everything_is_saved(self):
        with tempfile.TemporaryDirectory(prefix="cover test ") as tmp:
            prov, calls = self.provider([reply(GOOD)], tmp)
            value, note = prov(Field("cl", "Cover Letter", "textarea", False))
            self.assertEqual(value, C.full_letter(RESUME, GOOD))                     # greeting + body + one sign-off
            self.assertTrue(value.startswith("Hello,\n\n") and value.endswith("Best regards,\nTest User"))
            self.assertEqual(value.lower().count("thank you,"), 0)                    # no second thank-you line
            self.assertIn("guard passed", note)
            value2, _ = prov(Field("cl2", "Cover letter (text)", "textarea", False))
            self.assertEqual((value2, len(calls)), (value, 1))                      # one letter per job
            job = Path(tmp) / "job"
            self.assertEqual((job / "cover_letter.txt").read_text(encoding="utf-8").strip(), value)
            attempts = json.loads((job / "cover_letter_attempts.json").read_text(encoding="utf-8"))
            self.assertEqual(len(attempts), 1)

    def test_file_field_gets_the_pdf_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            made = []

            def renderer(resume, body, build, out):
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(b"%PDF-fake")
                made.append(body)
                return out
            prov, _ = self.provider([reply(GOOD)], tmp, renderer)
            value, _ = prov(Field("cl", "Cover Letter", "file", True))
            self.assertEqual(Path(value).name, "cover_letter.pdf")
            self.assertEqual(made, [GOOD])

    def test_failed_letter_returns_none_with_reasons_and_saves_the_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            prov, calls = self.provider([reply("bad"), reply("worse")], tmp)
            value, note = prov(Field("cl", "Cover Letter", "textarea", True))
            self.assertIsNone(value)
            self.assertIn("words", note)
            self.assertEqual(len(calls), 2)
            attempts = json.loads((Path(tmp) / "job" / "cover_letter_attempts.json").read_text(encoding="utf-8"))
            self.assertEqual(len(attempts), 2)
            self.assertFalse((Path(tmp) / "job" / "cover_letter.txt").exists())

    def test_pdf_failure_is_reported_not_raised(self):
        def boom(*a):
            raise RuntimeError("xelatex missing")
        with tempfile.TemporaryDirectory() as tmp:
            prov, _ = self.provider([reply(GOOD)], tmp, boom)
            value, note = prov(Field("cl", "Cover Letter", "file", False))
            self.assertIsNone(value)
            self.assertIn("PDF render failed", note)

    def test_required_failure_switches_to_assist_and_optional_stays_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            prov, _ = self.provider([reply("bad"), reply("worse")], tmp)
            ctx = A.Context(profile=persona.profile(), resume=RESUME, today=persona.TODAY, cover_letter=prov)
            req = A.answer_fields([Field("cl", "Cover Letter", "textarea", True)], ctx)[0]
            self.assertEqual((req.status, req.value), (A.FLAGGED, None))
            self.assertIn("assist", req.note)
            opt = A.answer_fields([Field("cl2", "Cover Letter", "textarea", False)], ctx)[0]
            self.assertEqual((opt.status, opt.value), (A.BLANK, None))


@unittest.skipUnless(shutil.which("xelatex"), "XeLaTeX not on PATH")
class Pdf(unittest.TestCase):
    def test_renders_one_page_with_the_resume_fonts(self):
        from pypdf import PdfReader
        with tempfile.TemporaryDirectory(prefix="cover pdf ") as tmp:
            out = C.render_pdf(RESUME, GOOD.replace("Acme Test Co", "Acme & Sons_100%"), Path(tmp) / "b", Path(tmp) / "o" / "l.pdf")
            r = PdfReader(str(out))
            text = " ".join(p.extract_text() for p in r.pages)
            fonts = {str(f.get("/BaseFont", "")) for p in r.pages for f in (p["/Resources"].get("/Font", {}) or {}).values()
                     for f in [f.get_object()]}
        self.assertEqual(len(r.pages), 1)
        squashed = "".join(text.split())                             # small caps kern as "T est User"
        self.assertIn("TestUser", squashed)
        self.assertIn("Hello,", text)
        self.assertIn("Bestregards,", squashed)
        self.assertNotIn("Thank you,", text)                         # the body carries the one thank-you
        self.assertIn("Acme&Sons_100%", squashed)                    # LaTeX specials survive escaping
        self.assertTrue(any("Lato" in f or "Raleway" in f for f in fonts), fonts)

    def test_latex_escaping(self):
        self.assertEqual(C.tex_escape("50% of A&B_c #1 {x} ~ ^"),
                         r"50\% of A\&B\_c \#1 \{x\} \textasciitilde{} \textasciicircum{}")


if __name__ == "__main__":
    unittest.main()
