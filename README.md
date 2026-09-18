# vgi-typesafe

A [VGI](https://query.farm/vgi/) worker that exposes [TypeSafe](https://docs.typesafe.ai/introduction)
System One questions — **choice**, **noul** and **score** — to DuckDB as table-in-out functions you can
`LATERAL` join against.

```sql
-- The entry script carries a PEP-723 header, so `uv run` resolves its
-- dependencies on the fly: this works from any directory, on a machine that has
-- never seen this project.
ATTACH 'typesafe' (TYPE vgi, LOCATION 'uv run /path/to/typesafe_worker.py');
CREATE SECRET (TYPE typesafe, api_key 'ts-...');

-- Route, flag and grade every ticket: three judgments, ONE request per row.
SELECT t.id, a.dept.choice, a.dept.confidence, a.urgent.noul, a.severity.score
FROM tickets t,
     LATERAL typesafe.main.ask(t, questions => {
         'dept':     {'type': 'choice', 'instructions': 'Which team should handle this?',
                      'criteria': {'returns':  'Exchanges, refunds, wrong or damaged items',
                                   'shipping': 'Delivery status, delays, lost packages',
                                   'billing':  'Charges, invoices, payment problems'}},
         'urgent':   {'type': 'noul',  'instructions': 'Does this need a reply today?'},
         'severity': {'type': 'score', 'instructions': 'How bad is it?',
                      'criteria': ['minor question', 'disruptive delay', 'critical outage']}}) a
WHERE a.dept.confidence > 0.8;
```

| Function | Use it for |
| --- | --- |
| [`ask()`](#askstate-questions--) | Any number of questions per row, any mix of types, structured state. |
| [`choice()`](#choicestate-instructions--criteria---model--concurrency-) | The one-question shorthand, with flat output columns. |

Both are *blended* table-in-out functions (`RowTransformFunction` in vgi-python): the positional argument
**is** the per-row input column, so one registration serves a literal call, `FROM t, f(t.x)` and
`LATERAL f(t.x)` alike.

## `ask(state, questions => ...)`

A System One request carries one `state` and a map of named questions that the API answers in parallel —
so five questions about a row cost the same single request as one.

### State — the positional argument

| You pass | The API receives |
| --- | --- |
| `VARCHAR` | a string |
| `STRUCT`, e.g. `{'message': t.body, 'tier': t.tier}` | a JSON object |
| the row alias itself: `ask(t, ...)` | a JSON object of every column of `t` |
| `LIST` / `MAP` | a JSON array / object |
| a `JSON` column, with `parse_json => true` | the parsed object or array |

TypeSafe recommends an object for most requests: descriptive field names tell the model how the parts
relate. (A DuckDB `JSON` column reaches the worker as plain text, indistinguishable from `VARCHAR`, which
is why parsing it is opt-in.) Anything else — a bare `INTEGER`, a `BLOB` — is rejected at bind with a
suggestion. Nested decimals, dates and timestamps are converted to JSON numbers and ISO strings.

### Questions — `questions =>`

A struct keyed by question name; the name becomes the output column.

| `type` | `criteria` | Answer column |
| --- | --- | --- |
| `choice` | struct or `MAP` of option → description (1–255) | `STRUCT(choice VARCHAR, confidence DOUBLE, probabilities MAP(VARCHAR, DOUBLE))` |
| `noul` | optional `{'true': ..., 'false': ...}` | `STRUCT(noul DOUBLE)` — near 1 is yes, near 0 is no, 0.5 is genuinely undecided |
| `score` | ordered list of 2–10 levels, lowest first | `STRUCT(score DOUBLE, confidence DOUBLE, probabilities MAP(INTEGER, DOUBLE))`, keyed by level (0 = first) |

A criterion may be a plain string or a structured `{'what': ..., 'not_for': ..., 'examples': [...]}` object.
A trailing `usage STRUCT(model, input_tokens, output_tokens)` column reports what each row cost, so a
question may not be named `usage`. A JSON string or a `MAP` is accepted in place of the struct literal.

Questions are validated **at bind**, before any row is sent, with errors that name the question at fault
(`ask(): score question 'severity' requires 'criteria': an ordered list of 2-10 level descriptions`).

**Why a struct, not a list?** The three shapes differ — a choice's criteria is a map, a score's an ordered
list, a noul's optional — so DuckDB cannot unify them as elements of one `LIST`, and a `LIST` of `UNION`s
does not type-check as a literal either (`The member 'noul' is not present in target union`). As fields of
one struct each question keeps its own type, and its key is the column name for free.

Other named arguments: `model =>` (default `jev-latest`), `concurrency =>` (default 8, max 64),
`parse_json =>` (default false).

### Row semantics

- **Strictly one output row per input row**, so answers always pair with the row that produced them.
- **A NULL state makes no request** and yields NULL answers. So does a structured state with *no non-null
  content* — `{'message': NULL, 'tier': NULL}` is not NULL in SQL, but there is nothing in it to judge, and
  asking anyway would bill a request for a meaningless answer. (`0`, `false` and `''` are content.)
- **Identical states within a batch are asked, and billed, once** — regardless of key order.
- **Errors raise; they never become NULL.** `429` and `529` are retried with exponential backoff first.

## `choice(state, instructions =>, criteria => [, model =>, concurrency =>])`

The shorthand for a single choice question over a text column. It returns flat columns rather than a
struct, and serves every call shape —

```sql
SELECT * FROM typesafe.main.choice('I was charged twice', instructions => ..., criteria => ...);  -- literal
SELECT ... FROM t, typesafe.main.choice(t.body, ...) c;                                            -- implicit lateral
SELECT ... FROM t, LATERAL typesafe.main.choice(t.body, ...) c;                                    -- explicit lateral
```

| Argument | | |
| --- | --- | --- |
| `state` | positional `VARCHAR` | The content to evaluate. A literal or a column. |
| `instructions` | named `VARCHAR`, required | The question. |
| `criteria` | named `MAP(VARCHAR, VARCHAR)`, required | Option name → when that option applies. 1–255 options. |
| `model` | named `VARCHAR` | Defaults to `jev-latest`. |
| `concurrency` | named `INTEGER` | In-flight requests per input batch. Default 8, max 64. |

| Output column | Type | |
| --- | --- | --- |
| `choice` | `VARCHAR` | The highest-probability option. |
| `confidence` | `DOUBLE` | 0–1, how concentrated the distribution is. |
| `probabilities` | `MAP(VARCHAR, DOUBLE)` | Every option's probability, in `criteria` order. |
| `model` | `VARCHAR` | The model that answered. |
| `input_tokens`, `output_tokens` | `BIGINT` | Usage for that row's request. |

Behaviour worth knowing:

- **Strictly one output row per input row.** A `NULL` state yields a row of `NULL`s and makes no request.
- **Identical states within a batch are asked (and billed) once.**
- **Errors raise; they never become `NULL`.** A failed classification that looked like a `NULL` input would
  be silently wrong. `429` and `529` are retried with exponential backoff (honouring `Retry-After`) first.
- `instructions` and `criteria` are validated at bind, so a malformed question fails before any row is sent.

## API key

The key is a DuckDB secret, so it never appears in query text or `duckdb_databases()`, and
`duckdb_secrets()` shows it redacted:

```sql
CREATE SECRET (TYPE typesafe, api_key 'ts-...');
-- optionally: base_url 'http://127.0.0.1:8787'
```

With no secret, the worker falls back to `TYPESAFE_API_KEY` / `TYPESAFE_BASE_URL` in its environment — the
same names the official TypeSafe SDKs read. With neither, the query fails with a message saying how to fix it.

## The mocked endpoint

`vgi_typesafe/mock_server.py` implements `POST /v1/systemone` for all three question types with the real
wire format: Bearer auth (401), request validation (422), and the `answers` / `usage` response. In place
of a model it uses keyword overlap between the state and the question's text — options and score levels
are scored then softmaxed; a noul moves toward 1 on hits against its instructions and `true` criterion and
toward 0 on hits against `false`. Deterministic, so tests can assert exact answers. Stdlib only.

```sh
uv run vgi-typesafe-mock --port 8787 --api-key test-key
```

```sql
CREATE SECRET (TYPE typesafe, api_key 'test-key', base_url 'http://127.0.0.1:8787');
```

## Development

```sh
uv sync --all-extras          # creates .venv; dependencies come from PyPI
uv run pytest                 # everything, hermetic — no network, no real key
uv run ruff check . && uv run ruff format --check .
uv run mypy vgi_typesafe/     # strict
```

Ruff, mypy and pydoclint settings are mirrored from
[vgi-python](https://github.com/Query-farm/vgi-python) so the fleet lints identically: 120-column
lines, Google-style docstrings enforced on tests as well as the package, and mypy `strict`.
pydoclint runs inside the suite (`tests/test_docstrings.py`) rather than as a separate gate — it
cannot be a project dependency, because it pulls `docstring-parser-fork`, which clobbers
`vgi-rpc`'s `docstring-parser` in the shared `docstring_parser` import namespace.

`pyproject.toml` deliberately carries **no `[tool.uv.sources]`**. A local path pin
(`vgi-python = { path = "../vgi-python" }`) makes the project installable only on a machine that
has that sibling checkout — CI, and everyone else, cannot sync it. To develop against a local
framework, `uv pip install -e ../vgi-python` into the venv instead of committing the pin.

### Catalog metadata

The worker is linted by [vgi-lint-check](https://github.com/Query-farm/vgi-lint-check), which
checks that the catalog documents itself well enough for an agent to use it:

```sh
uvx --from vgi-lint-check vgi-lint lint "uv run typesafe_worker.py" \
    --no-execute --no-check-links --fail-on warning   # currently 100/100, 0 findings
```

CI runs the **structural** tier only (`--no-execute`): the executable tier would bill every
shipped example against the real TypeSafe API. The one `vgi.executable_examples` entry is a
`DESCRIBE`, which binds without issuing a request, so it is runnable by anyone.

`vgi.agent_test_tasks` publishes only each task's `{name, prompt}`. The graders live in
`vgi-agent-tests.yaml`, outside the catalog, so an agent being measured by `vgi-lint simulate`
cannot read the answer key out of the worker it is querying.

### CI

| Job | Gates |
| --- | --- |
| Lint, types, offline tests | ruff check, ruff format, mypy strict, pytest, and both entry points run with `--no-project` from a scratch directory |
| Catalog metadata | `vgi-lint` structural tier, failing on warning |

The entry-point step is the one the other 200-odd tests structurally cannot be: they all run from
inside a synced venv at the project root, which is exactly where a broken entry script still works.

## License

MIT — see [LICENSE](LICENSE). Worker © 2026 Query Farm LLC. Judgments are produced by TypeSafe's
System One models and are subject to TypeSafe's terms of use.

| Tests | |
| --- | --- |
| `test_mock_server.py` | The mock's scoring for each question type, validation, and HTTP behaviour. |
| `test_typesafe_api.py` | Wire format, answer parsing, retries, errors, de-duplication (`httpx.MockTransport`). |
| `test_auth.py` | Secret / environment key resolution and redaction. |
| `test_ask_logic.py` | `ask()`'s rules: question validation messages, state → JSON conversion, output shape. |
| `test_ask_function.py`, `test_choice_function.py` | The worker as a subprocess over the real VGI protocol, against the mock. |
| `test_end_to_end.py` | Real SQL — `ATTACH`, `CREATE SECRET`, `LATERAL`, whole-row state — against the mock. |

The end-to-end tests drive the `haybarn` shell built from `../vgi` (`../vgi/build/release/haybarn`); set
`HAYBARN` to use another binary. They skip if none is found. `ask()`'s structured state needs a vgi
extension that accepts ANY-typed blended input columns (Query-farm/vgi `d39ca9e` or later).

## Layout

```
typesafe_worker.py        stdio entry point (the ATTACH LOCATION), PEP-723 self-resolving
serve.py                  HTTP entry point (uv run serve.py --port 8000)
vgi-agent-tests.yaml      private graders for the published agent test tasks
vgi_typesafe/
  ask.py                  ask(): several questions per row, structured state
  choice.py               choice(): the one-question shorthand
  typesafe_api.py         the only HTTP: POST /v1/systemone, answer parsing, retries, batching
  auth.py                 the `typesafe` secret type and key resolution
  mock_server.py          the mocked endpoint
  worker.py               catalog + Worker
  meta.py                 vgi.* documentation-tag helpers
```
