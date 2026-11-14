# Copyright 2026 Query Farm LLC - https://query.farm

"""Execute the notebooks in fresh kernels and save their outputs after success."""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic

import nbformat
from jupyter_client import KernelManager
from jupyter_client.kernelspec import KernelSpecManager
from nbclient import NotebookClient


def main() -> None:
    """Run selected notebooks, or all numbered examples, with this interpreter."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("notebooks", nargs="*", type=Path)
    parser.add_argument(
        "--parameters",
        default="{}",
        help="JSON object overriding visible parameters in a copy.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="Save executed copies here, leaving the originals unchanged.",
    )
    parser.add_argument("--timeout", type=int, default=600, help="Maximum seconds per cell.")
    args = parser.parse_args()
    overrides = json.loads(args.parameters)
    if not isinstance(overrides, dict):
        parser.error("--parameters must be a JSON object")
    if overrides and args.output_dir is None:
        parser.error(
            "Use --output-dir with overrides to preserve the saved notebook's parameters and outputs."
        )
    if args.output_dir:
        args.output_dir = args.output_dir.resolve()
        args.output_dir.mkdir(parents=True, exist_ok=True)
    directory = Path(__file__).resolve().parent
    if args.output_dir == directory:
        parser.error("--output-dir must differ from the notebook source directory.")
    paths = args.notebooks or sorted(directory.glob("[0-9][0-9]_*.ipynb"))
    os.environ["PLOOMBER_STATS_ENABLED"] = "false"

    # A temporary spec avoids changing the user's registered Jupyter kernels.
    with TemporaryDirectory(prefix="vgi-typesafe-kernel-") as temporary:
        kernel_dir = Path(temporary) / "typesafe-examples"
        kernel_dir.mkdir()
        (kernel_dir / "kernel.json").write_text(
            json.dumps(
                {
                    "argv": [
                        sys.executable,
                        "-m",
                        "ipykernel_launcher",
                        "-f",
                        "{connection_file}",
                    ],
                    "display_name": "Python (vgi-typesafe notebooks)",
                    "language": "python",
                }
            )
        )
        for path in paths:
            path = path.resolve()
            if args.output_dir and (args.output_dir / path.name).resolve() == path:
                parser.error(
                    "--output-dir must preserve the source notebook; choose a separate directory."
                )
            notebook = nbformat.read(path, as_version=4)
            nbformat.validate(notebook)
            parameter_cell = next(
                c for c in notebook.cells if "parameters" in c.metadata.get("tags", [])
            )
            names = {
                node.targets[0].id
                for node in ast.parse(parameter_cell.source).body
                if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
            }
            if unknown := overrides.keys() - names:
                raise ValueError(f"Unknown parameters for {path.name}: {sorted(unknown)}")
            for key, value in overrides.items():
                parameter_cell.source = re.sub(
                    rf"^{re.escape(key)}\s*=.*$",
                    lambda _, name=key, replacement=value: f"{name} = {replacement!r}",
                    parameter_cell.source,
                    flags=re.MULTILINE,
                )
            manager = KernelManager(
                kernel_name="typesafe-examples",
                kernel_spec_manager=KernelSpecManager(kernel_dirs=[temporary]),
            )
            print(f"Running {path.name} ...", flush=True)
            start = monotonic()
            client = NotebookClient(
                notebook,
                km=manager,
                timeout=args.timeout,
                allow_errors=False,
                resources={"metadata": {"path": str(directory)}},
            )
            try:
                client.execute(cleanup_kc=True)
            except Exception:
                print(f"FAILED: {path.name}; saved notebook left unchanged.", flush=True)
                raise
            nbformat.validate(notebook)
            destination = args.output_dir / path.name if args.output_dir else path
            staged = destination.with_suffix(".ipynb.tmp")
            nbformat.write(notebook, staged)
            staged.replace(destination)
            if args.output_dir:
                for asset_directory in ("assets", "fixtures"):
                    shutil.copytree(
                        directory / asset_directory,
                        args.output_dir / asset_directory,
                        dirs_exist_ok=True,
                    )
                for companion in (
                    "README.md",
                    "notebook_support.py",
                    "execute.py",
                    "pyproject.toml",
                    "uv.lock",
                    ".gitignore",
                    "ruff.toml",
                    ".python-version",
                ):
                    shutil.copy2(directory / companion, args.output_dir / companion)
            code_cells = [c for c in notebook.cells if c.cell_type == "code"]
            plots = sum("image/png" in o.get("data", {}) for c in code_cells for o in c.outputs)
            print(
                f"OK: {len(code_cells)} code cells, {plots} charts, {monotonic() - start:.1f}s",
                flush=True,
            )
            if args.output_dir:
                print(f"Saved {destination}", flush=True)


if __name__ == "__main__":
    main()
