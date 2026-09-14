"""
Tests for quote helpers — FEP-0449 fields, FEP-044f requests and
authorizations.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from pubby import (
    PUBLIC_QUOTE_POLICY,
    allow_public_quotes,
    build_quote_authorization,
    build_quote_request_activity,
    extract_quote_target,
    set_quote_target,
)
from pubby.handlers._outbox import OutboxProcessor
from pubby.quotes import FEP_044F_CONTEXT, QUOTE_FIELD_KEYS

ACTOR = "https://blog.example.com/ap/actor"
QUOTED = "https://blog.example.com/post/1"
QUOTER = "https://remote.example.com/users/bob"


@pytest.fixture
def mock_storage():
    storage = MagicMock()
    storage.get_followers.return_value = []
    storage.get_activities.return_value = []
    return storage


@pytest.fixture
def outbox_processor(mock_storage, private_key):
    return OutboxProcessor(
        storage=mock_storage,
        actor_id=ACTOR,
        private_key=private_key,
        key_id=f"{ACTOR}#main-key",
        followers_collection_url="https://blog.example.com/ap/followers",
        async_delivery=False,
    )


class TestExtractQuoteTarget:
    def test_each_spelling(self):
        for key in QUOTE_FIELD_KEYS:
            assert extract_quote_target({key: QUOTED}) == QUOTED

    def test_fep0449_quote_wins(self):
        obj = {"quote": "https://a.example/1", "quoteUrl": "https://b.example/2"}
        assert extract_quote_target(obj) == "https://a.example/1"

    def test_missing(self):
        assert extract_quote_target({"content": "<p>hi</p>"}) is None

    def test_non_string_ignored(self):
        assert extract_quote_target({"quote": {"id": QUOTED}}) is None
        assert extract_quote_target({"quoteUrl": ""}) is None


class TestSetQuoteTarget:
    def test_stamps_all_spellings(self):
        obj = {"id": f"{ACTOR}/objects/1", "type": "Note"}
        result = set_quote_target(obj, QUOTED)
        assert result is obj
        for key in QUOTE_FIELD_KEYS:
            assert obj[key] == QUOTED


class TestAllowPublicQuotes:
    def test_stamps_policy(self):
        obj = {"id": f"{ACTOR}/objects/1"}
        result = allow_public_quotes(obj)
        assert result is obj
        assert obj["interactionPolicy"] == PUBLIC_QUOTE_POLICY

    def test_policy_is_copied(self):
        obj = allow_public_quotes({})
        obj["interactionPolicy"]["canQuote"]["manualApproval"].append("x")
        assert PUBLIC_QUOTE_POLICY["canQuote"]["manualApproval"] == []


class TestBuildQuoteRequestActivity:
    def test_fields_and_default_addressing(self):
        instrument = {"id": f"{ACTOR}/objects/9", "type": "Note"}
        activity = build_quote_request_activity(
            actor_id=ACTOR,
            quoted_object_id=QUOTED,
            instrument=instrument,
            target_actor_id=QUOTER,
        )
        assert activity["type"] == "QuoteRequest"
        assert activity["actor"] == ACTOR
        assert activity["object"] == QUOTED
        assert activity["instrument"] == instrument
        assert activity["to"] == [QUOTER]
        assert activity["cc"] == []
        assert activity["id"].startswith(f"{ACTOR}/activities/")
        assert "published" in activity

    def test_context_declares_quote_request(self):
        activity = build_quote_request_activity(
            actor_id=ACTOR,
            quoted_object_id=QUOTED,
            instrument=f"{ACTOR}/objects/9",
            target_actor_id=QUOTER,
        )
        assert activity["@context"] == FEP_044F_CONTEXT
        terms = activity["@context"][1]
        assert terms["QuoteRequest"] == "https://w3id.org/fep/044f#QuoteRequest"

    def test_explicit_addressing_and_id(self):
        instrument = {"id": f"{ACTOR}/objects/9"}
        activity = build_quote_request_activity(
            actor_id=ACTOR,
            quoted_object_id=QUOTED,
            instrument=instrument,
            target_actor_id=QUOTER,
            to=["https://other.example/users/carol"],
            cc=["https://blog.example.com/ap/followers"],
            activity_id=f"{ACTOR}/activities/qr-1",
            published=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        assert activity["to"] == ["https://other.example/users/carol"]
        assert activity["cc"] == ["https://blog.example.com/ap/followers"]
        assert activity["id"] == f"{ACTOR}/activities/qr-1"
        assert activity["published"] == "2025-01-01T00:00:00+00:00"

    def test_outbox_processor_binds_actor(self, outbox_processor):
        activity = outbox_processor.build_quote_request_activity(
            quoted_object_id=QUOTED,
            instrument={"id": f"{ACTOR}/objects/9"},
            target_actor_id=QUOTER,
        )
        assert activity["actor"] == ACTOR
        assert activity["to"] == [QUOTER]


class TestBuildQuoteAuthorization:
    def test_fields_and_generated_id(self):
        auth = build_quote_authorization(
            ACTOR,
            interacting_object=f"{QUOTER}/statuses/1",
            interaction_target=QUOTED,
        )
        assert auth["type"] == "QuoteAuthorization"
        assert auth["attributedTo"] == ACTOR
        assert auth["interactingObject"] == f"{QUOTER}/statuses/1"
        assert auth["interactionTarget"] == QUOTED
        assert auth["id"].startswith(f"{ACTOR}/quote_authorizations/")
        assert "to" not in auth
        assert "cc" not in auth

    def test_explicit_id_and_audience(self):
        auth = build_quote_authorization(
            ACTOR,
            interacting_object=f"{QUOTER}/statuses/1",
            interaction_target=QUOTED,
            authorization_id=f"{ACTOR}/quote_authorizations/fixed",
            to=["https://www.w3.org/ns/activitystreams#Public"],
            cc=[f"{ACTOR}/followers"],
        )
        assert auth["id"] == f"{ACTOR}/quote_authorizations/fixed"
        assert auth["to"] == ["https://www.w3.org/ns/activitystreams#Public"]
        assert auth["cc"] == [f"{ACTOR}/followers"]

    def test_context_declares_gts_terms(self):
        auth = build_quote_authorization(
            ACTOR,
            interacting_object=f"{QUOTER}/statuses/1",
            interaction_target=QUOTED,
        )
        terms = auth["@context"][1]
        assert (
            terms["QuoteAuthorization"]
            == "https://w3id.org/fep/044f#QuoteAuthorization"
        )
        assert terms["interactingObject"]["@id"] == "gts:interactingObject"
        assert terms["interactionTarget"]["@id"] == "gts:interactionTarget"
