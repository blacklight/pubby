"""
Tests for pubby.attribution.validate — opt-in inbound object authorship checks.
"""

import pytest

from pubby import AttributionMismatch
from pubby.attribution import validate

ACTOR = "https://remote.example.com/users/alice"


class TestValidate:
    def test_matching_attribution_and_host(self):
        validate(
            ACTOR,
            {
                "id": f"{ACTOR}/notes/1",
                "attributedTo": ACTOR,
            },
        )

    def test_absent_attributed_to_accepted(self):
        validate(ACTOR, {"id": f"{ACTOR}/notes/1"})

    def test_empty_attributed_to_accepted(self):
        validate(ACTOR, {"id": f"{ACTOR}/notes/1", "attributedTo": ""})

    def test_mismatched_attributed_to_rejected(self):
        with pytest.raises(AttributionMismatch):
            validate(
                ACTOR,
                {
                    "id": f"{ACTOR}/notes/1",
                    "attributedTo": "https://other.example.com/users/mallory",
                },
            )

    def test_attributed_to_list_containing_actor_accepted(self):
        validate(
            ACTOR,
            {
                "id": f"{ACTOR}/notes/1",
                "attributedTo": ["https://other.example.com/u", ACTOR],
            },
        )

    def test_attributed_to_dict_matching_actor_accepted(self):
        validate(
            ACTOR,
            {
                "id": f"{ACTOR}/notes/1",
                "attributedTo": {"id": ACTOR, "type": "Person"},
            },
        )

    def test_attributed_to_dict_mismatch_rejected(self):
        with pytest.raises(AttributionMismatch):
            validate(
                ACTOR,
                {
                    "id": f"{ACTOR}/notes/1",
                    "attributedTo": {"id": "https://other.example.com/u"},
                },
            )

    def test_attributed_to_list_with_dict_member_accepted(self):
        validate(
            ACTOR,
            {
                "id": f"{ACTOR}/notes/1",
                "attributedTo": [
                    {"id": ACTOR},
                    "https://other.example.com/u",
                ],
            },
        )

    def test_attributed_to_list_without_actor_rejected(self):
        with pytest.raises(AttributionMismatch):
            validate(
                ACTOR,
                {
                    "id": f"{ACTOR}/notes/1",
                    "attributedTo": [
                        "https://other.example.com/u",
                        {"id": "https://other.example.com/v"},
                    ],
                },
            )

    def test_attributed_to_unverifiable_type_rejected(self):
        with pytest.raises(AttributionMismatch):
            validate(
                ACTOR,
                {"id": f"{ACTOR}/notes/1", "attributedTo": 42},
            )

    def test_missing_object_id_skips_host_check(self):
        validate(ACTOR, {"type": "Note", "content": "hi"})

    def test_foreign_hosted_object_id_rejected(self):
        with pytest.raises(AttributionMismatch):
            validate(
                ACTOR,
                {"id": "https://victim.example.com/notes/1"},
            )

    def test_unparseable_object_id_rejected(self):
        with pytest.raises(AttributionMismatch):
            validate(ACTOR, {"id": "not a url"})

    def test_host_comparison_is_case_insensitive(self):
        validate(
            "https://Remote.Example.COM/users/alice",
            {"id": "https://remote.example.com/users/alice/notes/1"},
        )
