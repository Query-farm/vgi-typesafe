# Copyright 2026 Query Farm LLC - https://query.farm

"""The only tests that touch the real TypeSafe API.

Everything else in this suite asserts the *mock's* behaviour. That is fast and
free, but it means the mock is simultaneously the thing under test and the thing
defining correct — so a change on TypeSafe's side is invisible until a user hits
it. These tests close that loop: they assert the contract this worker depends on
against production, and nothing more.

They already earned their keep. The mock used to echo the requested model back
(``jev-latest``); production resolves the alias to a concrete version
(``jev-1.13.0``). Six offline tests had pinned the echo as if it were the API's
behaviour.

Deselected by default (``addopts = -m 'not live'``). To run them::

    TYPESAFE_API_KEY=... uv run pytest -m live

Every test here costs real tokens, so the file stays deliberately small: one
request per behaviour, the cheapest question that can demonstrate it, and no
parametrised sweeps.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from vgi_typesafe import typesafe_api as api
from vgi_typesafe.auth import Credentials

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def credentials() -> Credentials:
    """Real credentials, or skip — never fall back to the mock and call it live."""
    key = os.environ.get("TYPESAFE_API_KEY", "").strip()
    if not key:
        pytest.skip("TYPESAFE_API_KEY is not set")
    return Credentials(api_key=key)


@pytest.fixture(scope="module")
def client() -> Iterator[api.httpx.Client]:
    """One client for the module; the default timeout is tight for a live call."""
    with api.open_client(timeout=30.0) as session:
        yield session


@pytest.fixture(scope="module")
def answers(credentials: Credentials, client: api.httpx.Client) -> api.Response:
    """One request carrying all three question types, shared by every assertion.

    Module-scoped on purpose: the point is to verify the contract, not to spend
    a request per assertion.
    """
    return api.ask(
        {"message": "My package never arrived and I am furious about it", "tier": "gold"},
        {
            "dept": {
                "type": "choice",
                "instructions": "Which team should handle this?",
                "criteria": {
                    "shipping": "Delivery status, delays, lost packages",
                    "billing": "Charges, invoices, payment problems",
                },
            },
            "angry": {"type": "noul", "instructions": "Is the customer angry?"},
            "severity": {
                "type": "score",
                "instructions": "How severe is this?",
                "criteria": ["minor question", "disruptive delay", "critical outage"],
            },
        },
        credentials=credentials,
        client=client,
    )


class TestTheContractHolds:
    """Each assertion is something this worker's own code would break without."""

    def test_every_question_is_answered_in_one_request(self, answers: api.Response) -> None:
        """The premise of ask(): N questions, one request, N answers keyed by our names."""
        assert set(answers.answers) == {"dept", "angry", "severity"}

    def test_a_choice_answer_has_the_fields_we_expose(self, answers: api.Response) -> None:
        """These become the fields of the STRUCT column a SQL user reads."""
        dept = answers.answers["dept"]
        assert dept["choice"] in {"shipping", "billing"}
        assert 0.0 <= dept["confidence"] <= 1.0
        assert set(dept["probabilities"]) == {"shipping", "billing"}
        assert sum(dept["probabilities"].values()) == pytest.approx(1.0, abs=0.01)

    def test_a_noul_answer_is_a_probability(self, answers: api.Response) -> None:
        """We expose it as a DOUBLE and document 0-1; that has to be true."""
        assert 0.0 <= answers.answers["angry"]["noul"] <= 1.0

    def test_score_probabilities_are_keyed_by_level_number(self, answers: api.Response) -> None:
        """The API keys them as strings; we expose MAP(INTEGER, DOUBLE), so the cast must hold."""
        severity = answers.answers["severity"]
        assert set(severity["probabilities"]) == {0, 1, 2}
        assert 0.0 <= severity["score"] <= 2.0

    def test_usage_is_reported(self, answers: api.Response) -> None:
        """The `usage` column exists so a user can see what a query cost."""
        assert answers.input_tokens and answers.input_tokens > 0
        assert answers.output_tokens and answers.output_tokens > 0

    def test_an_alias_resolves_to_a_concrete_version(self, answers: api.Response) -> None:
        """We ask for `jev-latest`; production answers with the version it used.

        The mock echoed the alias until this test's real-world counterpart showed
        otherwise — the exact drift these tests exist to catch.
        """
        assert answers.model != api.DEFAULT_MODEL
        assert answers.model.startswith("jev-")


@pytest.fixture(scope="module")
def models(credentials: Credentials, client: api.httpx.Client) -> list[api.Model]:
    """The live model listing, fetched once for the module. A GET costs no tokens."""
    return api.list_models(credentials=credentials, client=client)


class TestTheModelListingIsWhatWePublish:
    """`models()` claims a fixed schema and a fixed set of rows; only production can confirm it.

    The mock serves a copy of this listing, so every other test of `models()`
    asserts our own transcription. These are the assertions that would notice
    TypeSafe renaming a field, dropping the preview line, or changing the
    timestamp format. A GET costs no tokens, so this is the one place in this
    file where extra requests are affordable.
    """

    def test_the_listing_is_not_empty(self, models: list[api.Model]) -> None:
        """An empty result would make the function useless and would not fail any offline test."""
        assert models

    def test_every_row_has_the_three_columns_we_expose(self, models: list[api.Model]) -> None:
        """These are `models()`'s entire output, declared statically in vgi.result_columns_schema."""
        assert all(model.name.strip() for model in models)
        assert all(model.description.strip() for model in models)
        assert all(model.release_date is not None for model in models)

    def test_release_dates_are_timezone_aware(self, models: list[api.Model]) -> None:
        """The column is TIMESTAMP WITH TIME ZONE; a naive value there would be a silent offset."""
        assert all(model.release_date is not None and model.release_date.tzinfo is not None for model in models)

    def test_the_default_model_is_one_of_the_listed_names(self, models: list[api.Model]) -> None:
        """`model =>` defaults to DEFAULT_MODEL, so the listing has to contain it or the docs lie."""
        assert api.DEFAULT_MODEL in {model.name for model in models}

    def test_the_mock_serves_the_same_shape(self, models: list[api.Model]) -> None:
        """Every offline test of models() asserts the mock; this is what keeps the mock honest."""
        from vgi_typesafe.mock_server import MODELS

        assert {key for model in MODELS for key in model} == {"name", "description", "release_date"}
        assert {model["name"] for model in MODELS} <= {model.name for model in models}

    def test_a_listed_name_is_accepted_as_a_model(
        self, models: list[api.Model], credentials: Credentials, client: api.httpx.Client
    ) -> None:
        """A name the listing publishes but a request rejects would make discovery actively misleading."""
        preview = next((m.name for m in models if m.name != api.DEFAULT_MODEL), api.DEFAULT_MODEL)
        response = api.ask(
            "hello",
            {"q": {"type": "noul", "instructions": "Is this a greeting?"}},
            credentials=credentials,
            client=client,
            model=preview,
        )
        assert 0.0 <= response.answers["q"]["noul"] <= 1.0

    def test_a_bad_key_is_rejected_here_too(self, client: api.httpx.Client) -> None:
        """Discovery must not be an unauthenticated side door, and must name the failure our way."""
        bad = Credentials(api_key="ts-definitely-not-a-real-key")
        with pytest.raises(api.TypeSafeError) as excinfo:
            api.list_models(credentials=bad, client=client)
        assert excinfo.value.status == 401
        assert "rejected the API key" in str(excinfo.value)
        assert "ts-definitely-not-a-real-key" not in str(excinfo.value)


class TestTheModelIsUsable:
    """Not "is the model correct" — that is TypeSafe's job — but "is it usable from SQL"."""

    def test_an_obvious_classification_is_answered_obviously(self, answers: api.Response) -> None:
        """A lost parcel routes to shipping and reads as angry, or the docs oversell it."""
        assert answers.answers["dept"]["choice"] == "shipping"
        assert answers.answers["angry"]["noul"] > 0.5


class TestErrorsAreWhatWeClaim:
    """The worker turns these into DuckDB errors, so the shape matters."""

    def test_a_bad_key_is_a_401_we_name_clearly(self, client: api.httpx.Client) -> None:
        """Our message must say the key was rejected, and must not echo the key."""
        bad = Credentials(api_key="ts-definitely-not-a-real-key")
        with pytest.raises(api.TypeSafeError) as excinfo:
            api.ask("hello", {"q": {"type": "noul", "instructions": "Is this a test?"}}, credentials=bad, client=client)
        assert excinfo.value.status == 401
        assert "rejected the API key" in str(excinfo.value)
        assert "ts-definitely-not-a-real-key" not in str(excinfo.value)

    def test_a_malformed_question_is_a_422(self, credentials: Credentials, client: api.httpx.Client) -> None:
        """A score question with no criteria at all is rejected upstream, as we assume."""
        with pytest.raises(api.TypeSafeError) as excinfo:
            api.ask(
                "hello",
                {"q": {"type": "score", "instructions": "How bad?"}},
                credentials=credentials,
                client=client,
            )
        assert excinfo.value.status == 422

    def test_an_unknown_question_type_is_rejected_upstream(
        self, credentials: Credentials, client: api.httpx.Client
    ) -> None:
        """We reject these at bind; this confirms we are not inventing a restriction."""
        with pytest.raises(api.TypeSafeError) as excinfo:
            api.ask(
                "hello", {"q": {"type": "essay", "instructions": "Discuss."}}, credentials=credentials, client=client
            )
        assert excinfo.value.status == 422


class TestWeAreStricterThanProductionOnPurpose:
    """Two places where the API is laxer than its own documentation.

    We follow the documentation. These tests record the gap so that a future
    reader knows it is a decision, not an oversight — and so they find out here
    rather than from a user who relied on the laxer behaviour.
    """

    def test_production_accepts_a_one_level_score_but_we_do_not(
        self, credentials: Credentials, client: api.httpx.Client
    ) -> None:
        """The docs say 2-10 levels. Production takes one, and scores every row 0.0."""
        response = api.ask(
            "hello",
            {"q": {"type": "score", "instructions": "How bad?", "criteria": ["only one level"]}},
            credentials=credentials,
            client=client,
        )
        # Accepted, and useless: a one-level scale has exactly one answer.
        assert response.answers["q"]["score"] == 0.0

        # ask() refuses it at bind, before a row is billed for that answer.
        from vgi_typesafe.ask import questions_of

        with pytest.raises(ValueError, match="2-10 level descriptions"):
            questions_of({"q": {"type": "score", "instructions": "How bad?", "criteria": ["only one level"]}})

    def test_production_allows_a_noul_with_criteria_and_no_instructions(
        self, credentials: Credentials, client: api.httpx.Client
    ) -> None:
        """The reference says every type requires `instructions`; noul in fact accepts criteria alone.

        We require instructions, matching the documentation: a question with no
        prose is far harder to read back in a SQL result than it is to write.
        """
        response = api.ask(
            "I was charged twice",
            {"q": {"type": "noul", "criteria": {"true": "about money", "false": "about delivery"}}},
            credentials=credentials,
            client=client,
        )
        assert 0.0 <= response.answers["q"]["noul"] <= 1.0

        from vgi_typesafe.ask import questions_of

        with pytest.raises(ValueError, match="requires 'instructions'"):
            questions_of({"q": {"type": "noul", "criteria": {"true": "about money"}}})


class TestStructuredInputIsAccepted:
    """Shapes we advertise in the catalog docs must actually be accepted upstream."""

    def test_structured_criteria_and_instructions(self, credentials: Credentials, client: api.httpx.Client) -> None:
        """A rubric object for an option, and for a score level, in one request."""
        response = api.ask(
            ["Hi", "I was charged twice on my invoice"],
            {
                "dept": {
                    "type": "choice",
                    "instructions": {"what": "Route the ticket", "examples": ["a lost parcel -> shipping"]},
                    "criteria": {
                        "shipping": {"what": "Delivery problems", "not_for": "Payment problems"},
                        "billing": "Charges, invoices, payment problems",
                    },
                },
                "sev": {
                    "type": "score",
                    "instructions": "How severe?",
                    "criteria": [
                        {"summary": "minor", "signals": ["a question"]},
                        {"summary": "major", "signals": ["money lost"]},
                    ],
                },
            },
            credentials=credentials,
            client=client,
        )
        # An array state, a structured instruction and both criteria shapes, all accepted.
        assert response.answers["dept"]["choice"] == "billing"
        assert set(response.answers["sev"]["probabilities"]) == {0, 1}
