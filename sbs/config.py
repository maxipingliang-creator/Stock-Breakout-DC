"""Configuration loading and versioning.

Two YAML files drive the platform:

* ``config/config.yaml``            -> carries ``config_version``
* ``config/universe_filters.yaml``  -> carries ``universe_version``

Both version stamps are recorded on every signal/backtest for reproducibility.
Environment variables (``SBS_*``) override selected values so CI/cron can tweak
behaviour without editing files.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml


def find_project_root(start: Path | None = None) -> Path:
    """Walk upward until we find the directory containing ``pyproject.toml``."""
    here = (start or Path(__file__)).resolve()
    for parent in [here, *here.parents]:
        if (parent / "pyproject.toml").exists():
            return parent
    # Fallback: package parent (sbs/..)
    return Path(__file__).resolve().parents[1]


PROJECT_ROOT = find_project_root()


def _deep_get(data: dict, dotted: str, default: Any = None) -> Any:
    cur: Any = data
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


@dataclass
class Config:
    """In-memory view of the merged configuration."""

    raw: dict = field(default_factory=dict)
    universe: dict = field(default_factory=dict)
    root: Path = PROJECT_ROOT

    # -- version stamps -----------------------------------------------------
    @property
    def config_version(self) -> str:
        return str(self.raw.get("config_version", "0"))

    @property
    def universe_version(self) -> str:
        return str(self.universe.get("universe_version", "0"))

    # -- accessors ----------------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        return _deep_get(self.raw, dotted, default)

    def universe_get(self, dotted: str, default: Any = None) -> Any:
        return _deep_get(self.universe, dotted, default)

    # -- resolved paths -----------------------------------------------------
    def path(self, dotted: str, default: str) -> Path:
        value = self.get(dotted, default)
        p = Path(value)
        return p if p.is_absolute() else (self.root / p)

    @property
    def db_path(self) -> str:
        """Either a SQLite file path or a SQLAlchemy-style URL (prod)."""
        url = os.environ.get("SBS_DB_URL")
        if url:
            return url
        return str(self.path("database.path", "data/sbs.sqlite"))

    @property
    def cache_dir(self) -> Path:
        return self.path("data.cache_dir", "data/cache")

    @property
    def reports_dir(self) -> Path:
        return self.path("reporting.output_dir", "public/reports")

    @property
    def default_provider(self) -> str:
        return os.environ.get("SBS_PROVIDER") or self.get("data.default_provider", "synthetic")


@lru_cache(maxsize=8)
def load_config(config_path: str | None = None, universe_path: str | None = None) -> Config:
    """Load and cache the configuration. Pass explicit paths in tests."""
    root = PROJECT_ROOT
    cfg_file = Path(config_path) if config_path else root / "config" / "config.yaml"
    uni_file = Path(universe_path) if universe_path else root / "config" / "universe_filters.yaml"

    raw = _read_yaml(cfg_file)
    universe = _read_yaml(uni_file)
    return Config(raw=raw, universe=universe, root=root)


def _read_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}
