"""Write the config's JSON Schema to docs/ for editors and review.

Run: python tools/export_schema.py [--check]. With --check, fail if the file is out of date.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from breakout_runner.config.models import Config

TARGET = Path(__file__).resolve().parent.parent / "docs" / "breakout.config.schema.json"


def render() -> str:
    return json.dumps(Config.model_json_schema(), indent=2, sort_keys=True) + "\n"


def main(argv: list[str]) -> int:
    if "--check" in argv:
        if not TARGET.exists() or TARGET.read_text("utf-8") != render():
            print(f"{TARGET} is out of date; run tools/export_schema.py", file=sys.stderr)
            return 1
        return 0
    TARGET.write_text(render(), "utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
