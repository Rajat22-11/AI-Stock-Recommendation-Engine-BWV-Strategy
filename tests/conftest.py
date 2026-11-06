from __future__ import annotations

from pathlib import Path

import pytest

from breakout_runner.config.loader import load_config, read_raw

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "breakout.config.yaml"
FIXTURES = ROOT / "fixtures"


@pytest.fixture
def cfg():
    return load_config(CONFIG_PATH)[0]


@pytest.fixture
def raw():
    return read_raw(CONFIG_PATH)
