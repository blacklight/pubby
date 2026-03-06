# 04 — Implementation Plan

## 1. Component Breakdown

```
mypub/
├── __init__.py              # Public API re-exports
├── _model.py                # Dataclasses + enums: Actor, Activity, Object, Interaction, Follower
├── _exceptions.py           # ActivityPubError, SignatureVerificationError, DeliveryError, RateLimitError
├── _rate_limit.py           # In-memory per-IP sliding window rate limiter
├── crypto/
│   ├── _keys.py             # RSA key pair generation/loading/export (cryptography lib)
│   └── _signatures.py       # HTTP Signature sign/verify (draft-cavage-12)
├── handlers/
│   ├── _handler.py          # Main ActivityPubHandler (façade over inbox/outbox/discovery)
│   ├── _inbox.py            # InboxProcessor — dispatch by activity type
│   ├── _outbox.py           # OutboxProcessor — build activities, fan-out delivery
│   └── _discovery.py        # WebFinger + NodeInfo response builders
├── render/
│   └── _renderer.py         # Jinja2 template renderer for interactions
├── templates/
│   ├── interaction.html     # Single interaction template
│   └── interactions.html    # Collection template (with CSS)
├── storage/
│   ├── _base.py             # Abstract ActivityPubStorage interface
│   └── adapters/
│       ├── db/              # SQLAlchemy adapter (followers, interactions, activities, actor_cache)
│       └── file/            # File-based JSON adapter (thread-safe with RLock)
└── server/
    └── adapters/
        └── flask.py         # bind_activitypub(app, handler) — registers all routes
```

### Dependency Graph

```
_model.py, _exceptions.py          ← no internal deps
    ↓
crypto/_keys.py                    ← depends on cryptography
    ↓
crypto/_signatures.py              ← depends on crypto/_keys.py, _exceptions.py
    ↓
storage/_base.py                   ← depends on _model.py
    ↓
storage/adapters/*                 ← depends on storage/_base.py, _model.py
    ↓
handlers/_discovery.py             ← depends on _model.py (stateless builders)
handlers/_inbox.py                 ← depends on crypto, storage, _model.py
handlers/_outbox.py                ← depends on crypto, storage, _model.py
    ↓
handlers/_handler.py               ← depends on all handlers, render, storage, crypto
    ↓
server/adapters/flask.py           ← depends on handlers, _rate_limit.py, _exceptions.py
```

## 2. Architecture Decisions

### Why no `httpsig` dependency

The HTTP Signatures spec (draft-cavage-12) is small enough (~50 lines of signing logic) that implementing it directly avoids:
- A transitive dependency that may lag behind or become unmaintained
- Difficulty debugging opaque signature failures
- The `cryptography` library alone covers all RSA operations needed

The implementation covers: building signing strings, computing SHA-256 digests, RSA-SHA256 signing/verification, and Signature header parsing.

### Why the crypto module is self-contained

All cryptographic operations (key generation, loading, export, HTTP signing, verification) are isolated in `crypto/`. This:
- Makes the crypto surface auditable in one place
- Allows reuse without pulling in handler or storage logic
- Keeps the `cryptography` dependency cleanly scoped

### Why both file and DB storage

- **File-based**: Zero additional dependencies, inspectable, version-controllable. Ideal for single-user blogs (Madblog's use case). Thread-safe via per-path RLock.
- **DB-based**: Scales to thousands of followers, supports complex queries, transactions. Available when `sqlalchemy` is installed. Follows the exact same `init_db_storage()` helper pattern as the webmentions library.

## 3. Storage Interface Contract

`ActivityPubStorage` defines 10 abstract methods across 4 resource types:

| Resource | Operations |
|----------|-----------|
| **Followers** | `store_follower()`, `remove_follower()`, `get_followers()` |
| **Interactions** | `store_interaction()`, `delete_interaction()`, `get_interactions()` |
| **Activities** | `store_activity()`, `get_activities()` |
| **Actor Cache** | `cache_remote_actor()`, `get_cached_actor()` |

Interactions are keyed on `(source_actor_id, target_resource, interaction_type)`. Delete is a soft-delete (marks status as `DELETED`). Actor cache entries have a configurable TTL (default 24h).

## 4. Configuration Model

The handler accepts a flat `actor_config` dict:

```python
{
    "base_url": "https://blog.example.com",
    "username": "blog",
    "name": "My Blog",
    "summary": "A blog about things",
    "icon_url": "https://blog.example.com/icon.png",
    "type": "Person",                        # or "Application"
    "manually_approves_followers": False,
    "actor_path": "/ap/actor",               # customizable
}
```

**Single-user for now.** The handler manages one actor. To scale to multi-user:
- Create multiple `ActivityPubHandler` instances with different configs
- Use the server adapter to route to the correct handler based on the actor path
- No architectural changes needed — just instantiation patterns

## 5. HTTP Signature Implementation

### Outgoing (sign_request)

1. Parse the target URL to extract host and path
2. Generate `Date` and `Host` headers if not provided
3. Compute `Digest: SHA-256=<base64>` for request body
4. Build the signing string: `(request-target): post /path\nhost: ...\ndate: ...\ndigest: ...`
5. RSA-SHA256 sign with PKCS1v15 padding
6. Encode as `Signature` header: `keyId="...",algorithm="rsa-sha256",headers="...",signature="..."`

### Incoming (verify_request)

1. Parse the `Signature` header to extract `keyId`, `headers`, `signature`
2. Derive actor URL from `keyId` (strip fragment)
3. Fetch actor document → extract `publicKey.publicKeyPem`
4. Rebuild signing string from declared headers
5. Verify `Digest` header matches SHA-256 of body
6. RSA-SHA256 verify the signature

## 6. Content Negotiation Strategy

The actor URL (`/ap/actor`) checks the `Accept` header:
- If it contains `application/activity+json` or `application/ld+json` → return the JSON-LD actor document with `Content-Type: application/activity+json`
- Otherwise → still returns JSON-LD (the consuming blog app can override this to return HTML for browser clients)

All other AP endpoints always return `application/activity+json`.

## 7. Delivery Fan-out and Retry Strategy

When publishing an activity:

1. Collect all followers from storage
2. Deduplicate inboxes: prefer `endpoints.sharedInbox` over individual `inbox` URLs
3. For each unique inbox, POST the signed activity
4. On 5xx or connection error: retry with exponential backoff
   - Default: 3 attempts, delays of 10s, 20s (configurable)
5. On 4xx: no retry (permanent failure)
6. Log all delivery outcomes

Delivery is synchronous (blocking). For async/background delivery, the consuming app can call `outbox.publish()` in a background thread/task.

## 8. Rate Limiting Approach

Simple in-memory sliding window per IP:
- Default: 60 requests per 60 seconds
- Configurable via `RateLimiter(max_requests=N, window_seconds=T)`
- Injected into Flask adapter via `bind_activitypub(..., rate_limiter=limiter)`
- Raises `RateLimitError` → returns HTTP 429

Not persisted across restarts. Sufficient for single-server blog deployments.

## 9. Testing Strategy

11 test modules covering every component:

| Module | What it tests | Strategy |
|--------|--------------|----------|
| `test_model.py` | Dataclass build/serialize/deserialize, enum parsing | Pure unit tests |
| `test_crypto.py` | Key generation, export, load, sign/verify raw | Uses `cryptography` directly |
| `test_signatures.py` | HTTP signature sign/verify round-trip, tampering | Tests internal functions + integration |
| `test_inbox.py` | All activity types (Follow, Undo, Create, Like, etc.) | Mock storage + mock HTTP |
| `test_outbox.py` | Activity building, fan-out, retry logic | Mock storage + mock HTTP |
| `test_discovery.py` | WebFinger + NodeInfo response format | Pure unit tests |
| `test_flask_adapter.py` | All Flask routes | Flask test client, in-memory DB |
| `test_file_storage.py` | File-based CRUD | Temp directory |
| `test_db_storage.py` | SQLAlchemy CRUD | In-memory SQLite |
| `test_rate_limit.py` | Allows/blocks correctly, window expiry | Time-based with short windows |
| `test_renderer.py` | HTML output for reply/like/boost | Template rendering |

All tests run with `pytest` and use no network I/O (HTTP calls are mocked).
