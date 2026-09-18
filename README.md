<p align="center">
  <a href="https://query.farm/vgi/">
    <img src="https://raw.githubusercontent.com/Query-farm/vgi-typesafe/main/docs/vgi-logo.png" alt="Vector Gateway Interface logo" width="320">
  </a>
</p>

<h1 align="center">vgi-typesafe</h1>

<p align="center">
  <a href="https://docs.typesafe.ai/introduction">TypeSafe</a> System One questions — <strong>choice</strong>,<br>
  <strong>noul</strong> and <strong>score</strong> — as DuckDB table functions you can <code>LATERAL</code> join against.<br>
  A <a href="https://query.farm/vgi/">VGI</a> worker, built by <a href="https://query.farm">🚜 Query.Farm</a>
</p>

<p align="center">
  <a href="https://github.com/Query-farm/vgi-typesafe/actions/workflows/ci.yml"><img src="https://github.com/Query-farm/vgi-typesafe/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/Query-farm/vgi-typesafe/actions/workflows/live.yml"><img src="https://github.com/Query-farm/vgi-typesafe/actions/workflows/live.yml/badge.svg" alt="Live API"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-blue.svg" alt="License: MIT"></a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.13%2B-blue.svg" alt="Python 3.13+">
  <a href="https://duckdb.org"><img src="https://img.shields.io/badge/DuckDB-extension-fff000.svg?logo=duckdb&logoColor=black" alt="DuckDB"></a>
  <a href="https://query.farm/vgi/"><img src="https://img.shields.io/badge/VGI-Vector%20Gateway%20Interface-2f7d32.svg" alt="VGI"></a>
  <a href="https://pypi.org/project/vgi-python/"><img src="https://img.shields.io/badge/vgi--python-%E2%89%A50.34.0-2f7d32.svg" alt="vgi-python >= 0.34.0"></a>
</p>

<p align="center">
  <a href="https://github.com/astral-sh/ruff"><img src="https://img.shields.io/badge/lint-ruff-261230.svg?logo=ruff&logoColor=d7ff64" alt="Ruff"></a>
  <a href="https://mypy-lang.org/"><img src="https://img.shields.io/badge/mypy-strict-2a6db2.svg" alt="mypy strict"></a>
  <img src="https://img.shields.io/badge/tests-472%20offline%20%C2%B7%2026%20live-2f7d32.svg" alt="472 offline tests, 26 live">
  <a href="https://github.com/Query-farm/vgi-lint-check"><img src="https://img.shields.io/badge/vgi--lint-100%2F100%20L2-2f7d32.svg" alt="vgi-lint 100/100, assurance L2 behavioural"></a>
</p>

---

## Install and attach

Nothing to clone — `uvx` fetches and runs the worker, and DuckDB speaks to it over
the [VGI](https://query.farm/vgi/) extension.

```sql
INSTALL vgi FROM community;
LOAD vgi;

ATTACH 'typesafe' (TYPE vgi,
  LOCATION 'uvx --from git+https://github.com/Query-farm/vgi-typesafe vgi-typesafe');

-- Your key, from https://typesafe.ai. Redacted in duckdb_secrets().
CREATE SECRET (TYPE typesafe, api_key 'ts-...');
```

## Start here

Ask one question about one value. The answer comes back as columns, not prose:

```sql
SELECT choice, confidence
FROM typesafe.main.choice('My package never arrived and tracking has not updated',
    instructions => 'Which team should handle this?',
    criteria => MAP {'shipping': 'Delivery status, delays, lost packages',
                     'billing':  'Charges, invoices, payment problems',
                     'returns':  'Exchanges, refunds, wrong or damaged items'});
-- shipping | 1.0
```

`confidence` is how concentrated the model's probability distribution was. It is the
thing that makes this usable in production: it tells you which rows you can act on
without a human.

### Classify a whole table

Put the function in a `LATERAL` join and the same question runs for every row. One
request per row, issued concurrently, and repeated values are asked once:

```sql
SELECT t.id, c.choice AS team, c.confidence
FROM tickets t,
     LATERAL typesafe.main.choice(t.body,
         instructions => 'Which team should handle this?',
         criteria => MAP {'shipping': 'Delivery status, delays, lost packages',
                          'billing':  'Charges, invoices, payment problems',
                          'returns':  'Exchanges, refunds, wrong or damaged items'}) c;
```

### Route the confident ones, keep the rest for a human

This is the pattern worth stealing. Judge once, then split on `confidence`:

```sql
CREATE TABLE routed AS
SELECT t.id, t.body, c.choice AS team, c.confidence
FROM tickets t,
     LATERAL typesafe.main.choice(t.body,
         instructions => 'Which team should handle this?',
         criteria => MAP {'shipping': 'Delivery status, delays, lost packages',
                          'billing':  'Charges, invoices, payment problems',
                          'returns':  'Exchanges, refunds, wrong or damaged items'}) c;

SELECT * FROM routed WHERE confidence >= 0.8;   -- auto-route these
SELECT * FROM routed WHERE confidence <  0.8;   -- queue these for review
```

### Ask several things at once

Each row is one API request no matter how many questions it carries, so asking three
things costs the same as asking one. Mix the question types freely:

```sql
SELECT t.id,
       a.team.choice     AS team,
       a.team.confidence AS team_confidence,
       a.urgent.noul     AS urgency,        -- 0..1; near 1 is yes
       a.severity.score  AS severity        -- 0..2 on the scale below
FROM tickets t,
     LATERAL typesafe.main.ask(t.body, questions => {
         'team':     {'type': 'choice', 'instructions': 'Which team should handle this?',
                      'criteria': {'shipping': 'Delivery status, delays, lost packages',
                                   'billing':  'Charges, invoices, payment problems',
                                   'returns':  'Exchanges, refunds, wrong or damaged items'}},
         'urgent':   {'type': 'noul',  'instructions': 'Does this need a reply today?'},
         'severity': {'type': 'score', 'instructions': 'How severe is this?',
                      'criteria': ['minor question', 'disruptive delay', 'critical outage']}}) a
ORDER BY a.severity.score DESC;
```

### Judge the whole row, not one column

When several columns matter together, pass the row itself. The model sees the field
names, which is how it knows a `tier` of `gold` relates to the `message`:

```sql
SELECT t.id, a.urgent.noul AS urgency
FROM tickets t,
     LATERAL typesafe.main.ask(t, questions => {
         'urgent': {'type': 'noul',
                    'instructions': 'Does this need a reply today? Weigh the customer tier.'}}) a;
```

### Filter directly, with no join

For a single yes/no, `is_true()` is a scalar — it goes wherever an expression goes:

```sql
SELECT * FROM tickets
WHERE typesafe.main.is_true(body, 'Is this an angry complaint?') > 0.5;
```

### See the shape before you spend anything

`DESCRIBE` binds the call — validating the questions and computing the result
columns — without sending a request or costing a token:

```sql
DESCRIBE SELECT * FROM typesafe.main.ask('x', questions => {
    'team':   {'type': 'choice', 'instructions': 'Which team?',
               'criteria': {'shipping': 'Lost packages', 'billing': 'Invoices'}},
    'urgent': {'type': 'noul', 'instructions': 'Needs a reply today?'}});
-- team   STRUCT(choice VARCHAR, confidence DOUBLE, probabilities MAP(VARCHAR, DOUBLE))
-- urgent STRUCT(noul DOUBLE)
-- usage  STRUCT(model VARCHAR, input_tokens BIGINT, output_tokens BIGINT)
```

## The functions

| Function | Use it for |
| --- | --- |
| [`ask()`](#askstate-questions--) | Any number of questions per row, any mix of types, structured state. |
| [`ask_dynamic()`](#ask_dynamicstate-questions--model--concurrency-) | Questions that **differ per row**. Answers come back as JSON, not typed columns. |
| [`choice()`](#choicestate-instructions--criteria---model--concurrency-) | One `choice` question: pick an option. Flat output columns. |
| [`noul()`](#noulstate-instructions---criteria--model--concurrency-) | One `noul` question: yes/no, answered as a probability. |
| [`score()`](#scorestate-instructions--criteria---model--concurrency-) | One `score` question: a position on an ordered scale. |
| [`is_true()`](#is_truestate-instructions) | A noul as a **scalar**, for `WHERE` / `CASE` / `ORDER BY` without a join. |
| [`models()`](#models) | Which models `model =>` will accept. No arguments, no tokens. |

For the five question table functions the first argument **is** the per-row input, so the same call
works on a literal, on `FROM t, f(t.x)`, and on `LATERAL f(t.x)` alike — and `ask_dynamic()` makes its
second argument a per-row input too, which is exactly what lets the questions vary. `is_true()` is an
ordinary scalar expression. `models()` takes no input at all.

## `ask(state, questions => ...)`

A [System One](https://docs.typesafe.ai/concepts/system-one) request carries one
[`state`](https://docs.typesafe.ai/concepts/state) and a map of named questions that the API answers in parallel —
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
| [`choice`](https://docs.typesafe.ai/primitives/choice) | struct or `MAP` of option → description (1–255) | `STRUCT(choice VARCHAR, confidence DOUBLE, probabilities MAP(VARCHAR, DOUBLE))` |
| [`noul`](https://docs.typesafe.ai/primitives/noul) | optional `{'true': ..., 'false': ...}` | `STRUCT(noul DOUBLE)` — near 1 is yes, near 0 is no, 0.5 is genuinely undecided |
| [`score`](https://docs.typesafe.ai/primitives/score) | ordered list of 2–10 levels, lowest first | `STRUCT(score DOUBLE, confidence DOUBLE, probabilities MAP(INTEGER, DOUBLE))`, keyed by level (0 = first) |

A criterion may be a plain string or a
[structured object](https://docs.typesafe.ai/primitives/advanced) — `{'what': ..., 'not_for': ...,
'examples': [...]}` for a choice option, `{'summary': ..., 'signals': [...]}` for a score level.
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
- **[`confidence`](https://docs.typesafe.ai/confidence) is how concentrated the distribution was** —
  it is the field to filter on when routing the clear cases automatically. A `noul` has none, because
  a probability near 0.5 already says the model could not decide.
- **A NULL state makes no request** and yields NULL answers. So does a structured state with *no non-null
  content* — `{'message': NULL, 'tier': NULL}` is not NULL in SQL, but there is nothing in it to judge, and
  asking anyway would bill a request for a meaningless answer. (`0`, `false` and `''` are content.)
- **Identical states within a batch are asked, and billed, once** — regardless of key order.
- **Errors raise; they never become NULL.** `429` and `529` are retried with exponential backoff first.

## `ask_dynamic(state, questions [, model =>, concurrency =>])`

The same request as `ask()`, with the questions moved from a **named** argument to a **positional**
one. For a blended table function a positional argument *is* a per-row input column, so the questions
become data: two rows in one scan may ask entirely different things.

**Reach for `ask()` first.** Its questions are fixed when the query is planned, which is what lets it
hand back one typed `STRUCT` column per question. `ask_dynamic()` trades those typed columns away —
nothing about the result shape is knowable at plan time, so every answer arrives inside one JSON
column you take apart yourself. It is the more awkward of the two and should not be anyone's default.
Use it when the questions genuinely vary per row: a rules table joined to the rows it governs, a work
queue where each item carries its own rubric, questions assembled by an application.

```sql
-- Each row carries the question it wants asked, and they are not the same kind of question.
SELECT t.id,
       a.answers->'dept'->>'choice'            AS dept,
       (a.answers->'urgent'->>'noul')::DOUBLE  AS urgent
FROM queue t, LATERAL typesafe.main.ask_dynamic(t.body, t.questions) a
ORDER BY t.id;
```

| Argument | | |
| --- | --- | --- |
| `state` | positional, per-row | The content to evaluate — same shapes `ask()` accepts, except that `parse_json` does not exist here. |
| `questions` | positional, per-row | That row's questions, keyed by name. JSON text, a `STRUCT` or a `MAP`. |
| `model` | named `VARCHAR` | Defaults to `jev-latest`. |
| `concurrency` | named `INTEGER` | In-flight requests per input batch. Default 8, max 64. |

`model` and `concurrency` stay **named** because a blended function cannot take a positional
bind-time constant — DuckDB sweeps one into the input subquery, where it is indistinguishable from an
input column.

### Writing the questions

| Form | |
| --- | --- |
| **JSON text** | Each row carries its own shape verbatim, reconciled against no other row. **Prefer this when the questions really differ.** |
| `STRUCT` / `MAP` | Works, and reads better when the questions vary only in wording — but DuckDB unifies a column's type across rows. |

The unification is the catch. A question only some rows ask is padded onto *every* row as `NULL`;
that part is harmless, because those NULLs are dropped before the request (a row is never billed for,
or answered with, a question it did not ask). But two rows that give **one question name two
different criteria shapes** — a choice's map on one row, a score's list on the next — cannot be
unified at all, and DuckDB rejects the query before this worker ever sees it. JSON text has neither
constraint.

### Output

| Output column | Type | |
| --- | --- | --- |
| `answers` | `VARCHAR` | A JSON object keyed by question name. `NULL` when the state was `NULL`. |
| `model` | `VARCHAR` | The model that answered. |
| `input_tokens`, `output_tokens` | `BIGINT` | Usage for that row's request. |

Each value inside `answers` is the answer object for that question's type — `{"choice", "confidence",
"probabilities"}`, `{"noul"}`, or `{"score", "confidence", "probabilities"}`. A score's probabilities
are keyed by **level number** as `ask()`'s are, but JSON keys are text, so they read back as `"0"`,
`"1"`, … — reachable with `a.answers->'sev'->'probabilities'->>'0'`. The cost columns stay real
columns precisely because their shape never varies: reading what a query spent should not cost a JSON
parse.

### Row semantics

Identical to `ask()`, with two differences:

- **Questions are validated per row, not at bind**, because they are data. A row carrying no
  questions is an error that names the row *and quotes its content* — under a correlated `LATERAL`
  DuckDB hands over one row at a time, so an index alone would locate nothing. A row whose state is
  `NULL` is never asked anything, so its questions are not examined at all.
- **De-duplication is on the `(state, questions)` pair**, not on the state alone. The same text asked
  two different things is two requests; the same text asked the same thing twice is one.

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

## `noul(state, instructions => [, criteria =>, model =>, concurrency =>])`

The shorthand for a single yes/no question. A *noul* answer is one probability and nothing else —
there is no chosen option and no distribution, so there is no `confidence` column either: the number
already carries its own certainty, and 0.5 is exactly what low confidence looks like.

| Argument | | |
| --- | --- | --- |
| `state` | positional `VARCHAR` | The content to evaluate. A literal or a column. |
| `instructions` | named `VARCHAR`, required | The question, phrased so yes and no both make sense. |
| `criteria` | named `MAP(VARCHAR, VARCHAR)`, optional | `'true'` → what a yes looks like, `'false'` → what a no looks like. Either alone is fine; no other key is accepted. |
| `model` | named `VARCHAR` | Defaults to `jev-latest`. |
| `concurrency` | named `INTEGER` | In-flight requests per input batch. Default 8, max 64. |

| Output column | Type | |
| --- | --- | --- |
| `noul` | `DOUBLE` | Probability the answer is yes: near 1 yes, near 0 no, 0.5 genuinely undecided. |
| `model` | `VARCHAR` | The model that answered. |
| `input_tokens`, `output_tokens` | `BIGINT` | Usage for that row's request. |

Compare it against a threshold you pick, and keep the rows near 0.5 for a human. Row semantics,
error handling and billing are identical to `choice()`. Omitting `criteria` sends no `criteria` key
at all, which is not the same request as sending an empty one.

## `score(state, instructions =>, criteria => [, model =>, concurrency =>])`

The shorthand for a single ordered-scale question. Where a choice's criteria is a `MAP` of named
options, a score's is an ordered `VARCHAR[]`: the *position* of a level is its meaning, and the
answer is expressed in those positions.

| Argument | | |
| --- | --- | --- |
| `state` | positional `VARCHAR` | The content to evaluate. A literal or a column. |
| `instructions` | named `VARCHAR`, required | The question. |
| `criteria` | named `VARCHAR[]`, required | The rungs of the scale, lowest first. 2–10 of them. |
| `model` | named `VARCHAR` | Defaults to `jev-latest`. |
| `concurrency` | named `INTEGER` | In-flight requests per input batch. Default 8, max 64. |

| Output column | Type | |
| --- | --- | --- |
| `score` | `DOUBLE` | Probability-weighted position: `0.0` is the first level, and a row read as halfway between the first two rungs scores `0.5`. |
| `confidence` | `DOUBLE` | 0–1, how concentrated the distribution is. |
| `probabilities` | `MAP(INTEGER, DOUBLE)` | Each level's probability, keyed by level number (0 = first). |
| `model` | `VARCHAR` | The model that answered. |
| `input_tokens`, `output_tokens` | `BIGINT` | Usage for that row's request. |

`score` is a position, not a level: round it to snap to a rung, or leave it alone to rank rows
against each other. `probabilities` is keyed by number rather than by the level's text, so the key
stays meaningful when two levels read similarly. A scale with fewer than two rungs is rejected at
bind — production accepts one and scores every row `0.0`, which is a scale that cannot place
anything.

## `is_true(state, instructions)`

The same yes/no question as `noul()`, as a **scalar** returning just the probability:

```sql
SELECT * FROM tickets WHERE typesafe.main.is_true(body, 'Is this urgent?') > 0.8;

UPDATE tickets SET urgent = typesafe.main.is_true(body, 'Is this urgent?') > 0.8;
```

A noul answer *is* one number, so a scalar loses nothing by returning only that — and one number
drops straight into a `WHERE`, a `CASE`, an `ORDER BY` or an `UPDATE ... SET`, where the table
function needs a `LATERAL` join or a scalar subquery around it. **This is conciseness, not
capability:** `noul()` can already express every one of those, and `tests/test_end_to_end.py` pins
that the two agree on the same row.

- It returns a **`DOUBLE`, not a `BOOLEAN`** — it is the probability that the answer is yes, so
  compare it against a threshold. The name says which question is being asked, not what comes back.
- DuckDB scalars take no named arguments, so the signature is the whole surface: the default model,
  no `criteria`, no usage columns. `noul()` is where those live.
- It still **batches**: one request per *distinct non-null value* in each chunk, issued
  concurrently — the same accounting the table functions do, through the same code path.
- `NULL` in, `NULL` out, with no request. Errors raise, so a failure cannot silently drop a row from
  a `WHERE` clause. The question is checked at bind.

## `models()`

Every question table function takes `model =>` and defaults it to `jev-latest`
([model docs](https://docs.typesafe.ai/models)). Nothing else in the catalog says what else is allowed — and TypeSafe publishes a preview line alongside the stable one,
so the default is not the only answer.

```sql
SELECT name, description, release_date FROM typesafe.main.models ORDER BY name;
-- jev-latest   The latest iteration of TypeSafe's System One Model: Jev       2026-09-10 18:38:01+00
-- jev-preview  A preview version of `jev-latest`: should be better in most ways  2026-09-10 18:39:06+00
```

| Output column | Type | |
| --- | --- | --- |
| `name` | `VARCHAR` | The model id, exactly as `model =>` wants it. Primary key. |
| `description` | `VARCHAR` | TypeSafe's own description. |
| `release_date` | `TIMESTAMP WITH TIME ZONE` | When it was published; `NULL` if the API did not say. |

Readable as a table (`typesafe.main.models`) or as a function (`typesafe.main.models()`) — same rows
either way. A listing that takes no arguments simply reads better without the parentheses.

- It **judges nothing and bills no tokens**, so it is safe to call for discovery.
- Results are cacheable for **5 minutes**, so repeated calls in one session cost one request.
- A rejected key or an unreachable endpoint **raises**. Zero rows would read as "this account has no
  models", which is a different fact.
- `model =>` is fixed when the query is planned, so it takes a literal: paste a `name` from here into
  the call rather than joining this listing into it.

## API key

The key is a DuckDB secret, so it never appears in query text or `duckdb_databases()`, and
`duckdb_secrets()` shows it redacted:

```sql
CREATE SECRET (TYPE typesafe, api_key 'ts-...');
-- optionally: base_url '...' to target a non-production endpoint
```

With no secret, the worker falls back to `TYPESAFE_API_KEY` / `TYPESAFE_BASE_URL` in its environment — the
same names the official TypeSafe SDKs read. With neither, the query fails with a message saying how to fix it.

## Development

```sh
uv sync --all-extras          # creates .venv; dependencies come from PyPI
uv run pytest                 # the full suite: hermetic, no network and no key needed
uv run ruff check . && uv run ruff format --check .
uv run mypy vgi_typesafe/     # strict

TYPESAFE_API_KEY=... uv run pytest -m live   # the only tests that call the real API
```

The suite runs against a bundled endpoint that speaks TypeSafe's wire format, so it needs neither a
key nor a network. Start it by hand to point a DuckDB session at it:

```sh
uv run vgi-typesafe-mock --port 8787 --api-key test-key
```

```sql
CREATE SECRET (TYPE typesafe, api_key 'test-key', base_url 'http://127.0.0.1:8787');
```

Ruff, mypy and pydoclint settings are mirrored from
[vgi-python](https://github.com/Query-farm/vgi-python), so the fleet lints identically: 120-column
lines, Google-style docstrings enforced on tests as well as the package, and mypy `strict`.

## Where we are stricter than the API

Production is laxer than its own [API reference](https://docs.typesafe.ai/api) in two places. We
follow the reference, and record both here so the gap stays a decision rather than a surprise:

| | Documented | Production actually | We |
| --- | --- | --- | --- |
| `score` levels | 2–10 | accepts 1 (and scores every row `0.0`) | reject at bind |
| `noul` `instructions` | required | optional if `criteria` is given | require it |

## License

MIT — see [LICENSE](LICENSE). Worker © 2026 Query Farm LLC. Judgments are produced by TypeSafe's
System One models and are subject to TypeSafe's terms of use.
