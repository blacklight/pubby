![Pubby](img/pubby-banner.svg)

[![build](https://github.com/blacklight/pubby/actions/workflows/build.yml/badge.svg)](https://github.com/blacklight/pubby/actions/workflows/build.yml)
[![Coverage Badge](https://app.codacy.com/project/badge/Coverage/7a135acdc1e3427ab381d91c0046790c)](https://app.codacy.com/gh/blacklight/pubby/dashboard?utm_source=gh&utm_medium=referral&utm_content=&utm_campaign=Badge_coverage)
[![Codacy Badge](https://app.codacy.com/project/badge/Grade/7a135acdc1e3427ab381d91c0046790c)](https://app.codacy.com/gh/blacklight/pubby/dashboard?utm_source=gh&utm_medium=referral&utm_content=&utm_campaign=Badge_grade)
[![Issues](https://img.shields.io/gitea/issues/open/blacklight/pubby?gitea_url=https://git.platypush.tech)](https://git.platypush.tech/blacklight/pubby/issues)
[![Last Commit](https://img.shields.io/github/last-commit/blacklight/pubby.svg)](https://git.fabiomanganiello.com/pubby/commits/branch/main)

[![License](https://img.shields.io/github/license/blacklight/pubby.svg)](https://git.fabiomanganiello.com/pubby/src/branch/main/LICENSE.txt)
[![pip version](https://img.shields.io/pypi/v/pubby.svg?style=flat)](https://pypi.python.org/pypi/pubby/)
[![Github stars](https://img.shields.io/github/stars/blacklight/pubby?style=flat&logo=Github)](https://github.com/blacklight/pubby)
[![Github forks](https://img.shields.io/github/forks/blacklight/pubby?style=flat&logo=Github)](https://github.com/blacklight/pubby)
[![Sponsor](https://img.shields.io/github/sponsors/blacklight)](https://github.com/sponsors/blacklight)

<!--TOC-->

- [What is ActivityPub?](#what-is-activitypub)
- [What is Pubby?](#what-is-pubby)
- [Installation](#installation)
- [Quick Start](#quick-start)
  - [Flask](#flask)
  - [FastAPI](#fastapi)
  - [Tornado](#tornado)
  - [Registered Routes](#registered-routes)
  - [Mastodon-compatible API](#mastodon-compatible-api)
    - [Mastodon API Routes](#mastodon-api-routes)
    - [`bind_mastodon_api` Parameters](#bind_mastodon_api-parameters)
    - [Status & Account IDs](#status--account-ids)
- [Publishing Content](#publishing-content)
  - [Custom Delivery](#custom-delivery)
- [Rendering Plain-Text Content](#rendering-plain-text-content)
- [Key Management](#key-management)
- [Custom Storage](#custom-storage)
  - [Async Database URLs](#async-database-urls)
  - [File-based Storage](#file-based-storage)
  - [Multi-actor Support](#multi-actor-support)
    - [Upgrading from single-actor deployments](#upgrading-from-single-actor-deployments)
- [Configuration Reference](#configuration-reference)
  - [`ActivityPubHandler` Parameters](#activitypubhandler-parameters)
  - [`actor_config`](#actor_config)
    - [Profile Metadata (Verified Links)](#profile-metadata-verified-links)
  - [Instance Allow/Block Lists](#instance-allowblock-lists)
- [Rendering Interactions](#rendering-interactions)
- [Rate Limiting](#rate-limiting)
- [Interaction Callbacks](#interaction-callbacks)
  - [Private Messages](#private-messages)
- [Strict Attribution](#strict-attribution)
- [Signature Verification](#signature-verification)
- [API](#api)
  - [Data Model](#data-model)
    - [`ActorConfig`](#actorconfig)
    - [`Object`](#object)
    - [`Interaction`](#interaction)
    - [`Follower`](#follower)
    - [Quote policies (Mastodon)](#quote-policies-mastodon)
    - [QuoteAuthorization (FEP-044f)](#quoteauthorization-fep-044f)
    - [`Mention`](#mention)
  - [WebFinger Client](#webfinger-client)
    - [`resolve_actor_url(username, domain, *, timeout=10) -> str`](#resolve_actor_urlusername-domain--timeout10---str)
    - [`extract_mentions(text, *, timeout=10) -> list\[Mention\]`](#extract_mentionstext--timeout10---listmention)
  - [Actor / Inbox Resolution](#actor--inbox-resolution)
    - [`resolve_actor_inbox(actor_url, storage, *, private_key=None, key_id=None, allowed_instances=None, blocked_instances=None, user_agent=None, timeout=10.0) -> str | None`](#resolve_actor_inboxactor_url-storage--private_keynone-key_idnone-allowed_instancesnone-blocked_instancesnone-user_agentnone-timeout100---str--none)
    - [`extract_actor_inbox(actor_data) -> str | None`](#extract_actor_inboxactor_data---str--none)
  - [Audience Helpers — `pubby.audience`](#audience-helpers--pubbyaudience)
  - [Publishing](#publishing)
    - [`handler.publish_object(obj, activity_type="Create")`](#handlerpublish_objectobj-activity_typecreate)
    - [`handler.publish_activity(activity)`](#handlerpublish_activityactivity)
    - [Module-level builders](#module-level-builders)
    - [`handler.publish_actor_update(document=None)`](#handlerpublish_actor_updatedocumentnone)
  - [Content Rendering](#content-rendering)
  - [Storage](#storage)
    - [`ActivityPubStorage`](#activitypubstorage)
    - [`get_interaction_by_object_id(object_id, status=CONFIRMED)`](#get_interaction_by_object_idobject_id-statusconfirmed)
    - [DB Storage: Mention Index](#db-storage-mention-index)
  - [Migrations](#migrations)
    - [`backfill_mentions(storage, dry_run=False)`](#backfill_mentionsstorage-dry_runfalse)
    - [`backfill_object_id_index(storage, dry_run=False)`](#backfill_object_id_indexstorage-dry_runfalse)
  - [Crypto](#crypto)
- [Tests](#tests)
- [Development](#development)
- [License](#license)

<!--TOC-->

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

### Custom Delivery

To route deliveries through your own task queue (Celery, RQ, …) instead of
the built-in thread pool, pass a `deliver` callable to the handler. Pubby
still stores the activity and computes the deduplicated inbox set — with
shared-inbox preference and instance allow/block filtering — then invokes
your callable once per inbox:

```python
from pubby import ActivityPubHandler

def deliver(inbox_url, activity):
    send_activity.delay(inbox_url, activity)   # e.g. a Celery task

handler = ActivityPubHandler(
    storage=storage,
    actor_config={...},
    private_key=private_key,
    deliver=deliver,
)
```

Inside the task, `pubby.deliver_activity` performs a single signed POST and
returns the HTTP status code so your worker can apply its own retry policy:

```python
from pubby import deliver_activity

@app.task(bind=True, max_retries=5)
def send_activity(self, inbox_url, activity):
    status = deliver_activity(
        activity,
        inbox_url,
        key_id=handler.key_id,
        private_key=private_key,
    )
    if status == 429 or status >= 500:
        raise self.retry(countdown=60)
```

`deliver_activity` never raises on 4xx/5xx responses — it returns the status
code. Network-level failures (connection errors, timeouts) still raise the
underlying `requests` exception. Applications that build a fully custom
fan-out can reuse `pubby.collect_inboxes(followers)` to get the same
shared-inbox-preferred, deduplicated inbox list `publish()` uses.

## Rendering Plain-Text Content

`pubby.content` turns plain user text into safe ActivityPub HTML and `Hashtag`
tags. It is stdlib-only and independent of the inbound HTML sanitiser in
`pubby.render`.

Render a post and publish it:

```python
from pubby import Object, build_hashtag_tags, render_post_html

hashtag_url = lambda name: f"https://example.com/tags/{name}"

rc = render_post_html(
    "Hello fediverse! #intro https://example.com/about",
    hashtag_url,
)

post = Object(
    id="https://example.com/posts/1",
    type="Note",
    content=rc.html,
    url="https://example.com/posts/1",
    attributed_to="https://example.com/ap/actor",
    tag=build_hashtag_tags(rc.hashtags, hashtag_url),
)

handler.publish_object(post)
```

For object *dictionaries* — e.g. `Audio`/`Video` payloads you build by hand —
`set_object_content` does the same in one call: it renders the text into
`obj["content"]` and appends any detected hashtags to `obj["tag"]`,
deduplicating against tags you already set:

```python
from pubby import set_object_content

obj = {
  "type": "Audio",
  "id": "https://example.com/tracks/1",
  "tag": build_hashtag_tags(["jazz"], hashtag_url),
}
set_object_content(obj, "Live set #jazz #fusion", hashtag_url)
# obj["content"] holds rendered HTML; obj["tag"] now includes #fusion
```

For bios that should only linkify URLs, use `render_bio_html` on the `summary`
field:

```python
from pubby import ActivityPubHandler, render_bio_html

handler = ActivityPubHandler(
    storage=storage,
    actor_config={
        "base_url": "https://example.com",
        "username": "blog",
        "summary": render_bio_html("Find me at https://example.com/links."),
    },
    private_key=private_key,
)
```

See the [Content Rendering](#content-rendering) API reference for the full
list of helpers.

## Key Management

**Important:** your RSA keypair is your server's identity. Persist it — if you
regenerate it, other servers won't be able to verify your signatures.

The simplest option is `ensure_private_key_file`, which generates an RSA-2048
keypair and writes it with `0o600` permissions the first time, then reuses the
existing file on subsequent runs:

```python
from pubby.crypto import ensure_private_key_file

handler = ActivityPubHandler(
    storage=storage,
    actor_config={...},
    private_key_path=ensure_private_key_file("/var/lib/myapp/actor.pem"),
)
```

Or manage the keypair yourself:

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

    def remove_follower(self, actor_id: str, target_actor_id: str = ""):
        ...

    def get_followers(self, actor_id: str | None = None) -> list[Follower]:
        ...

    def store_interaction(self, interaction: Interaction):
        ...

    def delete_interaction(self, source_actor_id: str, target_resource: str, interaction_type: str):
        ...

    def get_interactions(self, target_resource: str | None = None, interaction_type: str | None = None) -> list[Interaction]:
        ...

    def get_interactions_mentioning(self, actor_url: str, interaction_type: str | None = None) -> list[Interaction]:
        ...  # Optional: returns interactions where actor_url is in mentioned_actors

    def get_interaction_by_object_id(self, object_id: str, status: InteractionStatus = InteractionStatus.CONFIRMED) -> Interaction | None:
        ...  # Optional: look up interaction by remote object URL

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

### Async Database URLs

`DbActivityPubStorage` is synchronous. If your application runs on an async
database stack, pass its URL to `init_db_storage` directly — known async
drivers are converted automatically (`sqlite+aiosqlite` → `sqlite`,
`postgresql+asyncpg` → `postgresql+psycopg2`):

```python
storage = init_db_storage("sqlite+aiosqlite:////tmp/pubby.db")
```

Unknown async drivers raise `ValueError`. Extend or override the default
mapping with `driver_map` (e.g. for psycopg3):

```python
storage = init_db_storage(
    "postgresql+asyncpg://user:pass@db.example.com/mydb",
    driver_map={"postgresql+asyncpg": "postgresql+psycopg"},
)
```

`to_sync_url(url, driver_map=None)` is also exported if you only need the
converted URL:

```python
from pubby.storage.adapters.db import to_sync_url

to_sync_url("sqlite+aiosqlite:////tmp/pubby.db")  # "sqlite:////tmp/pubby.db"
```

### File-based Storage

For apps that don't need a database (e.g. static-site generators):

```python
from pubby.storage.adapters.file import FileActivityPubStorage

storage = FileActivityPubStorage(data_dir="/var/lib/myapp/activitypub")
```

Data is stored as JSON files in a structured directory layout, with
thread-safe access via `RLock` per resource.

**Automatic schema migrations**: On initialization, the storage checks a
`.schema_version` file and automatically runs any pending migrations
(e.g., rebuilding indexes). To disable this:

```python
storage = FileActivityPubStorage(data_dir="...", auto_migrate=False)
```

### Multi-actor Support

Pubby now supports multiple local actors sharing the same storage backend.
A `Follower` record has a `target_actor_id` field that identifies which local
actor is being followed. The `InboxProcessor` extracts this from the
`Follow.object` field and `OutboxProcessor.publish()` fans out only to the
publishing actor's followers.

```python
from pubby import ActivityPubHandler
from pubby.storage.adapters.db import init_db_storage

storage = init_db_storage("sqlite:////tmp/pubby.db")

alice = ActivityPubHandler(
    storage=storage,
    actor_config={
        "base_url": "https://alice.example.com",
        "username": "alice",
    },
    private_key=alice_key,
)

bob = ActivityPubHandler(
    storage=storage,
    actor_config={
        "base_url": "https://bob.example.com",
        "username": "bob",
    },
    private_key=bob_key,
)
```

When implementing a custom storage backend, honour the `actor_id` parameter
in `get_followers()` and the `target_actor_id` parameter in `remove_follower()`.
Legacy followers with an empty `target_actor_id` are treated as unassigned and
returned for any actor until they are backfilled.

#### Upgrading from single-actor deployments

When upgrading an existing single-actor instance, existing followers have an
empty `target_actor_id`. They remain visible to all local actors (via
`get_followers(actor_id=...)` and `get_followers_collection()`) until the
application backfills them with the correct actor URL.

Backfill existing followers by removing the unassigned record and re-storing
it with the correct `target_actor_id`:

```python
local_actor = "https://example.com/ap/actor"
for follower in storage.get_followers():
    if not follower.target_actor_id:
        storage.remove_follower(follower.actor_id)
        follower.target_actor_id = local_actor
        storage.store_follower(follower)
```

After backfilling, each follower appears only in the collection of the actor
they follow. `remove_follower(actor_id)` without a `target_actor_id` removes
all follow records from the given remote actor, so multi-actor code should
always pass `target_actor_id` for precise removal.

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
| `user_agent` | `str` | `pubby/{__version__}` | Outgoing User-Agent |
| `http_timeout` | `float` | `15.0` | HTTP request timeout (seconds) |
| `max_retries` | `int` | `3` | Delivery retry attempts |
| `max_delivery_workers` | `int` | `10` | Concurrent delivery threads |
| `auto_approve_quotes` | `bool` | `True` | Auto-send `QuoteAuthorization` for incoming quotes |
| `store_local_only` | `bool` | `False` | Only store interactions targeting local URLs or mentioning the actor |
| `local_base_urls` | `list[str]` | `None` | Base URLs considered "local" (defaults to actor's base URL) |
| `software_name` | `str` | `"pubby"` | NodeInfo software name |
| `software_version` | `str` | `pubby.__version__` | NodeInfo software version |
| `async_delivery` | `bool` | `True` | Run delivery fan-out in background thread (non-blocking) |
| `allowed_instances` | `Collection[str]` | `None` | Only federate with these instance domains (allow-list) |
| `blocked_instances` | `Collection[str]` | `None` | Never federate with these instance domains (block-list) |
| `deliver` | `Callable[[str, dict], None]` | `None` | Custom delivery callable invoked per inbox (see Custom Delivery) |
| `strict_attribution` | `bool` | `False` | Reject inbound `Create`/`Update` objects whose `attributedTo` or `id` authority does not match the delivering actor (see Strict Attribution) |

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

You can also build attachments safely with `property_value_attachment`, which
renders the URL as a `rel="me"` link when valid and escaped text otherwise:

```python
from pubby import ActivityPubHandler, property_value_attachment

handler = ActivityPubHandler(
    storage=storage,
    actor_config={
        "base_url": "https://example.com",
        "username": "blog",
        "name": "My Blog",
        "summary": "A blog with ActivityPub support",
        "attachment": [
            property_value_attachment("Website", "https://example.com"),
            property_value_attachment("GitHub", "https://github.com/me", label="@me"),
        ],
    },
    private_key=private_key,
)
```

### Instance Allow/Block Lists

Restrict federation to a known set of instances — or block specific ones —
with the `allowed_instances` and `blocked_instances` handler parameters:

```python
handler = ActivityPubHandler(
    storage=storage,
    actor_config={...},
    private_key=private_key,
    # Only federate with these instances (optional)
    allowed_instances=["mastodon.social", "pixelfed.social"],
    # Never federate with these instances (optional)
    blocked_instances=["spam.example"],
)
```

Both lists accept bare domains or URLs — comparisons are case-insensitive and
ignore schemes, paths, and ports. When `allowed_instances` is non-empty, only
those instances are permitted; `blocked_instances` always wins over
`allowed_instances`. Leaving both unset (the default) disables filtering.

The policy is enforced in two places:

- **Inbound**: incoming activities whose `actor` domain is blocked (or not in
  the allow-list) are dropped *before* HTTP signature verification, so
  rejected instances never trigger an actor key fetch.
- **Outbound**: inboxes on blocked/non-allowed domains are skipped during
  delivery fan-out, and recipient actor documents on those domains are never
  fetched.

The same helpers are available for application-level checks:

```python
from pubby import extract_domain, is_domain_blocked, normalize_domain

is_domain_blocked(
    "https://spam.example/users/bot",
    allowed=["good.example"],
    blocked=["spam.example"],
)  # True

extract_domain("https://spam.example/users/bot")  # "spam.example"
normalize_domain("HTTPS://Example.COM:8443/path")  # "example.com"
```

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

### Private Messages

Only **publicly addressed** interactions (those with
`https://www.w3.org/ns/activitystreams#Public` — or the `Public`/`as:Public`
aliases — in `to`, `cc`, `bto`, or `bcc`) are persisted
to storage. This includes both public and unlisted posts. Private/direct
messages and followers-only posts are **not stored**, preventing them from
appearing in public contexts like blog comments.

However, the `on_interaction_received` callback is still invoked for **all**
interactions, including private ones. This allows applications to send
notifications (e.g., email alerts) for direct messages without exposing them
publicly.

## Strict Attribution

HTTP signatures prove *who delivered* an activity — not that the delivered
object was authored by that actor. Without a sanity check, a signed actor on
one host can publish an object claiming a foreign `id` or `attributedTo`,
letting your server index or re-federate a forgery.

Set `strict_attribution=True` on `ActivityPubHandler` to reject inbound
`Create` and `Update` objects where a non-empty `attributedTo` does not name
the delivering actor, or where the object `id` is hosted on a different
authority. Mismatched objects are logged and dropped before the interaction
callback or storage — they never surface as an HTTP error.

```python
handler = ActivityPubHandler(
    storage=storage,
    actor_config={...},
    private_key=private_key,
    strict_attribution=True,
)
```

The check is opt-in (default `False`) because relays, reverse proxies, and
account-migration deployments can legitimately deliver objects whose id is
hosted on a different host than the actor. The same validation is available
standalone as `pubby.validate_attribution(actor, obj)`, which raises
`pubby.AttributionMismatch` on failure.

## Signature Verification

Inbound HTTP signatures do two things: prove the request was signed by the
key identified in the signature's `keyId`, and bind that signer to the
activity's `actor`. When the `keyId` resolves to a different actor than
`activity.actor` — e.g. an instance-level signing key — the claimed actor's
document must advertise the key (`publicKey.id` equal to the `keyId`;
`publicKey` may be a list), otherwise the delivery is rejected with
`SignatureVerificationError` (HTTP 401 on the bundled routes).

Verification is **required by default**: calling `process_inbox_activity`
(or `InboxProcessor.process`) without request headers raises
`SignatureVerificationError` rather than silently skipping the check. Pass
`skip_verification=True` when processing activities outside an HTTP
context (tests, replayed queues) — the opt-out must be explicit.

**Relays.** Relays that wrap inbound activities in a relay-authored
`Announce` (the LitePub/FEP-1b12 style, used by most relay software) work
unchanged — the relay signs its own `Announce`, and the announced object
is dereferenced from its origin. Relays that instead *forward* the
original activity body signed with the relay's key are rejected: the
HTTP signer does not match `activity.actor`. Mastodon accepts such
forwarded payloads only when they carry a verifiable Linked Data
signature (`RsaSignature2017`) by the claimed actor; pubby does not
verify LD signatures, so forwarded content without an `Announce`
wrapper is not supported.

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
`quote_control`, `quote_policy`, `interaction_policy`, `duration`.

`url` also accepts a list of `Link` dicts, and `attributed_to` a list of
actor URLs — the shapes used by federated media objects such as `Audio`:

```python
from pubby import Object, format_duration

audio = Object(
    id="https://example.com/tracks/1",
    type="Audio",
    name="Track title",
    url=[
        {
            "type": "Link",
            "href": "https://example.com/files/1/download",
            "mediaType": "audio/mpeg",
        },
        {
            "type": "Link",
            "href": "https://example.com/tracks/1",
            "mediaType": "text/html",
        },
    ],
    attributed_to=[
        "https://example.com/artists/1",
        "https://example.com/ap/actor",
    ],
    duration=format_duration(185),  # "PT3M5S", serialized as "duration"
    attachment=[
        {
            "type": "Document",
            "mediaType": "audio/mpeg",
            "url": "https://example.com/files/1/download",
            "name": "Track title",
        }
    ],
)

handler.publish_object(audio)
```

#### `Interaction`

Represents a stored interaction from the fediverse (reply, like, boost, mention, quote):

```python
from pubby import Interaction, InteractionType, InteractionStatus

interaction = Interaction(
    source_actor_id="https://mastodon.social/users/alice",
    target_resource="https://example.com/posts/1",
    interaction_type=InteractionType.REPLY,
    content="<p>Great post!</p>",
    author_name="Alice",
    author_url="https://mastodon.social/@alice",
    mentioned_actors=["https://example.com/ap/actor"],
)
```

| Field | Type | Description |
|-------|------|-------------|
| `source_actor_id` | `str` | Actor URL of the interaction author |
| `target_resource` | `str` | URL of the resource being interacted with |
| `interaction_type` | `InteractionType` | `REPLY`, `LIKE`, `BOOST`, `MENTION`, or `QUOTE` |
| `activity_id` | `str` | ActivityPub activity ID |
| `object_id` | `str` | ActivityPub object ID (for replies/quotes) |
| `content` | `str` | HTML content (for replies/quotes/mentions) |
| `author_name` | `str` | Display name of the author |
| `author_url` | `str` | Profile URL of the author |
| `author_photo` | `str` | Avatar URL of the author |
| `published` | `datetime` | When the interaction was published |
| `status` | `InteractionStatus` | `PENDING`, `CONFIRMED`, or `DELETED` |
| `metadata` | `dict` | Additional data (e.g. `raw_object`) |
| `mentioned_actors` | `list[str]` | Actor URLs mentioned in this interaction |

#### `Follower`

Represents a stored follower (a remote actor that follows a local actor):

```python
from pubby import Follower

follower = Follower(
    actor_id="https://mastodon.social/users/alice",
    inbox="https://mastodon.social/users/alice/inbox",
    shared_inbox="https://mastodon.social/inbox",
    target_actor_id="https://example.com/ap/actor",
)
```

| Field | Type | Description |
|-------|------|-------------|
| `actor_id` | `str` | Remote actor URL |
| `inbox` | `str` | Inbox URL of the remote actor |
| `shared_inbox` | `str` | Shared inbox URL (optional) |
| `followed_at` | `datetime` | When the follow was received |
| `actor_data` | `dict` | Cached actor document |
| `target_actor_id` | `str` | Local actor URL being followed (empty for unassigned/legacy) |

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

For raw object documents (or to stamp the policy on an `Object`'s
serialized dict), `pubby.allow_public_quotes` applies the same policy —
available as the `pubby.PUBLIC_QUOTE_POLICY` constant:

```python
from pubby import allow_public_quotes

allow_public_quotes(note_document)   # sets note_document["interactionPolicy"]
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
`quoteUri`, `quoteUrl`, or `_misskey_quote` field are stored as
`InteractionType.QUOTE` interactions. The same spellings are recognized
by `pubby.extract_quote_target`, and `pubby.set_quote_target` stamps all
of them on an outgoing object so every compatible server recognizes the
quote:

```python
from pubby import extract_quote_target, set_quote_target

set_quote_target(note_document, "https://remote.example.com/post/42")
extract_quote_target(incoming_object)   # → quoted URL or None
```

This behaviour is controlled by the `auto_approve_quotes` parameter
(default `True`). Set it to `False` to ignore `QuoteRequest` activities:

```python
handler = ActivityPubHandler(
    ...,
    auto_approve_quotes=False,
)
```

For the outgoing side, `build_quote_request_activity` builds the
`QuoteRequest` activity to deliver to the quoted author's inbox (before
the quoting post's `Create`), and `build_quote_authorization` builds the
authorization document — useful when an application approves a quote of
its own post itself rather than going through a remote `Accept`:

```python
from pubby import build_quote_authorization, build_quote_request_activity

request = build_quote_request_activity(
    actor_id="https://example.com/ap/actor",
    quoted_object_id="https://remote.example.com/post/42",
    instrument=quoting_note_document,      # the quoting Note, embedded
    target_actor_id="https://remote.example.com/users/bob",
)
handler.publish_activity(request)        # delivered to bob's inbox
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

### Actor / Inbox Resolution

#### `resolve_actor_inbox(actor_url, storage, *, private_key=None, key_id=None, allowed_instances=None, blocked_instances=None, user_agent=None, timeout=10.0) -> str | None`

Resolve a remote actor's inbox by fetching their actor document, with optional
HTTP Signature support, domain filtering, and actor-cache integration.

```python
from pubby import resolve_actor_inbox
from pubby.storage.adapters.db import init_db_storage

storage = init_db_storage("sqlite:////tmp/pubby.db")
inbox = resolve_actor_inbox(
    "https://remote.example.com/users/bob",
    storage,
    private_key=private_key,
    key_id="https://example.com/ap/actor#main-key",
    allowed_instances={"remote.example"},
    user_agent="MyApp/1.0.0",
)
# "https://remote.example.com/inbox" or None on failure
```

- Consults the actor cache first (`storage.get_cached_actor`).
- Signs the GET when both `private_key` and `key_id` are provided; accepts an
  `RSAPrivateKey` object or a PEM string/bytes.
- Filters blocked/non-allowed instances before making a network call.
- Returns `endpoints.sharedInbox` when available, otherwise `inbox`.

#### `extract_actor_inbox(actor_data) -> str | None`

Extract the preferred inbox from a raw actor document:

```python
from pubby.client import extract_actor_inbox

inbox = extract_actor_inbox(actor_data)
```

### Audience Helpers — `pubby.audience`

Pure parsers for ActivityPub addressing fields, shared by the inbox processor
and available to applications that need the same interpretation:

```python
from pubby import PUBLIC_URIS, addressees, is_public, mentioned_actors

addressees({"to": "https://a.example/u", "cc": ["https://b.example/u"]})
# {"https://a.example/u", "https://b.example/u"}

is_public({"cc": ["https://www.w3.org/ns/activitystreams#Public"]})  # True

mentioned_actors({"tag": [{"type": "Mention", "href": "https://a.example/u"}]})
# ["https://a.example/u"]
```

- `PUBLIC_URIS` — the recognized public-audience identifiers: the canonical
  `https://www.w3.org/ns/activitystreams#Public` plus the `Public` and
  `as:Public` aliases. It is an immutable `frozenset` — do not mutate it;
  matching is exact string equality.
- `addressees(obj_or_activity)` — union of string values in `to`, `cc`,
  `bto`, and `bcc`, returned as an unordered `set`. Only the supplied mapping
  is inspected — it never descends into an activity's embedded `object`.
  Missing or malformed fields are ignored.
- `is_public(obj_or_activity)` — `True` when any `PUBLIC_URIS` member appears
  in the mapping's audience fields.
- `mentioned_actors(obj_data)` — actor URLs from `Mention` tags, in
  first-seen order with duplicates removed; malformed and non-`Mention`
  entries are skipped.

### Publishing

#### `handler.publish_object(obj, activity_type="Create")`

Publish an `Object` to all followers. Fan-out is concurrent with automatic
retry and shared-inbox deduplication.

```python
handler.publish_object(article)                              # Create
handler.publish_object(updated_article, activity_type="Update")
handler.publish_object(deleted_article, activity_type="Delete")
```

#### `handler.publish_activity(activity)`

Publish a pre-built activity dict as-is, without wrapping it in a
Create/Update envelope. Use this for activity types that are not Object
wrappers — `Like`, `Announce`, `Undo`, `Follow`, etc.

The `OutboxProcessor` provides builders for common activity types:

```python
# Like a remote post
like = handler.outbox.build_like_activity("https://remote.example.com/post/42")
handler.publish_activity(like)

# Boost (Announce) a remote post
boost = handler.outbox.build_announce_activity("https://remote.example.com/post/42")
handler.publish_activity(boost)

# Update a local post (e.g. after an edit)
handler.publish_activity(handler.outbox.build_update_activity(updated_object))

# Delete a local post
handler.publish_activity(
    handler.outbox.build_delete_activity("https://example.com/posts/hello-world")
)

# Undo the like
undo = handler.outbox.build_undo_activity(like)
handler.publish_activity(undo)
```

Available builders on `handler.outbox`:

| Builder | Returns |
|---|---|
| `build_delete_activity(object_id)` | `Delete` activity dict wrapping a `Tombstone` |
| `build_like_activity(object_url, *, activity_id=None, published=None)` | `Like` activity dict |
| `build_announce_activity(object_url, *, activity_id=None, published=None)` | `Announce` (boost) activity dict |
| `build_undo_activity(inner_activity)` | `Undo` activity dict wrapping any activity |
| `build_update_activity(obj)` | `Update` activity dict wrapping the updated `Object` |
| `build_quote_request_activity(quoted_object_id, instrument, target_actor_id, *, activity_id=None, published=None)` | FEP-044f `QuoteRequest` activity dict |

`build_undo_activity` is intentionally generic — it works for
`Undo Like`, `Undo Announce`, `Undo Follow`, etc.

`build_quote_request_activity` addresses the request to
`target_actor_id` — the quoted object's author — so `publish_activity`
delivers it to their inbox as a direct recipient. See
[QuoteAuthorization (FEP-044f)](#quoteauthorization-fep-044f) for the
full outgoing-quote flow.

#### Module-level builders

You can also build payloads without a handler. This is useful when your
application controls addressing, timestamps, or activity IDs itself:

```python
from pubby import (
    build_announce_activity,
    build_delete_activity,
    build_like_activity,
    build_quote_authorization,
    build_quote_request_activity,
    build_undo_activity,
    build_update_activity,
)
from datetime import datetime, timezone

like = build_like_activity(
    actor_id="https://example.com/ap/actor",
    object_id="https://remote.example.com/post/42",
    to=["https://www.w3.org/ns/activitystreams#Public"],
    cc=["https://remote.example.com/users/bob"],
    activity_id="https://example.com/activities/like-1",
    published=datetime.now(timezone.utc),
)

undo = build_undo_activity(like, actor_id="https://example.com/ap/actor")

# Delete a local object by wrapping it in a Tombstone
delete = build_delete_activity(
    actor_id="https://example.com/ap/actor",
    object_id="https://example.com/posts/hello-world",
)

# Announce an edited object: the embedded document gets the activity's
# to/cc audience and an `updated` stamp (defaulting to `published`)
update = build_update_activity(
    actor_id="https://example.com/ap/actor",
    object_doc=updated_post_document,
)

# Request permission to quote a remote post (FEP-044f): the quoting
# Note document travels embedded in `instrument` and the request is
# addressed to the quoted post's author
quote_request = build_quote_request_activity(
    actor_id="https://example.com/ap/actor",
    quoted_object_id="https://remote.example.com/post/42",
    instrument=quoting_note_document,
    target_actor_id="https://remote.example.com/users/bob",
)

# Issue a QuoteAuthorization document (e.g. when approving a quote of
# your own post yourself); serve it at its `id` URL so remote servers
# can verify it
authorization = build_quote_authorization(
    "https://example.com/ap/actor",
    interacting_object="https://example.com/objects/quote-1",
    interaction_target="https://example.com/objects/post-42",
    to=["https://www.w3.org/ns/activitystreams#Public"],
)
```

These free functions are the same helpers `handler.outbox` uses internally,
but with explicit `actor_id`, `to`/`cc`, and `@context` parameters so you can
shape the payload to match your application's addressing rules.

#### `handler.publish_actor_update(document=None)`

Push the current actor profile to all followers. Call this after changing
any actor properties (name, summary, icon, attachment/fields) so remote
instances refresh their cached copy. This is the standard mechanism used
by Mastodon when a user edits their profile.

```python
handler.publish_actor_update()
```

The method builds an `Update` activity whose `object` is the full actor
document, and fans it out to every follower inbox.

When your actor profile lives in your own models rather than in
`actor_config`, pass a prebuilt actor document as `document` — it becomes
the activity's `object` verbatim:

```python
handler.publish_actor_update(document=my_actor_document)
```

### Content Rendering

`pubby.content` produces outbound ActivityPub HTML from plain text. These
helpers escape all input and only turn validated `http`/`https` URLs into
anchors, so the output is safe to federate.

```python
from pubby import (
    RenderedContent,
    build_hashtag_tags,
    render_bio_html,
    render_post_html,
)

rc = render_post_html("New post #fediverse", lambda name: f"https://example.com/tags/{name}")
assert isinstance(rc, RenderedContent)
assert rc.html == 'New post <a href="https://example.com/tags/fediverse" rel="tag">#fediverse</a>'
assert rc.hashtags == ["fediverse"]

tags = build_hashtag_tags(rc.hashtags, lambda name: f"https://example.com/tags/{name}")
# tags == [{"type": "Hashtag", "name": "#fediverse", "href": "..."}]
```

| Function | Description |
|---|---|
| `render_post_html(text, hashtag_url)` | Render a post with URL + hashtag linkification; newlines become `<br>`. |
| `render_bio_html(bio)` | Render a bio with URL linkification only; newlines become `<br>`. |
| `build_hashtag_tags(names, hashtag_url)` | Build `Hashtag` tag dicts from normalized names. |
| `set_object_content(obj, text, hashtag_url)` | Set `obj['content']` from plain text and merge detected hashtags into `obj['tag']`. |
| `format_duration(seconds)` | Format seconds as an ISO-8601 duration (`PT[h]H[m]M[s]S`) for media objects. |
| `render_verified_link(url, label=None)` | Render a `rel="me"` link or escaped text. |
| `property_value_attachment(name, url, label=None)` | Build a `PropertyValue` dict for `ActorConfig.attachment`. |
| `is_linkable_url(url)` | `True` for safe `http`/`https` URLs with a hostname. |
| `display_url(url)` | Scheme-less URL for use as link text. |
| `render_link_anchor(url, rel=None, label=None)` | Render an HTML `<a>` element. |

### Storage

#### `ActivityPubStorage`

Abstract base class. Built-in adapters:

- `pubby.storage.adapters.db.init_db_storage(url)` — SQLAlchemy (any DB)
- `pubby.storage.adapters.file.FileActivityPubStorage(data_dir)` — JSON files

See [Custom Storage](#custom-storage) for implementing your own.

#### `get_interaction_by_object_id(object_id, status=CONFIRMED)`

Look up an interaction by its remote object URL (e.g., a Mastodon status URL).
Useful when you need to find an interaction without knowing its target resource:

```python
# Find who sent a particular reply
interaction = storage.get_interaction_by_object_id(
    "https://mastodon.social/users/alice/statuses/123456"
)
if interaction:
    print(f"Reply from: {interaction.source_actor_id}")
```

Both storage adapters implement this efficiently:
- **DB storage**: SQL query on the indexed `object_id` column
- **File storage**: Uses an `_object_ids/` index directory for O(1) lookup

#### DB Storage: Mention Index

To enable `get_interactions_mentioning()` with the DB adapter, add the
`DbInteractionMention` model to your schema:

```python
from sqlalchemy.orm import declarative_base
from pubby.storage.adapters.db import (
    DbActivityPubStorage,
    DbFollower,
    DbInteraction,
    DbInteractionMention,
    DbActivity,
    DbActorCache,
)

Base = declarative_base()

class InteractionMention(Base, DbInteractionMention):
    __tablename__ = "interaction_mentions"

# ... other models ...

storage = DbActivityPubStorage(
    engine=engine,
    follower_model=Follower,
    interaction_model=Interaction,
    activity_model=Activity,
    actor_cache_model=ActorCache,
    interaction_mention_model=InteractionMention,  # Enable mention index
    session_factory=session_factory,
)
```

### Migrations

#### `backfill_mentions(storage, dry_run=False)`

Backfill `mentioned_actors` for existing interactions by extracting mentions
from the `raw_object` stored in metadata. Useful after upgrading to a version
with mention indexing:

```python
from pubby.storage import backfill_mentions
from pubby.storage.adapters.file import FileActivityPubStorage

storage = FileActivityPubStorage("/path/to/data")

# Preview changes
stats = backfill_mentions(storage, dry_run=True)
print(stats)
# {'scanned': 42, 'updated': 15, 'skipped_no_metadata': 10, ...}

# Apply changes
stats = backfill_mentions(storage)
```

Currently supports `FileActivityPubStorage`. For DB storage, run a direct SQL
migration to populate the `interaction_mentions` table from existing data.

#### `backfill_object_id_index(storage, dry_run=False)`

Backfill the `_object_ids/` index for existing interactions. Required after
upgrading to enable `get_interaction_by_object_id()` for pre-existing data:

```python
from pubby.storage import backfill_object_id_index
from pubby.storage.adapters.file import FileActivityPubStorage

storage = FileActivityPubStorage("/path/to/data")

# Preview changes
stats = backfill_object_id_index(storage, dry_run=True)
print(stats)
# {'scanned': 100, 'indexed': 85, 'skipped_no_object_id': 10, ...}

# Apply changes
stats = backfill_object_id_index(storage)
```

Only needed for `FileActivityPubStorage`. DB storage uses SQL indexes automatically.

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
