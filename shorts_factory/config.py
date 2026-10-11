"""Settings loader: YAML files + .env + environment overrides."""
from __future__ import annotations

import os
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .utils import ROOT

CONFIG_DIR = ROOT / "config"


class Cfg(dict):
    """dict with attribute access, recursively."""

    def __getattr__(self, item):
        try:
            val = self[item]
        except KeyError as e:
            raise AttributeError(item) from e
        return Cfg(val) if isinstance(val, dict) and not isinstance(val, Cfg) else val

    def __setattr__(self, key, value):
        self[key] = value


def _apply_env_overrides(cfg: Cfg) -> None:
    if os.environ.get("SHORTS_DAILY_TARGET"):
        cfg["daily_target"] = int(os.environ["SHORTS_DAILY_TARGET"])
    if os.environ.get("SHORTS_MAX_RUNTIME_MINUTES"):
        cfg["max_runtime_minutes"] = float(os.environ["SHORTS_MAX_RUNTIME_MINUTES"])
    if os.environ.get("SHORTS_TTS_ENGINES"):
        cfg["tts"]["engines"] = [e.strip() for e in os.environ["SHORTS_TTS_ENGINES"].split(",") if e.strip()]
    if os.environ.get("SHORTS_LLM_PROVIDERS"):
        cfg["llm"]["providers"] = [p.strip() for p in os.environ["SHORTS_LLM_PROVIDERS"].split(",") if p.strip()]
    if os.environ.get("SHORTS_PUBLISH_MODE"):
        cfg["youtube"]["publish_mode"] = os.environ["SHORTS_PUBLISH_MODE"].strip()
    if os.environ.get("SHORTS_THEMES"):
        cfg["video"]["themes"] = [t.strip() for t in os.environ["SHORTS_THEMES"].split(",") if t.strip()]


def load_settings(path: Path | None = None) -> Cfg:
    load_dotenv(ROOT / ".env", override=False)
    path = path or CONFIG_DIR / "settings.yaml"
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    cfg = Cfg(data)
    _apply_env_overrides(cfg)
    return cfg


def load_sources() -> list[dict]:
    with open(CONFIG_DIR / "sources.yaml", "r", encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("feeds", [])


def load_evergreen() -> list[dict]:
    with open(CONFIG_DIR / "evergreen_topics.yaml", "r", encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("topics", [])
