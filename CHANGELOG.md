# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

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
