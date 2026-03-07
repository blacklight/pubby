# Changelog

All notable changes to this project will be documented in this file.

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
