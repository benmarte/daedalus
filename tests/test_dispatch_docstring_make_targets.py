"""The dispatcher module docstring points at the make targets (issue #1335)."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import conftest  # noqa: E402
from conftest import check  # noqa: E402,F401

ROOT = Path(__file__).resolve().parent.parent


def _module_docstring() -> str:
    """Parse scripts/daedalus_dispatch.py and return its module docstring.

    Parsing the source (rather than importing) avoids the module's import-time
    side effects — this is a pure docstring assertion.
    """
    src = (ROOT / "scripts" / "daedalus_dispatch.py").read_text()
    return ast.get_docstring(ast.parse(src)) or ""


def test_docstring_mentions_make_targets():
    doc = _module_docstring()
    check("docstring mentions `make test`", "make test" in doc)
    check("docstring mentions `make lint`", "make lint" in doc)


if __name__ == "__main__":
    print("dispatch-docstring make-targets tests (issue #1335)")
    print("-" * 60)
    for _name, _fn in sorted(
        (n, f) for n, f in globals().items() if n.startswith("test_") and callable(f)
    ):
        _fn()
    print("-" * 60)
    print(f"Results: {conftest._passed} passed, {conftest._failed} failed")
    sys.exit(1 if conftest._failed else 0)
