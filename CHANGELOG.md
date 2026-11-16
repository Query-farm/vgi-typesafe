# Changelog

All notable changes to `vgi-typesafe` are documented here.

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
