# Copyright 2026 Query Farm LLC - https://query.farm

# /// script
# requires-python = ">=3.13"
# dependencies = [
#     "vgi-python[http]>=0.33.0",
#     "vgi-rpc>=0.46.0",
#     "httpx>=0.27",
# ]
# ///
"""HTTP entry point for the TypeSafe VGI worker.

    uv run /path/to/serve.py --port 8000

Then attach over HTTP instead of spawning a subprocess per connection::

    ATTACH 'typesafe' (TYPE vgi, LOCATION 'http://127.0.0.1:8000');

See ``typesafe_worker.py`` for why the PEP-723 header above matters.
"""

from __future__ import annotations

from vgi_typesafe.worker import main_http

if __name__ == "__main__":
    main_http()
