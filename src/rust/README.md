# pubby-sdk

Rust implementation of [Pubby](https://github.com/blacklight/pubby) — a
framework-agnostic ActivityPub federation library for your web
application. Feature-parity with the Python API, with idiomatic Rust
types (`serde_json::Value` documents, `Result` errors, builder-style
options).

```toml
[dependencies]
pubby-sdk = "0.3"
```

## Features

| Feature | Enables |
|---------|---------|
| `db` | `SqliteActivityPubStorage` via `rusqlite` |
| `redis` | `RedisDocumentStore` |
| `axum` | `server::axum::{activitypub_router, mastodon_router}` |
| `async` | `DocumentCache::get_or_render_async` (futures-based single-flight) |

## Quick start

```rust
use std::sync::Arc;
use pubby_sdk::{
    ActorConfig, ActivityPubHandler, FileActivityPubStorage, HandlerOptions,
    Object,
};
use pubby_sdk::crypto::generate_rsa_keypair;
use serde_json::json;

let storage = Arc::new(
    FileActivityPubStorage::new("/var/lib/myapp/ap", true)?,
);
let (private_key, _) = generate_rsa_keypair(2048)?;
let config = ActorConfig::from_json(&json!({
    "base_url": "https://blog.example.com",
    "username": "blog",
}))?;
let handler = ActivityPubHandler::new(
    storage, config, private_key, HandlerOptions::default(),
)?;

// Serve the actor document, WebFinger, NodeInfo, inbox, outbox,
// followers and following via your framework — with axum:
#[cfg(feature = "axum")]
let router = pubby_sdk::server::axum::activitypub_router(
    Arc::new(handler), "/ap", None, None, None,
);
```

For other frameworks, call `pubby_sdk::server::dispatch(handler, request,
"/ap", options)` from your route handlers — it takes a plain
`DispatchRequest` and returns `HttpResp`.

## Publish an object

```rust
let mut obj = Object::new("https://blog.example.com/posts/1");
obj.content = "Hello fediverse".into();
obj.attributed_to = json!(handler.actor_id);
obj.to = vec![pubby_sdk::AS_PUBLIC.to_string()];
handler.publish_object(&obj, "Create")?;
```

See the crate docs and `docs/ARCHITECTURE.md` in the repository for the
module-by-module mapping to the Python API and the semantic relaxations.

## License

AGPL-3.0-or-later
