# Copyright 2026 Query Farm LLC - https://query.farm

"""Docstring-consistency gate.

Runs pydoclint over ``vgi_typesafe/`` as part of the test suite. It complements
ruff's ``D`` rules: ruff checks a docstring's *shape*, while pydoclint verifies
that the documented arguments, returns and dataclass attributes actually match
the code. Configuration lives in ``[tool.pydoclint]`` in ``pyproject.toml`` —
this test invokes the same CLI, so there is no second copy of the rule set.

pydoclint runs through ``uvx`` (an isolated, ephemeral environment) rather than
as a project dependency. It requires ``docstring-parser-fork`` while ``vgi-rpc``
(a runtime dependency) requires the upstream ``docstring-parser``; both own the
``docstring_parser`` import namespace, so installing pydoclint into the project
env clobbers it non-deterministically and breaks unrelated imports. Running via
uvx keeps the fork out of the project tree.

Mirrors ``tests/test_docstrings.py`` in vgi-python, which is where this pattern
and its configuration come from.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

# A real violation line looks like ``    42: DOC101: ...``. If pydoclint exits
# non-zero without emitting any such code, it failed to *run* rather than
# finding violations — that is an environment problem, not a code problem.
_VIOLATION_RE = re.compile(r"\bDOC\d{3}\b")


def test_pydoclint_clean() -> None:
    """``vgi_typesafe/`` must pass the pydoclint gate (config in pyproject.toml)."""
    uvx = shutil.which("uvx") or shutil.which("uv")
    if uvx is None:  # pragma: no cover - uv is always present in dev/CI
        pytest.skip("uv/uvx is not available to run pydoclint")

    # Pin the ephemeral env to *this* interpreter: pydoclint parses with its own
    # runtime's AST, so it must run on a Python new enough for the repo's syntax.
    # uvx's default interpreter may be older and would report spurious DOC002s.
    base = [uvx] if Path(uvx).name == "uvx" else [uvx, "tool", "run"]
    cmd = [*base, "--python", sys.executable, "pydoclint", "--config", "pyproject.toml", "vgi_typesafe/"]
    result = subprocess.run(cmd, cwd=_REPO_ROOT, capture_output=True, text=True)
    output = result.stdout + result.stderr

    if result.returncode == 0:
        return

    if not _VIOLATION_RE.search(output):  # pragma: no cover - env-dependent (e.g. offline uvx)
        pytest.skip(f"pydoclint could not run via uvx:\n{output}")

    pytest.fail(f"pydoclint found docstring violations:\n\n{output}")
