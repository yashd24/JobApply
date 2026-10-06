"""Cover-letter style rules added 2026-10-02: no repeated openers, no announced links, no broadening of the JD,
and the user's own letter as the main style model (never copied)."""
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
from jobbot import coverletter as C  # noqa: E402
from test_coverletter import CFG, GOOD, JD, QUOTES, RESUME  # noqa: E402
from jobbot.fields import Field  # noqa: E402


def check(text, **kw):
    return C.check_letter(text, RESUME, CFG, JD, QUOTES, **kw)


def fails(text, fragment, **kw):
    problems = check(text, **kw)
    return any(fragment in p for p in problems), problems


class Openers(unittest.TestCase):
    def test_two_sentences_starting_with_the_same_two_words_are_rejected(self):
        for dup in ("I also cut it again. ", "I can share it too. "):
            text = GOOD.replace("I also cut a nightly", dup + "I also cut a nightly") if dup.startswith("I also") else \
                GOOD.replace("Your job description asks", dup + "Your job description asks")
            ok, problems = fails(text, "start each sentence differently")
            self.assertTrue(ok, problems)

    def test_the_good_letter_has_no_repeated_openers(self):
        self.assertEqual(C.repeated_openers(GOOD), [])

    def test_the_check_covers_the_whole_letter_not_just_a_paragraph(self):
        self.assertEqual(len(C.repeated_openers("I led it. Then more.\n\nI led another. Done.")), 1)
        self.assertEqual(C.repeated_openers("I led it. I built it. We shipped."), [])        # "i led" vs "i built"


class Announcements(unittest.TestCase):
    def test_signpost_phrases_are_banned(self):
        for phrase in ("This matters for your role because the services stay up.", "It fits well with your needs.",
                       "This matches your goal.", "This is close to your work.", "which matters for you"):
            text = GOOD.replace("Your job description asks", phrase + " Your job description asks", 1)
            ok, problems = fails(text, "banned phrase")
            self.assertTrue(ok, (phrase, problems))

    def test_the_prompt_forbids_announcing_the_link(self):
        prompt = C.build_prompt("Acme", "Backend", JD, RESUME, CFG, ["Hi,\n\nHello."])
        for needle in ("never announce the link", "This matters for your role because", "It fits well",
                       "No two sentences may start with the same two words"):
            self.assertIn(needle, prompt)


class Broadening(unittest.TestCase):
    def test_wider_wording_than_the_jd_is_rejected(self):
        for word in ("the world's", "global", "worldwide", "industry-leading", "everyone", "best"):
            text = GOOD.replace("payment APIs, and", f"payment APIs for {word} users, and", 1)
            ok, problems = fails(text, "broader than the job description")
            self.assertTrue(ok, (word, problems))

    def test_the_same_word_is_fine_when_the_jd_uses_it(self):
        jd = JD + " We serve global customers."
        text = GOOD.replace("payment APIs, and", "payment APIs for global customers, and", 1)
        self.assertEqual(C.check_letter(text, RESUME, CFG, jd, QUOTES), [])

    def test_the_prompt_tells_the_model_to_stay_within_the_jds_meaning(self):
        prompt = C.build_prompt("Acme", "Backend", JD, RESUME, CFG, ["Hi,\n\nHello."])
        self.assertIn("as close to the job description's own meaning as possible", prompt)
        self.assertIn("the world's customers", prompt)


MODEL = ("Hello team,\n\nI have built backend services for two years and enjoy keeping them quiet and dependable on a "
         "busy night shift every single week.\n\nPlease find my resume attached.\n\nThank you!")


class MainStyleModel(unittest.TestCase):
    def test_the_prompt_carries_the_model_as_the_main_style_and_the_samples_after_it(self):
        prompt = C.build_prompt("Acme", "Backend", JD, RESUME, CFG, ["Hi,\n\nSample text."], model=MODEL)
        self.assertIn("MAIN STYLE MODEL", prompt)
        self.assertIn(MODEL, prompt)
        self.assertLess(prompt.index("MAIN STYLE MODEL"), prompt.index("WRITING SAMPLES"))
        self.assertIn("TONE ONLY", prompt)
        self.assertNotIn("MAIN STYLE MODEL", C.build_prompt("Acme", "Backend", JD, RESUME, CFG, ["x"]))

    def test_copying_it_word_for_word_is_rejected(self):
        copied = GOOD.replace("Your job description asks", "keeping them quiet and dependable on a busy night shift. "
                              "Your job description asks", 1)
        ok, problems = fails(copied, "word for word", sources=[MODEL])
        self.assertTrue(ok, problems)
        self.assertEqual(check(GOOD, sources=[MODEL, "Hi,\n\nSample text."]), [])

    def test_a_short_shared_phrase_is_not_copying(self):
        self.assertEqual(C.copied_span("I would love to help the team build things", "I would love to help you"), "")

    def test_load_model_ignores_comments_and_a_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "m.txt"
            self.assertEqual(C.load_model(f), "")
            f.write_text("# private notes\nHello team,\n\nBody.\n", encoding="utf-8")
            self.assertEqual(C.load_model(f), "Hello team,\n\nBody.")

    def test_the_provider_uses_the_model_in_the_prompt_and_in_the_copy_guard(self):
        seen = []

        def llm(prompt):
            seen.append(prompt)
            return {"letter": GOOD, "jd_quotes": QUOTES}
        with tempfile.TemporaryDirectory() as tmp:
            prov = C.make_provider(Path(tmp) / "job", "Acme", "Backend", JD, RESUME, llm, cfg=CFG,
                                   samples=["Hi,\n\nHello."], history_path=Path(tmp) / "h.json", model=MODEL)
            value, note = prov(Field("cl", "Cover Letter", "textarea", False))
        self.assertIsNotNone(value, note)
        self.assertIn(MODEL, seen[0])

    def test_the_private_model_file_is_gitignored_and_the_example_is_not(self):
        ignore = (ROOT.parents[1] / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("resume-tailor/tailor/cover_letter_model.txt", ignore)
        self.assertTrue((ROOT / "cover_letter_model.example.txt").exists())


if __name__ == "__main__":
    unittest.main()


class UnstatedImprovements(unittest.TestCase):
    """Found in the regenerated project44 letter: "faster debugging during incidents" is a result the resume never states."""

    def test_a_comparative_result_the_resume_does_not_state_is_rejected(self):
        for word in ("faster", "easier", "safer", "improved", "fewer", "speed", "quickly", "efficiency"):
            text = GOOD.replace("so I know how to find", f"so I made it {word} and know how to find", 1)
            ok, problems = fails(text, "improvement the resume does not state")
            self.assertTrue(ok, (word, problems))

    def test_it_is_allowed_when_the_visible_resume_uses_the_word(self):
        resume = dict(RESUME)
        resume["experience"] = [dict(e) for e in RESUME["experience"]]
        resume["experience"][0]["bullets"] = list(resume["experience"][0]["bullets"]) + [
            {"id": "a9", "default": True, "text": "Made the nightly report faster by moving it to Celery workers."}]
        text = GOOD.replace("so I know how to find", "so I made it faster and know how to find", 1)
        self.assertNotIn("improvement the resume does not state", " ".join(
            C.check_letter(text, resume, CFG, JD, QUOTES)))


class ConfidentTone(unittest.TestCase):
    """2026-10-02: "I would love to" reads as desperate. Plain intent, specific links, a simple close."""

    def test_the_enthusiasm_phrases_are_banned(self):
        for phrase in ("I would love to help.", "I'd love to help.", "I am eager to help.", "I am excited to help.",
                       "I would be thrilled to help.", "This is my dream role.", "Please reach out whenever suits you.",
                       "Dear Hiring Team, I am applying.", "Dear Sir/Madam, I am applying.", "To whom it may concern, hello."):
            text = GOOD.replace("Your job description asks", phrase + " Your job description asks", 1)
            ok, problems = fails(text, "banned phrase")
            self.assertTrue(ok, (phrase, problems))

    def test_the_curly_apostrophe_form_is_caught_too(self):
        text = GOOD.replace("Your job description asks", "I\u2019d love to help. Your job description asks", 1)
        self.assertTrue(fails(text, "banned phrase")[0])

    def test_the_good_letter_states_intent_plainly_and_passes(self):
        self.assertTrue(GOOD.startswith("I'm applying"))
        self.assertEqual(check(GOOD), [])

    def test_the_config_no_longer_welcomes_the_phrase_and_bans_it(self):
        rules = " ".join(CFG["style_rules"]).lower()
        self.assertNotIn("are welcome", rules)
        self.assertIn("confident and professional", rules)
        banned = {b.lower() for b in CFG["banned_phrases"]}
        for phrase in ("i would love to", "love to", "eager to", "excited to", "would be thrilled", "dream",
                       "please reach out whenever", "dear hiring team", "dear sir/madam", "to whom it may concern"):
            self.assertIn(phrase, banned)
        self.assertFalse(any("love" in s.lower() for s in CFG["closing_styles"]))
        self.assertEqual(len(banned), len(CFG["banned_phrases"]))

    def test_the_prompt_asks_for_confidence_and_limits_the_samples_to_length_and_wording(self):
        prompt = C.build_prompt("Acme", "Backend", JD, RESUME, CFG, ["Hi,\n\nSample."])
        for needle in ("confident and professional", "I'm applying for the Backend role at Acme",
                       "ONLY for sentence length and plain wording", "no enthusiasm phrases"):
            self.assertIn(needle, prompt)
        self.assertNotIn("at most 2 times", prompt)

    def test_the_greeting_is_always_hello_and_a_body_greeting_is_rejected(self):
        full = C.full_letter(RESUME, GOOD)
        self.assertEqual(full.splitlines()[0], "Hello,")
        self.assertIn("Hello,", C.letter_tex(RESUME, GOOD))
        for greeting in ("Dear team, ", "Hi there, ", "Hello, "):
            self.assertTrue(fails(greeting + GOOD, "starts with a greeting")[0], greeting)
        self.assertFalse(fails(GOOD, "starts with a greeting")[0])


class FirstParagraph(unittest.TestCase):
    """2026-10-02: the first paragraph is at most two sentences: the role, connected to the candidate's work."""

    def test_the_good_letter_has_a_two_sentence_opening(self):
        self.assertEqual(len(C.paragraphs(GOOD)[0].split(". ")), 2)
        self.assertFalse(fails(GOOD, "first paragraph")[0])

    def test_a_third_sentence_in_paragraph_one_is_rejected(self):
        three = GOOD.replace("big rewrites.", "big rewrites. It also covers on-call work.", 1)
        ok, problems = fails(three, "the first paragraph has 3 sentences")
        self.assertTrue(ok, problems)
        self.assertIn("do not summarise the job description", " ".join(problems))

    def test_only_the_first_paragraph_is_limited(self):
        self.assertGreater(len([x for x in C.paragraphs(GOOD)[1].split(". ") if x]), 2)
        self.assertFalse(fails(GOOD, "first paragraph")[0])

    def test_the_limit_is_configurable(self):
        three = GOOD.replace("big rewrites.", "big rewrites. It also covers on-call work.", 1)
        cfg = {**CFG, "first_paragraph": {"max_sentences": 3}}
        self.assertNotIn("first paragraph", " ".join(C.check_letter(three, RESUME, cfg, JD, QUOTES)))

    def test_the_prompt_says_connect_the_role_to_your_work_and_do_not_summarise_the_jd(self):
        prompt = C.build_prompt("Acme", "Backend", JD, RESUME, CFG, ["Hi,\n\nHello."])
        for needle in ("AT MOST 2 sentences", "connect the role to your own work",
                       "Do NOT summarise", "No list of what the role involves"):
            self.assertIn(needle, prompt)
        self.assertEqual(CFG["first_paragraph"]["max_sentences"], 2)
