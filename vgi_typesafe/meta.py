"""Catalog-metadata helpers: column comments, result schemas, doc tags.

VGI publishes documentation as ``vgi.*`` tags on catalog objects. The tags are
strings carrying JSON, which is easy to get subtly wrong by hand, so they are
built here from the same Arrow schema the function returns — a column documented
once shows up in ``DESCRIBE`` and in the declared result schema alike.
"""

from __future__ import annotations

import json
from typing import Any

import pyarrow as pa

#: Arrow field-metadata key DuckDB reads column comments from.
_COMMENT_KEY = b"comment"


def field(name: str, type: pa.DataType, comment: str, *, nullable: bool = True) -> pa.Field:
    """A ``pa.Field`` carrying its column comment as Arrow field metadata."""
    return pa.field(name, type, nullable=nullable, metadata={_COMMENT_KEY: comment.encode()})


def comment_of(f: pa.Field) -> str:
    """The comment attached to ``f``, or an empty string when it has none."""
    if f.metadata and _COMMENT_KEY in f.metadata:
        return str(f.metadata[_COMMENT_KEY].decode())
    return ""


def _sql_type(kind: pa.DataType) -> str:
    """Render an Arrow type as the DuckDB type name a consumer will actually see."""
    if pa.types.is_float64(kind):
        return "DOUBLE"
    if pa.types.is_int64(kind):
        return "BIGINT"
    if pa.types.is_string(kind) or pa.types.is_large_string(kind):
        return "VARCHAR"
    if pa.types.is_map(kind):
        return f"MAP({_sql_type(kind.key_type)}, {_sql_type(kind.item_type)})"
    raise ValueError(f"no DuckDB type mapping for {kind}")


def result_columns_schema(schema: pa.Schema) -> str:
    """Render a table function's static result shape as ``vgi.result_columns_schema``."""
    return json.dumps(
        [{"name": f.name, "type": _sql_type(f.type), "description": comment_of(f)} for f in schema]
    )


def examples(*pairs: tuple[str, str]) -> str:
    """Render ``(description, sql)`` pairs as a ``vgi.example_queries`` tag."""
    return json.dumps([{"description": description, "sql": sql} for description, sql in pairs])


def keywords(*terms: str) -> str:
    """Render search terms as a ``vgi.keywords`` tag."""
    return json.dumps(list(terms))


def docs(
    *,
    llm: str,
    md: str,
    category: str | None = None,
    result_schema: pa.Schema | None = None,
    example_queries: str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """Assemble the ``vgi.*`` documentation tags for one catalog object."""
    tags: dict[str, Any] = {"vgi.doc_llm": llm.strip(), "vgi.doc_md": md.strip()}
    if category is not None:
        tags["vgi.category"] = category
    if result_schema is not None:
        tags["vgi.result_columns_schema"] = result_columns_schema(result_schema)
    if example_queries is not None:
        tags["vgi.example_queries"] = example_queries
    if extra:
        tags.update(extra)
    return tags
