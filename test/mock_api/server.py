#!/usr/bin/env python3
# Copyright 2026 Query Farm LLC - https://query.farm
"""The bundled mock TypeSafe endpoint, served for the SQL suite (test/sql/*.test).

It is ``vgi_typesafe.mock_server`` unchanged (stdlib only, so this runs under
``uv run --no-project python``), plus one test-only endpoint the SQL tests use to
assert how many requests a query made:

``GET /__test/requests?tag=<unique>``
    ``[{"n": <count>, "requests": "<JSON array of request bodies>"}]`` for every
    request accepted since the previous tag, then starts a new log. DuckDB's
    httpfs may fetch one URL more than once (HEAD, then one or more GETs), so
    the answer is remembered per tag and a repeat replays it rather than
    resetting again. Use a fresh tag for every read.

Prints ``PORT:<n>`` once listening, like the starter's mock (ci/run-integration.sh
waits for it). Requires ``--api-key`` (default ``test-key``).

    python3 test/mock_api/server.py [--port 0] [--api-key test-key]
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from vgi_typesafe import mock_server  # noqa: E402 - needs the path above


class _Server(mock_server.MockTypeSafeServer):
    """The mock, remembering each request-log answer by its tag."""

    def __init__(self, address: tuple[str, int], api_key: str | None = None) -> None:
        """Bind the mock on ``address`` with the request-log handler."""
        super().__init__(address, api_key=api_key)
        self.RequestHandlerClass = _Handler
        self.tagged_answers: dict[str, list[dict[str, object]]] = {}


class _Handler(mock_server._Handler):
    """The mock's handler with the request-log endpoint added."""

    def do_GET(self) -> None:  # noqa: N802 - http.server's naming
        """Serve ``/__test/requests``; everything else is the mock's."""
        url = urlparse(self.path)
        if url.path != "/__test/requests":
            super().do_GET()
            return
        tag = parse_qs(url.query).get("tag", [""])[0]
        server = self.server
        assert isinstance(server, _Server)
        with server._lock:
            answers = server.tagged_answers
            if tag not in answers:
                answers[tag] = [{"n": len(server.requests), "requests": json.dumps(server.requests)}]
                server.requests.clear()
            payload = answers[tag]
        self._send_list(payload)

    def do_HEAD(self) -> None:  # noqa: N802 - http.server's naming
        """Answer httpfs's probe without consuming the log."""
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()

    def _send_list(self, payload: list[dict[str, object]]) -> None:
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    """Start the mock and print ``PORT:<n>`` once it is listening."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--api-key", default="test-key")
    args = parser.parse_args()
    server = _Server((args.host, args.port), api_key=args.api_key)
    print(f"PORT:{server.server_address[1]}", flush=True)
    with contextlib.suppress(KeyboardInterrupt):
        server.serve_forever()


if __name__ == "__main__":
    main()
