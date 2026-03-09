# Changelog

All notable changes to this project will be documented in this file.

## _Unreleased_

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
