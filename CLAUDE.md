# CLAUDE.md — vgi-typesafe

Guidance for AI agents (and humans) working in this repository. The README is the
product doc: what the functions are and how to call them. This is the build doc:
how it fits together, and where it bites.

## What this is

A [VGI](https://query.farm/vgi/) worker exposing [TypeSafe](https://docs.typesafe.ai/introduction)
System One questions to DuckDB. TypeSafe answers *typed* questions about a *state*
and returns structured values, not prose — three question types: `choice` (pick an
option), `noul` (a yes/no probability), `score` (a position on an ordered scale).

## Layout

One module per published function, plus the four they share.

```
typesafe_worker.py        stdio entry point (the ATTACH LOCATION), PEP-723 self-resolving
serve.py                  HTTP entry point (uv run serve.py --port 8000)
vgi-agent-tests.yaml      private graders for the tasks published in vgi.agent_test_tasks
vgi-lint.toml             linter settings + one documented waiver
vgi_typesafe/
  worker.py               the catalog: which functions exist, catalog/schema docs
  ask.py                  ask() — N questions per row, dynamic output schema
  ask_dynamic.py          ask_dynamic() — questions as a per-row column, answers as JSON
  choice.py noul.py score.py    one-question shorthands, flat columns
  is_true.py              the scalar form of a noul
  models.py               models(), registered as both a function and a table
  typesafe_api.py         the ONLY module that speaks HTTP
  auth.py                 secret + environment resolution for the API key
  meta.py                 catalog-tag helpers
  mock_server.py          the bundled endpoint the whole suite runs against
```

## Core conventions

- **One batching path.** Every question function is a thin wrapper over
  `typesafe_api.ask_pairs()`, which turns a batch into requests — one per *distinct*
  `(state, questions)` pair, concurrently. `ask_many()` is the special case where
  every row asks the same thing. A new question type adds a module, not a request path.
- **Errors throw; only a NULL input is NULL.** An API failure must propagate and
  surface as a DuckDB error. A NULL answer is indistinguishable from a NULL input,
  so degrading to NULL makes a broken query look like a working one.
- **Validate at bind.** A malformed question should fail when the query is planned,
  before a single row is billed. The exception is `ask_dynamic()`, where questions
  are per-row and validation necessarily moves to `process()`.
- Python ≥ 3.13, `from __future__ import annotations`, Google docstrings.
- Copyright header on every module: `# Copyright 2026 Query Farm LLC - https://query.farm`.
- Ruff/mypy/pydoclint settings are mirrored from vgi-python so the fleet lints
  identically. Docstring rules apply to **tests too** — no per-file ignore.

## Sharp edges (learned the hard way)

1. **The mock is both the thing under test and the definition of correct.** Every
   test except `test_live.py` asserts `mock_server.py`'s behaviour. It once echoed
   the requested model back, where production resolves `jev-latest` to a concrete
   version (`jev-1.13.0`) — and six offline tests had pinned the echo as if it were
   the API's. When you change the mock, ask whether production actually does that.
2. **PEP-723 headers must move with `[project.dependencies]`.** The headers in
   `typesafe_worker.py` / `serve.py` are what an ephemeral `uv run typesafe_worker.py`
   resolves. Leaving them behind lets the *published* entry script pull a different
   framework version than the project pins, while every local test passes.
   `tests/test_packaging.py` guards the dependency *names*; versions are on you.
3. **`questions` is a struct keyed by name, not a LIST.** The three question types
   have different shapes (a choice's criteria is a map, a score's an ordered list),
   so DuckDB cannot unify them as elements of one `LIST`, and a `LIST` of `UNION`s
   does not type-check as a literal either. As struct fields each keeps its own
   type, and the key doubles as the output column name.
4. **`DESCRIBE` binds without billing.** Bind validates the questions and computes
   the result schema; no request is issued. That is why the one
   `vgi.executable_examples` entry is a `DESCRIBE` — genuinely runnable by anyone,
   with no key and no cost.
5. **`model =>` is a bind-time literal.** It cannot be correlated from a column, so
   you cannot join `models()` into a question. Pinned by
   `test_a_model_column_cannot_be_correlated_into_a_question`.
6. **Row indices are useless under `LATERAL`.** DuckDB hands the worker one row per
   batch, so "row 0" locates nothing. `ask_dynamic()`'s per-row errors carry a state
   excerpt for that reason.
7. **ANY-typed positional args need a recent extension.** `ask_dynamic()` and
   `ask()`'s whole-row state depend on Query-farm/vgi `d39ca9e` or later; before
   that the bind failed with `Unsupported Arrow type ANY`.
8. **A required ANY argument needs `AnyArrow | None` AND `default=None`.** Without
   both, omitting it raises a bare `KeyError` during argument parsing, before
   `on_bind()` can produce a message that names it. Needs vgi-python ≥ 0.34.
9. **The mock's listen backlog.** `ThreadingHTTPServer` defaults to 5; a multi-worker
   `LATERAL` burst dropped SYNs and surfaced as connect timeouts that looked like a
   worker bug. It sets `request_queue_size = 256`.
## Testing

```sh
uv run pytest -q                              # 470 offline; no key, no network
TYPESAFE_API_KEY=... uv run pytest -m live    # 26 live; real API, real tokens
```

| Area | Files |
| --- | --- |
| The bundled endpoint | `test_mock_server.py` |
| HTTP layer | `test_typesafe_api.py` |
| Auth | `test_auth.py` |
| Validation rules, as user-facing messages | `test_ask_logic.py`, `test_ask_dynamic_logic.py` |
| Each function over the real VGI protocol | `test_*_function.py` |
| Real SQL (`ATTACH`, `LATERAL`, whole-row state) | `test_end_to_end.py` |
| Every published example, executed | `test_examples.py` |
| Installable by someone who is not us | `test_packaging.py` |
| pydoclint, run inside the suite | `test_docstrings.py` |
| The real API | `test_live.py` |

The SQL tests drive the `haybarn` shell built from `../vgi`
(`../vgi/build/release/haybarn`); set `HAYBARN` to use another binary. They **skip**
when it is absent — which is why CI reports ~99 skips and a local run does not.

`test_examples.py` collects from **all five** carriers examples reach a client
through (`Meta.examples`, `vgi.example_queries` on functions and on the schema,
`vgi.executable_examples`, and the private graders) and executes every one. Four
grader queries once selected `FROM tickets`, a table this worker does not have and
`vgi-lint simulate` never creates — they could not run at all, and nothing noticed.

## Catalog metadata (vgi-lint)

```sh
uv run vgi-typesafe-mock --port 8787 --api-key k &
TYPESAFE_API_KEY=k TYPESAFE_BASE_URL=http://127.0.0.1:8787 \
  uvx --from vgi-lint-check vgi-lint lint --execute --audit-waivers --no-check-links
# 100/100, 0 findings, Assurance L2 behavioural
```

**Run `--execute`, not `--no-execute`.** The structural tier only reads tags; the
behavioural tier attaches the worker and runs the shipped examples, which is the
only way a declared result schema is checked against what a function really returns.
Pointing the worker at the bundled endpoint through its own environment fallback
keeps that free and keyless.

Rules this repo has actually tripped, so you do not rediscover them:

- **VGI173** — catalog/schema descriptions must not enumerate the worker's objects.
  An agent lists the schema for that; a description is for what the worker is *for*.
- **VGI174 / VGI179** — SQL in a description must not sit in an inline code span,
  *and* a complete runnable query must not sit in a ```sql fence either. It belongs
  in `vgi.example_queries`. Point at the examples instead.
- **VGI182** — code-format DuckDB type names in prose (`` `STRUCT` ``).
- **VGI313 / VGI314 / VGI317** — an argument description must not restate its type
  or enumerate allowed values without machine-readable constraints; a function
  description must not re-document its arguments.
- **VGI416** — `vgi.agent_test_tasks` carries only `{name, prompt}`. Graders live in
  `vgi-agent-tests.yaml` so an agent under measurement cannot read the answer key
  out of the catalog it is querying. Every grader must be self-contained.
- **VGI910** is waived per-object for `ask()` in `vgi-lint.toml`: its result columns
  are named after the caller's own questions, an unbounded set chosen at call time,
  so no fixed variant table can enumerate them. `--audit-waivers` fails if that
  waiver ever stops buying anything.
- **`criteria`'s type differs by question type** (a `MAP` for choice, a `LIST` for
  score). That is TypeSafe's own contract, declared intentional via
  `type_consistency_ignore_names`.

## The API

One endpoint does the work: `POST /v1/systemone`, one `state` and a map of named
questions. **There is no batch endpoint** — confirmed against the API reference, the
parallel-questions cookbook and the SDK client reference. What *is* batched is
questions: N questions about one state cost one request, which is why `ask()` exists.
`GET /v1/models` is the only other endpoint.

Retries mirror the official SDK: 408, 429 and all 5xx, plus transport failures,
with jittered backoff honouring `Retry-After` and `retry-after-ms`. Jitter matters
here more than in the SDK — this worker issues up to `concurrency` requests at once,
so without it every request that hits a rate limit retries in lockstep.

**Production is laxer than its own reference in two places**; we follow the
reference and record both in `test_live.py`, so the gap stays a decision:
a `score` question with ONE level is accepted upstream (docs say 2–10) and scores
every row `0.0`; a `noul` question with criteria and no `instructions` is accepted
(the reference says every type requires them).
