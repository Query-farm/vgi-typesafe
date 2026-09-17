# Copyright 2026 Query Farm LLC - https://query.farm

"""The package must be installable, and runnable, by someone who is not us.

Every check here failed for real at some point in this project's short life:
the entry script carried no PEP-723 header, so ``ATTACH ... LOCATION 'uv run
typesafe_worker.py'`` died with ``ModuleNotFoundError: No module named 'vgi'``
anywhere but the project directory; ``pyproject.toml`` declared ``license =
"MIT"`` with no LICENSE file behind it; and a ``[tool.uv.sources]`` path pin
made the project syncable only on the author's laptop.

None of the other 214 tests can see any of that — they all run from inside a
synced venv, which is exactly the environment these bugs hide in.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
PYPROJECT = tomllib.loads((PROJECT / "pyproject.toml").read_text())
#: The scripts a LOCATION can point at; both are run by `uv run <script>`.
ENTRY_SCRIPTS = ("typesafe_worker.py", "serve.py")


def _pep723_dependencies(script: Path) -> list[str]:
    """The dependency names declared in a script's ``# /// script`` header."""
    text = script.read_text()
    block = re.search(r"^# /// script\n(.*?)^# ///$", text, re.M | re.S)
    assert block, f"{script.name} has no PEP-723 header; `uv run` cannot resolve it standalone"
    meta = tomllib.loads(
        "".join(line.removeprefix("# ").removeprefix("#") for line in block.group(1).splitlines(True))
    )
    return [re.split(r"[<>=!\[]", dep)[0].strip() for dep in meta.get("dependencies", [])]


def _requirement_names(specs: list[str]) -> set[str]:
    return {re.split(r"[<>=!\[]", spec)[0].strip() for spec in specs}


class TestEntryScriptsAreSelfContained:
    """A LOCATION is run from DuckDB's cwd, not ours, on a machine that may lack the project."""

    @pytest.mark.parametrize("name", ENTRY_SCRIPTS)
    def test_declares_every_runtime_dependency(self, name: str) -> None:
        declared = set(_pep723_dependencies(PROJECT / name))
        required = _requirement_names(PYPROJECT["project"]["dependencies"])
        missing = required - declared
        assert not missing, (
            f"{name}'s PEP-723 header is missing {sorted(missing)} from [project.dependencies]"
        )

    @pytest.mark.parametrize("name", ENTRY_SCRIPTS)
    def test_declares_nothing_unknown(self, name: str) -> None:
        """A stray dependency here is one nobody sees until an install fails."""
        extras = _requirement_names(PYPROJECT["project"]["optional-dependencies"]["serve"])
        known = _requirement_names(PYPROJECT["project"]["dependencies"]) | extras | {"vgi-rpc"}
        unknown = set(_pep723_dependencies(PROJECT / name)) - known
        assert not unknown, f"{name} declares {sorted(unknown)}, which is in no dependency group"

    @pytest.mark.parametrize("name", ENTRY_SCRIPTS)
    def test_imports_only_this_package(self, name: str) -> None:
        """The script must be a shim: `uv run` puts its directory on sys.path, nothing more."""
        body = [
            line
            for line in (PROJECT / name).read_text().splitlines()
            if line.startswith(("import ", "from ")) and "__future__" not in line
        ]
        assert body and all("vgi_typesafe" in line for line in body), (
            f"{name} imports more than vgi_typesafe: {body}"
        )


class TestLicensing:
    def test_the_declared_license_is_actually_shipped(self) -> None:
        assert PYPROJECT["project"]["license"] == "MIT"
        text = (PROJECT / "LICENSE").read_text()
        assert text.startswith("MIT License")
        assert text.rstrip().endswith("SOFTWARE.")

    def test_the_copyright_names_query_farm(self) -> None:
        assert "Copyright (c) 2026 Query Farm LLC - https://query.farm" in (PROJECT / "LICENSE").read_text()

    def test_every_module_carries_the_copyright_header(self) -> None:
        modules = sorted(PROJECT.glob("vgi_typesafe/*.py")) + [PROJECT / n for n in ENTRY_SCRIPTS]
        missing = [p.name for p in modules if not p.read_text().startswith("# Copyright 2026 Query Farm LLC")]
        assert not missing, f"missing the copyright header: {missing}"


class TestInstallableByAnyone:
    def test_no_local_path_sources(self) -> None:
        """A path pin (e.g. vgi-python = {path = '../vgi-python'}) breaks CI and every other machine."""
        assert "sources" not in PYPROJECT.get("tool", {}).get("uv", {}), (
            "[tool.uv.sources] pins a dependency to a local checkout; the project is then "
            "installable only where that sibling directory exists"
        )

    def test_dependencies_are_released_versions(self) -> None:
        for spec in PYPROJECT["project"]["dependencies"]:
            assert "@" not in spec and "file://" not in spec, f"{spec} is not a released requirement"

    def test_typing_is_advertised(self) -> None:
        assert (PROJECT / "vgi_typesafe" / "py.typed").is_file(), "annotated package without a PEP 561 marker"
        assert "Typing :: Typed" in PYPROJECT["project"]["classifiers"]

    def test_the_http_entry_point_is_reachable(self) -> None:
        """`main_http` was defined for a while with no way to invoke or install it."""
        scripts = PYPROJECT["project"]["scripts"]
        assert scripts["vgi-typesafe-http"] == "vgi_typesafe.worker:main_http"
        assert PYPROJECT["project"]["optional-dependencies"]["serve"], "no extra installs the HTTP transport"
