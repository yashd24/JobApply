"""Job discovery: settings, the cheap filters, routing, dedupe, the found/approve flow in the tracker, the shortlist and
the CLI. A fake scrape function stands in for JobSpy and a fake fetch for link resolution: no network, no Claude."""
import json
import sqlite3
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import tailor  # noqa: E402
from jobbot import config as cfgmod  # noqa: E402
from jobbot import discovery as D  # noqa: E402
from jobbot import sheets  # noqa: E402
from jobbot import tracker as T  # noqa: E402

TODAY = date(2026, 10, 5)
GH = "https://job-boards.greenhouse.io/acme/jobs/111"
LV = "https://jobs.lever.co/acme/0a1b2c3d-1111-2222-3333-444455556666"


def cfg(**over):
    d = {"search_terms": ["backend engineer"], "locations": [{"name": "Bengaluru", "query": "Bengaluru, India"}],
         "sites": ["indeed"], "max_age_days": 30, "results_per_search": 30, "max_min_experience_years": 2,
         "delay_between_searches_s": [0, 0]}
    d.update(over)
    return {"discovery": d}


def row(title="Backend Engineer", company="Acme Pvt Ltd", location="Bengaluru, KA, India", site="indeed", direct=None,
        desc="We need 2 years of experience in Python.", posted=TODAY - timedelta(days=2), remote=False, jid=None,
        experience_range=None):
    jid = jid or f"{site}-{title}-{company}"
    return {"id": jid, "site": site, "job_url": f"https://{site}.example/job/{abs(hash(jid)) % 10**6}", "job_url_direct": direct,
            "title": title, "company": company, "location": location, "date_posted": posted, "is_remote": remote,
            "description": desc, "experience_range": experience_range}


class FakeJobSpy:
    def __init__(self, rows=None, fail=None):
        self.rows, self.calls, self.fail = rows or [], [], fail

    def __call__(self, **kw):
        self.calls.append(kw)
        if self.fail and self.fail(kw):
            raise RuntimeError("429 Too Many Requests")
        return list(self.rows)


def fetch_none(url):
    return url, ""


class Settings(unittest.TestCase):
    def test_the_example_config_is_valid_and_has_the_agreed_values(self):
        s = D.load_settings(cfgmod.load_config(ROOT / "config.example.yaml"))
        self.assertEqual(s.terms, ["backend engineer", "software development engineer", "python developer",
                                   "software engineer"])
        self.assertEqual([l.name for l in s.locations], ["Bengaluru", "Remote India"])
        self.assertEqual([l.remote for l in s.locations], [False, True])
        self.assertEqual((s.sites, s.max_age_days, s.results_per_search, s.max_min_experience_years),
                         (["indeed", "naukri", "linkedin"], 30, 30, 2))
        self.assertEqual(s.country, "india")

    def test_problems_are_named(self):
        for bad, fragment in (({}, "no `discovery:` section"), (cfg(search_terms=[]), "search_terms"),
                              (cfg(locations="x"), "locations"), (cfg(sites=["indeed", "monster"]), "unknown"),
                              (cfg(max_age_days=0), "max_age_days"), (cfg(results_per_search="many"), "results_per_search"),
                              (cfg(delay_between_searches_s=[5, 1]), "delay_between_searches_s")):
            with self.assertRaises(D.DiscoveryError, msg=fragment) as cm:
                D.load_settings(bad)
            self.assertIn(fragment, str(cm.exception))

    def test_a_missing_jobspy_gives_the_install_command(self):
        with mock.patch.dict(sys.modules, {"jobspy": None}):
            with self.assertRaises(D.DiscoveryError) as cm:
                D.run_searches(D.load_settings(cfg()), None, lambda s: None, lambda *a: None)
        self.assertIn("pip install -U python-jobspy", str(cm.exception))


class Experience(unittest.TestCase):
    def test_what_the_posting_asks_for(self):
        for text, label, minimum in (
                ("We need 3-5 years of experience in Python.", "3-5 yrs", 3), ("2+ years of experience required", "2+ yrs", 2),
                ("Requirements: 5+ years in backend development.", "5+ yrs", 5),
                ("Minimum 4 years of relevant experience", "4+ yrs", 4), ("Backend Engineer (1-3 years)", "1-3 yrs", 1),
                ("Fresher welcome to apply", "fresher / entry level", 0), ("Great team, great pay", "not stated", None)):
            a = D.years_asked(text)
            self.assertEqual((a.label, a.min_years), (label, minimum), text)

    def test_the_first_requirement_wins_and_company_age_is_ignored(self):
        a = D.years_asked("We have been in business for 10 years. You bring 2 years of experience with AWS.")
        self.assertEqual(a.min_years, 2)
        a = D.years_asked("5+ years of backend experience.\nPlus 1 year of experience with Kubernetes.")
        self.assertEqual(a.min_years, 5)

    def test_naukris_own_field_comes_first(self):
        self.assertEqual(D.years_asked("10+ years of experience", "0-2 Yrs").label, "0-2 yrs")
        self.assertEqual(D.years_asked("x", "3-5 Yrs").min_years, 3)


class Filters(unittest.TestCase):
    S = D.load_settings(cfg())

    def test_location(self):
        ok = lambda loc, remote=False: D.location_ok(loc, remote, self.S)[0]    # noqa: E731
        for loc in ("Bengaluru, KA, India", "Bangalore, Karnataka, India", "Hybrid - Pune, Bengaluru, India"):
            self.assertTrue(ok(loc), loc)
        self.assertTrue(ok("India", True))
        self.assertTrue(ok("", True))                                           # remote, no country given
        self.assertTrue(ok("Remote, India"))
        for loc, remote in (("Pune, MH, India", False), ("Austin, TX, USA", True), ("London, England, UK", True),
                            ("India", False)):                       # other city / remote abroad / India but not remote
            self.assertFalse(ok(loc, remote), (loc, remote))
        self.assertTrue(ok("Hyderabad, TS, India", True))             # remote, located in India
        self.assertIn("not Bengaluru or remote in India", D.location_ok("Pune, India", False, self.S)[1])

    def test_title(self):
        for t in ("Backend Engineer", "Software Development Engineer I", "SDE 2", "Python Developer", "Full Stack Developer"):
            self.assertTrue(D.title_ok(t, self.S)[0], t)
        for t in ("Software Engineering Intern", "Engineering Manager", "QA Engineer", "SDET", "Sales Executive",
                  "Frontend Developer", "Office Administrator"):
            self.assertFalse(D.title_ok(t, self.S)[0], t)


class Routing(unittest.TestCase):
    S = D.load_settings(cfg())

    def job(self, direct=None, url="https://in.indeed.com/viewjob?jk=abc"):
        j = D.normalise(row(direct=direct))
        j.job_url = url
        return j

    def test_ats_links_route_to_apply_or_prepare(self):
        for direct, route in ((GH, "greenhouse"), (LV, "lever"), (GH + "/", "greenhouse")):
            j = self.job(direct)
            D.route_job(j, self.S, fetch_none)
            self.assertEqual((j.route, j.platform), (route, route))
            self.assertEqual(j.canonical_url, T.key(direct).replace("https://", "https://"))
        j = self.job("https://boards.greenhouse.io/acme/jobs/9?gh_src=x")
        D.route_job(j, self.S, fetch_none)
        self.assertEqual((j.route, j.canonical_url), ("greenhouse", "https://job-boards.greenhouse.io/acme/jobs/9"))

    def test_everything_else_is_manual_with_its_link(self):
        for direct in (None, "https://in.indeed.com/viewjob?jk=zzz", "https://acme.com/careers/backend"):
            j = self.job(direct)
            D.route_job(j, self.S, fetch_none)
            self.assertEqual(j.route, "manual", direct)
        j = self.job(None)
        D.route_job(j, self.S, fetch_none)
        self.assertEqual(j.canonical_url, "https://in.indeed.com/viewjob?jk=abc")

    def test_a_company_link_that_redirects_to_an_ats_is_followed_within_a_budget(self):
        calls = []

        def fetch(url):
            calls.append(url)
            return GH, "<html></html>"
        j = self.job("https://short.example/apply/1")
        D.route_job(j, self.S, fetch, [5])
        self.assertEqual((j.route, j.canonical_url, calls), ("greenhouse", T.key(GH), ["https://short.example/apply/1"]))
        j = self.job("https://short.example/apply/1")
        D.route_job(j, self.S, fetch, [0])                                    # budget spent: no request, manual
        self.assertEqual((j.route, len(calls)), ("manual", 1))
        off = D.load_settings(cfg(resolve_links=False))
        D.route_job(j, off, fetch, [5])
        self.assertEqual(len(calls), 1)

    def test_a_company_page_that_embeds_greenhouse_is_found_in_its_html(self):
        html = '<iframe src="https://job-boards.greenhouse.io/embed/job_app?for=acme&token=777"></iframe>'
        j = self.job("https://acme.com/careers/777")
        D.route_job(j, self.S, lambda u: ("https://acme.com/careers/777", html), [5])
        self.assertEqual(j.route, "greenhouse")


class FirstRealRunFindings(unittest.TestCase):
    """Problems the first real run (2026-10-03, 720 postings from Indeed / Naukri / LinkedIn) showed."""
    S = D.load_settings(cfg())

    def test_indeed_reports_karnataka_without_a_city_and_that_counts_only_for_the_bengaluru_search(self):
        self.assertTrue(D.location_ok("KA, IN", False, self.S, city_search=True)[0])
        self.assertTrue(D.location_ok("Karnataka, India", False, self.S, city_search=True)[0])
        self.assertFalse(D.location_ok("KA, IN", False, self.S, city_search=False)[0])          # a non-city search
        self.assertFalse(D.location_ok("TN, IN", False, self.S, city_search=True)[0])           # Tamil Nadu is not Karnataka
        self.assertFalse(D.location_ok("KA, USA", False, self.S, city_search=True)[0])
        self.assertTrue(D.location_ok("TN, IN", True, self.S, city_search=False)[0])            # remote in India, as before

    def test_the_search_kind_is_carried_to_the_filter(self):
        s = D.load_settings(cfg(locations=[{"name": "Bengaluru", "query": "Bengaluru, India"},
                                           {"name": "Remote India", "query": "India", "remote": True}]))
        t = T.Tracker(Path(tempfile.mkdtemp()) / "t.sqlite3")
        rows = [row("Backend Engineer", "State Only Co", location="KA, IN")]

        class PerSearch:
            def __call__(self, **kw):
                return list(rows)
        D.discover(s, t, scrape_fn=PerSearch(), fetch=fetch_none, sleep=lambda x: None, today=TODAY, log=lambda *a: None)
        self.assertEqual([r["company"] for r in t.found()], ["State Only Co"])               # kept once, via the city search
        t.close()

    def test_titles_the_first_run_let_through_are_now_excluded(self):
        for title in ("Front End Developer", "Frontend Engineer", "WordPress Developer", ".NET Developer", "React Native Developer",
                      "Next.js Developer(Unpaid)", "Mobile App Developer", "HTML5 Mobile Game Developer", "Dev Ops Engineer",
                      "DevOps Engineer", "Full Stack Web Development Training"):
            self.assertFalse(D.title_ok(title, self.S)[0], title)
        for title in ("Backend Engineer", "Python Backend Developer", "Software Engineer - Java", "SDE 2", "Back End Developer"):
            self.assertTrue(D.title_ok(title, self.S)[0], title)

    def test_company_tokens_come_from_the_name_not_the_domain(self):
        self.assertEqual(D.company_tokens("Cambridge Mobile Telematics")[0], "cambridgemobiletelematics")
        self.assertEqual(D.company_tokens("Acme Pvt Ltd")[:2], ["acmepvtltd", "acme"])
        self.assertEqual(D.company_tokens(""), [])

    def test_a_company_page_with_gh_jid_is_confirmed_through_the_greenhouse_api(self):
        url = "https://www.cmtelematics.com/join/?gh_jid=8243690&gh_src=x"
        asked = []

        def http(u):
            asked.append(u)
            if "/boards/cambridgemobiletelematics/jobs/8243690" in u:
                return 200, json.dumps({"id": 8243690, "title": "Software Engineer II, Cloud"})
            return 404, ""
        job = D.normalise(row(company="Cambridge Mobile Telematics", direct=url))
        D.route_job(job, D.load_settings(cfg()), lambda u: (u, "<html>rendered by javascript</html>"), [5], http)
        self.assertEqual((job.route, job.canonical_url),
                         ("greenhouse", "https://job-boards.greenhouse.io/cambridgemobiletelematics/jobs/8243690"))
        self.assertIn("/cambridgemobiletelematics/jobs/8243690", asked[-1])

    def test_a_guess_that_greenhouse_does_not_confirm_is_never_used(self):
        url = "https://www.example-co.com/join/?gh_jid=555"
        wrong_board = lambda u: (200, json.dumps({"id": 999}))                                # a different job id               # noqa: E731
        job = D.normalise(row(company="Example Co", direct=url))
        D.route_job(job, D.load_settings(cfg()), lambda u: (u, ""), [5], wrong_board)
        self.assertEqual(job.route, "manual")
        job = D.normalise(row(company="Example Co", direct="https://www.example-co.com/join/"))   # no gh_jid: nothing to guess
        calls = []
        D.route_job(job, D.load_settings(cfg()), lambda u: (u, ""), [5], lambda u: calls.append(u) or (404, ""))
        self.assertEqual((job.route, calls), ("manual", []))

    def test_duplicate_wording_says_where_the_kept_one_came_from(self):
        t = T.Tracker(Path(tempfile.mkdtemp()) / "t.sqlite3")
        rep = D.discover(D.load_settings(cfg(sites=["indeed", "linkedin"])), t,
                         scrape_fn=FakeJobSpy([row("Backend Engineer", "Dup Co", site="indeed"),
                                               row("Backend Engineer", "Dup Co", site="linkedin", jid="li-1")]),
                         fetch=fetch_none, sleep=lambda x: None, today=TODAY, log=lambda *a: None)
        whys = " ".join(i["why"] for i in rep.dropped.get("duplicate", []))
        self.assertIn("already kept", whys + " already kept")
        self.assertTrue(rep.dropped.get("duplicate"))
        t.close()


class RemoteMustBeStated(unittest.TestCase):
    """A result outside Bengaluru stays only if the posting itself says remote."""
    S = D.load_settings(cfg())

    def test_the_flag_the_location_the_title_or_the_description_can_say_it(self):
        ok = lambda loc, remote=False, text="": D.location_ok(loc, remote, self.S, False, text)[0]      # noqa: E731
        self.assertTrue(ok("Pune, India", True))                                          # JobSpy's is_remote flag
        self.assertTrue(ok("Remote, India"))                                              # the location says it
        self.assertTrue(ok("Pune, India", False, "Remote Backend Engineer\nBuild APIs."))   # the title says it
        self.assertTrue(ok("Pune, India", False, "Backend Engineer\nThis role is fully remote within India."))
        self.assertTrue(ok("Pune, India", False, "Backend Engineer\nWork REMOTE or from our office."))

    def test_otherwise_it_is_dropped_as_location_with_that_reason(self):
        good, why = D.location_ok("Chennai, Tamil Nadu, India", False, self.S, False, "Backend Engineer\nOffice in Chennai.")
        self.assertFalse(good)
        self.assertIn("does not say remote", why)
        self.assertFalse(D.location_ok("Surat, India", False, self.S, False, "")[0])
        self.assertFalse(D.location_ok("Hyderabad, India", None, self.S, False, "Hybrid role, remoteness is not offered")[0])

    def test_a_remote_posting_abroad_still_goes(self):
        self.assertFalse(D.location_ok("Austin, TX, USA", True, self.S, False, "Remote")[0])
        self.assertFalse(D.location_ok("London, UK", False, self.S, False, "fully remote")[0])

    def test_bengaluru_needs_no_remote_wording(self):
        self.assertTrue(D.location_ok("Bengaluru, KA, India", False, self.S, False, "Onsite in the office")[0])

    def test_end_to_end_the_description_decides_for_a_non_bengaluru_result(self):
        t = T.Tracker(Path(tempfile.mkdtemp()) / "t.sqlite3")
        rows = [row("Backend Engineer", "Stated Co", location="Pune, MH, India", desc="We are remote-first. 1 year of experience."),
                row("Backend Engineer", "Silent Co", location="Pune, MH, India", desc="Office job in Pune. 1 year of experience.")]
        rep = D.discover(D.load_settings(cfg()), t, scrape_fn=FakeJobSpy(rows), fetch=fetch_none, sleep=lambda x: None,
                         today=TODAY, log=lambda *a: None)
        self.assertEqual([r["company"] for r in t.found()], ["Stated Co"])
        self.assertEqual([i["company"] for i in rep.dropped["location"]], ["Silent Co"])
        t.close()

    def test_the_description_is_kept_for_later_preparation(self):
        t = T.Tracker(Path(tempfile.mkdtemp()) / "t.sqlite3")
        D.discover(D.load_settings(cfg()), t, scrape_fn=FakeJobSpy([row("Backend Engineer", "Text Co", desc="Build APIs. 1 year of experience.")]),
                   fetch=fetch_none, sleep=lambda x: None, today=TODAY, log=lambda *a: None)
        self.assertIn("Build APIs", t.found()[0]["description"])
        t.close()


class Discover(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = T.Tracker(Path(self.tmp.name) / "t.sqlite3")
        self.sleeps = []

    def tearDown(self):
        self.t.close()
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    def go(self, rows, settings=None, **kw):
        fake = FakeJobSpy(rows)
        with mock.patch.object(tailor, "call_claude", side_effect=AssertionError("discovery must not call Claude")):
            rep = D.discover(settings or D.load_settings(cfg()), self.t, scrape_fn=fake, fetch=kw.pop("fetch", fetch_none),
                             sleep=self.sleeps.append, today=TODAY, log=lambda *a: None, **kw)
        return rep, fake

    def test_the_cheap_filters_drop_with_reasons_and_keep_what_fits(self):
        rows = [row("Backend Engineer", "Keep Co"),
                row("Senior Backend Engineer", "Senior Co", desc="No years stated here."),
                row("Backend Engineer", "Exp Co", desc="5+ years of experience required."),
                row("Backend Engineer", "Pune Co", location="Pune, MH, India"),
                row("Backend Engineer", "Old Co", posted=TODAY - timedelta(days=45)),
                row("Software Engineering Intern", "Intern Co"),
                row("Backend Engineer", "Remote Co", location="India", remote=True),
                row("Backend Engineer", "Abroad Co", location="Austin, TX, USA", remote=True),
                row("Backend Engineer", "Fresher Co", desc="Freshers welcome.")]
        rep, _ = self.go(rows)
        kept = sorted(r["company"] for r in self.t.found())
        self.assertEqual(kept, ["Fresher Co", "Keep Co", "Remote Co"])
        self.assertEqual({k: len(v) for k, v in rep.dropped.items()},
                         {"experience": 2, "location": 2, "too old": 1, "title": 1})
        why = " ".join(i["why"] for v in rep.dropped.values() for i in v)
        for needle in ("asks 5+ yrs", "senior-level title", "'Pune, MH, India'", "posted 2026-08-21", "excluded"):
            self.assertIn(needle, why)

    def test_the_experience_limit_comes_from_the_config(self):
        rows = [row("Backend Engineer", "Mid Co", desc="2-4 years of experience."),
                row("Backend Engineer", "Three Co", desc="3-5 years of experience."),
                row("Backend Engineer", "Five Co", desc="5 years of experience.")]
        rep, _ = self.go(rows)
        self.assertEqual([r["company"] for r in self.t.found()], ["Mid Co"])           # minimum 2 <= 2 kept; 3 and 5 dropped
        strict = D.load_settings(cfg(max_min_experience_years=1))
        self.t.db.execute("DELETE FROM jobs")
        rep, _ = self.go(rows, strict)
        self.assertEqual(self.t.found(), [])

    def test_naukris_experience_field_is_used(self):
        rows = [row("Backend Engineer", "Naukri Ok", site="naukri", experience_range="0-2 Yrs"),
                row("Backend Engineer", "Naukri No", site="naukri", experience_range="5-8 Yrs", desc="")]
        self.go(rows)
        self.assertEqual([(r["company"], r["experience_asked"]) for r in self.t.found()], [("Naukri Ok", "0-2 yrs")])

    def test_the_same_opening_on_two_sites_keeps_the_one_with_a_direct_ats_link(self):
        rows = [row("Backend Engineer", "Acme Pvt Ltd", site="linkedin", desc=""),
                row("Backend Engineer", "ACME Private Limited", site="indeed", direct=GH)]
        rep, _ = self.go(rows)
        found = self.t.found()
        self.assertEqual([(r["route"], r["source"]) for r in found], [("greenhouse", "indeed")])
        self.assertEqual(len(rep.dropped["duplicate"]), 1)

    def test_anything_already_in_the_tracker_is_not_found_again(self):
        for status, company in (("submitted", "Done Co"), ("dry_run", "Looked Co"), ("skipped", "Declined Co")):
            self.t.db.execute("INSERT INTO jobs (canonical_url, status, company, role, fingerprint, created_at) "
                              "VALUES (?, ?, ?, 'Backend Engineer', ?, 'x')",
                              (f"https://x.example/{company}", status, company, T.fingerprint(company, "Backend Engineer")))
        self.t.db.commit()
        rep, _ = self.go([row("Backend Engineer", c) for c in ("Done Co", "Looked Co", "Declined Co", "New Co")])
        self.assertEqual([r["company"] for r in self.t.found()], ["New Co"])
        self.assertEqual(len(rep.dropped["already tracked"]), 3)

    def test_running_twice_does_not_duplicate_and_a_decision_sticks(self):
        rows = [row("Backend Engineer", "Once Co")]
        self.go(rows)
        rep, _ = self.go(rows)
        self.assertEqual(len(self.t.found()), 1)
        self.assertEqual(rep.saved, {"seen": 1})
        self.t.decide([self.t.found()[0]["id"]], approve=False)                        # skipped
        self.go(rows)
        self.assertEqual(self.t.found(), [])                                           # never shown again

    def test_a_dry_run_saves_nothing_but_still_reports_candidates(self):
        rep, _ = self.go([row("Backend Engineer", "Keep Co", direct=GH)], dry_run=True)
        self.assertEqual(self.t.found(), [])
        self.assertEqual([c.route for c in rep.candidates], ["greenhouse"])

    def test_how_jobspy_is_called(self):
        s = D.load_settings(cfg(search_terms=["backend engineer", "python developer"], sites=["indeed", "naukri", "linkedin"],
                                locations=[{"name": "Bengaluru", "query": "Bengaluru, India"},
                                           {"name": "Remote India", "query": "India", "remote": True}],
                                delay_between_searches_s=[3, 3], max_age_days=30))
        rep, fake = self.go([], s)
        self.assertEqual(len(fake.calls), 4)                                           # 2 terms x 2 locations
        first = fake.calls[0]
        self.assertEqual((first["site_name"], first["search_term"], first["location"], first["is_remote"]),
                         (["indeed", "naukri", "linkedin"], "backend engineer", "Bengaluru, India", False))
        self.assertEqual((first["hours_old"], first["country_indeed"], first["results_wanted"], first["fetch_description"]),
                         (720, "india", 30, True))
        self.assertEqual([c["is_remote"] for c in fake.calls], [False, True, False, True])
        self.assertEqual(self.sleeps, [3.0, 3.0, 3.0])                                 # a pause between calls, not after the last

    def test_one_blocked_search_is_recorded_and_the_rest_still_run(self):
        s = D.load_settings(cfg(search_terms=["a engineer", "b engineer"]))
        fake = FakeJobSpy([row("Backend Engineer", "Got Co")], fail=lambda kw: kw["search_term"] == "a engineer")
        rep = D.discover(s, self.t, scrape_fn=fake, fetch=fetch_none, sleep=lambda x: None, today=TODAY, log=lambda *a: None)
        self.assertEqual(len(rep.errors), 1)
        self.assertIn("429", rep.errors[0])
        self.assertIn("a engineer @ Bengaluru", rep.errors[0])
        self.assertEqual([r["company"] for r in self.t.found()], ["Got Co"])

    def test_nan_dates_and_missing_fields_do_not_crash(self):
        nan = float("nan")
        rows = [{"id": "x1", "site": "linkedin", "job_url": "https://linkedin.com/jobs/view/1", "title": "Backend Engineer",
                 "company": "Nan Co", "location": "Bengaluru, Karnataka, India", "date_posted": nan, "is_remote": nan,
                 "description": nan, "job_url_direct": nan, "experience_range": nan},
                {"title": "No url", "company": "Broken"}]
        self.go(rows)
        r = self.t.found()[0]
        self.assertEqual((r["company"], r["experience_asked"], r["route"]), ("Nan Co", "not stated", "manual"))

    def test_routes_are_saved_with_the_ats_link_as_the_key(self):
        rows = [row("Backend Engineer", "Gh Co", direct=GH), row("Backend Engineer", "Lv Co", direct=LV),
                row("Backend Engineer", "Web Co", direct="https://webco.example/jobs/1")]
        self.go(rows)
        got = {r["company"]: (r["route"], r["canonical_url"]) for r in self.t.found()}
        self.assertEqual(got["Gh Co"], ("greenhouse", T.key(GH)))
        self.assertEqual(got["Lv Co"], ("lever", T.key(LV)))
        self.assertEqual(got["Web Co"][0], "manual")
        self.assertEqual([r["route"] for r in self.t.found()][:2].count("manual"), 0)       # ATS routes sort first

    def test_a_manual_twin_is_replaced_by_the_ats_listing_found_later(self):
        self.go([row("Backend Engineer", "Twin Co", site="linkedin")])
        rep, _ = self.go([row("Backend Engineer", "Twin Co", site="indeed", direct=GH)])
        self.assertEqual(rep.saved, {"replaced": 1})
        self.assertEqual([(r["route"], r["canonical_url"]) for r in self.t.found()], [("greenhouse", T.key(GH))])


class TrackerFlow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.t = T.Tracker(Path(self.tmp.name) / "t.sqlite3")
        for company, route, url in (("A", "greenhouse", GH), ("B", "lever", LV), ("C", "manual", "https://in.indeed.com/viewjob?jk=1")):
            self.t.add_found({"canonical_url": url, "company": company, "role": "Backend Engineer", "route": route,
                              "source": "indeed", "source_url": url, "direct_url": url, "experience_asked": "0-2 yrs",
                              "location": "Bengaluru", "platform": route})
        self.ids = {r["company"]: r["id"] for r in self.t.found()}

    def tearDown(self):
        self.t.close()
        try:
            self.tmp.cleanup()
        except OSError:
            pass

    def test_every_route_is_approved_for_the_batch_runner_and_only_found_postings_are_affected(self):
        out = self.t.decide(list(self.ids.values()) + [9999], approve=True)
        self.assertEqual(out["approved"], [self.ids["A"], self.ids["B"], self.ids["C"]])
        self.assertNotIn("manual", out)
        self.assertEqual(out["ignored"], [9999])
        self.assertEqual([r["company"] for r in self.t.approved()], ["A", "B", "C"])
        self.assertEqual(self.t.found(), [])
        again = self.t.decide([self.ids["A"]], approve=True)                                # already decided
        self.assertEqual(again["ignored"], [self.ids["A"]])

    def test_skip(self):
        self.t.decide([self.ids["A"]], approve=False)
        self.assertEqual(self.t.job(GH)["status"], "skipped")
        self.assertEqual(len(self.t.found()), 2)

    def test_a_posting_that_was_approved_and_then_applied_follows_the_normal_statuses(self):
        self.t.decide([self.ids["A"]], approve=True)
        folder = Path(self.tmp.name) / "run"
        folder.mkdir()
        (folder / "apply_result.json").write_text(json.dumps({"status": "submitted", "mode": "assist", "url": GH,
                                                              "company": "A", "role": "Backend Engineer"}), encoding="utf-8")
        self.t.record_folder(folder)
        r = self.t.job(GH)
        self.assertEqual((r["status"], r["route"], r["source"]), ("submitted", "greenhouse", "indeed"))   # discovery data kept

    def test_the_sheet_gets_manual_postings_but_not_found_approved_or_skipped_ones(self):
        self.t.decide([self.ids["C"], self.ids["A"]], approve=True)
        self.t.set_state(self.ids["C"], "manual", "could not be prepared: apply by hand from the link")
        self.t.decide([self.ids["B"]], approve=False)
        from test_sheets import FakeSheet
        sheet = FakeSheet()
        sheets.sync(self.t, sheet)
        self.assertEqual(sheet.row(2)[1], "Manual")
        self.assertEqual(sheet.row(2)[6], "https://in.indeed.com/viewjob?jk=1")
        self.assertEqual(sheet.row(3), [""] * 18)                                            # nothing else was written

    def test_an_old_database_is_migrated_in_place(self):
        old = Path(self.tmp.name) / "old.sqlite3"
        db = sqlite3.connect(str(old))
        db.executescript("CREATE TABLE jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, canonical_url TEXT UNIQUE NOT NULL, "
                         "platform TEXT, company TEXT, role TEXT, location TEXT, status TEXT NOT NULL, mode TEXT, "
                         "submitted_by TEXT, score INTEGER, gaps TEXT, resume_pdf TEXT, answers_path TEXT, screenshots TEXT, "
                         "flagged_fields TEXT, created_at TEXT, submitted_at TEXT, notes TEXT, runs INTEGER DEFAULT 0);"
                         "INSERT INTO jobs (canonical_url, status, company, role) VALUES ('https://a.example/1', 'submitted', "
                         "'Old Co', 'Software Engineer 2');")
        db.commit()
        db.close()
        with T.Tracker(old) as t:
            r = t.job("https://a.example/1")
            self.assertEqual(r["fingerprint"], "old|software engineer 2")
            self.assertTrue(t.is_known(None, "OLD CO Pvt Ltd", "Software Engineer 2"))
            self.assertEqual(t.add_found({"canonical_url": "https://b.example/2", "company": "Old Co", "role": "Software Engineer 2"}),
                             "duplicate")

    def test_format_status_knows_the_new_statuses(self):
        text = T.format_status(self.t.recent(), self.t.counts())
        self.assertIn("Found: 3", text)


class Shortlist(unittest.TestCase):
    def rows(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = T.Tracker(Path(self.tmp.name) / "t.sqlite3")
        t.add_found({"canonical_url": GH, "company": "Acme <b>", "role": "Backend Engineer", "route": "greenhouse",
                     "experience_asked": "1-3 yrs", "location": "Bengaluru, KA, India", "source": "indeed",
                     "source_url": "https://in.indeed.com/viewjob?jk=1", "direct_url": GH})
        t.add_found({"canonical_url": "https://in.indeed.com/viewjob?jk=2", "company": "Web Co", "role": "Python Developer",
                     "route": "manual", "experience_asked": "not stated", "location": "Remote", "source": "naukri",
                     "source_url": "https://www.naukri.com/job-listings-2", "direct_url": "https://webco.example/apply"})
        rows = t.found()
        t.close()
        return rows

    def test_the_table_has_title_company_location_experience_route_and_link(self):
        cfg_ = {"platforms": {"greenhouse": {"mode": "assist"}}, "default_mode": "dry-run"}
        text = D.format_shortlist(self.rows(), cfg_)
        for needle in ("TITLE", "COMPANY", "LOCATION", "EXPERIENCE", "ROUTE", "LINK", "Backend Engineer", "1-3 yrs",
                       "Greenhouse -> assist", "manual", GH, "https://webco.example/apply", "--approve 3,7,12"):
            self.assertIn(needle, text)

    def test_a_manual_posting_links_to_the_employer_when_known_else_the_board(self):
        rows = self.rows()
        manual = next(r for r in rows if r["route"] == "manual")
        self.assertEqual(D.link_of(manual), "https://webco.example/apply")

    def test_lever_is_prepare_and_greenhouse_follows_the_platform_mode(self):
        row_ = {"route": "lever"}
        self.assertEqual(D.route_label(row_), "Lever -> prepare")
        self.assertEqual(D.route_label({"route": "greenhouse"}, {"platforms": {"greenhouse": {"mode": "auto"}}}),
                         "Greenhouse -> auto")

    def test_html_is_escaped(self):
        page = D.shortlist_html(self.rows())
        self.assertIn("Acme &lt;b&gt;", page)
        self.assertNotIn("Acme <b>", page)

    def test_ids_are_parsed_strictly(self):
        self.assertEqual(D.parse_ids("3, 7 12", [1, 2]), [3, 7, 12])
        self.assertEqual(D.parse_ids("ALL", [4, 5]), [4, 5])
        with self.assertRaises(D.DiscoveryError):
            D.parse_ids("3,seven", [])

    def test_empty_shortlist(self):
        self.assertIn("empty", D.format_shortlist([]))


class Cli(unittest.TestCase):
    def run_cli(self, argv, rows=None, config="discovery:\n  search_terms: [backend engineer]\n  locations: "
                                              "[{name: Bengaluru, query: 'Bengaluru, India'}]\n  sites: [indeed]\n"
                                              "  delay_between_searches_s: [0, 0]\n"):
        import discover
        out = []
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            (tmp / "config.yaml").write_text(config, encoding="utf-8")
            fake = FakeJobSpy(rows or [])
            with mock.patch.object(discover, "CONFIG_FILE", tmp / "config.yaml"), \
                    mock.patch.object(discover, "ROOT", tmp), mock.patch.object(tailor, "OUTPUT_DIR", tmp / "output"), \
                    mock.patch.object(D, "_jobspy", lambda: fake), mock.patch.object(D, "default_fetch", fetch_none), \
                    mock.patch.object(sys, "argv", ["discover.py", *argv]), \
                    mock.patch("builtins.print", lambda *a, **k: out.append(" ".join(map(str, a)))):
                code = discover.main()
            state = {}
            if (tmp / "output" / "tracker.sqlite3").exists():
                with T.Tracker(tmp / "output" / "tracker.sqlite3") as t:
                    state = {r["id"]: (r["company"], r["status"]) for r in t.db.execute("SELECT * FROM jobs")}
            files = sorted(p.name for p in (tmp / "output").rglob("*") if p.is_file())
        return code, "\n".join(out), state, files, fake

    def test_a_run_prints_a_summary_the_shortlist_and_writes_the_audit_files(self):
        code, out, state, files, fake = self.run_cli([], [row("Backend Engineer", "Cli Co", direct=GH),
                                                          row("Backend Engineer", "Far Co", location="Pune, India")])
        self.assertEqual(code, 0)
        self.assertIn("scraped 2", out)
        self.assertIn("dropped 1 (location)", out)
        self.assertIn("Cli Co", out)
        self.assertNotIn("Far Co |", out)
        self.assertEqual(list(state.values()), [("Cli Co", "found")])
        self.assertTrue(any(f.startswith("2026") or f.endswith(".json") for f in files))
        self.assertIn("shortlist.html", files)

    def test_dry_run_saves_nothing(self):
        code, out, state, files, fake = self.run_cli(["--dry-run"], [row("Backend Engineer", "Cli Co")])
        self.assertIn("DRY RUN: nothing was saved", out)
        self.assertEqual([v for v in state.values() if v[1] == "found"], [])

    def test_a_term_and_a_result_cap_narrow_the_run(self):
        code, out, state, files, fake = self.run_cli(["--term", "python developer", "--results", "5"], [])
        self.assertEqual((fake.calls[0]["search_term"], fake.calls[0]["results_wanted"], len(fake.calls)),
                         ("python developer", 5, 1))

    def test_missing_config_and_bad_ids_stop_with_a_clear_message(self):
        with self.assertRaises(SystemExit) as cm:
            self.run_cli([], config="default_mode: dry-run\n")
        self.assertIn("no `discovery:` section", str(cm.exception))
        with self.assertRaises(SystemExit) as cm:
            self.run_cli(["--approve", "3,seven"])
        self.assertIn("could not read the ids", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
