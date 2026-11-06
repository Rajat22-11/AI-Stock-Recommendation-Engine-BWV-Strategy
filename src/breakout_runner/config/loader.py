"""Load, validate, hash and version the config. Runs before any network call."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .models import Config

SHA_PREFIX_LEN = 8
SECRET_LIKE = re.compile(r"\btpt[_.]")

# Each concept has one home (spec: "One key per concept"). The left side was a second
# name for the concept in the seed config; naming both makes the fix obvious.
DUPLICATE_CONCEPTS = {
    "risk.stop_pct_below_fill": "ledger_mirror.stop_cap_pct",
    "risk.position_pct_of_capital": "ledger_mirror.position_cap_pct",
    "risk.half_size_factor": "ledger_mirror.reduced_size_multiple",
    "trim.trim_fraction": "ledger_mirror.trim_fraction",
    "trim.gain_trigger_pct": "ledger_mirror.trim_multiple",
    "universe.min_market_cap_cr": "screens.common",
    "universe.min_avg_volume_1y": "screens.common",
    "universe.max_debt_to_equity": "screens.common",
    "universe.max_pledged_pct": "screens.common",
}


class ConfigError(Exception):
    """Invalid config. Messages carry key paths and never the offending values."""


def _lookup(tree: Any, dotted: str) -> bool:
    node = tree
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return False
        node = node[part]
    return True


def _scan_for_secrets(node: Any, path: str = "") -> list[str]:
    if isinstance(node, dict):
        return [p for k, v in node.items() for p in _scan_for_secrets(v, f"{path}.{k}".lstrip("."))]
    if isinstance(node, list):
        return [p for i, v in enumerate(node) for p in _scan_for_secrets(v, f"{path}[{i}]")]
    if isinstance(node, str) and SECRET_LIKE.search(node):
        return [path]
    return []


def parse_config(raw: dict[str, Any]) -> Config:
    duplicates = [f"{a} duplicates {b}" for a, b in DUPLICATE_CONCEPTS.items() if _lookup(raw, a)]
    if duplicates:
        raise ConfigError("one key per concept: " + "; ".join(duplicates))
    secrets = _scan_for_secrets(raw)
    if secrets:
        raise ConfigError("secret-like value in config at: " + ", ".join(secrets))
    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        problems = [
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
            for e in exc.errors(include_input=False, include_context=False)
        ]
        raise ConfigError("invalid config: " + "; ".join(problems)) from None


def read_raw(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return raw


def canonical_json(raw: dict[str, Any]) -> str:
    """Stable form: comments and whitespace in the YAML do not survive the parse."""
    return json.dumps(raw, sort_keys=True, separators=(",", ":"), default=str)


def config_hash(raw: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(raw).encode("utf-8")).hexdigest()


def skill_version(config_date: dt.date, sha256: str) -> str:
    return f"runner-{config_date.isoformat()}-{sha256[:SHA_PREFIX_LEN]}"


def load_config(path: Path) -> tuple[Config, str, str]:
    """Return (config, sha256, skill_version) or raise ConfigError."""
    raw = read_raw(path)
    config = parse_config(raw)
    sha = config_hash(raw)
    return config, sha, skill_version(config.config_date, sha)
