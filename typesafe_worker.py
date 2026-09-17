# Copyright 2026 Query Farm LLC - https://query.farm

# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "vgi-python[http]>=0.33.0",
#     "vgi-rpc>=0.46.0",
#     "httpx>=0.27",
# ]
# ///
"""Stdio entry point for the TypeSafe VGI worker.

    ATTACH 'typesafe' (TYPE vgi, LOCATION 'uv run /path/to/typesafe_worker.py');

The PEP-723 header above is load-bearing: it lets ``uv run`` resolve the
worker's dependencies from an ephemeral environment, so the ATTACH works from
any directory and on a machine that has never seen this project. Without it the
script only runs from inside the project with a pre-synced venv — which looks
fine in a local test and fails for everyone else.

The dependency list must stay in step with ``[project.dependencies]``;
``tests/test_packaging.py`` asserts it.
"""

from __future__ import annotations

from vgi_typesafe.worker import main

if __name__ == "__main__":
    main()
