"""
Simple adapter import and basic structure tests.
"""


def test_fastapi_adapter_import():
    """Test that FastAPI adapter can be imported."""
    from pubby.server.adapters.fastapi import bind_activitypub

    assert callable(bind_activitypub)


def test_tornado_adapter_import():
    """Test that Tornado adapter can be imported."""
    from pubby.server.adapters.tornado import bind_activitypub

    assert callable(bind_activitypub)


def test_fastapi_adapter_structure():
    """Test that FastAPI adapter has correct structure."""
    from fastapi import FastAPI
    from pubby.server.adapters.fastapi import bind_activitypub
    from pubby.handlers import ActivityPubHandler
    from pubby.storage.adapters.db import init_db_storage
    from pubby.crypto._keys import generate_rsa_keypair

    # Create handler
    storage = init_db_storage("sqlite:///:memory:")
    private_key, _ = generate_rsa_keypair()
    handler = ActivityPubHandler(
        storage=storage,
        actor_config={
            "base_url": "https://blog.example.com",
            "username": "blog",
            "name": "Test Blog",
            "summary": "A test blog",
        },
        private_key=private_key,
    )

    # Create app and bind routes
    app = FastAPI()
    bind_activitypub(app, handler)

    # Check that routes were added
    route_paths = [route.path for route in app.routes]

    expected_paths = [
        "/.well-known/webfinger",
        "/.well-known/nodeinfo",
        "/nodeinfo/2.1",
        "/ap/actor",
        "/ap/inbox",
        "/ap/outbox",
        "/ap/followers",
        "/ap/following",
    ]

    for path in expected_paths:
        assert any(
            path in route_path for route_path in route_paths
        ), f"Missing route: {path}"


def test_tornado_adapter_structure():
    """Test that Tornado adapter has correct structure."""
    import tornado.web
    from pubby.server.adapters.tornado import bind_activitypub
    from pubby.handlers import ActivityPubHandler
    from pubby.storage.adapters.db import init_db_storage
    from pubby.crypto._keys import generate_rsa_keypair

    # Create handler
    storage = init_db_storage("sqlite:///:memory:")
    private_key, _ = generate_rsa_keypair()
    handler = ActivityPubHandler(
        storage=storage,
        actor_config={
            "base_url": "https://blog.example.com",
            "username": "blog",
            "name": "Test Blog",
            "summary": "A test blog",
        },
        private_key=private_key,
    )

    # Create app and bind routes
    app = tornado.web.Application()
    url_patterns = bind_activitypub(app, handler)

    # Check that correct patterns were returned
    expected_patterns = [
        r"/.well-known/webfinger",
        r"/.well-known/nodeinfo",
        r"/nodeinfo/2.1",
        r"/ap/actor",
        r"/ap/inbox",
        r"/ap/outbox",
        r"/ap/followers",
        r"/ap/following",
    ]

    pattern_strings = [pattern[0] for pattern in url_patterns]

    for expected in expected_patterns:
        assert any(
            expected == pattern for pattern in pattern_strings
        ), f"Missing pattern: {expected}"
