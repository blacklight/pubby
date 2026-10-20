//! Pubby — a general-purpose library to add ActivityPub federation support
//! to any Rust web application.
//!
//! This is the Rust port of the Python `pubby` package. It exposes the same
//! API surface: a data model for ActivityStreams documents, HTTP Signatures,
//! WebFinger/NodeInfo discovery, inbox/outbox processors, pluggable storage,
//! a short-TTL document cache with single-flight coalescing, a
//! Mastodon-compatible API layer, and an `axum` server adapter (feature
//! `axum`).

pub mod attribution;
pub mod audience;
pub mod cache;
pub mod client;
pub mod collections;
pub mod content;
pub mod crypto;
pub mod error;
pub mod follows;
pub mod handlers;
pub mod http;
pub mod model;
pub mod moderation;
pub mod quotes;
pub mod rate_limit;
pub mod render;
pub mod server;
pub mod storage;
pub mod webfinger;

pub use attribution::validate as validate_attribution;
pub use audience::{addressees, is_public, mentioned_actors, PUBLIC_URIS};
pub use cache::{
    cache_headers, etag_matches, CachedResponse, DocumentCache, DocumentStore,
    InMemoryDocumentStore,
};
pub use client::{extract_actor_inbox, resolve_actor_inbox};
pub use collections::{ordered_collection, ordered_collection_index, ordered_collection_page};
pub use content::{
    build_hashtag_tags, display_url, format_duration, is_linkable_url, parse_duration,
    property_value_attachment, render_bio_html, render_link_anchor, render_post_html,
    render_verified_link, set_object_content, RenderedContent,
};
pub use error::{ActivityPubError, Result};
pub use follows::{accept_follow_request, build_follow_response, reject_follow_request};
pub use handlers::outbox::{
    build_announce_activity, build_delete_activity, build_like_activity, build_quote_authorization,
    build_quote_request_activity, build_undo_activity, build_update_activity, collect_inboxes,
    deliver_activity,
};
pub use handlers::ActivityPubHandler;
pub use model::{
    Activity, ActivityType, Actor, ActorConfig, DeliveryStatus, FollowPolicy, FollowRequest,
    Follower, Interaction, InteractionStatus, InteractionType, Object, ObjectType, AP_CONTEXT,
    AS_PUBLIC,
};
pub use moderation::{extract_domain, is_domain_blocked, normalize_domain};
pub use quotes::{
    allow_public_quotes, extract_quote_target, set_quote_target, PUBLIC_QUOTE_POLICY,
    QUOTE_FIELD_KEYS,
};
pub use rate_limit::RateLimiter;
pub use storage::ActivityPubStorage;
pub use webfinger::{extract_mentions, resolve_actor_url, Mention};

#[cfg(feature = "redis")]
pub use cache::RedisDocumentStore;

#[cfg(feature = "db")]
pub use storage::db::SqliteActivityPubStorage;

pub use storage::file::FileActivityPubStorage;
pub use storage::migrations::{backfill_mentions, backfill_object_id_index};

/// The crate version — mirrors Python's `pubby.__version__`.
pub const VERSION: &str = env!("CARGO_PKG_VERSION");
