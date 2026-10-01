"""ATS adapters. `adapter_for(platform)` returns the adapter for a Job's platform."""
from __future__ import annotations

from jobbot.ats.base import ATS
from jobbot.ats.greenhouse import Greenhouse
from jobbot.ats.lever import Lever

_ADAPTERS = {"greenhouse": Greenhouse, "lever": Lever}


def adapter_for(platform: str) -> ATS:
    try:
        return _ADAPTERS[platform]()
    except KeyError:
        raise KeyError(f"no adapter for platform {platform!r} (supported: {sorted(_ADAPTERS)})") from None
