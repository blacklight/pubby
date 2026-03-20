# Changelog

All notable changes to this project will be documented in this file.

## Unreleased

### Fixed

- Inbox processor now rejects malformed activity payloads (e.g., JSON arrays
  instead of objects) with a proper 400 error instead of crashing.

## 0.2.19

### Fixed
- Outbox now respects explicitly provided `to`/`cc` fields (including
  preserving an intentionally empty `cc`) and only applies default addressing
  when posts are unaddressed.
- Prevented unintended follower inbox fanout for direct messages by only
  delivering to follower inboxes when the activity targets followers and/or
  `as:Public`.

## 0.2.18

### Fixed

- **Private interactions filtered**: `Create` activities that are not publicly
  addressed (i.e., lacking `https://www.w3.org/ns/activitystreams#Public` or
  its aliases in `to`/`cc`) are no longer stored. This prevents private/direct
  messages from appearing in public contexts like blog comments or guestbooks.
  The `on_interaction_received` callback is still invoked for these
  interactions, allowing notifications to be sent.

## 0.2.17

### Fixed
- Signed outgoing **GET** requests when fetching remote actors in
  **InboxProcessor** and **OutboxProcessor**, preventing `401 Unauthorized`
  responses from instances that require HTTP Signatures.
- Extended test coverage to ensure `sign_request` is invoked for actor fetches
  and that signed headers (`Signature`, `Date`, `Host`, etc.) are included in
  the request.

## 0.2.16

### Added
- `async_delivery` parameter for `ActivityPubHandler` and `OutboxProcessor`.
  When `True` (now the default), delivery fan-out runs in a background daemon
  thread so `publish()` returns immediately without blocking on slow or
  unreachable inboxes.

## 0.2.15

### Added
- `OutboxProcessor.build_like_activity(object_url, *, activity_id=None,
  published=None)` — build an outbound `Like` activity.
- `OutboxProcessor.build_announce_activity(object_url, *, activity_id=None,
  published=None)` — build an outbound `Announce` (boost) activity.
- `OutboxProcessor.build_undo_activity(inner_activity)` — wrap any activity
  in an `Undo` envelope (works for Like, Announce, Follow, etc.).
- `ActivityPubHandler.publish_activity(activity)` — publish a pre-built
  activity dict without Create/Update wrapping, for activity types like
  `Like`, `Announce`, `Undo`, and `Follow`.

## 0.2.14

### Added
- **Mention delivery** — `OutboxProcessor.publish()` now delivers activities to
  actors in the `to` and `cc` fields, not just followers. This enables
  notifications for mentioned users (e.g., `@user@domain` mentions in posts).
  Actor inboxes are fetched via HTTP (with caching) and deliveries are
  deduplicated to avoid sending twice if a mentioned actor is also a follower.

## 0.2.13

### Added
- File storage schema versioning via a `.schema_version` file, with automatic
  migrations on `FileActivityPubStorage` initialization.
- New `auto_migrate` option for `FileActivityPubStorage` to opt out of
  automatic migrations.
- Migration/backfill support for the `_object_ids/` index (v2) to enable fast
  `get_interaction_by_object_id()` lookups on existing data.
- New migration helper `backfill_object_id_index(storage, dry_run=False)`
  exported from `pubby.storage`.

## 0.2.12

### Added
- Added `ActivityPubStorage.get_interaction_by_object_id(object_id,
  status=CONFIRMED)` to look up interactions by remote object URL.
- Implemented efficient object-id lookup in both adapters:
  - DB: indexed query on `object_id` + `status`
  - File: new `interactions/_object_ids/` index for O(1) lookups
- Added file-storage tests covering object-id lookup and status filtering.

### Changed
- File storage `delete_interaction_by_object_id()` now uses the `_object_ids/` index instead of scanning the whole interactions directory.
- Updated README and architecture docs to document the new API and file index layout.
- Refined contributor/agent execution style guidance in `AGENTS.md`.

## 0.2.11

### Docs
- Added missing **AGPL-3.0** license text (`LICENSE.txt`).
- Enhanced `README.md` with additional project badges and a generated table of
  contents with section anchors.

### Chore
- Updated pre-commit configuration to auto-generate/maintain the README TOC via
  the `md-toc` hook (GitHub style, max depth 6).

## 0.2.10

## Changed
- Outbox delivery retry logs now include the exception type and message for
  failed attempts, while omitting full stack traces.

## 0.2.9

### Changed
- Removed `max-height` from expanded interactions

## 0.2.8

### Changed
- Removed `max-height` from expanded interactions

## 0.2.7

### Fixed
- **File storage:** Made `delete_interaction_by_object_id` more robust when
  scanning `data_dir/interactions/*/*.json` by skipping non-dict JSON files
  (e.g., reverse-mention index files stored as JSON lists), preventing
  `AttributeError: 'list' object has no attribute 'get'`.
- **Mention index consistency:** When deleting an interaction by `object_id`,
  its entries are now also removed from `interactions/_mentions`, matching the
  behavior of `delete_interaction`.

## 0.2.6

### Added

- **Mention indexing** — Interactions now track which actors they mention via
  the `mentioned_actors` field. Mentions are automatically extracted from
  ActivityPub `tag` arrays when processing incoming `Create` activities.
- **`get_interactions_mentioning(actor_url)`** — New storage method to retrieve
  all interactions that mention a given actor URL. Implemented in both file and
  DB storage adapters.
- **`DbInteractionMention`** — New SQLAlchemy model for the mention join table.
  Pass `interaction_mention_model` to `DbActivityPubStorage` to enable the
  mention index for DB storage.
- **`backfill_mentions(storage)`** — Migration utility to populate
  `mentioned_actors` for existing interactions by extracting mentions from
  stored `raw_object` metadata.
- **`store_local_only`** parameter on `ActivityPubHandler` — When `True`, only
  stores interactions targeting local URLs or mentioning the local actor.
  Useful for filtering out interactions on remote resources.
- **`local_base_urls`** parameter on `ActivityPubHandler` — List of base URLs
  considered "local" for the `store_local_only` filter.

### Changed

- DB storage now uses idempotent upsert operations (`INSERT ... ON CONFLICT DO
  UPDATE`) for SQLite and PostgreSQL, improving performance and replay safety.

## 0.2.5

### Changed
- Collapsed long interaction bodies (reply/quote/mention) over 1000 characters with a “show more/less” toggle to improve rendering readability.

## 0.2.4

### Fixed
- **ActivityPub QuoteAuthorization routing:** QuoteAuthorization resources are
  now served under `actor_path` (`/ap/actor/quote_authorizations/...`) instead
  of the server prefix (`/ap/quote_authorizations/...`), preventing 404s when
  remote servers dereference authorization IDs (per FEP-044f). Applied across
  **Flask**, **FastAPI**, and **Tornado**.

### Tests
- Added regression tests ensuring stored QuoteAuthorizations are resolvable at
  `{actor_path}/quote_authorizations/{id}` and missing ones return **404**.
- Adapter test clients now expose the bound handler to support adapter-agnostic tests.

## 0.2.3

### Fixed
- Inbox actor fetching now treats **HTTP 410 Gone** (deleted remote actors) as non-fatal: returns `None` and logs at **DEBUG** instead of warning.

## 0.2.2

### Fixed
- Always render the content of an interaction when available, even if
  the type is unknown.
- Minor style fix for FQN rendering in the interaction component.

## 0.2.1

### Changed
- Treat incoming ActivityPub `Create` activities that directly mention the
  local actor as interactions (guestbook entries).

### Added
- Detection of direct mentions via `to`/`cc` recipients and `tag` objects of
  type `Mention` targeting the actor.

### Updated
- Interaction classification precedence is now: **quote > reply > mention**;
  `Create` activities without any of these signals are ignored.

## _0.2.0_

### Added

- **Mastodon-compatible API layer** — read-only subset of the Mastodon REST
  API backed by Pubby's handler and storage. New `bind_mastodon_api()` adapter
  function for Flask, FastAPI, and Tornado:
  - `GET /api/v1/instance` — instance metadata (v1)
  - `GET /api/v2/instance` — instance metadata (v2)
  - `GET /api/v1/instance/peers` — peer domains from followers
  - `GET /api/v1/accounts/lookup` — resolve `acct:user@domain` to Account
  - `GET /api/v1/accounts/:id` — account by ID
  - `GET /api/v1/accounts/:id/statuses` — paginated statuses with cursor
    pagination and filtering (`limit`, `max_id`, `since_id`, `only_media`,
    `tagged`)
  - `GET /api/v1/accounts/:id/followers` — paginated followers list
  - `GET /api/v1/statuses/:id` — single status lookup
- **NodeInfo aliases** — `GET /nodeinfo/2.0`, `/nodeinfo/2.0.json`, and
  `/nodeinfo/2.1.json` registered by the Mastodon API adapter for
  compatibility with older clients.
- `pubby.server.mastodon` subpackage with framework-agnostic mappers
  (`actor_to_account`, `activity_to_status`, `follower_to_account`,
  `tag_to_mastodon_tag`) and route logic (`MastodonAPI`).

## _0.1.9_

### Fixed

- **render**: Render interactions by creation date (descending)

## _0.1.8_

### Fixed

- **webfinger**: Accept leading `@` in `acct` resource identifiers.
- **outbox**: Sign `Content-Type` and `Content-Length` headers for ActivityPub payloads.

## 0.1.7

### Added

- **Profile metadata fields** — `ActorConfig` now supports `attachment`
  list for `PropertyValue` profile metadata (verified links, custom
  fields) displayed on Mastodon and other Fediverse platforms.
- **Actor URL override** — optional `url` field in `ActorConfig` for
  custom human-readable profile URLs, separate from the ActivityPub
  actor `id`. Useful for Mastodon profile-link verification.
- **Actor profile updates** — `publish_actor_update()` method to broadcast
  actor profile changes (name, summary, icon, fields) to all followers
  via `Update` activity, following the standard Mastodon profile-edit
  mechanism.
- Schema.org context mappings for `PropertyValue` and `value` fields
  in `AP_CONTEXT`.

## 0.1.6

### Added

- `QUOTE` member on `InteractionType` enum — incoming quotes (via
  `quote`, `quoteUrl`, or `_misskey_quote` fields on `Create`
  activities) are now stored as `InteractionType.QUOTE`.
- `QUOTE_REQUEST` member on `ActivityType` enum.
- **QuoteAuthorization** ([FEP-044f](https://codeberg.org/fediverse/fep/src/branch/main/fep/044f/fep-044f.md))
  — incoming `QuoteRequest` activities are answered with an `Accept`
  whose `result` points to a dereferenceable `QuoteAuthorization`
  object, so Mastodon (and other servers) clear the "pending" state.
- `GET <prefix>/quote_authorizations/<id>` endpoint in all three
  server adapters (Flask, FastAPI, Tornado) to serve stored
  `QuoteAuthorization` objects.
- `store_quote_authorization` / `get_quote_authorization` on
  `ActivityPubStorage` (with implementations in file and DB adapters).
- `auto_approve_quotes` parameter on `ActivityPubHandler` and
  `InboxProcessor` (default `True`). Set to `False` to ignore
  incoming `QuoteRequest` activities.

## [0.1.5]

### Added

- Default HTTP `User-Agent` header set to `pubby/{version} (+{actor_id})`
  for all outgoing requests when no custom user-agent is configured.

### Fixed

- Incoming `Delete` activities now correctly remove interactions. Previously
  the handler passed the deleted object's URL as `target_resource`, which
  never matched the storage key (keyed by article URL). Added
  `delete_interaction_by_object_id()` to file and DB storage adapters,
  which looks up interactions by their `object_id` field. The inbox handler
  tries this first and falls back to the brute-force approach.

## [0.1.4]

### Added

- `language` field on `Object` — when set, `to_dict()` emits `contentMap`
  (and `summaryMap` if a summary is present), allowing Mastodon, Pleroma,
  and Akkoma to detect the post language instead of showing a "Translate"
  button. `Object.build()` parses language from incoming `contentMap` keys
  or an explicit `language` field.
- Summary header in the interactions container template showing counters
  for replies (💬), boosts (🔁), likes (⭐), and mentions (📣). Only
  non-zero counters are displayed.
- Actor FQN (`@user@domain`) shown next to author name in interaction
  cards, derived from the actor URL via `actor_fqn()` template helper.
- "original post ↗" link in the interaction footer for replies and
  mentions, linking to the source `object_id`.

### Fixed

- Interaction reply content now rendered as HTML instead of escaped text.
  Added `_sanitize_html()` allowlist-based sanitizer and `sanitize_html()`
  Jinja2 template helper to safely render federated HTML content.

## 0.1.3

### Added

- `ActorConfig` dataclass for typed actor configuration. Replaces the
  plain-dict `actor_config` parameter with documented, IDE-friendly
  fields. Plain dicts are still accepted for backwards compatibility
  (auto-converted via `ActorConfig.from_dict()`).
- `pubby.webfinger` module with WebFinger client utilities:
  - `resolve_actor_url(username, domain)` — resolve an ActivityPub
    actor URL via WebFinger (RFC 7033), with fallback.
  - `extract_mentions(text)` — find all `@user@domain` patterns in
    text and resolve each via WebFinger. Returns `Mention` objects.
  - `Mention` dataclass with `username`, `domain`, `actor_url`,
    `.acct` property, and `.to_tag()` for ActivityPub tag dicts.

## [0.1.2]

### Added

- `media_type` field on `Object` — serialized as `mediaType` in the
  ActivityPub JSON-LD output. Allows specifying the content type
  (e.g. `text/html`) for the object's `content` field.
  Also parsed from incoming objects via `Object.build()`.

## [0.1.1]

### Fixed

- `export_private_key_pem` is now exported from `pubby.crypto` (was missing
  from the package `__init__`).

## [0.1.0]

### Added

- **README.md** with full documentation: installation, quick start (Flask/FastAPI/Tornado),
  publishing, key management, custom storage, configuration reference, rendering,
  rate limiting, interaction callbacks.
- SVG banner image (`img/pubby-banner.svg`).
- **FastAPI server adapter** (`pubby.server.adapters.fastapi`) with `bind_activitypub()`.
- **Tornado server adapter** (`pubby.server.adapters.tornado`) with `bind_activitypub()`.
- Shared parametrized test suite for all three framework adapters (Flask, FastAPI, Tornado).
- Concurrent fan-out delivery via `ThreadPoolExecutor` in `OutboxProcessor.publish()`.
  Deliveries to follower inboxes now run in parallel (default 10 workers) instead
  of sequentially.
- New `max_delivery_workers` parameter on `OutboxProcessor` and `ActivityPubHandler`
  to control the thread pool size.
