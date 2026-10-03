"""M7 tracker: SQLite from job folders, submitted sticks, dedupe, and the status table."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jobbot import tracker as T  # noqa: E402

GH = "https://job-boards.greenhouse.io/acme/jobs/1"
LV = "https://jobs.lever.co/acme/2222"


def folder(root, name, url, status, mode="assist", company="Acme", role="Backend Engineer", finished="2026-10-02T10:00:00",
           score=7, flagged=(), **extra):
    d = Path(root) / name
    d.mkdir(parents=True)
    (d / "apply_result.json").write_text(json.dumps(
        {"status": status, "mode": mode, "url": url, "company": company, "role": role, **extra}), encoding="utf-8")
    (d / "answers.json").write_text(json.dumps({
        "answers": [{"label": l, "status": "flagged"} for l in flagged] + [{"label": "Name", "status": "filled"}],
        "meta": {"platform": "greenhouse", "location": "Bengaluru", "resume_pdf": str(d / "r.pdf"),
                 "screenshots": [str(d / "s.png")]},
        "outcome": {"status": status, "finished": finished}}), encoding="utf-8")
    (d / "result.json").write_text(json.dumps({"scores": {"overall": score}, "gaps": ["Kafka"]}), encoding="utf-8")
    return d


class Tracking(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.t = T.Tracker(self.root / "t.sqlite3")

    def tearDown(self):
        self.t.close()
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    def test_a_folder_becomes_a_row_with_the_planned_columns(self):
        self.t.record_folder(folder(self.root, "a", GH, "submitted", flagged=["Why us?"]))
        r = self.t.job(GH)
        self.assertEqual((r["status"], r["mode"], r["company"], r["role"], r["platform"], r["location"], r["score"]),
                         ("submitted", "assist", "Acme", "Backend Engineer", "greenhouse", "Bengaluru", 7))
        self.assertEqual(json.loads(r["gaps"]), ["Kafka"])
        self.assertEqual(json.loads(r["flagged_fields"]), ["Why us?"])
        self.assertTrue(r["submitted_at"] and r["created_at"] and r["answers_path"] and r["resume_pdf"])
        self.assertEqual(json.loads(r["screenshots"]), [str(self.root / "a" / "s.png")])

    def test_recording_twice_is_idempotent(self):
        d = folder(self.root, "a", GH, "dry_run", mode="dry-run")
        self.t.record_folder(d)
        self.t.record_folder(d)
        self.assertEqual((len(self.t.recent()), self.t.job(GH)["runs"]), (1, 1))

    def test_a_later_dry_run_never_downgrades_a_submission(self):
        self.t.record_folder(folder(self.root, "real", GH, "submitted", finished="2026-10-02T10:00:00"))
        self.t.record_folder(folder(self.root, "dry", GH + "/", "dry_run", mode="dry-run", finished="2026-10-03T10:00:00"))
        r = self.t.job(GH)
        self.assertEqual((r["status"], r["mode"], r["runs"]), ("submitted", "assist", 2))
        self.assertTrue(self.t.is_submitted(GH))

    def test_without_a_submission_the_latest_run_wins(self):
        self.t.record_folder(folder(self.root, "a", LV, "failed", finished="2026-10-01T10:00:00"))
        self.t.record_folder(folder(self.root, "b", LV, "prepared", mode="prepare", finished="2026-10-02T10:00:00"))
        self.assertEqual(self.t.job(LV)["status"], "ready_for_you")            # a run's "prepared" is Ready for you
        self.assertFalse(self.t.is_submitted(LV))

    def test_manual_submission_is_shown_as_manual(self):
        self.t.record_folder(folder(self.root, "m", LV, "submitted", mode="assist", submitted_by="manual",
                                    submitted_on="2026-10-02"))
        text = T.format_status(self.t.recent(), self.t.counts())
        self.assertIn("manual", text)
        self.assertIn("Submitted: 1", text)
        self.assertEqual(self.t.job(LV)["submitted_at"], "2026-10-02")

    def test_url_variants_are_the_same_posting(self):
        self.t.record_folder(folder(self.root, "a", GH, "submitted"))
        for variant in (GH + "/", GH.upper(), GH + "?gh_src=x#apply"):
            self.assertTrue(self.t.is_submitted(variant), variant)
        self.assertFalse(self.t.is_submitted("https://job-boards.greenhouse.io/acme/jobs/2"))

    def test_import_reads_every_run_folder_and_skips_the_rest(self):
        folder(self.root, "out/a", GH, "submitted")
        folder(self.root, "out/b", LV, "dry_run", mode="dry-run")
        (self.root / "out" / "base").mkdir()
        (self.root / "out" / "stray.txt").write_text("x", encoding="utf-8")
        self.assertEqual(self.t.import_existing(self.root / "out"), 2)
        self.assertEqual(self.t.counts(), {"submitted": 1, "dry_run": 1})

    def test_empty_tracker_says_so(self):
        self.assertIn("No applications", T.format_status([], {}))

    def test_the_database_survives_reopening(self):
        self.t.record_folder(folder(self.root, "a", GH, "submitted"))
        self.t.close()
        with T.Tracker(self.root / "t.sqlite3") as again:
            self.assertTrue(again.is_submitted(GH))
        self.t = T.Tracker(self.root / "t.sqlite3")


if __name__ == "__main__":
    unittest.main()
