"""API-key resolution: the secret first, then the environment, never silently nothing."""

from __future__ import annotations

import pyarrow as pa
import pytest

from vgi_typesafe import auth


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(auth.API_KEY_ENV, raising=False)
    monkeypatch.delenv(auth.BASE_URL_ENV, raising=False)


def test_secret_keyed_by_type() -> None:
    credentials = auth.for_call({"typesafe": {"api_key": "k"}})
    assert credentials == auth.Credentials(api_key="k", base_url=auth.DEFAULT_BASE_URL)


def test_secret_with_a_custom_name_is_found_by_its_type_field() -> None:
    """Resolved secrets are keyed by *name*; `CREATE SECRET my_ts (TYPE typesafe, ...)` must work."""
    secrets = {
        "some_s3": {"type": "s3", "api_key": "not-this-one"},
        "my_ts": {"type": "typesafe", "api_key": "k"},
    }
    assert auth.for_call(secrets).api_key == "k"


def test_arrow_scalars_are_unwrapped() -> None:
    secrets = {"typesafe": {"api_key": pa.scalar("k"), "base_url": pa.scalar("http://localhost:8787/")}}
    assert auth.for_call(secrets) == auth.Credentials(api_key="k", base_url="http://localhost:8787")


def test_null_base_url_falls_back_to_production() -> None:
    secrets = {"typesafe": {"api_key": pa.scalar("k"), "base_url": pa.scalar(None, pa.string())}}
    assert auth.for_call(secrets).base_url == auth.DEFAULT_BASE_URL


def test_environment_is_the_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(auth.API_KEY_ENV, "env-key")
    monkeypatch.setenv(auth.BASE_URL_ENV, "http://mock:1")
    assert auth.for_call(None) == auth.Credentials(api_key="env-key", base_url="http://mock:1")


def test_the_secret_wins_over_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(auth.API_KEY_ENV, "env-key")
    assert auth.for_call({"typesafe": {"api_key": "secret-key"}}).api_key == "secret-key"


@pytest.mark.parametrize(
    "secrets", [None, {}, {"typesafe": {"api_key": ""}}, {"typesafe": {"api_key": "  "}}]
)
def test_no_key_raises_with_the_fix(secrets: dict | None) -> None:
    with pytest.raises(auth.TypeSafeAuthError, match=r"CREATE SECRET \(TYPE typesafe, api_key"):
        auth.for_call(secrets)


def test_the_key_is_redacted_everywhere_it_could_leak() -> None:
    assert "k-123" not in repr(auth.Credentials(api_key="k-123"))
    field = auth.SECRET_SPEC.schema.field(auth.API_KEY)
    assert field.metadata == {b"redact": b"true"}
