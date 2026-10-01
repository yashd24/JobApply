"""Lever (jobs.lever.co/<site>/<id>/apply). The server-rendered form works with the generic discovery and filler;
the one special control is the "Current location" autocomplete, which filler.py handles.

Lever loads hCaptcha on the page. A captcha that merely might appear does NOT stop a run: only a visible
challenge does (base.challenge -> filler.captcha_challenge_visible)."""
from __future__ import annotations

import re

from jobbot.ats.base import ATS


class Lever(ATS):
    name = "lever"
    success_url = re.compile(r"/thanks(/|\?|$)", re.I)

    def submit_button(self, page):
        for sel in ('button[data-qa="btn-submit"]', "#btn-submit", 'button:has-text("Submit application")'):
            loc = page.locator(sel)
            if loc.count() == 1 and loc.first.is_visible():
                return loc.first
        return super().submit_button(page)
