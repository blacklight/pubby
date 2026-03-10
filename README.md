![Pubby](img/pubby-banner.svg)

[![build](https://github.com/blacklight/pubby/actions/workflows/build.yml/badge.svg)](https://github.com/blacklight/pubby/actions/workflows/build.yml)
[![Coverage Badge](https://app.codacy.com/project/badge/Coverage/7a135acdc1e3427ab381d91c0046790c)](https://app.codacy.com/gh/blacklight/pubby/dashboard?utm_source=gh&utm_medium=referral&utm_content=&utm_campaign=Badge_coverage)
[![Codacy Badge](https://app.codacy.com/project/badge/Grade/7a135acdc1e3427ab381d91c0046790c)](https://app.codacy.com/gh/blacklight/pubby/dashboard?utm_source=gh&utm_medium=referral&utm_content=&utm_campaign=Badge_grade)

A general-purpose Python library to add [ActivityPub](https://www.w3.org/TR/activitypub/)
federation support to your website.

## What is ActivityPub?

[ActivityPub](https://www.w3.org/TR/activitypub/) is a W3C standard for
decentralized social networking. Servers exchange JSON-LD activities (posts,
likes, follows, boosts) over HTTP, enabling federation across platforms like
Mastodon, Pleroma, Misskey, and others. It's the protocol that powers the
[Fediverse](https://en.wikipedia.org/wiki/Fediverse).

## What is Pubby?

Pubby is a framework-agnostic library that handles the ActivityPub plumbing so
you can focus on your app:

- **Inbox processing** — receive and dispatch Follow, Like, Announce, Create,
  Update, Delete activities
- **Outbox delivery** — concurrent fan-out to follower inboxes with retry and
  shared-inbox deduplication
- **HTTP Signatures** — sign outgoing requests and verify incoming ones
  (draft-cavage, using `cryptography` directly — no `httpsig` dependency)
- **Discovery** — WebFinger and NodeInfo 2.1 endpoints
- **Interaction storage** — followers, interactions, activities, actor cache
- **Framework adapters** — Flask, FastAPI, Tornado
- **Storage adapters** — SQLAlchemy (any supported database) and file-based JSON

## Installation

Base install:

```bash
pip install pubby
```

With extras:

```bash
pip install "pubby[db,flask]"        # SQLAlchemy + Flask
pip install "pubby[db,fastapi]"      # SQLAlchemy + FastAPI
pip install "pubby[db,tornado]"      # SQLAlchemy + Tornado
```

Available extras: `db`, `flask`, `fastapi`, `tornado`.

## Quick Start

### Flask

```bash
pip install "pubby[db,flask]"
```

```python
from flask import Flask
from pubby import ActivityPubHandler
from pubby.crypto import generate_rsa_keypair, export_private_key_pem
from pubby.storage.adapters.db import init_db_storage
from pubby.server.adapters.flask import bind_activitypub

app = Flask(__name__)
storage = init_db_storage("sqlite:////tmp/pubby.db")

# Generate a keypair (persist this — don't regenerate on restart!)
private_key, _ = generate_rsa_keypair()

handler = ActivityPubHandler(
    storage=storage,
    actor_config={
        "base_url": "https://example.com",
        "username": "blog",
        "name": "My Blog",
        "summary": "A blog with ActivityPub support",
    },
    private_key=private_key,
)

bind_activitypub(app, handler)
app.run()
```

### FastAPI

```bash
pip install "pubby[db,fastapi]"
```

```python
from fastapi import FastAPI
from pubby import ActivityPubHandler
from pubby.crypto import generate_rsa_keypair
from pubby.storage.adapters.db import init_db_storage
from pubby.server.adapters.fastapi import bind_activitypub

app = FastAPI()
storage = init_db_storage("sqlite:////tmp/pubby.db")
private_key, _ = generate_rsa_keypair()

handler = ActivityPubHandler(
    storage=storage,
    actor_config={
        "base_url": "https://example.com",
        "username": "blog",
        "name": "My Blog",
        "summary": "A blog with ActivityPub support",
    },
    private_key=private_key,
)

bind_activitypub(app, handler)
```

### Tornado

```bash
pip install "pubby[db,tornado]"
```

```python
from tornado.web import Application
from tornado.ioloop import IOLoop
from pubby import ActivityPubHandler
from pubby.crypto import generate_rsa_keypair
from pubby.storage.adapters.db import init_db_storage
from pubby.server.adapters.tornado import bind_activitypub

app = Application()
storage = init_db_storage("sqlite:////tmp/pubby.db")
private_key, _ = generate_rsa_keypair()

handler = ActivityPubHandler(
    storage=storage,
    actor_config={
        "base_url": "https://example.com",
        "username": "blog",
        "name": "My Blog",
        "summary": "A blog with ActivityPub support",
    },
    private_key=private_key,
)

bind_activitypub(app, handler)
app.listen(8000)
IOLoop.current().start()
```

### Registered Routes

All adapters register the same endpoints:

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/.well-known/webfinger` | WebFinger discovery |
| `GET` | `/.well-known/nodeinfo` | NodeInfo discovery |
| `GET` | `/nodeinfo/2.1` | NodeInfo 2.1 document |
| `GET` | `/ap/actor` | Actor profile (JSON-LD) |
| `POST` | `/ap/inbox` | Receive activities |
| `GET` | `/ap/outbox` | Outbox collection |
| `GET` | `/ap/followers` | Followers collection |
| `GET` | `/ap/following` | Following collection |

The `/ap` prefix is configurable via the `prefix` parameter on `bind_activitypub`.

### Mastodon-compatible API

Pubby ships a read-only subset of the
[Mastodon REST API](https://docs.joinmastodon.org/methods/) so that
Mastodon-compatible clients and crawlers can discover the instance, look up the
actor, list published statuses, and inspect followers.

Call `bind_mastodon_api` alongside `bind_activitypub`:

```python
from pubby.server.adapters.flask import bind_activitypub
from pubby.server.adapters.flask_mastodon import bind_mastodon_api

bind_activitypub(app, handler)
bind_mastodon_api(
    app,
    handler,
    title="My Blog",               # instance title (default: actor name)
    description="A cool blog",      # instance description (default: actor summary)
    contact_email="me@example.com", # optional contact e-mail
    software_name="MyApp",          # shown in /api/v1/instance version string
    software_version="1.0.0",
)
```

The same function is available for all three frameworks:

- `pubby.server.adapters.flask_mastodon.bind_mastodon_api`
- `pubby.server.adapters.fastapi_mastodon.bind_mastodon_api`
- `pubby.server.adapters.tornado_mastodon.bind_mastodon_api`

#### Mastodon API Routes

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/v1/instance` | Instance metadata (v1) |
| `GET` | `/api/v2/instance` | Instance metadata (v2) |
| `GET` | `/api/v1/instance/peers` | Peer domains from followers |
| `GET` | `/api/v1/accounts/lookup` | Resolve `acct:user@domain` → Account |
| `GET` | `/api/v1/accounts/:id` | Account by ID (`"1"` = local actor) |
| `GET` | `/api/v1/accounts/:id/statuses` | Paginated statuses for account |
| `GET` | `/api/v1/accounts/:id/followers` | Paginated followers list |
| `GET` | `/api/v1/statuses/:id` | Single status by ID |
| `GET` | `/nodeinfo/2.0` | NodeInfo 2.0 alias |
| `GET` | `/nodeinfo/2.0.json` | NodeInfo 2.0 `.json` alias |
| `GET` | `/nodeinfo/2.1.json` | NodeInfo 2.1 `.json` alias |

#### `bind_mastodon_api` Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `app` | framework app | *required* | Flask / FastAPI / Tornado application |
| `handler` | `ActivityPubHandler` | *required* | The handler instance |
| `title` | `str` | actor name | Instance title |
| `description` | `str` | actor summary | Instance description |
| `contact_email` | `str` | `""` | Contact e-mail |
| `software_name` | `str` | handler's `software_name` | Software name in version string |
| `software_version` | `str` | handler's `software_version` | Software version string |

#### Status & Account IDs

- The local actor always has account ID `"1"`.
- Status IDs are URL-safe base64 encodings of the AP object URL, making them
  deterministic and reversible.

## Publishing Content

Publish an article to all followers:

```python
from pubby import Object

article = Object(
    id="https://example.com/posts/hello-world",
    type="Article",
    name="Hello World",
    content="<p>My first federated post!</p>",
    url="https://example.com/posts/hello-world",
    attributed_to="https://example.com/ap/actor",
)

handler.publish_object(article)
```

To update or delete:

```python
# Update
handler.publish_object(updated_article, activity_type="Update")

# Delete
handler.publish_object(deleted_article, activity_type="Delete")
```

Delivery is concurrent (configurable via `max_delivery_workers`, default 10)
with automatic retry and exponential backoff on failure.

## Key Management

**Important:** your RSA keypair is your server's identity. Persist it — if you
regenerate it, other servers won't be able to verify your signatures.

```python
from pubby.crypto import (
    generate_rsa_keypair,
    export_private_key_pem,
    load_private_key,
)

# Generate once and save
private_key, public_key = generate_rsa_keypair()
pem = export_private_key_pem(private_key)

with open("/path/to/private_key.pem", "w") as f:
    f.write(pem)

# Load on startup
handler = ActivityPubHandler(
    storage=storage,
    actor_config={...},
    private_key_path="/path/to/private_key.pem",
)
```

## Custom Storage

If you don't want to use SQLAlchemy or the file-based adapter, extend
`ActivityPubStorage`:

```python
from pubby import ActivityPubStorage, Follower, Interaction

class MyStorage(ActivityPubStorage):
    def store_follower(self, follower: Follower):
        ...

    def remove_follower(self, actor_id: str):
        ...

    def get_followers(self) -> list[Follower]:
        ...

    def store_interaction(self, interaction: Interaction):
        ...

    def delete_interaction(self, source_actor_id: str, target_resource: str, interaction_type: str):
        ...

    def get_interactions(self, target_resource: str | None = None, interaction_type: str | None = None) -> list[Interaction]:
        ...

    def store_activity(self, activity_id: str, activity_data: dict):
        ...

    def get_activities(self, limit: int = 20, offset: int = 0) -> list[dict]:
        ...

    def cache_remote_actor(self, actor_id: str, actor_data: dict):
        ...

    def get_cached_actor(self, actor_id: str, max_age_seconds: int = 86400) -> dict | None:
        ...

handler = ActivityPubHandler(
    storage=MyStorage(),
    actor_config={...},
    private_key=private_key,
)
```

### File-based Storage

For apps that don't need a database (e.g. static-site generators):

```python
from pubby.storage.adapters.file import FileActivityPubStorage

storage = FileActivityPubStorage(data_dir="/var/lib/myapp/activitypub")
```

Data is stored as JSON files in a structured directory layout, with
thread-safe access via `RLock` per resource.

## Configuration Reference

### `ActivityPubHandler` Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `storage` | `ActivityPubStorage` | *required* | Storage backend |
| `actor_config` | `dict` | *required* | Actor configuration (see below) |
| `private_key` | key / str / bytes | — | RSA private key |
| `private_key_path` | str / Path | — | Path to PEM private key file |
| `on_interaction_received` | `Callable` | `None` | Callback on new interaction |
| `webfinger_domain` | `str` | from `base_url` | Domain for `acct:` URIs |
| `user_agent` | `str` | `"pubby/0.0.1"` | Outgoing User-Agent |
| `http_timeout` | `float` | `15.0` | HTTP request timeout (seconds) |
| `max_retries` | `int` | `3` | Delivery retry attempts |
| `max_delivery_workers` | `int` | `10` | Concurrent delivery threads |
| `auto_approve_quotes` | `bool` | `True` | Auto-send `QuoteAuthorization` for incoming quotes |
| `store_local_only` | `bool` | `False` | Only store interactions targeting local URLs or mentioning the actor |
| `local_base_urls` | `list[str]` | `None` | Base URLs considered "local" (defaults to actor's base URL) |
| `software_name` | `str` | `"pubby"` | NodeInfo software name |
| `software_version` | `str` | `"0.0.1"` | NodeInfo software version |

### `actor_config`

Pass an `ActorConfig` dataclass (recommended) or a plain `dict` (backwards compatible):

```python
from pubby import ActorConfig

config = ActorConfig(
    base_url="https://example.com",
    username="blog",
    name="My Blog",
    summary="A blog with ActivityPub support",
)

handler = ActivityPubHandler(storage=storage, actor_config=config, ...)
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `base_url` | `str` | *required* | Public base URL of your site |
| `username` | `str` | `"blog"` | Actor username (WebFinger handle) |
| `name` | `str` | *username* | Display name shown on remote instances |
| `summary` | `str` | `""` | Actor bio/description (HTML allowed) |
| `icon_url` | `str` | `""` | Avatar image URL |
| `actor_path` | `str` | `"/ap/actor"` | URL path to the actor endpoint |
| `type` | `str` | `"Person"` | ActivityPub actor type (`Person`, `Application`, `Service`) |
| `manually_approves_followers` | `bool` | `False` | Require explicit follow approval |
| `attachment` | `list[dict]` | `[]` | Profile metadata fields (see below) |

#### Profile Metadata (Verified Links)

Mastodon and other Fediverse software display profile metadata fields (the
key-value pairs shown on a user's profile page). These are passed as
`PropertyValue` attachments in the actor config:

```python
handler = ActivityPubHandler(
    storage=storage,
    actor_config={
        "base_url": "https://example.com",
        "username": "blog",
        "name": "My Blog",
        "summary": "A blog with ActivityPub support",
        "attachment": [
            {
                "type": "PropertyValue",
                "name": "Website",
                "value": '<a href="https://example.com" rel="me">https://example.com</a>',
            },
        ],
    },
    private_key=private_key,
)
```

For Mastodon's green verified-link checkmark to appear, the linked page must
contain a `<link rel="me" href="https://example.com/ap/actor">` tag pointing
back to the actor URL.

## Rendering Interactions

Pubby includes a Jinja2-based renderer for displaying interactions (replies,
likes, boosts) on your pages:

```python
from pubby import InteractionType

interactions = handler.storage.get_interactions(
    target_resource="https://example.com/posts/hello-world"
)

html = handler.render_interactions(interactions)
```

Then in your template:

```html
<article>
  <h1>Hello World</h1>
  <p>My first federated post!</p>
</article>

<section class="interactions">
  {{ interactions_html }}
</section>
```

`render_interactions` returns a safe `Markup` object with theme-aware styling.
You can also pass a custom Jinja2 template.

## Rate Limiting

Protect your inbox with the built-in per-IP sliding window rate limiter:

```python
from pubby import RateLimiter
from pubby.server.adapters.flask import bind_activitypub

rate_limiter = RateLimiter(max_requests=100, window_seconds=60)
bind_activitypub(app, handler, rate_limiter=rate_limiter)
```

## Interaction Callbacks

Get notified when interactions arrive:

```python
from pubby import Interaction

def on_interaction(interaction: Interaction):
    print(f"New {interaction.interaction_type}: {interaction.source_actor_id}")

handler = ActivityPubHandler(
    storage=storage,
    actor_config={...},
    private_key=private_key,
    on_interaction_received=on_interaction,
)
```

## API

### Data Model

#### `ActorConfig`

Typed configuration for an ActivityPub actor (replaces the old plain-dict approach):

```python
from pubby import ActorConfig

config = ActorConfig(
    base_url="https://example.com",
    username="blog",
    name="My Blog",
    summary="A federated blog",
    type="Person",
)
```

See [`actor_config`](#actor_config) in the Configuration Reference for the full field table.

#### `Object`

Represents an ActivityPub object (Note, Article, etc.):

```python
from pubby import Object

obj = Object(
    id="https://example.com/posts/1",
    type="Note",
    content="<p>Hello!</p>",
    url="https://example.com/posts/1",
    attributed_to="https://example.com/ap/actor",
    media_type="text/html",  # optional, serialized as "mediaType" in JSON-LD
    quote_control={"quotePolicy": "public"},  # optional, serialized as "quoteControl"
    quote_policy="public",  # optional, serialized as "quotePolicy"
    interaction_policy={
        "canQuote": {
            "automaticApproval": ["https://www.w3.org/ns/activitystreams#Public"],
            "manualApproval": [],
        },
    },  # optional, serialized as "interactionPolicy"
)
```

Key fields: `id`, `type`, `name`, `content`, `url`, `attributed_to`,
`published`, `updated`, `summary`, `to`, `cc`, `tag`, `media_type`,
`quote_control`, `quote_policy`, `interaction_policy`.

#### Quote policies (Mastodon)

Mastodon reads quote permissions from the ActivityPub object's
`interactionPolicy.canQuote` field. To allow public quoting without
approval, set `automaticApproval` to the public collection and leave
`manualApproval` empty:

```python
obj = Object(
    ...,
    interaction_policy={
        "canQuote": {
            "automaticApproval": ["https://www.w3.org/ns/activitystreams#Public"],
            "manualApproval": [],
        }
    },
)
```

If you include a non-empty `manualApproval`, Mastodon will create a
pending quote request instead of immediately allowing it.

#### QuoteAuthorization (FEP-044f)

Advertising `interactionPolicy.canQuote` is **advisory only**. Mastodon
and other servers won't clear the "pending" state on a remote quote
until they can verify a `QuoteAuthorization` stamp from the quoted
post's author.

The approval flow defined by [FEP-044f](https://codeberg.org/fediverse/fep/src/branch/main/fep/044f/fep-044f.md) works as follows:

1. The remote server sends a `QuoteRequest` activity to your inbox.
2. Pubby responds with an `Accept` activity whose `result` points to a
   dereferenceable `QuoteAuthorization` URL.
3. The remote server fetches the `QuoteAuthorization` at that URL and
   clears the pending state.

Pubby handles this automatically. The `QuoteAuthorization` objects are
stored and served at `<prefix>/quote_authorizations/<id>`.

Additionally, incoming `Create` activities that contain a `quote`,
`quoteUrl`, or `_misskey_quote` field are stored as
`InteractionType.QUOTE` interactions.

This behaviour is controlled by the `auto_approve_quotes` parameter
(default `True`). Set it to `False` to ignore `QuoteRequest` activities:

```python
handler = ActivityPubHandler(
    ...,
    auto_approve_quotes=False,
)
```

#### `Mention`

A resolved `@user@domain` mention:

```python
from pubby import Mention

m = Mention(username="alice", domain="mastodon.social", actor_url="https://mastodon.social/users/alice")
m.acct        # "@alice@mastodon.social"
m.to_tag()    # {"type": "Mention", "href": "https://mastodon.social/users/alice", "name": "@alice@mastodon.social"}
```

### WebFinger Client

#### `resolve_actor_url(username, domain, *, timeout=10) -> str`

Resolve the ActivityPub actor URL for `@username@domain` via
[WebFinger](https://www.rfc-editor.org/rfc/rfc7033) (RFC 7033). Returns the
`self` link with an `application/*` media type, or falls back to
`https://{domain}/@{username}` on failure.

```python
from pubby import resolve_actor_url

url = resolve_actor_url("alice", "mastodon.social")
# "https://mastodon.social/@alice"

url = resolve_actor_url("bob", "pleroma.example")
# "https://pleroma.example/users/bob"
```

This works across all ActivityPub implementations (Mastodon, Pleroma, Akkoma,
Misskey, etc.) since WebFinger is the standard discovery mechanism.

#### `extract_mentions(text, *, timeout=10) -> list[Mention]`

Find all `@user@domain` patterns in a text string, resolve each via WebFinger,
and return a list of `Mention` objects. Duplicates are deduplicated
(case-insensitive).

```python
from pubby import extract_mentions

text = "Hello @alice@mastodon.social and @bob@pleroma.example!"
mentions = extract_mentions(text)

# Build ActivityPub tag array and cc list:
tags = [m.to_tag() for m in mentions]
cc = [m.actor_url for m in mentions]
```

### Publishing

#### `handler.publish_object(obj, activity_type="Create")`

Publish an `Object` to all followers. Fan-out is concurrent with automatic
retry and shared-inbox deduplication.

```python
handler.publish_object(article)                              # Create
handler.publish_object(updated_article, activity_type="Update")
handler.publish_object(deleted_article, activity_type="Delete")
```

#### `handler.publish_actor_update()`

Push the current actor profile to all followers. Call this after changing
any actor properties (name, summary, icon, attachment/fields) so remote
instances refresh their cached copy. This is the standard mechanism used
by Mastodon when a user edits their profile.

```python
handler.publish_actor_update()
```

The method builds an `Update` activity whose `object` is the full actor
document, and fans it out to every follower inbox.

### Storage

#### `ActivityPubStorage`

Abstract base class. Built-in adapters:

- `pubby.storage.adapters.db.init_db_storage(url)` — SQLAlchemy (any DB)
- `pubby.storage.adapters.file.FileActivityPubStorage(data_dir)` — JSON files

See [Custom Storage](#custom-storage) for implementing your own.

### Crypto

```python
from pubby.crypto import generate_rsa_keypair, export_private_key_pem, load_private_key

private_key, public_key = generate_rsa_keypair()
pem = export_private_key_pem(private_key)
private_key = load_private_key("/path/to/key.pem")
```

## Tests

```bash
pip install -e ".[test]"
pytest tests
```

## Development

```bash
pip install -e ".[dev]"
pre-commit install
pre-commit run --all-files
```

## License

[AGPL-3.0-or-later](https://www.gnu.org/licenses/agpl-3.0.html)
