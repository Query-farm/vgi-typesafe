# vgi-typesafe

A [VGI](https://query.farm/vgi/) worker that exposes [TypeSafe](https://docs.typesafe.ai/introduction)
System One **choice** questions to DuckDB as a table-in-out function you can `LATERAL` join against.

```sql
ATTACH 'typesafe' (TYPE vgi, LOCATION 'uv run typesafe_worker.py');
CREATE SECRET (TYPE typesafe, api_key 'ts-...');

SELECT t.id, c.choice, c.confidence
FROM tickets t,
     LATERAL typesafe.main.choice(
         t.body,
         instructions => 'Which team should handle this?',
         criteria => MAP {
             'returns':  'Exchanges, refunds, wrong or damaged items',
             'shipping': 'Delivery status, delays, lost packages',
             'billing':  'Charges, invoices, payment problems'}) c
WHERE c.confidence > 0.8;
```

## `choice(state, instructions =>, criteria => [, model =>, concurrency =>])`

`choice` is a *blended* table-in-out function (`RowTransformFunction` in vgi-python): its positional
argument **is** the per-row input column, so one registration serves every call shape —

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

`vgi_typesafe/mock_server.py` implements `POST /v1/systemone` for choice questions with the real wire
format: Bearer auth (401), request validation (422), and the `answers` / `usage` response. In place of a
model it scores each option by keyword overlap with the state and softmaxes — deterministic, so tests can
assert exact answers. Stdlib only.

```sh
uv run vgi-typesafe-mock --port 8787 --api-key test-key
```

```sql
CREATE SECRET (TYPE typesafe, api_key 'test-key', base_url 'http://127.0.0.1:8787');
```

## Development

```sh
uv sync                       # creates .venv; vgi-python comes from ../vgi-python (editable)
uv run pytest                 # everything, hermetic — no network, no real key
uv run ruff check . && uv run ruff format --check .
```

| Tests | |
| --- | --- |
| `test_mock_server.py` | The mock's scoring, validation, and HTTP behaviour. |
| `test_typesafe_api.py` | Wire format, retries, errors, de-duplication (`httpx.MockTransport`). |
| `test_auth.py` | Secret / environment key resolution and redaction. |
| `test_choice_function.py` | The worker as a subprocess over the real VGI protocol, against the mock. |
| `test_end_to_end.py` | Real SQL — `ATTACH`, `CREATE SECRET`, `LATERAL` — against the mock. |

The end-to-end tests need a DuckDB whose `vgi` extension speaks the same protocol as the local `vgi-python`
checkout. The community extension currently lags it, so they drive the `haybarn` shell built from `../vgi`
(`../vgi/build/release/haybarn`); set `HAYBARN` to use another binary. They skip if none is found.

## Layout

```
typesafe_worker.py        stdio entry point (the ATTACH LOCATION)
vgi_typesafe/
  choice.py               the choice() table-in-out function
  typesafe_api.py         the only HTTP: POST /v1/systemone, retries, batching
  auth.py                 the `typesafe` secret type and key resolution
  mock_server.py          the mocked endpoint
  worker.py               catalog + Worker
  meta.py                 vgi.* documentation-tag helpers
```
