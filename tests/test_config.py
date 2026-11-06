from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest
import yaml

from breakout_runner.config.ledger import (
    LedgerMirrorMismatch,
    check_mirror,
    sync_mirror,
)
from breakout_runner.config.loader import (
    ConfigError,
    config_hash,
    load_config,
    parse_config,
    skill_version,
)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from check_literals import find_literals  # noqa: E402


def test_shipped_config_is_valid(cfg):
    assert cfg.strategy == "breakout"
    assert len(cfg.tapetide.tokens) >= 3


def test_unknown_key_reports_path(raw):
    raw["volume"]["surprise"] = 1
    with pytest.raises(ConfigError, match=r"volume\.surprise"):
        parse_config(raw)


def test_missing_key_reports_path(raw):
    del raw["risk"]["entry_zone_max_above_close_pct"]
    with pytest.raises(ConfigError, match=r"risk\.entry_zone_max_above_close_pct"):
        parse_config(raw)


def test_out_of_range_reports_path(raw):
    raw["ledger_mirror"]["stop_cap_pct"] = 500
    with pytest.raises(ConfigError, match=r"ledger_mirror\.stop_cap_pct"):
        parse_config(raw)


def test_wrong_type_reports_path(raw):
    raw["indicators"]["atr_period"] = "fourteen"
    with pytest.raises(ConfigError, match=r"indicators\.atr_period"):
        parse_config(raw)


def test_rs_weights_must_sum_to_one(raw):
    raw["rs"]["weights"]["m3"] = 0.9
    with pytest.raises(ConfigError, match=r"rs"):
        parse_config(raw)


@pytest.mark.parametrize(
    "section, key, other",
    [
        ("risk", "stop_pct_below_fill", "ledger_mirror.stop_cap_pct"),
        ("risk", "position_pct_of_capital", "ledger_mirror.position_cap_pct"),
        ("risk", "half_size_factor", "ledger_mirror.reduced_size_multiple"),
    ],
)
def test_duplicate_concept_names_both_keys(raw, section, key, other):
    raw[section][key] = 8
    with pytest.raises(ConfigError) as exc:
        parse_config(raw)
    assert f"{section}.{key}" in str(exc.value) and other in str(exc.value)


def test_hash_ignores_comments_and_whitespace(tmp_path, raw):
    a = tmp_path / "a.yaml"
    b = tmp_path / "b.yaml"
    a.write_text(yaml.safe_dump(raw), encoding="utf-8")
    b.write_text("# a comment\n\n" + yaml.safe_dump(raw, indent=4), encoding="utf-8")
    assert load_config(a)[1] == load_config(b)[1]


def test_hash_changes_with_a_value(raw):
    changed = copy.deepcopy(raw)
    changed["volume"]["breakout_min_multiple"] = 2.5
    assert config_hash(raw) != config_hash(changed)


def test_version_format(cfg, raw):
    sha = config_hash(raw)
    assert skill_version(cfg.config_date, sha) == f"runner-2026-11-06-{sha[:8]}"


class StubStore:
    def __init__(self, values):
        self.values = dict(values)
        self.written = None

    def read_ledger_config(self, keys):
        return {k: self.values[k] for k in keys if k in self.values}

    def write_ledger_config(self, values):
        self.written = values
        self.values.update(values)


def test_mirror_passes_when_equal(cfg):
    check_mirror(cfg.ledger_mirror, StubStore(cfg.ledger_mirror.model_dump()))


def test_mirror_mismatch_aborts_with_both_values(cfg):
    values = cfg.ledger_mirror.model_dump() | {"stop_cap_pct": 7}
    with pytest.raises(LedgerMirrorMismatch) as exc:
        check_mirror(cfg.ledger_mirror, StubStore(values))
    assert exc.value.diffs == {"stop_cap_pct": (8.0, 7)}
    assert "yaml=8.0 db=7" in str(exc.value)


def test_mirror_missing_key_is_a_diff(cfg):
    values = cfg.ledger_mirror.model_dump()
    del values["trim_multiple"]
    with pytest.raises(LedgerMirrorMismatch):
        check_mirror(cfg.ledger_mirror, StubStore(values))


def test_sync_requires_confirmation(cfg):
    store = StubStore(cfg.ledger_mirror.model_dump() | {"stop_cap_pct": 7})
    with pytest.raises(PermissionError):
        sync_mirror(cfg.ledger_mirror, store, confirmed=False)
    assert store.written is None


def test_sync_writes_only_the_diff(cfg):
    store = StubStore(cfg.ledger_mirror.model_dump() | {"stop_cap_pct": 7})
    sync_mirror(cfg.ledger_mirror, store, confirmed=True)
    assert store.written == {"stop_cap_pct": 8.0}


def test_token_value_in_env_field_fails_without_echo(raw):
    raw["tapetide"]["tokens"][0]["env"] = "sk-not-a-name-12345"
    with pytest.raises(ConfigError) as exc:
        parse_config(raw)
    assert "tapetide.tokens.0.env" in str(exc.value)
    assert "sk-not-a-name" not in str(exc.value)


def test_tapetide_token_anywhere_fails_without_echo(raw):
    raw["data"]["partial_week_label"] = "tpt_rt_secretvalue"
    with pytest.raises(ConfigError) as exc:
        parse_config(raw)
    assert "data.partial_week_label" in str(exc.value)
    assert "secretvalue" not in str(exc.value)


def test_duplicate_token_env_rejected(raw):
    raw["tapetide"]["tokens"][1]["env"] = raw["tapetide"]["tokens"][0]["env"]
    with pytest.raises(ConfigError, match="env"):
        parse_config(raw)


def test_literal_lint_finds_and_allows():
    src = "a = 0\nb = 1\nc = 100\nd = 2.5\ne = True\nf = -7\n"
    assert [v for _, v in find_literals(src)] == [2.5, 7]


def test_source_tree_has_no_stray_literals():
    from check_literals import check_tree

    root = Path(__file__).resolve().parent.parent / "src" / "breakout_runner"
    assert check_tree(root) == []
