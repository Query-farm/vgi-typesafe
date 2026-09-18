# Copyright 2026 Query Farm LLC - https://query.farm

"""``models()`` — the models a key may name, as rows.

``ask()`` and ``choice()`` both take a ``model =>`` argument and default it to
``jev-latest``, and until this function existed nothing in SQL said what else
could go there. ``jev-preview`` has been published the whole time; a user could
only learn of it by leaving DuckDB.

It is a plain :class:`~vgi.table_function.TableFunctionGenerator` rather than a
blended one: there is no per-row input to transform, no state, and no arguments
at all — the whole listing is the answer::

    SELECT name, description, release_date FROM typesafe.main.models() ORDER BY name;

The schema is fixed, so ``@bind_fixed_schema`` supplies ``on_bind`` and the
result shape is declared statically with ``vgi.result_columns_schema``. The
listing is two rows and one unpaginated response, so ``@init_single_worker``
keeps the framework from spawning workers to split it.

:mod:`vgi_typesafe.worker` also registers this same function as the *table*
``typesafe.main.models``, so the listing can be read without parentheses. Both
forms run this code and return these rows.
"""

from __future__ import annotations

from typing import ClassVar

import pyarrow as pa
from vgi.arguments import SecretLookupEntry
from vgi.cache_control import CacheControl
from vgi.metadata import FunctionExample
from vgi.table_function import (
    ProcessParams,
    TableFunctionGenerator,
    bind_fixed_schema,
    init_single_worker,
)
from vgi_rpc.rpc import OutputCollector

from vgi_typesafe import auth
from vgi_typesafe import typesafe_api as api
from vgi_typesafe.meta import docs, examples, field

MODELS_SCHEMA = pa.schema(
    [
        field("name", pa.string(), "The model id, exactly as ask() and choice() want it in `model =>`."),
        field("description", pa.string(), "TypeSafe's own description of what the model is."),
        field(
            "release_date",
            pa.timestamp("us", tz="UTC"),
            "When TypeSafe published this model; NULL when the API did not say.",
        ),
    ]
)

#: How long a client may reuse a listing. The endpoint sends no ``Cache-Control``
#: of its own, so unlike the Kalshi worker there is no origin policy to forward
#: and this is our judgment: the list changed twice in its entire history (both
#: entries were published within a minute of each other), so five minutes of
#: staleness cannot hide anything a user is waiting for, while it collapses the
#: repeated `models()` calls of an interactive session into one request.
CACHE_TTL_SECONDS = 300

#: How long a stale listing may still be served when a refresh fails. Generous,
#: because the alternative is a failed query: a model list an hour old is
#: essentially certainly still right, and it is metadata, not a judgment — the
#: reason `choice()` refuses to degrade does not apply to it.
STALE_IF_ERROR_SECONDS = 3_600

MODELS_CACHE = CacheControl(ttl=CACHE_TTL_SECONDS, stale_if_error=STALE_IF_ERROR_SECONDS)

_LIST_EXAMPLE = "SELECT name, description, release_date FROM typesafe.main.models() ORDER BY name"
_NEWEST_EXAMPLE = "SELECT name, release_date FROM typesafe.main.models() ORDER BY release_date DESC LIMIT 1"


@init_single_worker
@bind_fixed_schema
class ModelsFunction(TableFunctionGenerator[None, None]):
    """Every model this key may name — one row each."""

    FIXED_SCHEMA: ClassVar[pa.Schema] = MODELS_SCHEMA

    class Meta:
        """Catalog metadata: name, docs, and the examples clients copy."""

        name = "models"
        description = "List the TypeSafe models that ask() and choice() accept in their `model =>` argument"
        categories = ["reference"]
        required_secrets = [SecretLookupEntry(secret_type=auth.SECRET_TYPE)]
        tags = docs(
            category="reference",
            result_schema=MODELS_SCHEMA,
            llm=(
                "The list of TypeSafe models, one row each. Read it before setting `model =>` on "
                "`ask()` or `choice()`: those default to `jev-latest`, and this is the only place "
                "in SQL that says what else is accepted — a preview model is usually published "
                "alongside the stable one. Takes no arguments and costs no tokens, so it is safe "
                "to call for discovery. A `name` from here goes straight into `model =>`. Needs a "
                "`typesafe` secret."
            ),
            md=(
                "Every model the API will accept, as rows.\n\n"
                "### Why it is here\n\n"
                "`ask()` and `choice()` take a `model =>` argument that defaults to `jev-latest`. "
                "Nothing else in this catalog enumerates the alternatives, and TypeSafe publishes "
                "a preview line (`jev-preview`) next to the stable one.\n\n"
                "### Using a name you find here\n\n"
                "`model =>` is a bind-time argument, so it takes a literal: paste the `name` into "
                "the call rather than joining this function's column into it. A correlated "
                "`LATERAL ... model => m.name` is rejected by the binder.\n\n"
                "### Cost and freshness\n\n"
                "This is a `GET`: it judges nothing and bills no tokens. Results are advertised as "
                "cacheable for five minutes, so repeated calls in one session cost one request.\n\n"
                "### Errors\n\n"
                "A rejected key or an unreachable endpoint raises, exactly as for the question "
                "functions — an empty listing would read as 'this account has no models'.\n\n"
                "### Authentication\n\n"
                "The same `typesafe` secret the question functions use.\n\n"
                "```sql\n"
                "CREATE SECRET (TYPE typesafe, api_key '...');\n"
                "```"
            ),
            example_queries=examples(
                ("List every model with what it is and when it shipped", _LIST_EXAMPLE),
                ("Find the most recently published model", _NEWEST_EXAMPLE),
            ),
        )
        examples = [
            FunctionExample(sql=_LIST_EXAMPLE, description="List every model with what it is and when it shipped"),
            FunctionExample(sql=_NEWEST_EXAMPLE, description="Find the most recently published model"),
        ]

    @classmethod
    def process(cls, params: ProcessParams[None], state: None, out: OutputCollector) -> None:
        """Fetch the whole listing, emit it as one batch, and finish.

        One tick is the whole scan: the endpoint is unpaginated and has never
        returned more than a handful of rows, so there is nothing to stream and
        no cursor to carry between calls.
        """
        credentials = auth.for_call(params.secrets)
        with api.open_client() as client:
            listed = api.list_models(credentials=credentials, client=client)
        batch = pa.RecordBatch.from_pydict(
            {
                "name": [model.name for model in listed],
                "description": [model.description for model in listed],
                "release_date": [model.release_date for model in listed],
            },
            schema=cls.FIXED_SCHEMA,
        )
        # The rendered `vgi.cache.*` keys rather than `cache_control=`: the
        # runtime collector accepts both and treats them identically, but only
        # `metadata` is on the `OutputCollector` signature this module is typed
        # against, and mypy runs strict here.
        out.emit(batch.select(params.output_schema.names), metadata=MODELS_CACHE.to_metadata())
        out.finish()
