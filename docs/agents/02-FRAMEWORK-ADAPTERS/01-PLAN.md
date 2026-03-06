# 02 — FastAPI and Tornado Adapters

## Goal

Add `fastapi.py` and `tornado.py` server adapters alongside the existing `flask.py`, following the exact same pattern: a single `bind_activitypub(app, handler, prefix, rate_limiter)` function that registers all routes.

## Routes (same for all adapters)

| Method | Path | Handler method |
|--------|------|----------------|
| GET | `/.well-known/webfinger` | `get_webfinger_response(resource)` |
| GET | `/.well-known/nodeinfo` | `get_nodeinfo_discovery()` |
| GET | `/nodeinfo/2.1` | `get_nodeinfo_document()` |
| GET | `{prefix}/actor` | `get_actor_document()` |
| POST | `{prefix}/inbox` | `process_inbox_activity(...)` |
| GET | `{prefix}/outbox` | `get_outbox(limit, offset)` |
| GET | `{prefix}/followers` | `get_followers_collection()` |
| GET | `{prefix}/following` | `get_following_collection()` |

## Changes

### `src/python/pubby/server/adapters/fastapi.py`

- `bind_activitypub(app: FastAPI, handler, prefix="/ap", rate_limiter=None)`
- Use `APIRouter` with the prefix, then `app.include_router(router)`.
- Well-known routes (`/.well-known/*`, `/nodeinfo/2.1`) go directly on the app since they're outside the prefix.
- Use `Request` object for headers/body access on the inbox endpoint.
- Return `JSONResponse` with appropriate content types (`application/activity+json`, `application/jrd+json`).
- Same content negotiation, rate limiting, error handling as Flask adapter.

### `src/python/pubby/server/adapters/tornado.py`

- `bind_activitypub(app: tornado.web.Application, handler, prefix="/ap", rate_limiter=None)` — returns a list of `(pattern, handler_class, kwargs)` tuples **and** adds them to the app.
- One `RequestHandler` subclass per endpoint (or a base class + subclasses).
- `self.request.body` for raw body, `self.request.headers` for headers.
- `self.set_header()` for content type, `self.write()` for JSON responses.
- Same logic as Flask/FastAPI adapters, just Tornado idioms.

### `pyproject.toml`

- Verify `fastapi` and `tornado` are already in optional deps (they should be from initial setup).

### Tests

- `tests/test_fastapi_adapter.py` — use `httpx.AsyncClient` + `ASGITransport` (FastAPI's test pattern). Mirror all tests from `test_flask_adapter.py`.
- `tests/test_tornado_adapter.py` — use `tornado.testing.AsyncHTTPTestCase`. Mirror all tests.
- Both test files should cover: webfinger, nodeinfo, actor, inbox (valid + invalid), outbox, followers, following, rate limiting, content negotiation.

### `src/python/pubby/server/adapters/__init__.py`

- No changes needed — adapters are imported explicitly by the user.

## Design Notes

- All three adapters are thin routing layers. Zero business logic — everything delegates to `ActivityPubHandler`.
- The `handler` object is synchronous. FastAPI routes will be regular `def` functions (not `async def`) so FastAPI runs them in a threadpool automatically. This is fine — the handler does I/O (storage, HTTP) but isn't async-native.
- For Tornado, the handlers can be synchronous too since the actual inbox processing and delivery already happen in the `ThreadPoolExecutor`.
