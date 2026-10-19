"""
Tests for model dataclasses: build, serialize, deserialize.
"""

from datetime import datetime, timezone

from pubby._model import (
    Activity,
    ActivityType,
    Actor,
    DeliveryStatus,
    Follower,
    Interaction,
    InteractionStatus,
    InteractionType,
    Object,
    ObjectType,
    AP_CONTEXT,
)


class TestEnums:
    def test_activity_type_from_raw(self):
        assert ActivityType.from_raw("Create") == ActivityType.CREATE
        assert ActivityType.from_raw("follow") == ActivityType.FOLLOW
        assert ActivityType.from_raw("LIKE") == ActivityType.LIKE

    def test_activity_type_from_raw_unknown(self):
        import pytest

        with pytest.raises(ValueError, match="Unknown activity type"):
            ActivityType.from_raw("FooBar")

    def test_object_type_from_raw(self):
        assert ObjectType.from_raw("Note") == ObjectType.NOTE
        assert ObjectType.from_raw("article") == ObjectType.ARTICLE

    def test_interaction_type_from_activity(self):
        assert InteractionType.from_activity_type("Create") == InteractionType.REPLY
        assert InteractionType.from_activity_type("Like") == InteractionType.LIKE
        assert InteractionType.from_activity_type("Announce") == InteractionType.BOOST

    def test_interaction_type_from_activity_unknown(self):
        import pytest

        with pytest.raises(ValueError):
            InteractionType.from_activity_type("Follow")

    def test_enum_values(self):
        assert InteractionStatus.PENDING.value == "pending"
        assert DeliveryStatus.DELIVERED.value == "delivered"


class TestActor:
    def test_build_and_to_dict(self):
        data = {
            "id": "https://example.com/actor",
            "type": "Person",
            "preferredUsername": "alice",
            "name": "Alice",
            "summary": "Hello!",
            "inbox": "https://example.com/inbox",
            "outbox": "https://example.com/outbox",
            "followers": "https://example.com/followers",
            "following": "https://example.com/following",
            "url": "https://example.com",
            "publicKey": {
                "id": "https://example.com/actor#main-key",
                "owner": "https://example.com/actor",
                "publicKeyPem": "-----BEGIN PUBLIC KEY-----\ntest\n-----END PUBLIC KEY-----",
            },
            "icon": {"type": "Image", "url": "https://example.com/icon.png"},
            "manuallyApprovesFollowers": False,
            "discoverable": True,
            "endpoints": {"sharedInbox": "https://example.com/shared-inbox"},
        }

        actor = Actor.build(data)
        assert actor.id == "https://example.com/actor"
        assert actor.preferred_username == "alice"
        assert actor.name == "Alice"
        assert actor.public_key_pem.startswith("-----BEGIN PUBLIC KEY-----")
        assert actor.icon == {"type": "Image", "url": "https://example.com/icon.png"}
        assert actor.endpoints == {"sharedInbox": "https://example.com/shared-inbox"}

        doc = actor.to_dict()
        assert doc["@context"] == AP_CONTEXT
        assert doc["id"] == "https://example.com/actor"
        assert doc["publicKey"]["owner"] == "https://example.com/actor"
        assert doc["endpoints"]["sharedInbox"] == "https://example.com/shared-inbox"

    def test_build_minimal(self):
        actor = Actor.build({"id": "https://example.com/actor"})
        assert actor.id == "https://example.com/actor"
        assert actor.type == "Person"
        assert actor.public_key_pem == ""

        doc = actor.to_dict()
        assert "publicKey" not in doc

    def test_icon_as_string(self):
        actor = Actor.build(
            {"id": "https://example.com/a", "icon": "https://example.com/icon.png"}
        )
        assert actor.icon == {"type": "Image", "url": "https://example.com/icon.png"}


class TestObject:
    def test_build_and_to_dict(self):
        now = datetime.now(timezone.utc)
        data = {
            "id": "https://example.com/note/1",
            "type": "Note",
            "content": "<p>Hello</p>",
            "attributedTo": "https://example.com/actor",
            "inReplyTo": "https://blog.example.com/post/1",
            "published": now.isoformat(),
            "to": ["https://www.w3.org/ns/activitystreams#Public"],
            "cc": ["https://example.com/followers"],
            "tag": [{"type": "Hashtag", "name": "#test"}],
            "summary": "CW text",
            "sensitive": True,
        }

        obj = Object.build(data)
        assert obj.id == "https://example.com/note/1"
        assert obj.content == "<p>Hello</p>"
        assert obj.in_reply_to == "https://blog.example.com/post/1"
        assert obj.sensitive is True
        assert len(obj.tag) == 1

        doc = obj.to_dict()
        assert doc["type"] == "Note"
        assert doc["summary"] == "CW text"
        assert doc["sensitive"] is True

    def test_build_from_string(self):
        obj = Object.build("https://example.com/note/1")
        assert obj.id == "https://example.com/note/1"

    def test_to_as_string(self):
        obj = Object.build({"id": "x", "to": "https://example.com/public"})
        assert obj.to == ["https://example.com/public"]

    def test_optional_fields_omitted(self):
        obj = Object(id="x", content="test")
        doc = obj.to_dict()
        assert "name" not in doc
        assert "inReplyTo" not in doc
        assert "summary" not in doc
        assert "sensitive" not in doc
        assert "tag" not in doc
        assert "contentMap" not in doc
        assert "summaryMap" not in doc
        assert "mediaType" not in doc

    def test_language_emits_content_map(self):
        obj = Object(id="x", content="<p>Hallo</p>", language="de")
        doc = obj.to_dict()
        assert doc["contentMap"] == {"de": "<p>Hallo</p>"}
        assert "summaryMap" not in doc

    def test_language_emits_summary_map(self):
        obj = Object(id="x", content="<p>Bonjour</p>", summary="Résumé", language="fr")
        doc = obj.to_dict()
        assert doc["contentMap"] == {"fr": "<p>Bonjour</p>"}
        assert doc["summaryMap"] == {"fr": "Résumé"}

    def test_language_not_emitted_when_none(self):
        obj = Object(id="x", content="hello")
        doc = obj.to_dict()
        assert "contentMap" not in doc

    def test_build_parses_language_from_content_map(self):
        data = {
            "id": "x",
            "content": "<p>Ciao</p>",
            "contentMap": {"it": "<p>Ciao</p>"},
        }
        obj = Object.build(data)
        assert obj.language == "it"

    def test_build_parses_language_field(self):
        data = {"id": "x", "content": "hello", "language": "en"}
        obj = Object.build(data)
        assert obj.language == "en"

    def test_build_content_map_takes_precedence(self):
        data = {
            "id": "x",
            "content": "hello",
            "contentMap": {"ja": "hello"},
            "language": "en",
        }
        obj = Object.build(data)
        assert obj.language == "ja"

    def test_build_no_language(self):
        obj = Object.build({"id": "x", "content": "hello"})
        assert obj.language is None

    def test_media_type_roundtrip(self):
        obj = Object(id="x", content="<p>test</p>", media_type="text/html")
        doc = obj.to_dict()
        assert doc["mediaType"] == "text/html"

        rebuilt = Object.build(doc)
        assert rebuilt.media_type == "text/html"

    def test_media_type_omitted_when_none(self):
        obj = Object(id="x", content="test")
        assert "mediaType" not in obj.to_dict()

    def test_url_as_link_list_roundtrip(self):
        links = [
            {
                "type": "Link",
                "href": "https://example.com/files/1/download",
                "mediaType": "audio/mpeg",
            },
            {
                "type": "Link",
                "href": "https://example.com/tracks/1",
                "mediaType": "text/html",
            },
        ]
        obj = Object(id="x", type="Audio", url=links)
        doc = obj.to_dict()
        assert doc["url"] == links

        rebuilt = Object.build(doc)
        assert rebuilt.url == links

    def test_url_string_unchanged(self):
        obj = Object(id="x", url="https://example.com/posts/1")
        doc = obj.to_dict()
        assert doc["url"] == "https://example.com/posts/1"
        assert Object.build(doc).url == "https://example.com/posts/1"

    def test_url_omitted_when_empty(self):
        obj = Object(id="x")
        assert "url" not in obj.to_dict()
        assert "url" not in Object(id="x", url=[]).to_dict()

    def test_attributed_to_as_list_roundtrip(self):
        actors = [
            "https://example.com/artists/1",
            "https://example.com/ap/actor",
        ]
        obj = Object(id="x", type="Audio", attributed_to=actors)
        doc = obj.to_dict()
        assert doc["attributedTo"] == actors

        rebuilt = Object.build(doc)
        assert rebuilt.attributed_to == actors

    def test_attributed_to_string_unchanged(self):
        obj = Object(id="x", attributed_to="https://example.com/ap/actor")
        doc = obj.to_dict()
        assert doc["attributedTo"] == "https://example.com/ap/actor"
        assert Object.build(doc).attributed_to == "https://example.com/ap/actor"

    def test_duration_emitted_verbatim(self):
        obj = Object(id="x", type="Audio", duration="PT1H2M3S")
        doc = obj.to_dict()
        assert doc["duration"] == "PT1H2M3S"

        rebuilt = Object.build(doc)
        assert rebuilt.duration == "PT1H2M3S"

    def test_duration_omitted_when_none(self):
        obj = Object(id="x", type="Audio")
        assert "duration" not in obj.to_dict()
        assert Object.build(obj.to_dict()).duration is None

    def test_audio_object_roundtrip(self):
        obj = Object(
            id="https://example.com/tracks/1",
            type="Audio",
            name="Track title",
            url=[
                {
                    "type": "Link",
                    "href": "https://example.com/files/1/download",
                    "mediaType": "audio/mpeg",
                },
                {
                    "type": "Link",
                    "href": "https://example.com/tracks/1",
                    "mediaType": "text/html",
                },
            ],
            attributed_to=[
                "https://example.com/artists/1",
                "https://example.com/ap/actor",
            ],
            duration="PT3M5S",
            attachment=[
                {
                    "type": "Document",
                    "mediaType": "audio/mpeg",
                    "url": "https://example.com/files/1/download",
                    "name": "Track title",
                }
            ],
        )
        doc = obj.to_dict()
        rebuilt = Object.build(doc)
        assert rebuilt.to_dict() == doc


class TestActivity:
    def test_build_and_to_dict(self):
        data = {
            "id": "https://example.com/activity/1",
            "type": "Create",
            "actor": "https://example.com/actor",
            "object": {
                "id": "https://example.com/note/1",
                "type": "Note",
                "content": "Hello",
            },
            "to": ["https://www.w3.org/ns/activitystreams#Public"],
            "cc": "https://example.com/followers",
            "published": "2024-01-01T00:00:00+00:00",
        }

        activity = Activity.build(data)
        assert activity.type == "Create"
        assert activity.actor == "https://example.com/actor"
        assert isinstance(activity.object, dict)
        assert activity.cc == ["https://example.com/followers"]
        assert activity.published is not None

        doc = activity.to_dict()
        assert doc["@context"] == AP_CONTEXT
        assert doc["type"] == "Create"

    def test_build_with_string_object(self):
        data = {
            "id": "https://example.com/like/1",
            "type": "Like",
            "actor": "https://example.com/actor",
            "object": "https://blog.example.com/post/1",
        }

        activity = Activity.build(data)
        assert activity.object == "https://blog.example.com/post/1"


class TestInteraction:
    def test_build_and_to_dict(self):
        now = datetime.now(timezone.utc)
        data = {
            "source_actor_id": "https://example.com/actor",
            "target_resource": "https://blog.example.com/post/1",
            "interaction_type": "reply",
            "content": "Great post!",
            "author_name": "Alice",
            "published": now.isoformat(),
            "status": "confirmed",
        }

        interaction = Interaction.build(data)
        assert interaction.interaction_type == InteractionType.REPLY
        assert interaction.status == InteractionStatus.CONFIRMED
        assert interaction.content == "Great post!"

        d = interaction.to_dict()
        assert d["interaction_type"] == "reply"
        assert d["status"] == "confirmed"
        assert isinstance(d["published"], str)

    def test_hash(self):
        i1 = Interaction(
            source_actor_id="a",
            target_resource="b",
            interaction_type=InteractionType.LIKE,
        )
        i2 = Interaction(
            source_actor_id="a",
            target_resource="b",
            interaction_type=InteractionType.LIKE,
        )
        assert hash(i1) == hash(i2)

    def test_mentioned_actors_field(self):
        interaction = Interaction(
            source_actor_id="https://remote.example.com/users/alice",
            target_resource="https://blog.example.com/posts/1",
            interaction_type=InteractionType.REPLY,
            mentioned_actors=[
                "https://blog.example.com/ap/actor",
                "https://other.example.com/users/bob",
            ],
        )
        assert len(interaction.mentioned_actors) == 2
        assert "https://blog.example.com/ap/actor" in interaction.mentioned_actors

        d = interaction.to_dict()
        assert d["mentioned_actors"] == [
            "https://blog.example.com/ap/actor",
            "https://other.example.com/users/bob",
        ]

    def test_mentioned_actors_build(self):
        data = {
            "source_actor_id": "https://remote.example.com/users/alice",
            "target_resource": "https://blog.example.com/posts/1",
            "interaction_type": "reply",
            "mentioned_actors": ["https://blog.example.com/ap/actor"],
        }
        interaction = Interaction.build(data)
        assert interaction.mentioned_actors == ["https://blog.example.com/ap/actor"]

    def test_mentioned_actors_defaults_empty(self):
        interaction = Interaction(
            source_actor_id="a",
            target_resource="b",
            interaction_type=InteractionType.LIKE,
        )
        assert interaction.mentioned_actors == []


class TestFollower:
    def test_build_and_to_dict(self):
        now = datetime.now(timezone.utc)
        data = {
            "actor_id": "https://mastodon.social/users/alice",
            "inbox": "https://mastodon.social/users/alice/inbox",
            "shared_inbox": "https://mastodon.social/inbox",
            "followed_at": now.isoformat(),
            "actor_data": {"name": "Alice"},
        }

        follower = Follower.build(data)
        assert follower.actor_id == "https://mastodon.social/users/alice"
        assert follower.shared_inbox == "https://mastodon.social/inbox"
        assert follower.actor_data == {"name": "Alice"}
        assert follower.target_actor_id == ""

        d = follower.to_dict()
        assert d["actor_id"] == "https://mastodon.social/users/alice"
        assert isinstance(d["followed_at"], str)

    def test_build_and_to_dict_with_target_actor_id(self):
        now = datetime.now(timezone.utc)
        data = {
            "actor_id": "https://mastodon.social/users/alice",
            "inbox": "https://mastodon.social/users/alice/inbox",
            "target_actor_id": "https://blog.example.com/ap/actor",
            "followed_at": now.isoformat(),
            "actor_data": {"name": "Alice"},
        }

        follower = Follower.build(data)
        assert follower.target_actor_id == "https://blog.example.com/ap/actor"

        d = follower.to_dict()
        rebuilt = Follower.build(d)
        assert rebuilt.target_actor_id == "https://blog.example.com/ap/actor"
