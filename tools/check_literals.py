"""M-02: fail on numeric literals in strategy code.

Allowed: 0, 1, 100, anything inside the config package, and units.py (declared conversions).
Run: python tools/check_literals.py [paths...]
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

ALLOWED = {0, 1, 100}
EXEMPT_PARTS = {"config"}
EXEMPT_FILES = {"units.py"}


def find_literals(source: str, filename: str = "<src>") -> list[tuple[int, object]]:
    found = []
    for node in ast.walk(ast.parse(source, filename=filename)):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, int | float)
            and not isinstance(node.value, bool)
            and node.value not in ALLOWED
        ):
            found.append((node.lineno, node.value))
    return sorted(found, key=lambda item: item[0])


def is_exempt(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    return path.name in EXEMPT_FILES or bool(EXEMPT_PARTS & set(rel.parts))


def check_tree(root: Path) -> list[str]:
    problems = []
    for path in sorted(root.rglob("*.py")):
        if is_exempt(path, root):
            continue
        for line, value in find_literals(path.read_text(encoding="utf-8"), str(path)):
            problems.append(f"{path}:{line}: numeric literal {value!r}")
    return problems


def main(argv: list[str]) -> int:
    default = Path(__file__).resolve().parent.parent / "src" / "breakout_runner"
    roots = [Path(a) for a in argv] or [default]
    problems = [p for root in roots for p in check_tree(root)]
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
