"""M2 intake on saved fixtures. No network.

tests/fixtures/synthetic/   hand-written files matching the verified API/markup shapes
tests/fixtures/real/<name>/urls.json   real postings saved with
                            `python -m jobbot.intake <url> --save-fixture tests/fixtures/real/<name>`
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from jobbot import intake as I  # noqa: E402

FX = ROOT / "tests" / "fixtures"
SYN = FX / "synthetic"


def text(name):
    return (SYN / name).read_text(encoding="utf-8")


def fake_http(mapping):
    calls = []

    def http(url):
        calls.append(url)
        body = mapping.get(url)
        return (200, body) if body is not None else (404, "")
    http.calls = calls
    return http


GH_URL = "https://job-boards.greenhouse.io/examplecorp/jobs/4111111"
GH_API = "https://boards-api.greenhouse.io/v1/boards/examplecorp/jobs/4111111?questions=true"
LV_URL = "https://jobs.lever.co/examplelever/0a1b2c3d-1111-2222-3333-444455556666"
LV_API = "https://api.lever.co/v0/postings/examplelever/0a1b2c3d-1111-2222-3333-444455556666"


class Detect(unittest.TestCase):
    def test_greenhouse_urls(self):
        for url in ["https://boards.greenhouse.io/examplecorp/jobs/4111111",
                    "https://job-boards.greenhouse.io/examplecorp/jobs/4111111?gh_jid=4111111",
                    "https://boards.greenhouse.io/embed/job_app?for=examplecorp&token=4111111",
                    "https://boards.greenhouse.io/examplecorp?gh_jid=4111111"]:
            t = I.detect(url)
            self.assertEqual((t.platform, t.board, t.job_id), ("greenhouse", "examplecorp", "4111111"), url)
            self.assertEqual(t.canonical_url, GH_URL)

    def test_lever_urls(self):
        for url in [LV_URL, LV_URL + "/apply", LV_URL + "/apply?lever-source=LinkedIn"]:
            t = I.detect(url)
            self.assertEqual((t.platform, t.board), ("lever", "examplelever"), url)
            self.assertEqual(t.canonical_url, LV_URL)

    def test_eu_hosts(self):
        t = I.detect("https://job-boards.eu.greenhouse.io/examplecorp/jobs/1")
        self.assertTrue(t.eu)
        self.assertIn("boards-api.eu.greenhouse.io", t.api_url)
        t = I.detect("https://jobs.eu.lever.co/examplelever/0a1b2c3d-1111")
        self.assertTrue(t.eu)
        self.assertIn("api.eu.lever.co", t.api_url)

    def test_company_site_needs_the_page(self):
        self.assertIsNone(I.detect("https://careers.example.com/jobs/backend?gh_jid=4111111"))
        self.assertIsNone(I.detect("https://www.linkedin.com/jobs/view/123"))


class Embedded(unittest.TestCase):
    def test_iframe_embed(self):
        t = I.resolve_embedded_greenhouse("https://www.example.com/careers", text("company_iframe.html"))
        self.assertEqual((t.board, t.job_id), ("examplecorp", "4111111"))

    def test_script_embed_uses_gh_jid_from_the_url(self):
        t = I.resolve_embedded_greenhouse("https://www.example.com/jobs?gh_jid=4111111", text("company_script.html"))
        self.assertEqual((t.board, t.job_id), ("examplecorp", "4111111"))
        self.assertIsNone(I.resolve_embedded_greenhouse("https://www.example.com/jobs", text("company_script.html")))

    def test_no_greenhouse_on_the_page(self):
        self.assertIsNone(I.resolve_embedded_greenhouse("https://x.com/?gh_jid=1", "<html>nothing</html>"))

    def test_fetch_job_through_a_company_page(self):
        page = "https://www.example.com/careers/backend?gh_jid=4111111"
        http = fake_http({page: text("company_iframe.html"), GH_API: text("gh_job.json")})
        job = I.fetch_job(page, http=http)
        self.assertEqual((job.platform, job.canonical_url), ("greenhouse", GH_URL))
        self.assertEqual(job.source_url, page)


class GuessedBoard(unittest.TestCase):
    """gh_jid on a company site whose HTML shows no embed: the board is guessed from the domain and
    accepted only if the Greenhouse API has that exact job id."""

    def test_board_token_candidates(self):
        self.assertEqual(I.board_token_candidates("https://stripe.com/jobs/search?gh_jid=1"), ["stripe"])
        self.assertEqual(I.board_token_candidates("https://careers.airbnb.com/positions/1"), ["airbnb"])
        self.assertEqual(I.board_token_candidates("https://careers.datadoghq.com/detail/1/"), ["datadoghq", "datadog"])
        self.assertEqual(I.board_token_candidates("https://www.samsara.com/company/careers/roles/1"), ["samsara"])

    def test_accepted_only_when_the_api_confirms_the_job_id(self):
        page = "https://www.examplecorp.com/careers/backend?gh_jid=4111111"
        http = fake_http({page: "<html>no embed here</html>", GH_API: text("gh_job.json")})
        job = I.fetch_job(page, http=http)
        self.assertEqual((job.board, job.canonical_url, job.source_url), ("examplecorp", GH_URL, page))

    def test_wrong_board_is_rejected(self):
        page = "https://www.othercorp.com/careers/backend?gh_jid=4111111"
        # the API for 'othercorp' returns 404, so nothing is guessed
        with self.assertRaises(I.UnsupportedPlatform):
            I.fetch_job(page, http=fake_http({page: "<html>no embed</html>"}))

    def test_job_id_mismatch_is_rejected(self):
        page = "https://www.examplecorp.com/careers/backend?gh_jid=999"
        http = fake_http({page: "<html/>",
                          "https://boards-api.greenhouse.io/v1/boards/examplecorp/jobs/999?questions=true":
                              text("gh_job.json")})     # returns job 4111111, not 999
        with self.assertRaises(I.UnsupportedPlatform):
            I.fetch_job(page, http=http)


class NetworkErrors(unittest.TestCase):
    def test_default_http_wraps_request_exceptions(self):
        import requests
        from unittest import mock
        with mock.patch("requests.get", side_effect=requests.ConnectionError("boom")):
            with self.assertRaises(I.IntakeError):
                I.default_http("https://example.com")


class Greenhouse(unittest.TestCase):
    def setUp(self):
        self.job = I.fetch_job(GH_URL, http=fake_http({GH_API: text("gh_job.json")}))

    def test_job_fields(self):
        j = self.job
        self.assertEqual((j.company, j.role, j.location, j.source), ("Examplecorp", "Backend Engineer",
                                                                   "Bengaluru, India", "api"))
        self.assertEqual(j.job_id, "4111111")
        self.assertEqual(j.source_url, "")
        self.assertIn("We build payment APIs.", j.jd_text)       # entity-escaped HTML -> text
        self.assertIn("- Python and Django", j.jd_text)
        self.assertNotIn("<", j.jd_text)

    def test_questions(self):
        qs = {q.label: q for q in self.job.questions}
        self.assertTrue(qs["First Name"].required)
        self.assertEqual(qs["First Name"].type, "text")
        self.assertEqual([f.type for f in qs["Resume/CV"].fields], ["file", "textarea"])
        auth = qs["Are you legally authorized to work in India?"]
        self.assertEqual((auth.type, auth.options, auth.description), ("select", ["Yes", "No"], "Pick one"))
        self.assertEqual(qs["Which of these have you used?"].type, "multiselect")
        self.assertFalse(qs["Why do you want to work here?"].required)
        self.assertEqual(qs["Location"].group, "location")
        self.assertEqual(qs["Gender"].group, "demographic")
        self.assertEqual(qs["Gender"].options, ["Male", "Female", "Decline to self-identify"])

    def test_source_url_kept_when_it_differs(self):
        url = GH_URL + "?gh_jid=4111111"
        job = I.fetch_job(url, http=fake_http({GH_API: text("gh_job.json")}))
        self.assertEqual(job.source_url, url)
        self.assertEqual(job.canonical_url, GH_URL)


class Lever(unittest.TestCase):
    def setUp(self):
        self.http = fake_http({LV_API: text("lever_posting.json"), LV_URL + "/apply": text("lever_apply.html")})
        self.job = I.fetch_job(LV_URL, http=self.http)

    def test_job_fields(self):
        j = self.job
        self.assertEqual((j.role, j.location, j.source), ("Software Engineer - Backend", "Remote - India", "api"))
        self.assertEqual(j.company, "Example Lever Co")          # from '<title>Company - Role'
        self.assertEqual(j.apply_url, LV_URL + "/apply")
        for piece in ("build APIs", "Requirements", "- Python", "equal opportunity"):
            self.assertIn(piece, j.jd_text)

    def test_questions_come_from_the_apply_page(self):
        qs = {q.label: q for q in self.job.questions}
        self.assertEqual(qs["Resume/CV"].type, "file")
        self.assertFalse(qs["Resume/CV"].required)
        self.assertTrue(qs["Full name"].required)
        self.assertEqual(qs["Email"].type, "email")
        elig = qs["Are you eligible to work in India?"]
        self.assertEqual((elig.type, elig.options, elig.group, elig.required),
                         ("radio", ["Yes", "No"], "custom", True))
        self.assertEqual(qs["Expected CTC"].type, "textarea")
        self.assertEqual(qs["Notice period"].type, "select")
        self.assertEqual(qs["Notice period"].options, ["15 days", "30 days"])
        self.assertEqual(qs["What gender do you identify as?"].group, "demographic")

    def test_unlabelled_consent_checkbox_gets_its_caption_and_group(self):
        consent = [q for q in self.job.questions if q.group == "consent"]
        self.assertEqual(len(consent), 1)
        self.assertEqual((consent[0].label, consent[0].type, consent[0].required),
                         ("Yes, Example can contact me about future roles", "checkbox", False))

    def test_captcha_is_flagged_as_possible(self):
        self.assertTrue(self.job.captcha_possible)

    def test_apply_url_input_is_canonicalised(self):
        job = I.fetch_job(LV_URL + "/apply", http=self.http)
        self.assertEqual(job.canonical_url, LV_URL)
        self.assertEqual(job.source_url, LV_URL + "/apply")

    def test_missing_apply_page_warns_but_keeps_the_jd(self):
        job = I.fetch_job(LV_URL, http=fake_http({LV_API: text("lever_posting.json")}))
        self.assertEqual(job.questions, [])
        self.assertTrue(any("questions unavailable" in w and "may be closed" in w for w in job.warnings))
        self.assertIn("build APIs", job.jd_text)


class Fallbacks(unittest.TestCase):
    def test_api_failure_falls_back_to_the_html_scraper(self):
        job = I.fetch_job(GH_URL, http=fake_http({}), jd_fallback=lambda u: "Scraped JD text " * 30)
        self.assertEqual((job.source, job.questions), ("html", []))
        self.assertIn("Scraped JD", job.jd_text)
        self.assertTrue(any("fell back" in w for w in job.warnings))

    def test_nothing_works_raises(self):
        with self.assertRaises(I.IntakeError):
            I.fetch_job(GH_URL, http=fake_http({}), jd_fallback=lambda u: None)

    def test_unsupported_platform(self):
        with self.assertRaises(I.UnsupportedPlatform):
            I.fetch_job("https://www.linkedin.com/jobs/view/123", http=fake_http({"https://www.linkedin.com/jobs/view/123": "<html/>"}))
        with self.assertRaises(I.UnsupportedPlatform):
            I.fetch_job("https://x.example.com/job", http=fake_http({}))


class Dedupe(unittest.TestCase):
    class T:
        def __init__(self, submitted):
            self.submitted = submitted

        def is_submitted(self, url):
            return url in self.submitted

    def test_already_submitted_stops_before_any_api_call(self):
        http = fake_http({GH_API: text("gh_job.json")})
        with self.assertRaises(I.AlreadySubmitted) as cm:
            I.fetch_job(GH_URL + "?gh_jid=4111111", http=http, tracker=self.T({GH_URL}))
        self.assertEqual(cm.exception.url, GH_URL)
        self.assertEqual(http.calls, [])

    def test_not_submitted_proceeds(self):
        job = I.fetch_job(GH_URL, http=fake_http({GH_API: text("gh_job.json")}), tracker=self.T(set()))
        self.assertEqual(job.role, "Backend Engineer")


class RealFixtures(unittest.TestCase):
    """Replays every real posting saved under tests/fixtures/real/*/urls.json."""

    @staticmethod
    def dirs():
        return sorted(p.parent for p in (FX / "real").glob("*/urls.json"))

    def load(self, d):
        data = json.loads((d / "urls.json").read_text(encoding="utf-8"))
        return data["input"], fake_http({u: t for u, t in data["responses"].items() if t})

    def test_every_real_fixture_parses(self):
        for d in self.dirs():
            with self.subTest(fixture=d.name):
                url, http = self.load(d)
                job = I.fetch_job(url, http=http)
                self.assertGreater(len(job.jd_text), 200)
                self.assertTrue(job.role and job.company)
                self.assertEqual(job.source, "api")
                self.assertTrue(job.questions, "no questions parsed")
                for q in job.questions:
                    self.assertTrue(q.label or q.group == "location")
                    self.assertTrue(q.fields)

    def job(self, name):
        url, http = self.load(FX / "real" / name)
        return I.fetch_job(url, http=http)

    def test_real_greenhouse_shapes(self):
        if not (FX / "real" / "gh_backbase_eeo").exists():
            self.skipTest("fixtures not saved")
        backbase = self.job("gh_backbase_eeo")
        demo = {q.label: q for q in backbase.questions if q.group == "demographic"}
        self.assertEqual(len(demo), 3)
        self.assertTrue(any("gender" in l.lower() and q.type == "multiselect" for l, q in demo.items()))
        self.assertTrue(any("disability" in l.lower() and q.type == "select" and q.options for l, q in demo.items()))
        zs = self.job("gh_zscaler_coverletter")
        cover = [q for q in zs.questions if q.label.lower() == "cover letter"]
        self.assertEqual([(q.type, q.required) for q in cover], [("file", False)])
        self.assertTrue(any(q.group == "demographic" for q in zs.questions))
        cv = self.job("gh_commvault")
        self.assertTrue(any(q.label.startswith("Willingness to relocate") for q in cv.questions))

    def test_real_lever_shapes(self):
        if not (FX / "real" / "lv_kwalee_eu").exists():
            self.skipTest("fixtures not saved")
        eu = self.job("lv_kwalee_eu")
        self.assertEqual(eu.canonical_url, "https://jobs.eu.lever.co/kwalee/82c341d0-f8dc-4bda-a92b-be525934e887")
        self.assertTrue(eu.questions and eu.source == "api")
        gw = self.job("lv_gushwork")
        scoped = [q for q in gw.questions if "excluding internship" in q.label.lower()]
        self.assertEqual(len(scoped), 1)
        self.assertTrue(all(j.captcha_possible for j in (gw, eu, self.job("lv_portcast"))))

    def test_real_embedded_cases(self):
        if not (FX / "real" / "gh_embedded_airbnb").exists():
            self.skipTest("fixtures not saved")
        air, stripe = self.job("gh_embedded_airbnb"), self.job("gh_embedded_stripe_guessed")
        for job, board in ((air, "airbnb"), (stripe, "stripe")):
            self.assertEqual((job.platform, job.board), ("greenhouse", board))
            self.assertIn("gh_jid=", job.source_url)
            self.assertIsNone(I.detect(job.source_url))         # needed the page / board guess
            self.assertEqual(job.source, "api")

    def test_m2_done_when_coverage(self):
        counts = {"greenhouse": 0, "lever": 0}
        embedded = 0
        for d in self.dirs():
            url, http = self.load(d)
            job = I.fetch_job(url, http=http)
            counts[job.platform] += 1
            embedded += bool(job.source_url and job.platform == "greenhouse" and I.detect(url) is None)
        if min(counts.values()) < 3 or not embedded:
            self.skipTest(f"M2 needs >=3 Greenhouse, >=3 Lever, 1 embedded Greenhouse; have {counts}, "
                          f"embedded {embedded}")


if __name__ == "__main__":
    unittest.main()
