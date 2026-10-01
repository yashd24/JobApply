"""config.yaml: the mode per platform. Missing file -> safe defaults (dry-run)."""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG_FILE = ROOT / "config.yaml"
MODES = ("dry-run", "assist", "auto")
DEFAULTS = {"default_mode": "dry-run", "platforms": {}}      # no config.yaml -> every platform is a dry run


class ConfigError(Exception):
    pass


def load_config(path: Path | None = None) -> dict:
    path = path or CONFIG_FILE
    cfg = {"default_mode": DEFAULTS["default_mode"],
           "platforms": {k: dict(v) for k, v in DEFAULTS["platforms"].items()}}
    if path.exists():
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ConfigError(f"{path.name} must be a mapping")
        cfg["default_mode"] = raw.get("default_mode", cfg["default_mode"])
        for name, body in (raw.get("platforms") or {}).items():
            cfg["platforms"].setdefault(name, {}).update(body or {})
    for where, mode in [("default_mode", cfg["default_mode"])] + [(f"platforms.{p}.mode", b.get("mode"))
                                                                  for p, b in cfg["platforms"].items()]:
        if mode not in MODES:
            raise ConfigError(f"{where} must be one of {MODES}, got {mode!r}")
    return cfg


def requested_mode(cfg: dict, platform: str, cli_mode: str | None = None, dry_run_flag: bool = False) -> str:
    """--dry-run wins over everything; then --mode; then the platform's mode from config; then the default.
    While the default is dry-run, an unconfigured platform is a dry run."""
    if dry_run_flag:
        return "dry-run"
    if cli_mode:
        if cli_mode not in MODES:
            raise ConfigError(f"--mode must be one of {MODES}")
        return cli_mode
    return cfg["platforms"].get(platform, {}).get("mode") or cfg["default_mode"]
