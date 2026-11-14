# TypeSafe SQL notebook

[01 · Keep uncertainty visible when routing typed answers](01_route_typed_answers_with_uncertainty.ipynb)
uses the real VGI worker with its bundled local mock endpoint. It demonstrates
`models`, `DESCRIBE`, mixed `choice`/`noul`/`score` questions, LATERAL pairing,
structured answers, confidence filtering, NULL handling and a request audit.
Two charts distinguish probability distributions from the policy applied to them.

**All inputs and answers are synthetic.** The mock scores keyword overlap; it is
not an AI model or a recording of production responses. Its confidence, probability,
model labels and usage counts do not establish model accuracy, calibration or cost.
No real API key or external inference service is used, and no ticket is actually
routed. POSTs go only to the disposable loopback fixture.

## Run from this directory

Install [uv](https://docs.astral.sh/uv/) and Git, then:

```sh
uv sync --locked
uv run --locked python execute.py
```

Only the complete `notebooks/` directory is needed. The runner creates a temporary
kernel specification, runs with this directory as cwd, and atomically replaces the
saved example only after success. It does not register a global kernel. Choose
`notebooks/.venv/bin/python` in VS Code for interactive execution. Keep the visible
connection and analytical SQL expanded; imports, charts and cleanup are folded.

Run the proposed experiment without replacing the saved default:

```sh
uv run --locked python execute.py --parameters '{"MIN_CONFIDENCE":0.30}' --output-dir validation/threshold-030
```

Overrides require a separate destination, unknown parameters fail, and invalid
values fail before attaching. The output copy includes its relative assets, fixture,
helpers and environment files. Restart the kernel and run all after changing a
parameter; reset `MIN_CONFIDENCE = 0.80` for the default. Validation charts/copies are
Git-ignored, while the main notebook retains outputs.

## Portable connection and fixture

The literal SQL ATTACH uses `uvx --from git+https://github.com/Query-farm/vgi-typesafe@c1a121445d9593fae027dccc90ae0ec6ae79ec49 vgi-typesafe`
with Python 3.13 and explicit VGI Python 0.51.0, VGI RPC 0.53.1 and PyArrow 26.0.1 pins.
No local worker build or parent checkout is needed. The revision's releases contain
Python archives, not native github-auto executables. Git/uv need network access on
the first run. The notebook environment is separately locked and uses Haybarn
2.0.0rc11. Updating the Git/runtime pins requires rerunning both parameter settings.

`fixtures/typesafe_mock.py` is an unchanged copy of
[`vgi_typesafe/mock_server.py` at the same revision](https://github.com/Query-farm/vgi-typesafe/blob/c1a121445d9593fae027dccc90ae0ec6ae79ec49/vgi_typesafe/mock_server.py),
with its MIT license and SHA-256 provenance manifest. It uses only the standard
library and binds an ephemeral port on `127.0.0.1`. The notebook checks its hash,
starts it in a background thread and uses an empty temporary SQL secret directory
with persistent secrets disabled. Existing saved credentials are not loaded. It shows a `CREATE SECRET` with the fake
`notebook-mock-only` key and loopback URL. That secret takes precedence over the
worker's environment fallback. Never insert production credentials into saved
notebook cells or outputs. Cleanup closes the SQL connection and fixture server;
restart the kernel if interrupted.

## What the example proves

Nine authored ticket records have seven distinct non-NULL bodies. There are eight
answered records because a repeated body retains two ticket IDs; one NULL input
remains unanswered. An empty string is content and receives a mock response.
`DESCRIBE` issues no System One request. The materialized `ask` call sends three
questions per distinct body; the fixture ledger verifies seven accepted System One
requests and 21 questions in this run. The model-list GET is a separate read and is
not included in that ledger.

The mock's confidence is one minus normalized entropy. Its winning probability is
a separate field; a uniform distribution has zero confidence and selects the first
option on a tie. These rules describe this fixture, not a calibration guarantee
about TypeSafe's production models. The ordered score is a probability-weighted
position on levels 0, 1 and 2, not necessarily a chosen class.

Lowering the inclusive confidence threshold from 0.80 to 0.30 changes only the
proposed routing partition. Raw distributions, question definitions and request
bodies stay fixed. Duplicate answer rows also repeat usage fields; adding those
fields across ticket records does not count distinct requests or establish a bill.
Deduplication here does not promise a cache across separate SQL queries or kernels.

Twelve visible SQL checks plus a fixture request audit cover row preservation,
bounds, missing inputs, duplicates and the policy predicate. Real deployment
accuracy/coverage decisions would need labelled evaluation data and real outputs.

Official unchanged Query.Farm Strata Sun and VGI assets are bundled in `assets/`;
the wordmark appears at both ends with a Query.Farm link.
