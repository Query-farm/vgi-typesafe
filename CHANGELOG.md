# Changelog

All notable changes to `vgi-typesafe` are documented here.

## 0.4.0 (2026-11-25)

- Speaks VGI protocol 3.4: require vgi-python `>=0.55.1,<0.56`; uv.lock re-locked. This release
  needs Haybarn 2.0.1 (or stock DuckDB 2.0 with `INSTALL vgi FROM query_farm`): Haybarn 2.0.0
  loads a 3.3 `vgi` extension that cannot attach it.
- SQL tests run on haybarn-unittest 2.0.1rc3.
- `serve` extra is `vgi-python[http,oauth]` (HTTP serving plus the JWT verification Orchard
  hosted mode needs).

## 0.3.2 (2026-11-16)

- Bump vgi-python to 0.52.2 (`>=0.52.2,<0.53`) and vgi-rpc to 0.53.1 in the entry scripts.
- Python 3.14 is now the floor (CI, Docker image, tooling).
- The SQL tests moved from `tests/test_end_to_end.py`, which needed a locally built
  DuckDB and so never ran in CI, to `test/sql/*.test`: run through the published
  DuckDB test runner against the bundled mock over stdio, launch and HTTP by
  `ci/run-integration.sh` (`make test-sql-all`), and in CI. Every published example
  and the README walkthrough are executed there.

## 0.3.1 (2026-11-14)

- Docs link to upstream sources: the catalog docs now link TypeSafe, its documentation,
  the System One model page, and the quickstart that explains how to get an API key
  (vgi-lint VGI183).
