#!/usr/bin/env python3
"""Find jobs with JobSpy, filter them cheaply, and let the user approve a shortlist.

    python discover.py                       scrape the saved searches (config.yaml: discovery), filter, save as "found",
                                             print the shortlist
    python discover.py --dry-run             same, but save nothing
    python discover.py --shortlist           show the current shortlist (found, undecided) without scraping
    python discover.py --approve 3,7,12      approve those ids (or: --approve all)
    python discover.py --skip 2,5            decline those ids (never shown again)
    python discover.py --term "python developer" --results 10     a smaller trial run

Nothing is applied here and no Claude usage is spent. Every approved posting waits ("approved") for the batch runner
(python batch.py), which fills Greenhouse in assist mode and prepares everything else (tailored resume, cover letter,
answer sheet) so it shows up as "Ready for you" with its link.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

import tailor
from jobbot import config as cfgmod
from jobbot import discovery as D
from jobbot import sheets
from jobbot import tracker as T

ROOT = Path(__file__).resolve().parent
CONFIG_FILE = ROOT / "config.yaml"


def _tracker_file() -> Path:
    return tailor.OUTPUT_DIR / "tracker.sqlite3"


def show_shortlist(t: "T.Tracker", cfg: dict, write_html: bool = True) -> str:
    rows = t.found()
    text = D.format_shortlist(rows, cfg)
    if write_html and rows:
        path = tailor.OUTPUT_DIR / "shortlist.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(D.shortlist_html(rows, cfg), encoding="utf-8")
        text += f"\n(also as a clickable page: {path})"
    return text


def run_discovery(args, cfg: dict) -> int:
    settings = D.load_settings(cfg)
    if args.term:
        settings.terms = list(args.term)
    print(f"Discovery: {len(settings.terms)} term(s) x {len(settings.locations)} location(s) on "
          f"{', '.join(settings.sites)}; postings up to {settings.max_age_days} days old; "
          f"{args.results or settings.results_per_search} per search.")
    with T.Tracker(_tracker_file()) as t:
        rep = D.discover(settings, t, results=args.results, dry_run=args.dry_run)
        audit = tailor.OUTPUT_DIR / "discovery"
        audit.mkdir(parents=True, exist_ok=True)
        path = audit / f"{datetime.now():%Y-%m-%d_%H%M%S}.json"
        path.write_text(json.dumps(rep.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")
        print("\n" + rep.summary())
        for e in rep.errors:
            print(f"  search failed: {e}")
        print(f"(everything dropped, with the reason: {path})\n")
        if args.why:
            for kind, items in rep.dropped.items():
                print(f"-- dropped: {kind}")
                for i in items:
                    print(f"   {i['title']} | {i['company']} | {i['site']}: {i['why']}")
            print()
        if args.dry_run:
            print("DRY RUN: nothing was saved. This is what would be added to the shortlist:\n")
            print(D.format_shortlist([j.as_row() for j in rep.candidates], cfg))
        else:
            print(show_shortlist(t, cfg))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Discover jobs with JobSpy and approve a shortlist.")
    ap.add_argument("--shortlist", action="store_true", help="show the current shortlist and exit")
    ap.add_argument("--approve", metavar="IDS", help="approve shortlisted ids: 3,7,12 or all")
    ap.add_argument("--skip", metavar="IDS", help="decline shortlisted ids")
    ap.add_argument("--dry-run", action="store_true", help="scrape and filter, but save nothing")
    ap.add_argument("--term", action="append", help="use only this search term (repeatable)")
    ap.add_argument("--results", type=int, help="results per search (overrides config)")
    ap.add_argument("--why", action="store_true", help="also print every dropped posting with its reason")
    args = ap.parse_args()
    try:
        cfg = cfgmod.load_config(CONFIG_FILE)
        if args.approve or args.skip:
            with T.Tracker(_tracker_file()) as t:
                available = [r["id"] for r in t.found()]
                if args.approve and args.skip:
                    ap.error("use --approve or --skip, not both at once")
                ids = D.parse_ids(args.approve or args.skip, available)
                out = t.decide(ids, approve=bool(args.approve))
                for label, key in (("approved (python batch.py prepares them)", "approved"),
                                   ("skipped", "skipped"), ("ignored (not on the shortlist)", "ignored")):
                    if out[key]:
                        print(f"{label}: {', '.join(map(str, out[key]))}")
            return 0
        if args.shortlist:
            with T.Tracker(_tracker_file()) as t:
                print(show_shortlist(t, cfg))
            return 0
        return run_discovery(args, cfg)
    except (D.DiscoveryError, cfgmod.ConfigError, sheets.SheetsError) as e:
        sys.exit(f"{type(e).__name__}: {e}")


if __name__ == "__main__":
    sys.exit(main())
