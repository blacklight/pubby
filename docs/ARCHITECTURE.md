# Pubby — Architecture

## Overview

Pubby is a framework-agnostic Python ≥ 3.8 library that adds
[ActivityPub](https://www.w3.org/TR/activitypub/) federation to any Python web
application.  It handles inbox processing, outbox delivery, HTTP Signatures,
WebFinger/NodeInfo discovery, interaction storage, and HTML rendering — exposing
a single `ActivityPubHandler` façade that framework adapters (Flask, FastAPI,
Tornado) wire to HTTP routes.

```
┌──────────────────────────────────────────────────────────┐
│                      Application                         │
│             (Flask / FastAPI / Tornado)                  │
└────────────────────────┬─────────────────────────────────┘
                         │  bind_activitypub() / bind_mastodon_api()
                         ▼
┌──────────────────────────────────────────────────────────┐
│               Server Adapter Layer                       │
│   pubby.server.adapters.{flask,fastapi,tornado}          │
│   pubby.server.adapters.{flask,fastapi,tornado}_mastodon │
└────────────────────────┬─────────────────────────────────┘
                         │
                         ▼
┌───────────────────────────────────────────────────────────────┐
│              ActivityPubHandler (façade)                      │
│                   pubby.handlers                              │
│  ┌──────────────┐  ┌───────────────┐  ┌────────────────────┐  │
│  │InboxProcessor│  │OutboxProcessor│  │InteractionsRenderer│  │
│  └───────┬──────┘  └──────┬────────┘  └──────────┬─────────┘  │
│          │                │                      │            │
│          ▼                ▼                      ▼            │
│     ┌─────────┐     ┌──────────┐           ┌───────────┐      │
│     │ crypto  │     │  crypto  │           │  render   │      │
│     └─────────┘     └──────────┘           └───────────┘      │
└────────────────────────┬──────────────────────────────────────┘
                         │
                         ▼
┌──────────────────────────────────────────────────────────┐
│               Storage Layer (ABC)                        │
│             pubby.storage._base                          │
│  ┌─────────────────────┐  ┌───────────────────────────┐  │
│  │ DbActivityPubStorage│  │ FileActivityPubStorage    │  │
│  │  (SQLAlchemy)       │  │  (JSON files + RLock)     │  │
│  └─────────────────────┘  └───────────────────────────┘  │
└──────────────────────────────────────────────────────────┘
```

---

## Package Layout

```
src/python/pubby/
├── __init__.py              # Public re-exports, __version__
├── _model.py                # Core data model (dataclasses + enums)
├── _exceptions.py           # Exception hierarchy
├── _rate_limit.py           # In-memory sliding-window rate limiter
├── webfinger.py             # WebFinger client (resolve_actor_url, extract_mentions)
│
├── crypto/
│   ├── _keys.py             # RSA key generation, import/export (PEM)
│   └── _signatures.py       # HTTP Signatures sign & verify (draft-cavage)
│
├── handlers/
│   ├── _handler.py          # ActivityPubHandler — main façade
│   ├── _inbox.py            # InboxProcessor — incoming activity dispatch
│   ├── _outbox.py           # OutboxProcessor — build activities, fan-out delivery
│   ├── _discovery.py        # WebFinger & NodeInfo response builders
│   └── _client.py           # Default User-Agent helper
│
├── render/
│   └── _renderer.py         # Jinja2-based HTML renderer for interactions
│
├── templates/
│   ├── interaction.html     # Single-interaction template
│   └── interactions.html    # Interaction-list wrapper template
│
├── storage/
│   ├── _base.py             # ActivityPubStorage ABC
│   └── adapters/
│       ├── db/
│       │   ├── _model.py    # SQLAlchemy mixin models (DbFollower, …)
│       │   ├── _storage.py  # DbActivityPubStorage (SQLAlchemy impl)
│       │   └── _helpers.py  # init_db_storage() quick-start helper
│       └── file/
│           └── _storage.py  # FileActivityPubStorage (JSON files)
│
└── server/
    ├── adapters/
    │   ├── flask.py             # bind_activitypub() for Flask
    │   ├── flask_mastodon.py    # bind_mastodon_api() for Flask
    │   ├── fastapi.py           # bind_activitypub() for FastAPI
    │   ├── fastapi_mastodon.py  # bind_mastodon_api() for FastAPI
    │   ├── tornado.py           # bind_activitypub() for Tornado
    │   └── tornado_mastodon.py  # bind_mastodon_api() for Tornado
    └── mastodon/
        ├── _mappers.py          # AP → Mastodon entity converters
        └── _routes.py           # MastodonAPI — framework-agnostic handlers
```

---

## Module Details

### 1. Data Model — `pubby._model`

All core types are plain `@dataclass` classes with `to_dict()` (→ JSON-LD)
and `build()` (← JSON-LD) round-trip methods.

| Class | Purpose |
|-------|---------|
| `ActorConfig` | Typed configuration for an ActivityPub actor (base URL, username, bio, type, attachments, …). Accepts a plain `dict` via `from_dict()` for backwards compatibility. |
| `Actor` | Full ActivityPub Actor document with public key, endpoints, and `to_dict()` serialization. |
| `Object` | ActivityPub Object (Note, Article, Image, …). Supports `mediaType`, `contentMap`, `quoteControl`, `interactionPolicy`. |
| `Activity` | ActivityPub Activity wrapper (Create, Follow, Like, …). |
| `Interaction` | Stored interaction from a remote actor — maps AP activities to a displayable format (analogous to a Webmention). |
| `Follower` | Stored follower record (actor ID, inbox, shared inbox, cached actor data). |

**Enums:**

| Enum | Values |
|------|--------|
| `ActivityType` | `Create`, `Update`, `Delete`, `Follow`, `Undo`, `Accept`, `Reject`, `Like`, `Announce`, `QuoteRequest` |
| `ObjectType` | `Note`, `Article`, `Image`, `Video`, `Audio`, `Page`, `Event`, `Tombstone` |
| `InteractionType` | `reply`, `like`, `boost`, `mention`, `quote` |
| `InteractionStatus` | `pending`, `confirmed`, `deleted` |
| `DeliveryStatus` | `pending`, `delivered`, `failed` |

The constant `AP_CONTEXT` holds the standard JSON-LD `@context` array
(ActivityStreams, Security, FEP-0449, GoToSocial interaction-policy terms).

### 2. Exceptions — `pubby._exceptions`

A simple hierarchy rooted at `ActivityPubError`:

- **`SignatureVerificationError`** — HTTP Signature check failed.
- **`DeliveryError`** — outbound delivery to a remote inbox failed.
- **`RateLimitError`** — per-IP rate limit exceeded.

### 3. Rate Limiter — `pubby._rate_limit`

`RateLimiter` is a thread-safe, in-memory, per-key sliding-window rate
limiter.  Server adapters optionally pass it to the inbox endpoint; the
`check(key)` method raises `RateLimitError` when the window is exceeded.

### 4. Crypto — `pubby.crypto`

Two internal modules, re-exported through `pubby.crypto.__init__`:

| Module | Exposed API |
|--------|-------------|
| `_keys.py` | `generate_rsa_keypair()`, `load_private_key()`, `load_public_key()`, `export_private_key_pem()`, `export_public_key_pem()` |
| `_signatures.py` | `sign_request()`, `verify_request()` |

**Key management** uses the `cryptography` library directly (RSA 2048-bit,
PKCS#8 PEM).  No `httpsig` dependency.

**HTTP Signatures** follow
[draft-cavage-http-signatures-12](https://tools.ietf.org/html/draft-cavage-http-signatures-12)
with RSA-SHA256.  `sign_request()` returns a dict of headers (`Date`,
`Digest`, `Host`, `Signature`) ready to merge into an outgoing request.
`verify_request()` reconstructs the signing string, verifies the RSA
signature, and optionally checks the `Digest` header.

### 5. Handlers — `pubby.handlers`

#### 5.1 `ActivityPubHandler` (façade)

The single entry point consumers interact with.  Accepts an
`ActivityPubStorage`, an `ActorConfig` (or dict), and a private key.
Internally it composes:

- **`InboxProcessor`** — incoming activity dispatch.
- **`OutboxProcessor`** — activity construction & fan-out delivery.
- **`InteractionsRenderer`** — Jinja2-based HTML rendering.

Public methods:

| Method | Description |
|--------|-------------|
| `process_inbox_activity()` | Delegate an incoming activity to `InboxProcessor`. |
| `publish_object(obj, activity_type)` | Build a Create / Update / Delete activity and fan-out via `OutboxProcessor`. |
| `publish_actor_update()` | Push the current actor profile to all followers. |
| `get_actor_document()` | Build the actor's JSON-LD representation. |
| `get_outbox()` | Return the outbox `OrderedCollection`. |
| `get_followers_collection()` | Return the followers `OrderedCollection`. |
| `get_following_collection()` | Return the (empty) following `OrderedCollection`. |
| `get_webfinger_response(resource)` | Build the WebFinger JRD response. |
| `get_nodeinfo_discovery()` | Build the `.well-known/nodeinfo` document. |
| `get_nodeinfo_document()` | Build the NodeInfo 2.1 document. |
| `get_quote_authorization(id)` | Retrieve a stored `QuoteAuthorization`. |
| `render_interaction(interaction)` | Render a single interaction as HTML. |
| `render_interactions(interactions)` | Render a list of interactions as HTML. |

#### 5.2 `InboxProcessor`

Dispatches incoming activities by type via a handler map:

```
ActivityType → method
─────────────────────
Follow       → _handle_follow      (store follower, send Accept)
Undo         → _handle_undo        (unfollow or undo like/boost)
Create       → _handle_create      (reply, quote, or mention)
Like         → _handle_like        (store like interaction)
Announce     → _handle_announce    (store boost interaction)
Delete       → _handle_delete      (soft-delete interaction)
Update       → _handle_update      (update stored reply)
QuoteRequest → _handle_quote_request (FEP-044f: auto-approve quotes)
```

Before dispatching, `verify_signature()` checks the HTTP Signature header
by fetching the sender's public key (with actor caching).

The `on_interaction_received` callback is invoked after every new
interaction is stored, enabling application-level notifications.

#### 5.3 `OutboxProcessor`

Responsible for:

1. **Building activities** — `build_create_activity()`,
   `build_update_activity()`, `build_delete_activity()`.
2. **Publishing** — `publish(activity)` stores the activity, collects
   follower inboxes (preferring shared inboxes for deduplication), then
   fans out delivery concurrently via `ThreadPoolExecutor`.
3. **Retry** — `_deliver_with_retry()` uses exponential backoff
   (`retry_base_delay × 2^attempt`); 5xx responses and connection errors
   are retried, 4xx errors are not.

#### 5.4 `_discovery`

Pure functions that build WebFinger JRD (RFC 7033) and NodeInfo 2.1
response dicts.

#### 5.5 `_client`

`get_default_user_agent(actor_id)` returns the default `User-Agent` string
(`pubby/{version} (+{actor_id})`).

### 6. WebFinger Client — `pubby.webfinger`

- **`resolve_actor_url(username, domain)`** — performs a WebFinger lookup
  and returns the `self` link, falling back to
  `https://{domain}/@{username}`.
- **`extract_mentions(text)`** — finds all `@user@domain` patterns,
  resolves each via WebFinger, returns deduplicated `Mention` objects.
- **`Mention`** dataclass — carries `username`, `domain`, `actor_url`,
  plus helpers `acct` (property) and `to_tag()` (→ AP Mention tag dict).

### 7. Storage — `pubby.storage`

#### 7.1 Abstract Base — `ActivityPubStorage`

Defines the contract every storage backend must fulfill:

| Group | Methods |
|-------|---------|
| **Followers** | `store_follower()`, `remove_follower()`, `get_followers()` |
| **Interactions** | `store_interaction()`, `delete_interaction()`, `delete_interaction_by_object_id()`, `get_interactions()` |
| **Activities** | `store_activity()`, `get_activities()` |
| **Actor cache** | `cache_remote_actor()`, `get_cached_actor()` |
| **Quote authorizations** | `store_quote_authorization()`, `get_quote_authorization()` |

`delete_interaction_by_object_id()` and the quote-authorization methods
have default (no-op) implementations so existing custom backends don't
break when Pubby adds new features.

#### 7.2 SQLAlchemy Adapter — `pubby.storage.adapters.db`

- **Mixin models** (`_model.py`): `DbFollower`, `DbInteraction`,
  `DbActivity`, `DbActorCache` — framework-neutral SQLAlchemy column
  definitions.  Users inherit these into their own declarative Base to
  choose table names.
- **`DbActivityPubStorage`** (`_storage.py`): full `ActivityPubStorage`
  implementation using a `session_factory` callable.  Upsert logic uses
  insert-then-update-on-`IntegrityError`.
- **`init_db_storage(engine)`** (`_helpers.py`): convenience function that
  creates a self-contained declarative Base, mapped models with default
  table names (`ap_followers`, `ap_interactions`, `ap_activities`,
  `ap_actor_cache`), calls `create_all()`, and returns a ready-to-use
  `DbActivityPubStorage`.

#### 7.3 File Adapter — `pubby.storage.adapters.file`

`FileActivityPubStorage` stores entities as individual JSON files in a
directory tree:

```
data_dir/
├── followers/{hash}.json
├── interactions/{target_hash}/{type}-{actor_hash}.json
├── activities/{hash}.json
├── cache/actors/{hash}.json
└── quote_authorizations/{hash}.json
```

Thread-safe via per-path `RLock`.  Writes use atomic rename
(`.tmp` → final).  Suitable for static-site generators or low-traffic
setups that don't need a database.

### 8. Render — `pubby.render`

`InteractionsRenderer` uses Jinja2 (`PackageLoader` on the `templates/`
directory) to produce safe HTML `Markup` for interactions.

- **`render_interaction()`** — renders a single interaction with the
  `interaction.html` template.
- **`render_interactions()`** — sorts by date, counts by type (likes,
  boosts, replies, quotes, mentions), renders the collection with the
  `interactions.html` wrapper.

`TemplateUtils` provides Jinja2 helper functions: `format_date`,
`format_datetime`, `hostname`, `safe_url`, `sanitize_html`, `actor_fqn`.

HTML sanitization (`_sanitize_html`) strips disallowed tags and attributes
via regex, permitting a safe subset (links, basic formatting,
blockquotes, lists) and only `http`/`https` href schemes.

### 9. Server Adapters — `pubby.server.adapters`

Each framework gets two modules:

| Module | Function | Routes |
|--------|----------|--------|
| `flask.py` | `bind_activitypub(app, handler)` | Core AP endpoints |
| `flask_mastodon.py` | `bind_mastodon_api(app, handler)` | Mastodon REST API |
| `fastapi.py` | `bind_activitypub(app, handler)` | Core AP endpoints |
| `fastapi_mastodon.py` | `bind_mastodon_api(app, handler)` | Mastodon REST API |
| `tornado.py` | `bind_activitypub(app, handler)` | Core AP endpoints |
| `tornado_mastodon.py` | `bind_mastodon_api(app, handler)` | Mastodon REST API |

All `bind_activitypub()` functions register the same set of routes:

| Method | Path | Handler method |
|--------|------|----------------|
| `GET` | `/.well-known/webfinger` | `get_webfinger_response()` |
| `GET` | `/.well-known/nodeinfo` | `get_nodeinfo_discovery()` |
| `GET` | `/nodeinfo/2.1` | `get_nodeinfo_document()` |
| `GET` | `{prefix}/actor` | `get_actor_document()` |
| `POST` | `{prefix}/inbox` | `process_inbox_activity()` |
| `GET` | `{prefix}/outbox` | `get_outbox()` |
| `GET` | `{prefix}/followers` | `get_followers_collection()` |
| `GET` | `{prefix}/following` | `get_following_collection()` |
| `GET` | `{actor_path}/quote_authorizations/{id}` | `get_quote_authorization()` |

The `prefix` (default `/ap`) is configurable.  The inbox route
optionally applies the `RateLimiter`.

### 10. Mastodon-Compatible API — `pubby.server.mastodon`

A read-only subset of the
[Mastodon REST API](https://docs.joinmastodon.org/methods/) so that
Mastodon clients and crawlers can discover the instance.

#### 10.1 Mappers (`_mappers.py`)

Pure functions that convert Pubby/AP types to Mastodon JSON shapes:

| Function | Converts |
|----------|----------|
| `actor_to_account()` | Local actor → Mastodon Account |
| `activity_to_status()` | Outbox activity → Mastodon Status |
| `follower_to_account()` | Follower → minimal Mastodon Account |
| `tag_to_mastodon_tag()` | Hashtag → Mastodon Tag |
| `stable_id()` / `id_to_url()` | Deterministic, reversible URL-safe base64 IDs |

#### 10.2 Route Handlers (`_routes.py`)

`MastodonAPI` is a stateless class whose methods return
`(body, status_code)` tuples.  Framework adapters call these methods and
wrap the result in a framework-specific HTTP response.

| Method | Mastodon endpoint |
|--------|-------------------|
| `instance_v1()` | `GET /api/v1/instance` |
| `instance_v2()` | `GET /api/v2/instance` |
| `instance_peers()` | `GET /api/v1/instance/peers` |
| `accounts_lookup(acct)` | `GET /api/v1/accounts/lookup` |
| `accounts_get(id)` | `GET /api/v1/accounts/:id` |
| `accounts_statuses(id)` | `GET /api/v1/accounts/:id/statuses` |
| `accounts_followers(id)` | `GET /api/v1/accounts/:id/followers` |
| `statuses_get(id)` | `GET /api/v1/statuses/:id` |

The framework-specific `bind_mastodon_api()` adapters also register
NodeInfo 2.0 aliases (`/nodeinfo/2.0`, `/nodeinfo/2.0.json`,
`/nodeinfo/2.1.json`).

---

## Dependency Graph (internal)

```
pubby.__init__
  ├── pubby._model            (dataclasses, enums, AP_CONTEXT)
  ├── pubby._exceptions       (exception hierarchy)
  ├── pubby._rate_limit       (RateLimiter)
  ├── pubby.webfinger          (Mention, resolve_actor_url, extract_mentions)
  ├── pubby.crypto             (_keys, _signatures)
  ├── pubby.handlers           (ActivityPubHandler)
  │     ├── _handler.py
  │     │     ├── _inbox.py    → crypto, storage, _model, _exceptions
  │     │     ├── _outbox.py   → crypto, storage, _model
  │     │     ├── _discovery.py (pure functions)
  │     │     └── _client.py   (User-Agent helper)
  │     └── render/            → _model, jinja2, markupsafe
  ├── pubby.storage            (ABC + adapters)
  │     ├── _base.py           → _model
  │     ├── adapters/db/       → _model, _base, sqlalchemy
  │     └── adapters/file/     → _model, _base
  └── pubby.server
        ├── adapters/{flask,fastapi,tornado}.py
        │     → handlers, _exceptions, _rate_limit
        ├── adapters/{flask,fastapi,tornado}_mastodon.py
        │     → handlers, mastodon._routes
        └── mastodon/
              ├── _mappers.py  → handlers, _model
              └── _routes.py   → handlers, _mappers
```

---

## External Dependencies

| Package | Usage |
|---------|-------|
| `cryptography` (≥ 41.0) | RSA key generation, HTTP Signature signing/verification |
| `jinja2` | Interaction HTML rendering |
| `requests` | Outgoing HTTP (actor fetch, delivery, WebFinger lookups) |
| `markupsafe` | Safe HTML markup from Jinja2 templates |
| `sqlalchemy` (optional `[db]`) | Database storage adapter |
| `flask` (optional `[flask]`) | Flask server adapter |
| `fastapi` / `uvicorn` (optional `[fastapi]`) | FastAPI server adapter |
| `tornado` (optional `[tornado]`) | Tornado server adapter |

---

## Key Design Decisions

1. **Façade pattern** — `ActivityPubHandler` is the only class consumers
   instantiate.  Inbox, outbox, discovery, and rendering are internal
   sub-components composed behind it.

2. **Framework-agnostic core** — all business logic lives in `handlers/`,
   `storage/`, `crypto/`, and `render/`.  Framework-specific code is
   confined to thin adapter modules under `server/adapters/`.

3. **Pluggable storage** — the `ActivityPubStorage` ABC lets users swap
   backends without touching handler code.  Two batteries-included
   adapters (SQLAlchemy, JSON files) cover common use cases.

4. **No `httpsig` dependency** — HTTP Signatures are implemented directly
   on top of `cryptography`, keeping the dependency tree small and the
   signing logic transparent.

5. **Concurrent delivery** — `OutboxProcessor` uses
   `ThreadPoolExecutor` for fan-out, with shared-inbox deduplication and
   exponential-backoff retry.

6. **Mastodon compatibility layer** — a read-only Mastodon REST API
   surface is separated into framework-agnostic mappers + route handlers,
   with a thin per-framework adapter — the same pattern as the core AP
   routes.

7. **Soft deletes** — interactions are marked `DELETED` rather than
   physically removed, preserving an audit trail.

8. **FEP-044f quote authorization** — incoming `QuoteRequest` activities
   are optionally auto-approved, with the `QuoteAuthorization` object
   stored and served via a dedicated endpoint.
