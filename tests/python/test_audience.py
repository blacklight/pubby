"""
Tests for the pubby.audience helpers — addressee, public and Mention parsing.
"""

import pytest

from pubby.audience import (
    PUBLIC_URIS,
    addressees,
    is_public,
    mentioned_actors,
)

PUBLIC_ALIASES = sorted(PUBLIC_URIS)
AUDIENCE_FIELDS = ("to", "cc", "bto", "bcc")


class TestAddressees:
    @pytest.mark.parametrize("field", AUDIENCE_FIELDS)
    def test_collects_each_field(self, field):
        obj = {field: ["https://remote.example.com/users/alice"]}
        assert addressees(obj) == {"https://remote.example.com/users/alice"}

    def test_collects_all_fields(self):
        obj = {
            "to": "https://a.example.com/1",
            "cc": ["https://a.example.com/2"],
            "bto": ["https://a.example.com/3"],
            "bcc": "https://a.example.com/4",
        }
        assert addressees(obj) == {
            "https://a.example.com/1",
            "https://a.example.com/2",
            "https://a.example.com/3",
            "https://a.example.com/4",
        }

    def test_string_field_value(self):
        assert addressees({"to": "https://a.example.com/u"}) == {
            "https://a.example.com/u"
        }

    def test_ignores_non_string_members(self):
        obj = {"to": ["https://a.example.com/u", 42, {"id": "x"}, None]}
        assert addressees(obj) == {"https://a.example.com/u"}

    def test_ignores_malformed_field_values(self):
        obj = {"to": 42, "cc": {"url": "x"}, "bto": None, "bcc": True}
        assert addressees(obj) == set()

    def test_missing_fields(self):
        assert addressees({}) == set()

    def test_does_not_descend_into_object(self):
        activity = {
            "to": ["https://a.example.com/u"],
            "object": {"to": ["https://b.example.com/u"]},
        }
        assert addressees(activity) == {"https://a.example.com/u"}

    def test_non_dict_input(self):
        assert addressees("https://a.example.com/u") == set()


class TestIsPublic:
    @pytest.mark.parametrize("alias", PUBLIC_ALIASES)
    @pytest.mark.parametrize("field", AUDIENCE_FIELDS)
    def test_each_alias_in_each_field(self, alias, field):
        assert is_public({field: [alias]}) is True

    @pytest.mark.parametrize("alias", PUBLIC_ALIASES)
    def test_string_field_value(self, alias):
        assert is_public({"to": alias}) is True

    def test_public_in_bcc_is_public(self):
        assert (
            is_public({"bcc": ["https://www.w3.org/ns/activitystreams#Public"]}) is True
        )

    def test_followers_only_is_not_public(self):
        obj = {
            "to": ["https://remote.example.com/users/alice/followers"],
            "cc": ["https://blog.example.com/ap/actor"],
        }
        assert is_public(obj) is False

    def test_actor_only_is_not_public(self):
        assert is_public({"to": ["https://blog.example.com/ap/actor"]}) is False

    def test_no_audience_is_not_public(self):
        assert is_public({}) is False

    @pytest.mark.parametrize(
        "alias",
        [
            " Public",
            "Public ",
            "https://www.w3.org/ns/activitystreams#Public ",
        ],
    )
    def test_alias_with_whitespace_is_not_public(self, alias):
        """PUBLIC_URIS membership is exact string equality — no trimming."""
        assert is_public({"to": [alias]}) is False

    @pytest.mark.parametrize("alias", ["public", "PUBLIC", "as:public"])
    def test_alias_wrong_case_is_not_public(self, alias):
        assert is_public({"to": [alias]}) is False

    def test_does_not_descend_into_object(self):
        activity = {
            "to": ["https://blog.example.com/ap/actor"],
            "object": {"to": ["https://www.w3.org/ns/activitystreams#Public"]},
        }
        assert is_public(activity) is False


class TestMentionedActors:
    def test_extracts_mention_hrefs(self):
        obj = {
            "tag": [
                {"type": "Mention", "href": "https://a.example.com/u/1"},
                {"type": "Mention", "href": "https://a.example.com/u/2"},
            ]
        }
        assert mentioned_actors(obj) == [
            "https://a.example.com/u/1",
            "https://a.example.com/u/2",
        ]

    def test_deduplicates_preserving_first_seen_order(self):
        obj = {
            "tag": [
                {"type": "Mention", "href": "https://a.example.com/u/1"},
                {"type": "Mention", "href": "https://a.example.com/u/2"},
                {"type": "Mention", "href": "https://a.example.com/u/1"},
            ]
        }
        assert mentioned_actors(obj) == [
            "https://a.example.com/u/1",
            "https://a.example.com/u/2",
        ]

    def test_ignores_non_mention_tags(self):
        obj = {
            "tag": [
                {"type": "Hashtag", "href": "https://a.example.com/t/x"},
                {"type": "Emoji", "href": "https://a.example.com/e/x"},
            ]
        }
        assert mentioned_actors(obj) == []

    def test_ignores_malformed_entries(self):
        obj = {
            "tag": [
                "not-a-dict",
                {"type": "Mention"},
                {"type": "Mention", "href": ""},
                {"type": "Mention", "href": 42},
                None,
            ]
        }
        assert mentioned_actors(obj) == []

    def test_no_tags(self):
        assert mentioned_actors({}) == []

    def test_non_list_tag(self):
        assert mentioned_actors({"tag": "not-a-list"}) == []
