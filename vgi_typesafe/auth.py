# Copyright 2026 Query Farm LLC - https://query.farm

"""TypeSafe API-key resolution.

The key arrives as a DuckDB secret rather than a function argument or an ATTACH
option, because both of those are visible in the query text and in
``duckdb_databases()``::

    CREATE SECRET typesafe (TYPE typesafe, api_key 'ts-...');

``api_key`` is marked redacted, so ``duckdb_secrets()`` shows it masked. The
secret may also carry ``base_url`` to point the worker somewhere other than
production — the bundled mock endpoint, or a proxy.

When no secret resolves, the worker falls back to the same environment variables
the official TypeSafe SDKs read (``TYPESAFE_API_KEY`` / ``TYPESAFE_BASE_URL``),
so a deployment can inject the key without any SQL at all.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

import pyarrow as pa
from vgi.catalog.secret_type import SecretTypeSpec

#: The DuckDB secret type this worker registers at ATTACH.
SECRET_TYPE = "typesafe"

#: Secret fields.
API_KEY = "api_key"
BASE_URL = "base_url"

#: Environment fallbacks — the names the official TypeSafe SDKs use.
API_KEY_ENV = "TYPESAFE_API_KEY"
BASE_URL_ENV = "TYPESAFE_BASE_URL"

DEFAULT_BASE_URL = "https://api.typesafe.ai"

SECRET_SPEC = SecretTypeSpec(
    name=SECRET_TYPE,
    description=(
        "TypeSafe API credentials. Provide api_key (sent as a Bearer token). Optionally set "
        "base_url to target a non-production endpoint such as the bundled mock server."
    ),
    schema=pa.schema(
        [
            pa.field(API_KEY, pa.string(), metadata={"redact": "true"}),
            pa.field(BASE_URL, pa.string()),
        ]
    ),
)


class TypeSafeAuthError(RuntimeError):
    """No usable API key could be resolved."""


@dataclass(slots=True, frozen=True)
class Credentials:
    """A resolved API key and the endpoint it is to be sent to."""

    api_key: str
    base_url: str = DEFAULT_BASE_URL

    def __repr__(self) -> str:
        """Render without the key — it must never reach a log line or a traceback."""
        # Redaction lives here rather than at every call site.
        return f"Credentials(api_key='***', base_url={self.base_url!r})"


def _text(values: dict[str, Any], key: str) -> str:
    raw = values.get(key)
    if raw is None:
        return ""
    # Resolved secrets arrive as Arrow scalars; a plain dict is what tests pass.
    value = raw.as_py() if hasattr(raw, "as_py") else raw
    return "" if value is None else str(value).strip()


def _secret_values(secrets: dict[str, dict[str, Any]] | None) -> dict[str, Any] | None:
    """The resolved ``typesafe`` secret, whichever way the mapping is keyed.

    Resolved secrets are keyed by secret *name*, which only coincides with the
    type when the user named it that way — so match on each entry's ``type``
    field first, and fall back to the key.
    """
    if not secrets:
        return None
    for values in secrets.values():
        if values and _text(values, "type") == SECRET_TYPE:
            return values
    return secrets.get(SECRET_TYPE) or None


def for_call(secrets: dict[str, dict[str, Any]] | None) -> Credentials:
    """Resolve the credentials for one call: the secret first, then the environment.

    Args:
        secrets: The resolved-secrets mapping from ``params.secrets``, or None.

    Returns:
        The API key and the base URL to send it to.

    Raises:
        TypeSafeAuthError: No key was found anywhere. Raised rather than sending
            an unauthenticated request, because the API's bare 401 would not
            tell the user how to fix it.
    """
    values = _secret_values(secrets) or {}
    api_key = _text(values, API_KEY) or os.environ.get(API_KEY_ENV, "").strip()
    base_url = _text(values, BASE_URL) or os.environ.get(BASE_URL_ENV, "").strip() or DEFAULT_BASE_URL
    if not api_key:
        raise TypeSafeAuthError(
            "no TypeSafe API key found; run "
            f"CREATE SECRET (TYPE {SECRET_TYPE}, {API_KEY} '...') or set {API_KEY_ENV} "
            "in the worker's environment"
        )
    return Credentials(api_key=api_key, base_url=base_url.rstrip("/"))
