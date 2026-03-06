# 05 — Follow-Up Tasks

Deferred tasks that are out of scope for the initial library implementation.

## 1. Madblog Integration

- [ ] **ContentMonitor callback**: Register `handler.publish_object()` on `ContentMonitor.on_content_change()` — send `Create`/`Update`/`Delete` activities when articles change.
- [ ] **config.yaml keys**: Add `enable_activitypub`, `activitypub_username`, `activitypub_display_name`, `activitypub_private_key`, `activitypub_manually_approves_followers`, `activitypub_data_dir`.
- [ ] **Environment variable overrides**: `MADBLOG_ACTIVITYPUB_*` environment variables.
- [ ] **Template changes**: Merge AP interactions alongside Webmentions on article pages. Both renderers produce compatible HTML fragments.
- [ ] **Auto key generation**: Generate RSA key pair on first run if `activitypub_private_key` doesn't exist.
- [ ] **File-based storage preference**: Madblog should default to `FileActivityPubStorage` with `data_dir` relative to `content_dir`.
- [ ] **Content negotiation on article URLs**: Return `application/activity+json` when Mastodon fetches article URLs (the `Object` document for that article).

## 2. FastAPI Server Adapter

- [ ] `bind_activitypub(app, handler)` for FastAPI — register routes using `APIRouter`.
- [ ] Follow the same pattern as `webmentions.server.adapters.fastapi`.
- [ ] Async support: FastAPI handler can call `process_inbox_activity()` in a thread pool executor.

## 3. Tornado Server Adapter

- [ ] `bind_activitypub(app, handler)` for Tornado — register `RequestHandler` subclasses.
- [ ] Follow the same pattern as `webmentions.server.adapters.tornado`.

## 4. Blog Author Reply Support

- [ ] Enable the blog author to reply to fediverse interactions from within the blog's admin interface.
- [ ] Applies to **both** ActivityPub and Webmentions — consider a unified reply interface.
- [ ] Create a `Note` activity with `inReplyTo` pointing to the original fediverse note.
- [ ] Sign and deliver to the original author's inbox.

## 5. Mastodon Client API Compatibility

- [ ] Subset of the Mastodon REST API (v1): `/api/v1/accounts/verify_credentials`, `/api/v1/accounts/:id/statuses`.
- [ ] Enables showing follower count and recent posts in Mastodon-compatible clients.
- [ ] Optional — only implement if there's a concrete use case.

## 6. Moderation

- [ ] **Domain allow/block lists**: Configurable list of domains to accept/reject activities from.
- [ ] **Actor allow/block lists**: Configurable list of individual actors.
- [ ] Check lists during inbox processing, before signature verification (to save compute).
- [ ] Configurable via `actor_config` or a separate moderation config.

## 7. Featured Collection (Pinned Posts)

- [ ] `GET /ap/featured` → `OrderedCollection` of pinned/featured objects.
- [ ] Mastodon uses this to show pinned posts on profile pages.
- [ ] Requires a way to mark articles as "featured" (e.g. front-matter metadata).

## 8. Hashtag Federation

- [ ] Add `Hashtag` tag objects to `Article` objects based on article tags/categories.
- [ ] Support `Follow` of hashtag collections (Mastodon 4.0+).
- [ ] Deliver to followers of specific hashtags.

## 9. Key Rotation

- [ ] Document the manual key rotation process:
  1. Generate new key pair
  2. Update `publicKey` on actor document
  3. Old signatures become unverifiable (acceptable for a blog)
  4. Remote servers will re-fetch the actor on next interaction
- [ ] Optional: Implement `Update` activity for the actor document on key change.

## 10. README.md

- [ ] Full documentation following the webmentions README as a template.
- [ ] Installation instructions (pip, optional deps).
- [ ] Quick start guide with code examples.
- [ ] Configuration reference.
- [ ] Storage backend comparison and selection guide.
- [ ] API reference for public classes and methods.

## 11. CI/CD Pipeline

- [x] GitHub Actions / Gitea CI workflow:
  - Run `pytest` on push/PR
  - Lint with `flake8` / `black`
  - Build wheel and sdist

## 12. PyPI Publishing

- [ ] Register `pubby` on PyPI.
- [ ] Set up trusted publishers for automated release on tag.
- [ ] Add classifiers: `Framework :: Flask`, `Topic :: Internet :: WWW/HTTP :: Dynamic Content`.

## 13. Additional Testing

- [ ] Integration tests that hit a real Mastodon instance (marked `@pytest.mark.integration`).
- [ ] Property-based testing with Hypothesis for model serialization.
- [ ] Benchmark tests for file storage with many followers.

## 14. Collection Pagination

- [ ] Paginate `outbox`, `followers`, `following` collections per ActivityPub spec.
- [ ] Use `first`/`next`/`prev` links in `OrderedCollectionPage`.
- [ ] Required when collections grow beyond ~40 items.
