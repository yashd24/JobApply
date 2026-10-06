"""cover_letter.yaml (committed settings) and the writing-samples files (real one private, example committed)."""
import re
import os
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("JOBBOT_NO_SHEET", "1")      # a test must never reach the real Google Sheet


def samples(text: str) -> list[str]:
    """Split a writing-samples file into its samples (ignores '#' comment lines)."""
    body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
    parts = re.split(r"^=== Sample[^\n]*$", body, flags=re.M)
    return [p.strip() for p in parts[1:] if p.strip()]


class CoverLetterConfig(unittest.TestCase):
    def setUp(self):
        self.cfg = yaml.safe_load((ROOT / "cover_letter.yaml").read_text(encoding="utf-8"))

    def test_length_rules(self):
        self.assertEqual(self.cfg["length"], {"words_min": 150, "words_max": 250, "paragraphs": 3})

    def test_banned_phrases_include_the_required_stock_phrases(self):
        banned = {p.lower() for p in self.cfg["banned_phrases"]}
        for phrase in ("i am writing to express", "passionate", "thrilled", "leverage", "dynamic team",
                       "great fit", "i hope you're doing well"):
            self.assertIn(phrase, banned)
        self.assertEqual(len(banned), len(self.cfg["banned_phrases"]), "duplicate banned phrases")

    def test_style_rules_say_tone_only_and_no_facts_from_samples(self):
        text = " ".join(self.cfg["style_rules"]).lower()
        self.assertIn("tone only", text)
        self.assertIn("never take facts", text)
        self.assertIn("visible resume", text)
        self.assertIn("long lists of technologies", text)


class WritingSamplesFiles(unittest.TestCase):
    def test_private_file_is_gitignored_and_example_is_not(self):
        ignore = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("resume-tailor/tailor/writing_samples.txt", ignore)
        self.assertNotIn("resume-tailor/tailor/writing_samples.example.txt", ignore)

    def test_example_has_fictional_samples_in_the_expected_format(self):
        example = (ROOT / "writing_samples.example.txt").read_text(encoding="utf-8")
        found = samples(example)
        self.assertGreaterEqual(len(found), 2)
        self.assertTrue(all(len(s.split()) > 10 for s in found))
        self.assertIn("TONE ONLY", example)

    def test_real_samples_never_leak_into_the_committed_example(self):
        real = ROOT / "writing_samples.txt"
        if not real.exists():
            self.skipTest("no private writing_samples.txt on this machine")
        example = (ROOT / "writing_samples.example.txt").read_text(encoding="utf-8")
        for sample in samples(real.read_text(encoding="utf-8")):
            for line in (l.strip() for l in sample.splitlines()):
                if len(line) > 25:
                    self.assertNotIn(line, example)
        self.assertEqual(len(samples(real.read_text(encoding="utf-8"))), 2)


if __name__ == "__main__":
    unittest.main()
