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
├── client.py                # One-off actor/inbox resolution
├── _model.py                # Core data model (dataclasses + enums)
├── _exceptions.py           # Exception hierarchy
├── _rate_limit.py           # In-memory sliding-window rate limiter
├── audience.py              # Audience/Mention parsing helpers
├── quotes.py                # Quote field/policy helpers (FEP-0449, FEP-044f)
├── attribution.py           # Inbound object attribution validation
├── moderation.py            # Instance domain allow/block list helpers
├── webfinger.py             # WebFinger client (resolve_actor_url, extract_mentions)
│
├── crypto/
│   ├── _keys.py             # RSA key generation, import/export (PEM)
│   └── _signatures.py       # HTTP Signatures sign & verify (draft-cavage)
│
├── handlers/
│   ├── _handler.py          # ActivityPubHandler — main façade
│   ├── _inbox.py            # InboxProcessor — incoming activity dispatch
│   ├── _outbox.py           # OutboxProcessor — build activities, fan-out delivery;
│   │                        #   module helpers collect_inboxes() / deliver_activity()
│   ├── _discovery.py        # WebFinger & NodeInfo response builders
│   └── _client.py           # Default User-Agent helper
│
├── content/
│   └── __init__.py          # Plain-text → ActivityPub HTML / Hashtag tags
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
│       │   └── _helpers.py  # init_db_storage() + to_sync_url() helpers
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
| `Object` | ActivityPub Object (Note, Article, Image, …). Supports `mediaType`, `contentMap`, `quoteControl`, `interactionPolicy`, and `duration`. `url` accepts `str` or a list of `Link` dicts; `attributedTo` accepts `str` or `list[str]` (federated media shapes such as `Audio`). |
| `Activity` | ActivityPub Activity wrapper (Create, Follow, Like, …). |
| `Interaction` | Stored interaction from a remote actor — maps AP activities to a displayable format (analogous to a Webmention). |
| `Follower` | Stored follower record (actor ID, inbox, shared inbox, cached actor data, plus `target_actor_id` identifying the local actor or followable object being followed). |

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
- **`AttributionMismatch`** — an inbound object's `attributedTo` or `id`
  authority does not match the delivering actor (raised by
  `pubby.attribution.validate`; caught and dropped by `InboxProcessor`
  when `strict_attribution` is enabled).

### 3. Rate Limiter — `pubby._rate_limit`

`RateLimiter` is a thread-safe, in-memory, per-key sliding-window rate
limiter.  Server adapters optionally pass it to the inbox endpoint; the
`check(key)` method raises `RateLimitError` when the window is exceeded.

### 4. Instance Moderation — `pubby.moderation`

Stdlib-only helpers for per-instance allow/block policy.  The application
owns the lists (`allowed_instances` / `blocked_instances` on
`ActivityPubHandler`, forwarded to both processors); Pubby normalizes and
matches domains and enforces the policy at two seams:

- **Inbound** — `InboxProcessor.process()` drops activities whose `actor`
  domain is blocked (or not in a non-empty allow-list) *before* signature
  verification, so rejected instances never trigger an actor key fetch.
- **Outbound** — `OutboxProcessor` skips inboxes on blocked/non-allowed
  domains during delivery fan-out, and never fetches recipient actor
  documents from those domains.

| Function | Purpose |
|----------|---------|
| `normalize_domain(value)` | Strip scheme/path/port, lower-case; `""` for empty input |
| `extract_domain(url_or_actor)` | Hostname of an actor URL, inbox URL, or bare domain |
| `is_domain_blocked(domain, allowed=None, blocked=None)` | `True` when blocked, or a non-empty allow-list excludes the domain (blocked wins over allowed) |

### 5. Audience Helpers — `pubby.audience`

Pure, stdlib-only parsers for ActivityPub addressing, shared by
`InboxProcessor` and available to applications:

| Function | Purpose |
|----------|---------|
| `addressees(obj_or_activity)` | Union of string values in `to`, `cc`, `bto`, `bcc` of the supplied mapping only (unordered `set`) |
| `is_public(obj_or_activity)` | `True` when any of `PUBLIC_URIS` appears in the mapping's audience fields |
| `mentioned_actors(obj_data)` | Actor URLs from `Mention` tags, first-seen order, deduplicated |

`PUBLIC_URIS` recognizes the canonical `as:Public` URI plus the `Public`
and `as:Public` shorthand aliases. The helpers inspect only the mapping
they are given — they never descend into an activity's embedded
`object` — and tolerate missing or malformed fields without raising.

### 6. Attribution Validation — `pubby.attribution`

`validate(actor, obj)` sanity-checks that an inbound object can plausibly
belong to the actor that signed the delivery: a non-empty `attributedTo`
must name the actor (string, list, or `{"id": ...}` forms), and a present
object `id` must share the actor's domain (via `extract_domain`).
Objects lacking comparable fields pass. On failure it raises
`AttributionMismatch`.

`InboxProcessor` applies this to `Create`/`Update` objects when
`strict_attribution=True` (default `False`): mismatched objects are
logged and dropped before any callback or storage mutation. Kept opt-in
because relays, proxies, and account migration can legitimately separate
actor and object hosts.

### 7. Crypto — `pubby.crypto`

Two internal modules, re-exported through `pubby.crypto.__init__`:

| Module | Exposed API |
|--------|-------------|
| `_keys.py` | `generate_rsa_keypair()`, `load_private_key()`, `load_public_key()`, `export_private_key_pem()`, `export_public_key_pem()`, `ensure_private_key_file()` |
| `_signatures.py` | `sign_request()`, `verify_request()` |

**Key management** uses the `cryptography` library directly (RSA 2048-bit,
PKCS#8 PEM).  No `httpsig` dependency.

**HTTP Signatures** follow
[draft-cavage-http-signatures-12](https://tools.ietf.org/html/draft-cavage-http-signatures-12)
with RSA-SHA256.  `sign_request()` returns a dict of headers (`Date`,
`Digest`, `Host`, `Signature`) ready to merge into an outgoing request.
`verify_request()` reconstructs the signing string, verifies the RSA
signature, and optionally checks the `Digest` header.

### 8. Handlers — `pubby.handlers`

#### 8.1 `ActivityPubHandler` (façade)

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
| `publish_activity(activity)` | Publish a pre-built activity dict as-is (Like, Undo, Announce, Follow, etc.). |
| `publish_actor_update(document=None)` | Push the actor profile to all followers; accepts an optional prebuilt actor document. |
| `get_actor_document()` | Build the actor's JSON-LD representation. |
| `get_outbox()` | Return the outbox `OrderedCollection`. |
| `get_followers_collection(actor_id=None)` | Return the followers `OrderedCollection`, optionally filtered by actor. |
| `get_following_collection()` | Return the (empty) following `OrderedCollection`. |
| `get_webfinger_response(resource)` | Build the WebFinger JRD response. |
| `get_nodeinfo_discovery()` | Build the `.well-known/nodeinfo` document. |
| `get_nodeinfo_document()` | Build the NodeInfo 2.1 document. |
| `get_quote_authorization(id)` | Retrieve a stored `QuoteAuthorization`. |
| `render_interaction(interaction)` | Render a single interaction as HTML. |
| `render_interactions(interactions)` | Render a list of interactions as HTML. |

#### 8.2 `InboxProcessor`

Dispatches incoming activities by type via a handler map:

```
ActivityType → method
─────────────────────
Follow       → _handle_follow      (store follower, send Accept; the target may
                                    be an actor or a local object — FEP-efda
                                    thread subscriptions — and non-local
                                    targets are dropped without an Accept)
Undo         → _handle_undo        (unfollow or undo like/boost)
Create       → _handle_create      (reply, quote, or mention)
Like         → _handle_like        (store like interaction)
Announce     → _handle_announce    (store boost interaction)
Delete       → _handle_delete      (soft-delete interaction; a Delete of the
                                    sender's own actor document also retracts
                                    the (remote, local) follow record)
Update       → _handle_update      (update stored reply)
QuoteRequest → _handle_quote_request (FEP-044f: auto-approve quotes)
```

Before dispatching, `verify_signature()` checks the HTTP Signature header
by fetching the sender's public key (with actor caching), and binds the
verified signer to the activity: the `keyId`'s actor must equal
`activity.actor`, or the claimed actor's document must advertise the key
(`publicKey.id == keyId`; `publicKey` may be a list).  Signer/actor
mismatches raise `SignatureVerificationError`.  Verification is required
unless `skip_verification=True` — calling `process()` without request
headers raises rather than silently skipping the check.  Consequently,
relays that forward the original activity body signed with the relay's
key are rejected (pubby does not verify embedded Linked Data
signatures); Announce-wrapped relayed content is unaffected.  When
`allowed_instances`/`blocked_instances` are configured, the activity's
`actor` domain is checked first and rejected instances are dropped without
any network call (see `pubby.moderation`).  When `strict_attribution` is
enabled, `Create`/`Update` objects are additionally validated by
`pubby.attribution.validate` before any callback or storage (see
`pubby.attribution`).

Audience parsing (`to`/`cc`/`bto`/`bcc`, `Mention` tags) delegates to the
`pubby.audience` helpers; only publicly addressed interactions are
persisted, while `on_interaction_received` still fires for all accepted
interactions, enabling application-level notifications.  Quote detection
delegates to `pubby.quotes.extract_quote_target`, which recognizes every
inbound spelling (`quote`, `quoteUri`, `quoteUrl`, `_misskey_quote`).

`_handle_quote_request` builds the issued `QuoteAuthorization` with the
module-level `build_quote_authorization()` helper from
`pubby.handlers._outbox` and returns an `Accept` activity sharing the
`pubby.quotes.FEP_044F_CONTEXT` JSON-LD context.

#### 8.3 `OutboxProcessor`

Responsible for:

1. **Building activities** — `build_create_activity()`,
   `build_update_activity()`, `build_delete_activity()`,
   `build_like_activity()`, `build_announce_activity()`,
   `build_undo_activity()`, `build_quote_request_activity()`.  These are
   thin wrappers around the module-level `build_like_activity()`,
   `build_announce_activity()`, `build_delete_activity()`,
   `build_undo_activity()`, `build_update_activity()`, and
   `build_quote_request_activity()` helpers, which accept explicit
   `actor_id`, `to`/`cc`, `activity_id`, `published`, and `@context`.
   `build_update_activity()` takes a full object document, syncs its
   `to`/`cc` to the envelope's audience, and stamps `updated` on it.
   `build_quote_request_activity()` builds a FEP-044f `QuoteRequest`
   (quoting object embedded in `instrument`, addressed to the quoted
   object's author), and the module-level `build_quote_authorization()`
   builds the `QuoteAuthorization` document issued when approving a
   request — also used internally by `InboxProcessor`.
2. **Publishing** — `publish(activity)` stores the activity, collects
   follower inboxes (preferring shared inboxes for deduplication, via the
   module-level `collect_inboxes()` helper), then fans out delivery
   concurrently via `ThreadPoolExecutor`.
   When `async_delivery=True`, delivery runs in a background daemon thread
   so `publish()` returns immediately without blocking on slow/unreachable
   inboxes.
   Collected inboxes are filtered against the configured instance
   allow/block lists (`pubby.moderation`), and recipient actor documents
   on blocked/non-allowed domains are never fetched.
3. **Pluggable delivery** — when a `deliver` callable is supplied (also
   exposed on `ActivityPubHandler`), `publish()` invokes
   `deliver(inbox_url, activity)` once per collected inbox instead of the
   `ThreadPoolExecutor` fan-out, letting applications route deliveries
   through a task queue while keeping inbox collection, dedup, and
   instance filtering.  The module-level `deliver_activity()` performs a
   single signed POST and returns the HTTP status code, so queue workers
   can apply their own retry policy.
4. **Retry** — `_deliver_with_retry()` uses exponential backoff
   (`retry_base_delay × 2^attempt`); 5xx responses and connection errors
   are retried, 4xx errors are not.

#### 8.4 `_discovery`

Pure functions that build WebFinger JRD (RFC 7033) and NodeInfo 2.1
response dicts.

#### 8.5 `_client`

`get_default_user_agent(actor_id)` returns the default `User-Agent` string
(`pubby/{version} (+{actor_id})`).

#### 8.6 `pubby.client`

One-off actor/inbox resolution for applications that build their own
delivery pipeline:

- **`extract_actor_inbox(actor_data)`** — returns `endpoints.sharedInbox`
  when present, otherwise `inbox`, or `None` if neither is available.
- **`resolve_actor_inbox(actor_url, storage, *, private_key=None,
  key_id=None, allowed_instances=None, blocked_instances=None,
  user_agent=None, timeout=10.0)`** — resolves a remote actor's inbox
  using the actor cache and an optional signed HTTP GET, with
  allow/block domain filtering.

### 9. WebFinger Client — `pubby.webfinger`

- **`resolve_actor_url(username, domain)`** — performs a WebFinger lookup
  and returns the `self` link, falling back to
  `https://{domain}/@{username}`.
- **`extract_mentions(text)`** — finds all `@user@domain` patterns,
  resolves each via WebFinger, returns deduplicated `Mention` objects.
- **`Mention`** dataclass — carries `username`, `domain`, `actor_url`,
  plus helpers `acct` (property) and `to_tag()` (→ AP Mention tag dict).

### 10. Storage — `pubby.storage`

#### 10.1 Abstract Base — `ActivityPubStorage`

Defines the contract every storage backend must fulfill:

| Group | Methods |
|-------|---------|
| **Followers** | `store_follower()`, `remove_follower(actor_id, target_actor_id="")`, `get_followers(actor_id=None)`, `get_followers_of_targets(target_ids)` |
| **Interactions** | `store_interaction()`, `delete_interaction()`, `delete_interaction_by_object_id()`, `get_interactions()`, `get_interaction_by_object_id()` |
| **Activities** | `store_activity()`, `get_activities()` |
| **Actor cache** | `cache_remote_actor()`, `get_cached_actor()` |
| **Quote authorizations** | `store_quote_authorization()`, `get_quote_authorization()` |

`delete_interaction_by_object_id()`, `get_followers_of_targets()` and the
quote-authorization methods have default (no-op/fallback) implementations
so existing custom backends don't break when Pubby adds new features.
`get_followers_of_targets(target_ids)` returns followers whose
`target_actor_id` is one of the given local target URLs — actor URLs or
object ids (object-scoped follows, e.g. Friendica thread subscriptions);
the base implementation filters `get_followers()`, the DB adapter
uses an `IN` query, and the file adapter reads only the follower files
named for the given targets.

#### 10.2 SQLAlchemy Adapter — `pubby.storage.adapters.db`

- **Mixin models** (`_model.py`): `DbFollower`, `DbInteraction`,
  `DbActivity`, `DbActorCache` — framework-neutral SQLAlchemy column
  definitions.  Users inherit these into their own declarative Base to
  choose table names.  `DbFollower` includes `target_actor_id` with a
  unique constraint on `(actor_id, target_actor_id)` so the same remote
  actor can follow multiple local actors or objects.
- **`DbActivityPubStorage`** (`_storage.py`): full `ActivityPubStorage`
  implementation using a `session_factory` callable.  Upsert logic uses
  insert-then-update-on-`IntegrityError`.
- **`init_db_storage(engine)`** (`_helpers.py`): convenience function that
  creates a self-contained declarative Base, mapped models with default
  table names (`ap_followers`, `ap_interactions`, `ap_activities`,
  `ap_actor_cache`), calls `create_all()`, and returns a ready-to-use
  `DbActivityPubStorage`.  String URLs using a known async driver are
  converted via `to_sync_url()` before `create_engine()`; `driver_map`
  extends or overrides `DEFAULT_ASYNC_DRIVER_MAP`.
- **`to_sync_url(url, driver_map=None)`** (`_helpers.py`): converts an
  async SQLAlchemy URL to its sync-driver equivalent
  (`DEFAULT_ASYNC_DRIVER_MAP`: `sqlite+aiosqlite` → `sqlite`,
  `postgresql+asyncpg` → `postgresql+psycopg2`).  Already-sync URLs pass
  through unchanged; unknown async drivers raise `ValueError`.

#### 10.3 File Adapter — `pubby.storage.adapters.file`

`FileActivityPubStorage` stores entities as individual JSON files in a
directory tree:

```
data_dir/
├── followers/{hash}.json                     # unassigned/legacy followers
├── followers/{target_hash}-{actor_hash}.json # per-actor followers (v4+)
├── interactions/
│   ├── {target_hash}/{type}-{actor_hash}.json
│   ├── _mentions/{actor_hash}.json      # mention index
│   └── _object_ids/{object_id_hash}.json # object_id index
├── activities/{hash}.json
├── cache/actors/{hash}.json
└── quote_authorizations/{hash}.json
```

Thread-safe via per-path `RLock`.  Writes use atomic rename
(`.tmp` → final).  Suitable for static-site generators or low-traffic
setups that don't need a database.

**Index directories** (prefixed with `_`) enable O(1) lookups:

- **`_mentions/`** — maps actor URLs to interactions that mention them,
  enabling `get_interactions_mentioning()`.
- **`_object_ids/`** — maps remote object URLs to their interactions,
  enabling `get_interaction_by_object_id()` without a full directory scan.

**Schema versioning**: A `.schema_version` file tracks the storage format.
On initialization, `FileActivityPubStorage` checks this version and
automatically runs any pending migrations (e.g., rebuilding indexes).
Pass `auto_migrate=False` to disable.  The current schema version is 4,
which adds `target_actor_id` to follower records.

### 11. Render — `pubby.render`

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

### 12. Content Rendering — `pubby.content`

A stdlib-only module that produces outbound ActivityPub HTML from local plain
text.  It is intentionally independent of `pubby.render` (which sanitises
inbound HTML): `pubby.content` escapes all input and only emits anchors for
validated `http`/`https` URLs.

- **`render_post_html(text, hashtag_url)`** — escapes text, linkifies URLs,
  turns `#hashtags` into `rel="tag"` links, converts newlines to `<br>`
  elements (remote servers render `content`/`summary` as HTML, where a
  literal newline would collapse), and returns a `RenderedContent`
  dataclass with the HTML and a deduplicated list of normalized tag names.
- **`render_bio_html(bio)`** — escapes bio text and linkifies URLs without
  hashtag processing; newlines become `<br>` elements as above.
- **`build_hashtag_tags(names, hashtag_url)`** — maps normalized tag names to
  ActivityPub `Hashtag` tag dicts, preserving order and without deduplication.
- **`set_object_content(obj, text, hashtag_url)`** — renders plain text into
  `obj['content']` and merges detected hashtags into `obj['tag']`,
  deduplicating case-insensitively against existing tag names.  A no-op on
  empty text; composes `render_post_html` + `build_hashtag_tags`.
- **`format_duration(seconds)`** — formats a number of seconds as an
  ISO-8601 `PT[h]H[m]M[s]S` duration string, for the `duration` field of
  `Audio`/`Video` objects.
- **`render_verified_link(url, label=None)`** — renders a `rel="me"` anchor for
  valid URLs or escaped text for invalid ones.
- **`property_value_attachment(name, url, label=None)`** — builds a
  `PropertyValue` dict suitable for `ActorConfig.attachment`.

### 13. Server Adapters — `pubby.server.adapters`

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

### 14. Mastodon-Compatible API — `pubby.server.mastodon`

A read-only subset of the
[Mastodon REST API](https://docs.joinmastodon.org/methods/) so that
Mastodon clients and crawlers can discover the instance.

#### 14.1 Mappers (`_mappers.py`)

Pure functions that convert Pubby/AP types to Mastodon JSON shapes:

| Function | Converts |
|----------|----------|
| `actor_to_account()` | Local actor → Mastodon Account |
| `activity_to_status()` | Outbox activity → Mastodon Status |
| `follower_to_account()` | Follower → minimal Mastodon Account |
| `tag_to_mastodon_tag()` | Hashtag → Mastodon Tag |
| `stable_id()` / `id_to_url()` | Deterministic, reversible URL-safe base64 IDs |

#### 14.2 Route Handlers (`_routes.py`)

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

### 15. Quote Helpers — `pubby.quotes`

Pure, stdlib-only helpers for the quote fields defined by
[FEP-0449](https://codeberg.org/fediverse/fep/src/branch/main/fep/0449/fep-0449.md)
and the FEP-044f interaction policy, shared by `InboxProcessor` and
available to applications:

| Function / constant | Purpose |
|---------------------|---------|
| `extract_quote_target(obj_data)` | Quoted object URL from any recognized spelling (`quote`, `quoteUri`, `quoteUrl`, `_misskey_quote`), or `None` |
| `set_quote_target(obj, quoted_uri)` | Stamp all recognized spellings on an outgoing object so every compatible server recognizes the quote |
| `allow_public_quotes(obj)` | Stamp `interactionPolicy` allowing anyone to quote without manual approval |
| `PUBLIC_QUOTE_POLICY` | The `interactionPolicy` value stamped by `allow_public_quotes` |
| `QUOTE_FIELD_KEYS` | Recognized quote field spellings, in precedence order |
| `FEP_044F_CONTEXT` / `FEP_044F_TERMS` | JSON-LD context declaring the `QuoteRequest`/`QuoteAuthorization` and gts interaction terms |

Outgoing-side payload construction lives with the other activity
builders: `build_quote_request_activity()` and
`build_quote_authorization()` in `pubby.handlers._outbox`.

---

## Dependency Graph (internal)

```
pubby.__init__
  ├── pubby._model            (dataclasses, enums, AP_CONTEXT)
  ├── pubby._exceptions       (exception hierarchy)
  ├── pubby._rate_limit       (RateLimiter)
  ├── pubby.moderation         (instance domain allow/block helpers)
  ├── pubby.audience           (audience/Mention parsing helpers) → _model
  ├── pubby.quotes             (quote field/policy helpers) → _model
  ├── pubby.attribution        (inbound object attribution validation)
  │                             → _exceptions, moderation
  ├── pubby.content            (plain-text HTML renderers, Hashtag tag builder,
  │                             object content/duration helpers)
  ├── pubby.webfinger          (Mention, resolve_actor_url, extract_mentions)
  ├── pubby.crypto             (_keys, _signatures)
  ├── pubby.client             (extract_actor_inbox, resolve_actor_inbox)
  ├── pubby.handlers           (ActivityPubHandler)
  │     ├── _handler.py
  │     │     ├── _inbox.py    → crypto, storage, _model, _exceptions,
  │     │     │                    moderation, quotes, _outbox
  │     │     ├── _outbox.py   → crypto, storage, _model, moderation, quotes
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
   exponential-backoff retry.  A `deliver` callable can replace the thread
   pool entirely so applications fan out via their own task queue.

6. **Mastodon compatibility layer** — a read-only Mastodon REST API
   surface is separated into framework-agnostic mappers + route handlers,
   with a thin per-framework adapter — the same pattern as the core AP
   routes.

7. **Soft deletes** — interactions are marked `DELETED` rather than
   physically removed, preserving an audit trail.

8. **FEP-044f quote authorization** — incoming `QuoteRequest` activities
   are optionally auto-approved, with the `QuoteAuthorization` object
   stored and served via a dedicated endpoint.  The outgoing side is
   covered by `build_quote_request_activity()` /
   `build_quote_authorization()` and the `pubby.quotes` field/policy
   helpers.
