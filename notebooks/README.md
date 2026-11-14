# TypeSafe System One examples

These independent notebooks use the real released VGI worker against its bundled **synthetic
local service**. The fixture scores keyword overlap; it is not an AI model or a recording of
production answers. Its probabilities, confidence, model labels and usage counters do not establish
accuracy, calibration, token cost or billing. No real API key, external inference request or ticket
update is involved.

- [01 · Keep uncertainty visible when routing typed answers](01_route_typed_answers_with_uncertainty.ipynb)
  retains the original nine-ticket story: discover models, inspect a typed schema without inference,
  ask three questions per distinct input, and apply a separate confidence policy. Two charts retain
  the full answered/review/missing population.
- [02 · Choose the follow-up per row](02_choose_the_follow_up_per_row.ipynb)
  distinguishes choice confidence, yes/no probability and ordered severity, reconciles scalar and
  table forms, then builds a different follow-up question for each proposed team. Two charts keep
  answer meanings and question changes explicit.

Read saved outputs on GitHub. **Open in Colab** installs the base shared package and retrieves
SHA-256-verified companions; a single downloaded notebook is sufficient. The shared local runner
is optional. Actual hosted Colab is not tested; the recorded fresh Python 3.12 notebook-only run
checks compatible preloaded libraries and this public bootstrap path.

## Locked local runner

Install [uv](https://docs.astral.sh/uv/) and Git, then run from this directory:

```sh
uv sync --locked --extra runner
uv run --locked --extra runner vgi-notebooks
```

The local lock pins the publication environment. The [shared tools v0.3.20](https://github.com/Query-farm/vgi-example-notebooks/tree/v0.3.20)
supply branding, SVG presentation, isolated SQL setup and an atomic runner. Colab installs only
its base package, accepting compatible preloaded data/plotting libraries without replacing the
kernel or Jupyter client. The notebook core supports Python 3.11+; the worker's release requires
Python 3.13+ and uv selects a compatible interpreter in its separate environment.

The literal reader-visible launch is:

```sql
INSTALL vgi FROM community;
LOAD vgi;
ATTACH 'typesafe' (TYPE vgi, LOCATION 'uvx --from git+https://github.com/Query-farm/vgi-typesafe@v0.3.0 vgi-typesafe');
```

No worker checkout, local build or transitive dependency overrides are required. The first run
fetches public packages, the VGI extension, release source and fixture. Notebook computations then
use only the disposable loopback service. The engine is Haybarn 2.0.0rc11. Upgrade pins explicitly
and re-execute both stories and experiments.

Optional local Jupyter kernel:

```sh
uv run --locked --extra runner python -m ipykernel install --user \
  --name vgi-typesafe-notebooks --display-name 'Python (vgi-typesafe notebooks)'
```

Restart and Run All after changing parameters. Visible installation, parameters, ATTACH and SQL
remain readable; Python setup/presentation carries folding metadata. Defaults embed native SVG and
committed image links for GitHub. Experiment copies retain native SVG without unpublished links.
Logs, review exports and generated copies stay ignored under `validation/` and `artifacts/`.

## Controlled experiments

```sh
uv run --locked --extra runner vgi-notebooks 01_route_typed_answers_with_uncertainty.ipynb \
  --parameters '{"MIN_CONFIDENCE":0.30}' --output-dir validation/threshold-030
uv run --locked --extra runner vgi-notebooks 02_choose_the_follow_up_per_row.ipynb \
  --parameters '{"BILLING_TOPIC":"overdue"}' --output-dir validation/overdue
```

Notebook 01 changes only the local routing partition; raw answers, question definitions, request
bodies and distribution chart remain fixed. Notebook 02 changes only billing's follow-up topic;
input text, typed measurements, scalar comparison, other teams' questions and request count stay
fixed. The first chart remains identical. These are illustrative policies/questions, not production
recommendations. Restore defaults 0.80 and `"refund"` for publication.

Overrides require a separate destination. Unknown names, source-overwriting destinations and
invalid parameter values fail while preserving saved successful outputs. The complete notebooks
directory can execute independently of the worker checkout.

## Fixture and credential boundary

`fixtures/typesafe_mock.py` is an unchanged copy of
[the repository's bundled mock](https://github.com/Query-farm/vgi-typesafe/blob/c1a121445d9593fae027dccc90ae0ec6ae79ec49/vgi_typesafe/mock_server.py),
with MIT license and [SHA-256 provenance](fixtures/manifest.json). These companions are retrieved
from immutable public commit `f307ada9abb3b42d32a850d7a4524f5ca048ba94`. They use only the Python
standard library and bind an ephemeral port on `127.0.0.1`.

`connect(globals())` creates empty temporary SQL-secret storage with persistent secrets disabled,
verifies it is empty before ATTACH, and prevents JupySQL default-connection autoload. Each notebook
creates an explicit **TEMPORARY** secret carrying the fake `notebook-mock-only` key and loopback
URL. Both override the worker's environment fallback. Cleanup verifies only a temporary secret
exists, closes the connection, removes its secret directory and shuts down the server. It never
reads or edits a user's existing SQL secrets. Restart the kernel after an interruption.

### A separate real-service session

The saved examples deliberately remain local. To use your own TypeSafe account, create a separate
private scratch notebook/session; do not replace the committed fixture, save real customer inputs
in these examples or reuse their synthetic calibration claims. Start a new `connect(globals())`,
run the same INSTALL/LOAD/ATTACH, then explicitly create a temporary credential for the production
endpoint. Read a key you provide interactively, never from existing SQL secrets:

```python
from getpass import getpass

api_key = getpass("Your TypeSafe API key: ")
session.connection.execute(
    "CREATE TEMPORARY SECRET own_typesafe (TYPE typesafe, api_key '"
    + api_key.replace("'", "''")
    + "', base_url 'https://api.typesafe.ai')"
)
del api_key
```

This snippet belongs only in that separate session, after ATTACH and before any question. Do not
create the mock secret there. Start with an authorized model-list read, then use only inputs you
intend to send; question calls are real inference requests and may incur account charges. Close
with `session.close()` and avoid saving credential diagnostics or private returned data. This
migration validates the local fixture path, not live service availability or model performance.

## Interpretation and coverage

The catalog's published subtitle uses “calibrated confidence.” The synthetic fixture cannot
validate that claim. In its deterministic rules, confidence is one minus normalized entropy,
winning probability is distinct, and ties choose the first option. An ordered score is a weighted
position, not necessarily a selected class. `is_true` returns a probability, not a Boolean.

Notebook 01 has nine rows, eight answered records and seven distinct non-NULL bodies. One bundled
`ask` call sends three questions per distinct body: seven local System One requests and 21 questions.
`DESCRIBE` makes no inference request. Model-list GETs are separate reads outside that ledger.
Duplicate answer rows repeat usage fields, so summing those fields does not count distinct requests.

Notebook 02 uses five rows, four non-NULL bodies: three shorthand calls make twelve one-question
requests, the scalar comparison adds four, and the dynamic follow-up adds four. The exact local
ledger checks those twenty requests and their per-row question payloads. No cross-query cache or
production bill is inferred. A missing input remains unanswered throughout.

[coverage.json](coverage.json) maps all **7/7 logical names** to successful visible SQL and meaningful
interpretations. Complete runtime functions/tables/views exports are retained under [coverage/](coverage/).
Seven functions and the same-named `models` table alias count as seven objects; the alias is also
reconciled in notebook 01. Current public source has the same interface. Its release-to-current
diff adds metadata/composed examples only; API/authentication/scoring behavior and dependencies
are unchanged. Coverage records seven exact released-runtime demonstrations and zero exact
current-runtime executions, with current compatibility reviewed separately.

Both stories execute default and detached experiment runs, check twelve SQL invariants each plus
request/payload/population controls, inspect all figures and verify published GitHub SVGs. Existing
green [source CI 43586188405](https://github.com/Query-farm/vgi-typesafe/actions/runs/43586188405)
is reused; no worker build, source-suite rerun, live credential test or source CI dispatch is needed
for these notebook-only changes. Source code, tests, root dependencies and original fixture bytes
are preserved. The engine's known scalar-WHERE-with-LATERAL limitation is avoided by materializing
the scalar before filtering.
