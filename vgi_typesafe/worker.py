"""VGI worker exposing TypeSafe System One choice questions to DuckDB/SQL.

    ATTACH 'typesafe' (TYPE vgi, LOCATION 'uv run typesafe_worker.py');
    CREATE SECRET (TYPE typesafe, api_key '...');

    SELECT t.id, c.choice, c.confidence
    FROM tickets t,
         LATERAL typesafe.main.choice(t.body,
             instructions => 'Which team should handle this?',
             criteria => MAP {'shipping': '...', 'billing': '...'}) c;

The function name is bare (``choice``, not ``typesafe_choice``) because it is
already qualified by the catalog it lives in.
"""

from __future__ import annotations

import sys

from vgi import Worker
from vgi.catalog import Catalog, ReadOnlyCatalogInterface, Schema
from vgi.catalog.catalog_interface import CatalogInfo

from vgi_typesafe import __version__, auth
from vgi_typesafe.choice import FUNCTIONS
from vgi_typesafe.meta import keywords

IMPLEMENTATION_VERSION = __version__
DATA_VERSION_SPEC = f"=={__version__}"
SOURCE_URL = "https://github.com/Query-farm/vgi-typesafe"

_KEYWORDS = keywords("typesafe", "system one", "classification", "choice", "routing", "confidence", "jev")

_CATALOG_TAGS = {
    "provider": "typesafe",
    "domain": "ai-classification",
    "vgi.title": "TypeSafe System One",
    "vgi.source_url": SOURCE_URL,
    "vgi.author": "Query Farm LLC <hello@query.farm>",
    "vgi.license": "MIT",
    "vgi.keywords": _KEYWORDS,
    "vgi.doc_llm": (
        "Structured AI judgments from TypeSafe's System One model, as SQL. Unlike a text-generating "
        "LLM, it answers a typed question about a piece of content and returns a value software "
        "can consume directly. Reach for this catalog to classify rows of a table into a fixed set "
        "of options — ticket routing, intent detection, content labelling — and get a calibrated "
        "confidence with each answer. Requires a `typesafe` secret holding an API key."
    ),
    "vgi.doc_md": (
        "TypeSafe evaluates typed questions against a *state* (the content to judge) and returns "
        "structured results rather than prose.\n\n"
        "### What is here\n\n"
        "`choice()` — pick one option from a set, with a probability for every option and a "
        "confidence score. It is a blended table function, so it composes under a correlated "
        "`LATERAL` to classify a whole table in one query.\n\n"
        "### Authentication\n\n"
        "`CREATE SECRET (TYPE typesafe, api_key '...')`. The key is redacted in "
        "`duckdb_secrets()`. The optional `base_url` field points the worker at another endpoint, "
        "such as the bundled mock server (`uv run vgi-typesafe-mock`).\n\n"
        "### Cost\n\n"
        "One API request per distinct state per input batch. Requests within a batch run "
        "concurrently; rate-limit and overload responses are retried with backoff."
    ),
}

_SCHEMA_TAGS = {
    "provider": "typesafe",
    "domain": "ai-classification",
    "vgi.title": "TypeSafe Questions",
    "vgi.keywords": _KEYWORDS,
}

_TYPESAFE_CATALOG = Catalog(
    name="typesafe",
    default_schema="main",
    comment="TypeSafe System One choice questions as a LATERAL-joinable table function",
    tags=_CATALOG_TAGS,
    source_url=SOURCE_URL,
    schemas=[
        Schema(
            path=["main"],
            comment="TypeSafe question functions — require a 'typesafe' secret",
            tags=_SCHEMA_TAGS,
            functions=list(FUNCTIONS),
        ),
    ],
)


class TypeSafeCatalog(ReadOnlyCatalogInterface):
    """Advertises the worker's versions and the ``typesafe`` secret type."""

    catalog = _TYPESAFE_CATALOG
    catalog_name = _TYPESAFE_CATALOG.name
    secret_types = [auth.SECRET_SPEC]

    def catalogs(self) -> list[CatalogInfo]:
        """Advertise the single TypeSafe catalog."""
        return [
            CatalogInfo(
                name=self._effective_catalog_name,
                implementation_version=IMPLEMENTATION_VERSION,
                data_version_spec=DATA_VERSION_SPEC,
                source_url=SOURCE_URL,
            )
        ]


class TypeSafeWorker(Worker):
    """Worker process hosting the TypeSafe catalog."""

    catalog = _TYPESAFE_CATALOG
    catalog_interface = TypeSafeCatalog


def main() -> None:
    """Run the worker (stdio by default; pass ``--http`` for the HTTP server)."""
    TypeSafeWorker.main()


def main_http() -> None:
    """Run the worker over HTTP."""
    argv = sys.argv[1:]
    if "--http" not in argv:
        argv = ["--http", *argv]
    sys.argv = [sys.argv[0], *argv]
    TypeSafeWorker.main()


if __name__ == "__main__":
    main()
