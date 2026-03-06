# ActivityPub Support for Madblog — Research & Implementation Proposal

## 1. Executive Summary

Add ActivityPub federation to Madblog so that:

1. The blog is **discoverable** from any Mastodon/fediverse client (`@blog@yourdomain.com`).
2. New/updated posts are **pushed** to followers as `Create`/`Update` activities.
3. **Reactions, replies, boosts** from fediverse users are received, stored, and rendered on article pages (alongside existing Webmentions).
4. A `Follow`/`Undo` state machine handles subscriptions.

**Recommendation:** Build this as a **standalone library** (`activitypub-federation` or similar), following the same architecture as the [`webmentions`](https://github.com/blacklight/webmentions) library — framework-agnostic core with thin Flask/FastAPI/Tornado adapters. Then integrate into Madblog the same way Webmentions are integrated today.

---

## 2. Protocol Surface Area

### 2.1 Discovery Layer

| Endpoint | Purpose | Spec |
|---|---|---|
| `/.well-known/webfinger?resource=acct:user@domain` | Actor lookup | [RFC 7033](https://tools.ietf.org/html/rfc7033) |
| `/.well-known/nodeinfo` | Server metadata | [NodeInfo 2.1](https://nodeinfo.diaspora.software/protocol) |
| `/nodeinfo/2.1` | NodeInfo document | — |

**WebFinger** returns a JRD pointing to the Actor URL. For a single-user blog this can be near-static — just match the configured username.

**NodeInfo** is optional but expected by Mastodon crawlers. Returns software name/version, protocol list (`activitypub`), user counts, etc.

### 2.2 Actor

A single `Person` (or `Application`) object served at a stable URL (e.g. `https://blog.example.com/ap/actor`):

```json
{
  "@context": [
    "https://www.w3.org/ns/activitystreams",
    "https://w3id.org/security/v1"
  ],
  "id": "https://blog.example.com/ap/actor",
  "type": "Person",
  "preferredUsername": "blog",
  "name": "My Blog",
  "summary": "<p>Blog description</p>",
  "inbox": "https://blog.example.com/ap/inbox",
  "outbox": "https://blog.example.com/ap/outbox",
  "followers": "https://blog.example.com/ap/followers",
  "following": "https://blog.example.com/ap/following",
  "icon": { "type": "Image", "url": "https://blog.example.com/img/icon.png" },
  "publicKey": {
    "id": "https://blog.example.com/ap/actor#main-key",
    "owner": "https://blog.example.com/ap/actor",
    "publicKeyPem": "-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----"
  },
  "manuallyApprovesFollowers": false,
  "discoverable": true,
  "url": "https://blog.example.com"
}
```

**Key Mastodon extensions to include:** `discoverable`, `indexable`, `featured` (pinned posts collection), `attachment` (profile metadata/links).

### 2.3 Inbox (Server-to-Server)

The inbox receives `POST` requests with signed JSON-LD payloads. Activities to handle:

| Activity | Action |
|---|---|
| `Follow` | Store follower, reply with `Accept` |
| `Undo` (Follow) | Remove follower |
| `Create` (Note) | Store as reply/comment on the target article |
| `Update` (Note) | Update stored reply |
| `Delete` (Note/Tombstone) | Mark reply as deleted |
| `Like` | Store as reaction |
| `Announce` | Store as boost/reshare |
| `Undo` (Like/Announce) | Remove reaction/boost |

Everything else: log and ignore (or 202 Accepted).

### 2.4 Outbox

A paginated `OrderedCollection` of the blog's published activities. Each article becomes a `Create` wrapping an `Article` (preferred) or `Note` object.

Mastodon will render `Article` objects using the `name` as title and `content` as body, with `url` linking back. The `summary` becomes the CW/spoiler text if present.

### 2.5 Delivery (Fan-out)

When a post is created or updated:

1. Build a `Create` or `Update` activity wrapping the `Article` object.
2. Set `to: ["https://www.w3.org/ns/activitystreams#Public"]` and `cc: [followersCollection]`.
3. Collect unique inbox URLs from all followers (shared inboxes where available).
4. POST the signed activity to each inbox.
5. Retry on 5xx/timeout with exponential backoff (3 attempts over ~15 min is sufficient for a blog).

### 2.6 HTTP Signatures

Every outgoing POST and (optionally) GET must be signed. The signature covers `(request-target)`, `host`, `date`, and `digest` headers.

**Libraries:**
- `cryptography` — RSA key generation and signing
- [`httpsig`](https://pypi.org/project/httpsig/) — HTTP Signature draft-cavage-http-signatures. Lightweight, well-maintained. Alternatively, implement the ~50 lines manually (as Mastodon's guide suggests).

For **incoming** requests: verify the signature by fetching the sender's Actor → `publicKey.publicKeyPem`. Cache fetched actor keys (TTL ~24h).

---

## 3. Architecture Decision: Standalone Library

### Why not implement directly in Madblog?

The same reasoning as Webmentions: ActivityPub federation is useful beyond Madblog. A standalone library can be reused by any Python web app. Madblog becomes just one consumer.

### Proposed library structure (mirroring `webmentions`)

```
activitypub_federation/
├── __init__.py              # Public API re-exports
├── _model.py                # Actor, Activity, Object dataclasses
├── _exceptions.py
├── crypto/
│   ├── __init__.py
│   ├── _keys.py             # RSA key pair generation/loading
│   └── _signatures.py       # HTTP Signature sign/verify
├── handlers/
│   ├── __init__.py
│   ├── _handler.py          # Main ActivityPubHandler (analogous to WebmentionsHandler)
│   ├── _inbox.py            # Inbox processing logic
│   ├── _outbox.py           # Outbox generation, delivery fan-out
│   └── _discovery.py        # WebFinger + NodeInfo response builders
├── render/
│   ├── __init__.py
│   └── _renderer.py         # Render interactions (replies, likes, boosts) as HTML
├── storage/
│   ├── __init__.py
│   ├── _base.py             # Abstract storage interface
│   └── adapters/
│       ├── file/            # File-based (JSON/Markdown, like Webmentions)
│       └── db/              # SQLite/SQL adapter
└── server/
    └── adapters/
        ├── flask.py         # bind_activitypub(app, handler) — registers routes
        ├── fastapi.py
        └── tornado.py
```

### Public API sketch

```python
from activitypub_federation import ActivityPubHandler
from activitypub_federation.storage.adapters.file import FileActivityPubStorage
from activitypub_federation.server.adapters.flask import bind_activitypub

storage = FileActivityPubStorage(
    data_dir="/path/to/ap-data",
    base_url="https://blog.example.com",
)

handler = ActivityPubHandler(
    storage=storage,
    actor_config={
        "username": "blog",
        "name": "My Blog",
        "summary": "A blog about things",
        "icon_url": "https://blog.example.com/img/icon.png",
    },
    private_key_path="/path/to/private.pem",
    # or private_key_pem="..."
)

# In Flask app setup:
bind_activitypub(app, handler)

# On content change (integrate with ContentMonitor):
handler.publish_article(
    article_id="https://blog.example.com/article/my-post",
    title="My Post",
    content_html="<p>Hello world</p>",
    published=datetime.now(timezone.utc),
    summary="A post about things",
    tags=["python", "activitypub"],
)
```

---

## 4. Storage Design

### 4.1 File-based (default, consistent with Madblog philosophy)

```
ap-data/
├── actor/
│   ├── private.pem
│   └── public.pem
├── followers/
│   ├── user-mastodon-social-abc123.json    # One file per follower
│   └── ...
├── interactions/
│   ├── in/
│   │   └── {article-slug}/
│   │       ├── reply-{domain}-{hash}.json
│   │       ├── like-{domain}-{hash}.json
│   │       └── boost-{domain}-{hash}.json
│   └── out/
│       └── {article-slug}/
│           └── create-{hash}.json          # Sent activity record
└── cache/
    └── actors/
        └── {domain}-{hash}.json            # Cached remote actor info (TTL)
```

Each follower/interaction is a JSON file with full activity data. This keeps everything inspectable, version-controllable, and zero-dependency.

**Thread-safety:** Same `RLock` per-resource pattern as `FileWebmentionsStorage`.

### 4.2 SQLite adapter (optional, phase 2)

For blogs with many followers (>1000), file-based fan-out becomes slow. A SQLite adapter with tables for `followers`, `interactions`, `delivery_queue` would handle this better. Not needed for initial release.

---

## 5. Integration with Madblog

### 5.1 Routes registered by `bind_activitypub(app, handler)`

| Method | Path | Content-Type | Purpose |
|---|---|---|---|
| GET | `/.well-known/webfinger` | `application/jrd+json` | Actor discovery |
| GET | `/.well-known/nodeinfo` | `application/json` | NodeInfo discovery |
| GET | `/nodeinfo/2.1` | `application/json` | NodeInfo document |
| GET | `/ap/actor` | `application/activity+json` | Actor profile |
| POST | `/ap/inbox` | — | Receive activities |
| GET | `/ap/outbox` | `application/activity+json` | Published articles collection |
| GET | `/ap/followers` | `application/activity+json` | Followers collection |
| GET | `/ap/following` | `application/activity+json` | Following collection (empty for blogs) |

### 5.2 Content Monitor integration

Register a callback on `ContentMonitor` (same pattern as Webmentions):

```python
# In BlogApp._init_activitypub():
self.content_monitor.register(self.ap_storage.on_content_change)
```

`on_content_change` triggers:
- **Created/Modified:** Parse article metadata → call `handler.publish_article()` → fan-out `Create` or `Update` to followers.
- **Deleted:** Send `Delete` with `Tombstone` to followers.

### 5.3 Rendering interactions on articles

Reuse the existing Webmentions rendering infrastructure. ActivityPub interactions map naturally:

| AP Activity | Webmention equivalent | Display |
|---|---|---|
| `Create` (Note, inReplyTo) | Reply mention | Comment with author, avatar, content |
| `Like` | Like mention | "⭐ liked by @user@domain" |
| `Announce` | Repost mention | "🔁 boosted by @user@domain" |

The `render/` module in the library produces the same data structure that `webmentions.render` does, so the article template can merge both sources seamlessly.

### 5.4 Configuration

New `config.yaml` keys:

```yaml
# ActivityPub
enable_activitypub: true
activitypub_username: "blog"          # → @blog@yourdomain.com
activitypub_display_name: "My Blog"   # defaults to config.title
activitypub_private_key: "/path/to/private.pem"  # auto-generated if missing
activitypub_manually_approves_followers: false
activitypub_data_dir: "ap-data"       # relative to content_dir
```

Environment variable overrides: `MADBLOG_ACTIVITYPUB_*`.

---

## 6. Mastodon Compatibility Notes

Based on [Mastodon's ActivityPub documentation](https://docs.joinmastodon.org/spec/activitypub/):

1. **Object types:** Mastodon handles `Article` by using `name` as title, `content` as body, appending `url`. This is ideal for blog posts.
2. **HTML sanitization:** Mastodon strips most HTML. Keep `content` simple: `<p>`, `<br>`, `<a>`, `<span>`. For articles, a brief summary + link-back works better than full HTML.
3. **Shared inbox:** Check followers' Actor for `endpoints.sharedInbox` to reduce delivery requests.
4. **Digest header:** Always include `Digest: SHA-256=...` on POST requests — Mastodon validates it.
5. **Content negotiation:** Actor URL must return `application/activity+json` when `Accept` header requests it. Return HTML (normal blog page) otherwise.
6. **`@context` must include** `https://www.w3.org/ns/activitystreams` and `https://w3id.org/security/v1` at minimum.

---

## 7. Existing Libraries Assessment

| Library | Verdict | Notes |
|---|---|---|
| **pyfed** (Funkwhale) | ❌ Not suitable | Funkwhale's internal GitLab, blocked by Anubis PoW. Not on PyPI (the PyPI `pyfed` is a *federated learning* lib). Tightly coupled to Funkwhale. |
| **Takahe** | ❌ Reference only | Full server (Django, PostgreSQL). Last commit 2y ago. Useful to study inbox/outbox patterns but far too heavy to depend on. |
| **microblog.pub** | ❌ Reference only | Single-user microblog (FastAPI, SQLite). Last commit 3y. Good reference for single-user AP patterns, but it's a full application, not a library. |
| **httpsig** | ✅ Use | Lightweight HTTP Signature implementation. |
| **cryptography** | ✅ Use | RSA key management. |
| **pyld** | ⚠️ Maybe | JSON-LD processing. In practice, Mastodon and most fediverse servers don't do full JSON-LD expansion — they just check `@context`. Skip unless needed for edge cases. |

**Conclusion:** No existing Python ActivityPub *library* is suitable. All existing projects are full applications. Building a thin, focused library is the right call — the protocol surface for a read-mostly blog is small enough that a from-scratch implementation (with `httpsig` + `cryptography`) is less work than adapting any existing codebase.

---

## 8. Implementation Phases

### Phase 1: Core Federation (MVP)

**Goal:** Blog is discoverable, followable, and pushes articles to followers.

- [ ] RSA key pair generation + management
- [ ] HTTP Signature signing (outgoing) and verification (incoming)
- [ ] WebFinger endpoint
- [ ] Actor endpoint (with content negotiation)
- [ ] Inbox: handle `Follow`, `Undo(Follow)`
- [ ] Outbox: paginated `OrderedCollection` of articles
- [ ] Delivery fan-out: `Create` / `Update` / `Delete` activities to followers
- [ ] ContentMonitor integration
- [ ] File-based storage for followers and sent activities
- [ ] Flask adapter (`bind_activitypub`)
- [ ] Config integration in Madblog
- [ ] Auto-generate key pair on first run

**Estimated effort:** ~2-3 weeks

### Phase 2: Incoming Interactions

**Goal:** Receive and display replies, likes, boosts.

- [ ] Inbox: handle `Create(Note)`, `Like`, `Announce`, `Delete`, `Update`
- [ ] File-based interaction storage (per-article, like Webmentions)
- [ ] Interaction renderer (HTML fragments for article pages)
- [ ] Merge AP interactions + Webmentions in article template
- [ ] Remote actor info caching (avatar, display name)
- [ ] Email notifications for new interactions (reuse SMTP infra)

**Estimated effort:** ~1-2 weeks

### Phase 3: Hardening & Extras

- [ ] NodeInfo endpoint
- [ ] Shared inbox optimization for delivery
- [ ] Delivery retry queue (in-process, with backoff)
- [ ] SQLite storage adapter (optional)
- [ ] `followers` / `following` collection pagination
- [ ] Mastodon API compatibility (subset: account lookup, statuses) — enables showing follower count, etc.
- [ ] Moderation: configurable allow/block lists (by domain or actor)
- [ ] Featured collection (pinned posts)
- [ ] `#hashtag` federation

**Estimated effort:** ~2-3 weeks

---

## 9. Dependencies (New)

```
cryptography>=41.0       # RSA keys, signing
httpsig>=1.3             # HTTP Signature implementation
requests>=2.28           # Outgoing HTTP (already a transitive dep)
```

No heavy frameworks. No database required for default deployment. `pyld` deliberately omitted — not needed for practical Mastodon interop.

---

## 10. Open Questions

1. **Article vs Note:** Should we send blog posts as `Article` (semantically correct, Mastodon renders with title) or `Note` (more native-feeling in timelines)? **Recommendation:** `Article` for full posts, with a short `content` summary + link. Users see a card-like preview in their timeline.

2. **Custom FQN domain:** If the blog is at `blog.example.com` but the user wants `@user@example.com`, we need WebFinger on the apex domain to redirect. Document this but don't overcomplicate — suggest a simple reverse-proxy rule.

3. **Key rotation:** Not urgent. Document the manual process (generate new key, update actor, old signatures become unverifiable but that's fine for a blog).

4. **Rate limiting the inbox:** Important for production. A simple IP-based rate limit or signed-request-only gate is sufficient.

---

## 11. References

- [W3C ActivityPub Spec](https://www.w3.org/TR/activitypub/)
- [ActivityStreams 2.0 Vocabulary](https://www.w3.org/TR/activitystreams-vocabulary/)
- [Mastodon ActivityPub Documentation](https://docs.joinmastodon.org/spec/activitypub/)
- [How to implement a basic ActivityPub server](https://blog.joinmastodon.org/2018/06/how-to-implement-a-basic-activitypub-server/) (Mastodon blog)
- [How to make friends and verify requests](https://blog.joinmastodon.org/2018/07/how-to-make-friends-and-verify-requests/) (Mastodon blog)
- [Adding ActivityPub to your static site](https://paul.kinlan.me/adding-activity-pub-to-your-static-site/) (Paul Kinlan)
- [NodeInfo Protocol 2.1](https://nodeinfo.diaspora.software/protocol)
- [RFC 7033 — WebFinger](https://tools.ietf.org/html/rfc7033)
- [HTTP Signatures (draft-cavage)](https://tools.ietf.org/html/draft-cavage-http-signatures-12)
- [microblog.pub source](https://github.com/tsileo/microblog.pub) (reference implementation)
- [Takahē source](https://github.com/jointakahe/takahe) (reference implementation)
