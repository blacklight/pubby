"""
Tests for 0.5.1 actor-endpoint changes: per-actor endpoint paths on
``ActorConfig``, actor-scoped outbox, the ``following`` storage seam,
and WebFinger URL resources.
"""

from pubby._model import ActorConfig
from pubby.crypto._keys import generate_rsa_keypair
from pubby.handlers import ActivityPubHandler
from pubby.handlers._multi import MultiActorHandler
from pubby.storage.adapters.file import FileActivityPubStorage

BASE = "https://blog.example.com"
ACTOR = f"{BASE}/ap/actor"


def _make_handler(storage, actor_config=None):
    private_key, _ = generate_rsa_keypair()
    return ActivityPubHandler(
        storage=storage,
        actor_config=actor_config
        or {
            "base_url": BASE,
            "username": "blog",
            "name": "Test Blog",
        },
        private_key=private_key,
        async_delivery=False,
    )


class TestEndpointPaths:
    def test_custom_paths(self, tmp_path):
        cfg = ActorConfig(
            base_url=BASE,
            username="alice",
            actor_path="/ap/users/alice",
            inbox_path="/ap/users/alice/inbox",
            outbox_path="/ap/users/alice/outbox",
            followers_path="/ap/users/alice/followers",
            following_path="/ap/users/alice/following",
        )
        h = _make_handler(FileActivityPubStorage(data_dir=tmp_path), cfg)
        assert h.inbox_url == f"{BASE}/ap/users/alice/inbox"
        assert h.outbox_url == f"{BASE}/ap/users/alice/outbox"
        assert h.followers_url == f"{BASE}/ap/users/alice/followers"
        assert h.following_url == f"{BASE}/ap/users/alice/following"
        # The shared inbox always stays on the prefix route.
        assert h.shared_inbox_url == f"{BASE}/ap/inbox"

        doc = h.get_actor_document()
        assert doc["inbox"] == h.inbox_url
        assert doc["outbox"] == h.outbox_url
        assert doc["followers"] == h.followers_url
        assert doc["following"] == h.following_url
        assert doc["endpoints"]["sharedInbox"] == h.shared_inbox_url

    def test_from_dict_paths(self):
        cfg = ActorConfig.from_dict(
            {
                "base_url": BASE,
                "inbox_path": "/ap/users/bob/inbox",
            }
        )
        assert cfg.inbox_path == "/ap/users/bob/inbox"
        assert cfg.outbox_path is None

    def test_default_unchanged(self, tmp_path):
        h = _make_handler(FileActivityPubStorage(data_dir=tmp_path))
        assert h.inbox_url == f"{BASE}/ap/inbox"
        assert h.outbox_url == f"{BASE}/ap/outbox"
        assert h.followers_url == f"{BASE}/ap/followers"
        assert h.following_url == f"{BASE}/ap/following"

    def test_actor_document_emits_config_tag(self, tmp_path):
        cfg = ActorConfig(
            base_url=BASE,
            username="blog",
            tag=[
                {
                    "type": "Hashtag",
                    "name": "#rust",
                    "href": f"{BASE}/tags/rust",
                },
                {
                    "type": "Emoji",
                    "name": ":blobcat:",
                    "icon": {
                        "type": "Image",
                        "mediaType": "image/png",
                        "url": f"{BASE}/emoji/blobcat.png",
                    },
                },
            ],
        )
        h = _make_handler(FileActivityPubStorage(data_dir=tmp_path), cfg)
        doc = h.get_actor_document()
        assert doc["tag"][0]["type"] == "Hashtag"
        assert doc["tag"][1]["name"] == ":blobcat:"

    def test_actor_document_omits_empty_tag(self, tmp_path):
        h = _make_handler(FileActivityPubStorage(data_dir=tmp_path))
        assert "tag" not in h.get_actor_document()


class TestScopedOutbox:
    def test_outbox_only_contains_own_activities(self, tmp_path):
        storage = FileActivityPubStorage(data_dir=tmp_path)
        h = _make_handler(storage)
        storage.store_activity(
            f"{ACTOR}/activities/1",
            {"id": f"{ACTOR}/activities/1", "type": "Create", "actor": ACTOR},
        )
        storage.store_activity(
            f"{BASE}/ap/users/bob/activities/2",
            {
                "id": f"{BASE}/ap/users/bob/activities/2",
                "type": "Create",
                "actor": f"{BASE}/ap/users/bob",
            },
        )

        outbox = h.get_outbox()
        assert outbox["totalItems"] == 1
        assert outbox["orderedItems"][0]["id"] == f"{ACTOR}/activities/1"


class _FollowingStorage(FileActivityPubStorage):
    def get_following(self, actor_id, limit=20, offset=0):
        return ["https://r.com/users/alice", "https://r.com/users/bob"]


class TestFollowing:
    def test_default_empty(self, tmp_path):
        h = _make_handler(FileActivityPubStorage(data_dir=tmp_path))
        coll = h.get_following_collection()
        assert coll["type"] == "OrderedCollection"
        assert coll["totalItems"] == 0

    def test_storage_items_rendered(self, tmp_path):
        h = _make_handler(_FollowingStorage(data_dir=tmp_path))
        coll = h.get_following_collection()
        assert coll["totalItems"] == 2
        assert coll["orderedItems"][1] == "https://r.com/users/bob"


class TestWebFingerUrlResources:
    def test_actor_url_resolves(self, tmp_path):
        h = _make_handler(FileActivityPubStorage(data_dir=tmp_path))
        assert h.is_valid_webfinger_resource(ACTOR)
        jrd = h.get_webfinger_response(ACTOR)
        assert jrd["subject"] == "acct:blog@blog.example.com"

    def test_acct_unchanged(self, tmp_path):
        h = _make_handler(FileActivityPubStorage(data_dir=tmp_path))
        assert h.is_valid_webfinger_resource("acct:blog@blog.example.com")
        assert h.is_valid_webfinger_resource("acct:@blog@blog.example.com")

    def test_foreign_url_rejected(self, tmp_path):
        h = _make_handler(FileActivityPubStorage(data_dir=tmp_path))
        assert not h.is_valid_webfinger_resource("https://other.example.com/ap/actor")
        assert not h.is_valid_webfinger_resource(f"{BASE}/ap/users/bob")

    def test_profile_url_when_configured(self, tmp_path):
        cfg = ActorConfig(
            base_url=BASE,
            url="https://blog.example.com/@blog",
        )
        h = _make_handler(FileActivityPubStorage(data_dir=tmp_path), cfg)
        assert h.is_valid_webfinger_resource("https://blog.example.com/@blog")

    def test_multi_actor_url_resolves(self, tmp_path):
        storage = FileActivityPubStorage(data_dir=tmp_path)
        instance = _make_handler(storage)
        bob = _make_handler(
            storage,
            {
                "base_url": BASE,
                "username": "bob",
                "actor_path": "/ap/users/bob",
            },
        )
        multi = MultiActorHandler(
            instance=instance,
            resolver=lambda u: bob if u.lower() == "bob" else None,
            actor_path_template="/ap/users/{username}",
        )

        jrd = multi.get_webfinger_response(f"{BASE}/ap/users/bob")
        assert jrd["subject"] == "acct:bob@blog.example.com"

        jrd = multi.get_webfinger_response(f"{BASE}/ap/actor")
        assert jrd["subject"] == "acct:blog@blog.example.com"

        assert multi.get_webfinger_response(f"{BASE}/ap/users/carol") is None
        assert (
            multi.get_webfinger_response("https://other.example.com/ap/users/bob")
            is None
        )
        assert multi.get_webfinger_response(f"{BASE}/ap/users/bob/outbox") is None
