"""The email/phone printed in the resume header live in a gitignored contact.yaml, never in committed files."""
import re
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[1]
REPO = ROOT.parents[1]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("JOBBOT_NO_SHEET", "1")      # a test must never reach the real Google Sheet

import render  # noqa: E402
import tailor  # noqa: E402


class LoadResumeData(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="contact test ")
        self.path = Path(self.tmp.name) / "contact.yaml"

    def tearDown(self):
        self.tmp.cleanup()

    def load(self, text=None):
        if text is not None:
            self.path.write_text(text, encoding="utf-8")
        with mock.patch.object(tailor, "CONTACT_FILE", self.path), \
                mock.patch.object(tailor, "PROFILE_FILE", Path(self.tmp.name) / "no-profile.yaml"):   # not the real one
            return tailor.load_resume_data()

    def test_contact_is_merged_into_the_resume_data_and_rendered(self):
        data = self.load('email: test.user@example.com\nphone: "+00 000 000 0000"\n')
        self.assertEqual((data["email"], data["phone"]), ("test.user@example.com", "+00 000 000 0000"))
        self.assertIn("bullets", data["experience"][0])                # the rest of resume_data.yaml is intact
        tex = render.render_tex(data, render.default_plan(data))
        self.assertIn(r"\href{mailto:test.user@example.com}", tex)
        self.assertIn("+00 000 000 0000", tex)

    def test_missing_contact_file_is_a_clear_error(self):
        with self.assertRaises(tailor.TailorError) as cm:
            self.load()
        self.assertIn("contact.example.yaml", str(cm.exception))

    def test_todo_or_blank_values_are_refused(self):
        for text in ("email: TODO\nphone: '+1'\n", "email: a@b.co\nphone: ''\n", "email: a@b.co\n", ""):
            with self.assertRaises(tailor.TailorError, msg=text):
                self.load(text)


class NothingPrivateIsCommitted(unittest.TestCase):
    """contact.yaml is gitignored, and no committed file carries an email address or phone number."""

    def test_contact_yaml_is_gitignored_and_the_example_is_not(self):
        ignore = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
        self.assertIn("resume-tailor/tailor/contact.yaml", ignore)
        self.assertNotIn("contact.example.yaml", " ".join(ignore))
        example = yaml.safe_load((ROOT / "contact.example.yaml").read_text(encoding="utf-8"))
        self.assertEqual(example, {"email": "TODO", "phone": "TODO"})

    def test_resume_data_has_no_email_or_phone(self):
        raw = (ROOT / "resume_data.yaml").read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
        self.assertNotIn("email", data)
        self.assertNotIn("phone", data)
        self.assertNotRegex(raw, r"[\w.+-]+@[\w-]+\.[\w.-]+")

    def test_original_template_uses_placeholders(self):
        tex = (ROOT / "template" / "original_resume.tex").read_text(encoding="utf-8")
        self.assertNotRegex(tex, r"[\w.+-]+@(?!edu\.com)[\w-]+\.[\w.-]+")    # only the template's own dummy address
        self.assertNotRegex(tex, r"\+\d{1,3}[ -]?\d{5}")
        self.assertIn(r"\underline{\yourEmail}", tex)
        self.assertIn(r"\yourPhone $|$", tex)


if __name__ == "__main__":
    unittest.main()
