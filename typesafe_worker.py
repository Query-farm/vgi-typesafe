"""Stdio entry point for the TypeSafe VGI worker.

Run from the project directory so ``uv run`` uses the project venv:

    ATTACH 'typesafe' (TYPE vgi, LOCATION 'uv run typesafe_worker.py');
"""

from __future__ import annotations

from vgi_typesafe.worker import main

if __name__ == "__main__":
    main()
