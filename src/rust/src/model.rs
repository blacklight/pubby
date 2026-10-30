//! Core data model — mirrors `pubby._model`.
//!
//! Enums map to their ActivityStreams string values; dataclasses map to
//! structs whose [`to_json`]/[`from_json`] methods produce/consume the same
//! JSON-LD dictionaries as Python's `to_dict`/`build`.

use std::collections::HashMap;
use std::fmt;
use std::sync::LazyLock;

use chrono::{DateTime, TimeZone, Utc};
use serde::{Deserialize, Deserializer, Serialize, Serializer};
use serde_json::{json, Map, Value};

use crate::error::ActivityPubError;

/// The ActivityStreams Public collection — objects addressed to this URI
/// (in `to` or `cc`) are considered publicly visible.
pub const AS_PUBLIC: &str = "https://www.w3.org/ns/activitystreams#Public";

/// The JSON-LD `@context` pubby emits on actor and activity documents.
pub static AP_CONTEXT: LazyLock<Value> = LazyLock::new(|| {
    json!([
        "https://www.w3.org/ns/activitystreams",
        "https://w3id.org/security/v1",
        {
            "schema": "http://schema.org#",
            "PropertyValue": "schema:PropertyValue",
            "value": "schema:value",
            "gts": "https://gotosocial.org/ns#",
            "interactionPolicy": {"@id": "gts:interactionPolicy", "@type": "@id"},
            "canQuote": {"@id": "gts:canQuote", "@type": "@id"},
            "automaticApproval": {"@id": "gts:automaticApproval", "@type": "@id"},
            "manualApproval": {"@id": "gts:manualApproval", "@type": "@id"},
        }
    ])
});

/// Return a fresh copy of [`AP_CONTEXT`] suitable for embedding in a
/// document.
pub fn ap_context() -> Value {
    AP_CONTEXT.clone()
}

// ---------- Enums ----------

macro_rules! str_enum {
    (
        $(#[$meta:meta])*
        pub enum $name:ident {
            $( $(#[$vmeta:meta])* $variant:ident = $value:literal ),+ $(,)?
        }
    ) => {
        $(#[$meta])*
        #[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
        pub enum $name {
            $( $(#[$vmeta])* $variant ),+
        }

        impl $name {
            /// The ActivityStreams / storage string value of this variant.
            pub fn as_str(&self) -> &'static str {
                match self {
                    $( $name::$variant => $value ),+
                }
            }

            /// All variants of the enum, in declaration order.
            pub fn variants() -> &'static [$name] {
                &[ $( $name::$variant ),+ ]
            }

            /// Case-insensitive lookup of a raw value.
            pub fn from_raw(raw: &str) -> Option<Self> {
                let normalized = raw.trim();
                Self::variants()
                    .iter()
                    .find(|v| v.as_str().eq_ignore_ascii_case(normalized))
                    .copied()
            }
        }

        impl fmt::Display for $name {
            fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
                f.write_str(self.as_str())
            }
        }

        impl Serialize for $name {
            fn serialize<S: Serializer>(&self, s: S) -> std::result::Result<S::Ok, S::Error> {
                s.serialize_str(self.as_str())
            }
        }

        impl<'de> Deserialize<'de> for $name {
            fn deserialize<D: Deserializer<'de>>(d: D) -> std::result::Result<Self, D::Error> {
                let s = String::deserialize(d)?;
                $name::from_raw(&s)
                    .ok_or_else(|| serde::de::Error::custom(format!("unknown {}: {s}", stringify!($name))))
            }
        }

        impl TryFrom<&str> for $name {
            type Error = ActivityPubError;
            fn try_from(value: &str) -> Result<Self, ActivityPubError> {
                Self::from_raw(value).ok_or_else(|| {
                    ActivityPubError::InvalidValue(format!(
                        "unknown {}: {value}",
                        stringify!($name)
                    ))
                })
            }
        }
    };
}

str_enum! {
    /// Supported ActivityPub activity types.
    pub enum ActivityType {
        Create = "Create",
        Update = "Update",
        Delete = "Delete",
        Follow = "Follow",
        Undo = "Undo",
        Accept = "Accept",
        Reject = "Reject",
        Like = "Like",
        Announce = "Announce",
        QuoteRequest = "QuoteRequest",
    }
}

str_enum! {
    /// Supported ActivityPub object types.
    pub enum ObjectType {
        Note = "Note",
        Article = "Article",
        Image = "Image",
        Video = "Video",
        Audio = "Audio",
        Page = "Page",
        Event = "Event",
        Tombstone = "Tombstone",
    }
}

str_enum! {
    /// Types of interactions received from the fediverse.
    #[derive(Default)]
pub enum InteractionType {
    /// Someone replied to a post.
        Reply = "reply",
        Like = "like",
        Boost = "boost",
        #[default]
        Mention = "mention",
        Quote = "quote",
    }
}

impl InteractionType {
    /// Map an activity type string (`"create"`, `"like"`, `"announce"`) to
    /// the corresponding interaction type.
    pub fn from_activity_type(activity_type: &str) -> Option<Self> {
        match activity_type.to_lowercase().as_str() {
            "create" => Some(InteractionType::Reply),
            "like" => Some(InteractionType::Like),
            "announce" => Some(InteractionType::Boost),
            _ => None,
        }
    }
}

str_enum! {
    /// Status of a stored interaction.
    #[derive(Default)]
pub enum InteractionStatus {
        Pending = "pending",
        #[default]
        Confirmed = "confirmed",
        Deleted = "deleted",
    }
}

str_enum! {
    /// Status of outbound activity delivery.
    pub enum DeliveryStatus {
        Pending = "pending",
        Delivered = "delivered",
        Failed = "failed",
    }
}

str_enum! {
    /// How an incoming `Follow` for a local target is handled.
    ///
    /// `Accept` stores the follower and replies `Accept` immediately.
    /// `Manual` stores a pending [`FollowRequest`] and sends no reply.
    /// `Reject` replies `Reject` and stores nothing.
    pub enum FollowPolicy {
        Accept = "accept",
        Manual = "manual",
        Reject = "reject",
    }
}

// ---------- Helpers ----------

/// Format a `DateTime<Utc>` the way Python's `datetime.isoformat()` does
/// (`+00:00` rather than `Z`, sub-second fraction only when non-zero),
/// for file-format compatibility.
pub fn dt_to_iso(dt: &DateTime<Utc>) -> String {
    if dt.timestamp_subsec_nanos() == 0 {
        dt.format("%Y-%m-%dT%H:%M:%S%:z").to_string()
    } else {
        dt.format("%Y-%m-%dT%H:%M:%S%.6f%:z").to_string()
    }
}

/// Parse a datetime from the JSON shapes Python's `_parse_dt` accepts:
/// ISO-8601 strings (with `Z` or offset, naive assumed UTC) and numeric
/// timestamps.
pub fn parse_dt(value: &Value) -> Option<DateTime<Utc>> {
    match value {
        Value::String(s) => parse_dt_str(s),
        Value::Number(n) => n.as_f64().and_then(|f| {
            if !f.is_finite() {
                return None;
            }
            // Floor (not truncation) so negative fractional timestamps
            // land on the correct second.
            let secs = f.floor() as i64;
            let nanos = ((f - secs as f64) * 1e9).round().clamp(0.0, 999_999_999.0) as u32;
            Utc.timestamp_opt(secs, nanos).single()
        }),
        _ => None,
    }
}

/// Parse an ISO-8601 datetime string; naive values are assumed UTC.
pub fn parse_dt_str(s: &str) -> Option<DateTime<Utc>> {
    let s = s.trim();
    if s.is_empty() {
        return None;
    }
    if let Ok(dt) = DateTime::parse_from_rfc3339(s) {
        return Some(dt.with_timezone(&Utc));
    }
    // Naive datetime (no timezone)
    for fmt in [
        "%Y-%m-%dT%H:%M:%S%.f",
        "%Y-%m-%d %H:%M:%S%.f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ] {
        if let Ok(naive) = chrono::NaiveDateTime::parse_from_str(s, fmt) {
            return Some(naive.and_utc());
        }
        if let Ok(d) = chrono::NaiveDate::parse_from_str(s, fmt) {
            return d.and_hms_opt(0, 0, 0).map(|n| n.and_utc());
        }
    }
    None
}

/// Serialize an `Option<DateTime<Utc>>` to an ISO string JSON value.
pub fn dt_value(dt: Option<&DateTime<Utc>>) -> Value {
    match dt {
        Some(dt) => Value::String(dt_to_iso(dt)),
        None => Value::Null,
    }
}

fn get_str<'a>(data: &'a Value, key: &str) -> Option<&'a str> {
    data.get(key).and_then(|v| v.as_str())
}

fn get_string(data: &Value, key: &str) -> String {
    get_str(data, key).unwrap_or_default().to_string()
}

/// Like [`get_string`] but coerces scalar non-string values (numbers,
/// booleans) to their JSON text, mirroring Python's `data.get(key, "")`
/// which preserves whatever type is stored.
fn get_string_coerced(data: &Value, key: &str) -> String {
    match data.get(key) {
        Some(Value::String(s)) => s.clone(),
        Some(v @ (Value::Number(_) | Value::Bool(_))) => v.to_string(),
        _ => String::new(),
    }
}

/// [`get_string_coerced`] returning `None` for absent or empty values.
fn get_string_coerced_opt(data: &Value, key: &str) -> Option<String> {
    match data.get(key) {
        Some(Value::String(s)) if !s.is_empty() => Some(s.clone()),
        Some(v @ (Value::Number(_) | Value::Bool(_))) => Some(v.to_string()),
        _ => None,
    }
}

fn get_bool(data: &Value, key: &str, default: bool) -> bool {
    data.get(key).and_then(|v| v.as_bool()).unwrap_or(default)
}

fn get_vec(data: &Value, key: &str) -> Vec<Value> {
    match data.get(key) {
        Some(Value::Array(a)) => a.clone(),
        _ => Vec::new(),
    }
}

fn get_map(data: &Value, key: &str) -> Map<String, Value> {
    match data.get(key) {
        Some(Value::Object(m)) => m.clone(),
        _ => Map::new(),
    }
}

/// Normalize a `to`/`cc`-style field that may be a string, a list, or
/// absent into a `Vec<String>`.
pub(crate) fn str_list(value: Option<&Value>) -> Vec<String> {
    match value {
        Some(Value::String(s)) => vec![s.clone()],
        Some(Value::Array(a)) => a
            .iter()
            .filter_map(|v| v.as_str().map(String::from))
            .collect(),
        _ => Vec::new(),
    }
}

fn parse_language(data: &Value) -> Option<String> {
    if let Some(Value::Object(m)) = data.get("contentMap") {
        if let Some(k) = m.keys().next() {
            return Some(k.clone());
        }
    }
    data.get("language")
        .and_then(|v| v.as_str())
        .map(String::from)
}

// ---------- Python-compatible JSON serialization ----------

/// Escape a string the way Python's `json.dumps` does with
/// `ensure_ascii=True` (the default): control characters as `\n`/`\\u00xx`
/// and non-ASCII as `\uXXXX` (with surrogate pairs for astral planes).
fn json_escape_python(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\x08' => out.push_str("\\b"),
            '\x0c' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => {
                out.push_str(&format!("\\u{:04x}", c as u32));
            }
            c if (c as u32) < 0x7f => out.push(c),
            c => {
                // ensure_ascii: escape non-ASCII, astral chars as
                // UTF-16 surrogate pairs.
                let mut buf = [0u16; 2];
                for unit in c.encode_utf16(&mut buf) {
                    out.push_str(&format!("\\u{:04x}", unit));
                }
            }
        }
    }
    out.push('"');
}

fn write_json_python(v: &Value, out: &mut String, indent: Option<usize>, level: usize) {
    let pad = |out: &mut String, lvl: usize| {
        if let Some(n) = indent {
            out.push('\n');
            out.push_str(&" ".repeat(n * lvl));
        }
    };
    let (item_sep, kv_sep) = match indent {
        Some(_) => (",", ": "),
        None => (", ", ": "),
    };
    match v {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => out.push_str(&n.to_string()),
        Value::String(s) => json_escape_python(s, out),
        Value::Array(items) => {
            if items.is_empty() {
                out.push_str("[]");
                return;
            }
            out.push('[');
            pad(out, level + 1);
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push_str(item_sep);
                    pad(out, level + 1);
                }
                write_json_python(item, out, indent, level + 1);
            }
            pad(out, level);
            out.push(']');
        }
        Value::Object(map) => {
            if map.is_empty() {
                out.push_str("{}");
                return;
            }
            out.push('{');
            pad(out, level + 1);
            for (i, (k, val)) in map.iter().enumerate() {
                if i > 0 {
                    out.push_str(item_sep);
                    pad(out, level + 1);
                }
                json_escape_python(k, out);
                out.push_str(kv_sep);
                write_json_python(val, out, indent, level + 1);
            }
            pad(out, level);
            out.push('}');
        }
    }
}

/// Serialize like Python's `json.dumps(doc)` — `", "`/`": "` separators
/// and `ensure_ascii` — so payloads and ETags match a Python process
/// sharing the same document store.
pub(crate) fn dumps_python(v: &Value) -> String {
    let mut out = String::new();
    write_json_python(v, &mut out, None, 0);
    out
}

/// Serialize like Python's `json.dumps(doc, indent=2)` — the on-disk
/// format of the file storage adapter.
pub(crate) fn dumps_python_pretty(v: &Value) -> String {
    let mut out = String::new();
    write_json_python(v, &mut out, Some(2), 0);
    out
}

// ---------- Dataclasses ----------

/// Configuration for an ActivityPub actor.
#[derive(Debug, Clone, PartialEq)]
pub struct ActorConfig {
    /// Public base URL of your site (e.g. `https://example.com`).
    pub base_url: String,
    /// Actor username, used in WebFinger `acct:` URIs.
    pub username: String,
    /// Display name shown on remote instances.
    pub name: Option<String>,
    /// Short bio / description (HTML allowed).
    pub summary: String,
    /// URL to an avatar image.
    pub icon_url: String,
    /// URL to a banner/header image (`image` on the actor document).
    pub image_url: String,
    /// URL path to the actor endpoint (appended to `base_url`).
    pub actor_path: String,
    /// ActivityPub actor type (`Person`, `Application`, `Service`, …).
    pub actor_type: String,
    /// If `true`, follow requests require explicit approval.
    pub manually_approves_followers: bool,
    /// `PropertyValue` attachments (profile fields).
    pub attachment: Vec<Value>,
    /// Human-readable profile URL. When set, the actor's `url` field uses
    /// this instead of `base_url`.
    pub url: Option<String>,
    /// Liked collection URL (advertised as `liked` when set).
    pub liked: Option<String>,
    /// Featured (pinned posts) collection URL (`featured`).
    pub featured: Option<String>,
    /// `movedTo` — the actor URL this account moved to.
    pub moved_to: Option<String>,
    /// `alsoKnownAs` — alias actor URLs.
    pub also_known_as: Vec<String>,
    /// Inbox URL path (default `/ap/inbox`). Multi-actor hosts set a
    /// per-actor path such as `/ap/users/{username}/inbox`.
    pub inbox_path: Option<String>,
    /// Outbox URL path (default `/ap/outbox`).
    pub outbox_path: Option<String>,
    /// Followers collection path (default `/ap/followers`).
    pub followers_path: Option<String>,
    /// Following collection path (default `/ap/following`).
    pub following_path: Option<String>,
}

impl ActorConfig {
    /// Create a config for `base_url`, with all other fields defaulted.
    pub fn new(base_url: impl Into<String>) -> Self {
        let mut cfg = ActorConfig {
            base_url: base_url.into(),
            username: "blog".to_string(),
            name: None,
            summary: String::new(),
            icon_url: String::new(),
            image_url: String::new(),
            actor_path: "/ap/actor".to_string(),
            actor_type: "Person".to_string(),
            manually_approves_followers: false,
            attachment: Vec::new(),
            url: None,
            liked: None,
            featured: None,
            moved_to: None,
            also_known_as: Vec::new(),
            inbox_path: None,
            outbox_path: None,
            followers_path: None,
            following_path: None,
        };
        cfg.normalize();
        cfg
    }

    /// Normalize fields — strips the trailing slash on `base_url` and
    /// defaults `name` to `username` (mirrors `__post_init__`).
    pub fn normalize(&mut self) {
        self.base_url = self.base_url.trim_end_matches('/').to_string();
        if self.name.is_none() {
            self.name = Some(self.username.clone());
        }
    }

    /// Create from a JSON object (backwards-compatible dict form).
    pub fn from_json(data: &Value) -> Result<Self, ActivityPubError> {
        let base_url = match data.get("base_url").and_then(|v| v.as_str()) {
            Some(u) => u.to_string(),
            None => {
                return Err(ActivityPubError::InvalidValue(
                    "actor config requires a base_url".to_string(),
                ))
            }
        };
        let mut cfg = ActorConfig {
            base_url,
            username: get_str(data, "username").unwrap_or("blog").to_string(),
            name: data.get("name").and_then(|v| v.as_str()).map(String::from),
            summary: get_string(data, "summary"),
            icon_url: get_string(data, "icon_url"),
            image_url: get_string(data, "image_url"),
            actor_path: get_str(data, "actor_path")
                .unwrap_or("/ap/actor")
                .to_string(),
            actor_type: get_str(data, "type").unwrap_or("Person").to_string(),
            manually_approves_followers: get_bool(data, "manually_approves_followers", false),
            attachment: get_vec(data, "attachment"),
            url: data.get("url").and_then(|v| v.as_str()).map(String::from),
            liked: data.get("liked").and_then(|v| v.as_str()).map(String::from),
            featured: data
                .get("featured")
                .and_then(|v| v.as_str())
                .map(String::from),
            moved_to: data
                .get("moved_to")
                .and_then(|v| v.as_str())
                .map(String::from),
            also_known_as: str_list(data.get("also_known_as")),
            inbox_path: data
                .get("inbox_path")
                .and_then(|v| v.as_str())
                .map(String::from),
            outbox_path: data
                .get("outbox_path")
                .and_then(|v| v.as_str())
                .map(String::from),
            followers_path: data
                .get("followers_path")
                .and_then(|v| v.as_str())
                .map(String::from),
            following_path: data
                .get("following_path")
                .and_then(|v| v.as_str())
                .map(String::from),
        };
        cfg.normalize();
        Ok(cfg)
    }
}

/// An ActivityPub Actor (Person, Application, Service, etc.).
#[derive(Debug, Clone)]
pub struct Actor {
    pub id: String,
    pub actor_type: String,
    pub preferred_username: String,
    pub name: String,
    pub summary: String,
    pub inbox: String,
    pub outbox: String,
    pub followers: String,
    pub following: String,
    pub icon: Option<Value>,
    /// Banner/header image (`image` on the actor document).
    pub image: Option<Value>,
    pub public_key_pem: String,
    pub manually_approves_followers: bool,
    pub discoverable: bool,
    pub url: String,
    pub endpoints: Map<String, Value>,
    pub attachment: Vec<Value>,
    /// `liked` collection URL.
    pub liked: Option<String>,
    /// `featured` collection URL (pinned posts).
    pub featured: Option<String>,
    /// `movedTo` actor URL.
    pub moved_to: Option<String>,
    /// `alsoKnownAs` actor URLs.
    pub also_known_as: Vec<String>,
}

impl Default for Actor {
    fn default() -> Self {
        Actor {
            id: String::new(),
            actor_type: "Person".to_string(),
            preferred_username: String::new(),
            name: String::new(),
            summary: String::new(),
            inbox: String::new(),
            outbox: String::new(),
            followers: String::new(),
            following: String::new(),
            icon: None,
            image: None,
            public_key_pem: String::new(),
            manually_approves_followers: false,
            discoverable: true,
            url: String::new(),
            endpoints: Map::new(),
            attachment: Vec::new(),
            liked: None,
            featured: None,
            moved_to: None,
            also_known_as: Vec::new(),
        }
    }
}

impl Actor {
    /// Create an empty actor with just an ID.
    pub fn new(id: impl Into<String>) -> Self {
        Actor {
            id: id.into(),
            ..Default::default()
        }
    }

    /// Return the ActivityPub JSON-LD representation.
    pub fn to_json(&self) -> Value {
        let mut doc = json!({
            "@context": AP_CONTEXT.clone(),
            "id": self.id,
            "type": self.actor_type,
            "preferredUsername": self.preferred_username,
            "name": self.name,
            "summary": self.summary,
            "inbox": self.inbox,
            "outbox": self.outbox,
            "followers": self.followers,
            "following": self.following,
            "url": if self.url.is_empty() { &self.id } else { &self.url },
            "manuallyApprovesFollowers": self.manually_approves_followers,
            "discoverable": self.discoverable,
        });

        if let Some(icon) = &self.icon {
            doc["icon"] = icon.clone();
        }
        if let Some(image) = &self.image {
            doc["image"] = image.clone();
        }
        if !self.public_key_pem.is_empty() {
            doc["publicKey"] = json!({
                "id": format!("{}#main-key", self.id),
                "owner": self.id,
                "publicKeyPem": self.public_key_pem,
            });
        }
        if !self.endpoints.is_empty() {
            doc["endpoints"] = Value::Object(self.endpoints.clone());
        }
        if !self.attachment.is_empty() {
            doc["attachment"] = Value::Array(self.attachment.clone());
        }
        if let Some(l) = &self.liked {
            doc["liked"] = json!(l);
        }
        if let Some(f) = &self.featured {
            doc["featured"] = json!(f);
        }
        if let Some(m) = &self.moved_to {
            doc["movedTo"] = json!(m);
        }
        if !self.also_known_as.is_empty() {
            doc["alsoKnownAs"] = json!(self.also_known_as);
        }
        doc
    }

    /// Build an `Actor` from an ActivityPub JSON-LD document.
    pub fn from_json(data: &Value) -> Self {
        let public_key_pem = data
            .get("publicKey")
            .and_then(|pk| pk.as_object())
            .and_then(|pk| pk.get("publicKeyPem"))
            .and_then(|v| v.as_str())
            .unwrap_or_default()
            .to_string();

        let icon = match data.get("icon") {
            Some(Value::String(s)) => Some(json!({"type": "Image", "url": s})),
            Some(v @ Value::Object(_)) => Some(v.clone()),
            // Python preserves any other non-null value verbatim.
            Some(v) if !v.is_null() => Some(v.clone()),
            _ => None,
        };
        let image = match data.get("image") {
            Some(Value::String(s)) => Some(json!({"type": "Image", "url": s})),
            Some(v) if !v.is_null() => Some(v.clone()),
            _ => None,
        };

        Actor {
            id: get_string(data, "id"),
            actor_type: get_str(data, "type").unwrap_or("Person").to_string(),
            preferred_username: get_string(data, "preferredUsername"),
            name: get_string_coerced(data, "name"),
            summary: get_string_coerced(data, "summary"),
            inbox: get_string(data, "inbox"),
            outbox: get_string(data, "outbox"),
            followers: get_string(data, "followers"),
            following: get_string(data, "following"),
            icon,
            image,
            public_key_pem,
            manually_approves_followers: get_bool(data, "manuallyApprovesFollowers", false),
            discoverable: get_bool(data, "discoverable", true),
            url: get_string(data, "url"),
            endpoints: get_map(data, "endpoints"),
            attachment: get_vec(data, "attachment"),
            liked: get_str(data, "liked").map(String::from),
            featured: get_str(data, "featured").map(String::from),
            moved_to: get_str(data, "movedTo").map(String::from),
            also_known_as: str_list(data.get("alsoKnownAs")),
        }
    }
}

/// An ActivityPub Object (Note, Article, Image, etc.).
///
/// `url` accepts either a plain string or a list of `Link` dicts;
/// `attributed_to` accepts a single actor URL or a list of actor URLs.
/// `duration` is emitted verbatim and should be an ISO-8601 duration
/// string (see [`crate::content::format_duration`]).
#[derive(Debug, Clone)]
pub struct Object {
    pub id: String,
    pub object_type: String,
    pub name: Option<String>,
    pub content: String,
    pub url: Value,
    pub attributed_to: Value,
    pub in_reply_to: Option<String>,
    pub published: Option<DateTime<Utc>>,
    pub updated: Option<DateTime<Utc>>,
    pub summary: Option<String>,
    pub sensitive: bool,
    pub tag: Vec<Value>,
    pub attachment: Vec<Value>,
    pub to: Vec<String>,
    pub cc: Vec<String>,
    pub media_type: Option<String>,
    pub language: Option<String>,
    pub quote_control: Option<Value>,
    pub quote_policy: Option<String>,
    pub interaction_policy: Option<Value>,
    pub duration: Option<String>,
}

impl Default for Object {
    fn default() -> Self {
        Object {
            id: String::new(),
            object_type: "Note".to_string(),
            name: None,
            content: String::new(),
            url: Value::String(String::new()),
            attributed_to: Value::String(String::new()),
            in_reply_to: None,
            published: None,
            updated: None,
            summary: None,
            sensitive: false,
            tag: Vec::new(),
            attachment: Vec::new(),
            to: Vec::new(),
            cc: Vec::new(),
            media_type: None,
            language: None,
            quote_control: None,
            quote_policy: None,
            interaction_policy: None,
            duration: None,
        }
    }
}

impl Object {
    /// Create an empty object with just an ID.
    pub fn new(id: impl Into<String>) -> Self {
        Object {
            id: id.into(),
            ..Default::default()
        }
    }

    /// Return the ActivityPub JSON-LD representation.
    pub fn to_json(&self) -> Value {
        let mut doc = json!({
            "id": self.id,
            "type": self.object_type,
            "content": self.content,
            "attributedTo": self.attributed_to,
            "to": self.to,
            "cc": self.cc,
        });

        if let Some(lang) = &self.language {
            doc["contentMap"] = json!({lang: self.content});
            if let Some(summary) = &self.summary {
                doc["summaryMap"] = json!({lang: summary});
            }
        }
        if let Some(name) = &self.name {
            doc["name"] = json!(name);
        }
        if !self.url.is_null() && self.url.as_str().map(|s| !s.is_empty()).unwrap_or(true) {
            doc["url"] = self.url.clone();
        }
        if let Some(irt) = &self.in_reply_to {
            if !irt.is_empty() {
                doc["inReplyTo"] = json!(irt);
            }
        }
        if let Some(p) = &self.published {
            doc["published"] = json!(dt_to_iso(p));
        }
        if let Some(u) = &self.updated {
            doc["updated"] = json!(dt_to_iso(u));
        }
        if let Some(s) = &self.summary {
            doc["summary"] = json!(s);
        }
        if self.sensitive {
            doc["sensitive"] = Value::Bool(true);
        }
        if !self.tag.is_empty() {
            doc["tag"] = json!(self.tag);
        }
        if !self.attachment.is_empty() {
            doc["attachment"] = json!(self.attachment);
        }
        if let Some(mt) = &self.media_type {
            doc["mediaType"] = json!(mt);
        }
        if let Some(qc) = &self.quote_control {
            doc["quoteControl"] = qc.clone();
        }
        if let Some(qp) = &self.quote_policy {
            doc["quotePolicy"] = json!(qp);
        }
        if let Some(ip) = &self.interaction_policy {
            doc["interactionPolicy"] = ip.clone();
        }
        if let Some(d) = &self.duration {
            doc["duration"] = json!(d);
        }
        doc
    }

    /// Build an `Object` from an ActivityPub JSON-LD document (or a bare
    /// string, treated as the object ID).
    pub fn from_json(data: &Value) -> Self {
        if let Value::String(s) = data {
            return Object::new(s.clone());
        }
        Object {
            id: get_string(data, "id"),
            object_type: get_str(data, "type").unwrap_or("Note").to_string(),
            name: get_string_coerced_opt(data, "name"),
            content: get_string_coerced(data, "content"),
            url: data
                .get("url")
                .cloned()
                .unwrap_or(Value::String(String::new())),
            attributed_to: data
                .get("attributedTo")
                .cloned()
                .unwrap_or(Value::String(String::new())),
            // An empty `inReplyTo` behaves like a missing one (Python's
            // `elif target:` truthiness check).
            in_reply_to: get_string_coerced_opt(data, "inReplyTo"),
            published: data.get("published").and_then(parse_dt),
            updated: data.get("updated").and_then(parse_dt),
            summary: get_string_coerced_opt(data, "summary"),
            sensitive: get_bool(data, "sensitive", false),
            tag: get_vec(data, "tag"),
            attachment: get_vec(data, "attachment"),
            to: str_list(data.get("to")),
            cc: str_list(data.get("cc")),
            media_type: get_string_coerced_opt(data, "mediaType"),
            language: parse_language(data),
            quote_control: data.get("quoteControl").cloned(),
            quote_policy: get_string_coerced_opt(data, "quotePolicy"),
            interaction_policy: data.get("interactionPolicy").cloned(),
            duration: get_string_coerced_opt(data, "duration"),
        }
    }
}

/// An ActivityPub Activity (Create, Follow, Like, etc.).
#[derive(Debug, Clone, Default)]
pub struct Activity {
    pub id: String,
    pub activity_type: String,
    pub actor: String,
    pub object: Value,
    pub to: Vec<String>,
    pub cc: Vec<String>,
    pub published: Option<DateTime<Utc>>,
    pub signature: Option<Value>,
}

impl Activity {
    /// Return the ActivityPub JSON-LD representation.
    pub fn to_json(&self) -> Value {
        let mut doc = json!({
            "@context": AP_CONTEXT.clone(),
            "id": self.id,
            "type": self.activity_type,
            "actor": self.actor,
            "to": self.to,
            "cc": self.cc,
        });
        if !self.object.is_null() {
            doc["object"] = self.object.clone();
        }
        if let Some(p) = &self.published {
            doc["published"] = json!(dt_to_iso(p));
        }
        if let Some(s) = &self.signature {
            doc["signature"] = s.clone();
        }
        doc
    }

    /// Build an `Activity` from an ActivityPub JSON-LD document.
    ///
    /// `actor` may be a bare URL string or an embedded actor object — the
    /// latter resolves to its `id` (a valid ActivityStreams form).
    pub fn from_json(data: &Value) -> Self {
        let actor = match data.get("actor") {
            Some(Value::String(s)) => s.clone(),
            Some(Value::Object(m)) => m
                .get("id")
                .and_then(|v| v.as_str())
                .unwrap_or_default()
                .to_string(),
            _ => String::new(),
        };
        Activity {
            id: get_string(data, "id"),
            activity_type: get_string(data, "type"),
            actor,
            object: data.get("object").cloned().unwrap_or(Value::Null),
            to: str_list(data.get("to")),
            cc: str_list(data.get("cc")),
            published: data.get("published").and_then(parse_dt),
            signature: data.get("signature").cloned(),
        }
    }
}

/// A stored interaction from a remote fediverse actor (reply, like, boost).
///
/// Analogous to a Webmention — maps AP interactions to a displayable format.
#[derive(Debug, Clone)]
pub struct Interaction {
    pub source_actor_id: String,
    pub target_resource: String,
    pub interaction_type: InteractionType,
    pub activity_id: String,
    pub object_id: String,
    pub content: String,
    pub author_name: String,
    pub author_url: String,
    pub author_photo: String,
    pub published: Option<DateTime<Utc>>,
    pub status: InteractionStatus,
    pub metadata: Map<String, Value>,
    pub created_at: Option<DateTime<Utc>>,
    pub updated_at: Option<DateTime<Utc>>,
    pub mentioned_actors: Vec<String>,
}

impl Default for Interaction {
    fn default() -> Self {
        Interaction {
            source_actor_id: String::new(),
            target_resource: String::new(),
            interaction_type: InteractionType::Mention,
            activity_id: String::new(),
            object_id: String::new(),
            content: String::new(),
            author_name: String::new(),
            author_url: String::new(),
            author_photo: String::new(),
            published: None,
            status: InteractionStatus::Confirmed,
            metadata: Map::new(),
            created_at: None,
            updated_at: None,
            mentioned_actors: Vec::new(),
        }
    }
}

impl Interaction {
    /// Create a new interaction with the required identity fields.
    pub fn new(
        source_actor_id: impl Into<String>,
        target_resource: impl Into<String>,
        interaction_type: InteractionType,
    ) -> Self {
        Interaction {
            source_actor_id: source_actor_id.into(),
            target_resource: target_resource.into(),
            interaction_type,
            status: InteractionStatus::Confirmed,
            ..Default::default()
        }
    }

    /// Return a JSON-serializable representation (same key layout as the
    /// Python `to_dict`).
    pub fn to_json(&self) -> Value {
        json!({
            "source_actor_id": self.source_actor_id,
            "target_resource": self.target_resource,
            "interaction_type": self.interaction_type.as_str(),
            "activity_id": self.activity_id,
            "object_id": self.object_id,
            "content": self.content,
            "author_name": self.author_name,
            "author_url": self.author_url,
            "author_photo": self.author_photo,
            "published": dt_value(self.published.as_ref()),
            "status": self.status.as_str(),
            "metadata": self.metadata,
            "created_at": dt_value(self.created_at.as_ref()),
            "updated_at": dt_value(self.updated_at.as_ref()),
            "mentioned_actors": self.mentioned_actors,
        })
    }

    /// Build an `Interaction` from a dictionary.
    ///
    /// Unknown `interaction_type`/`status` values raise
    /// [`ActivityPubError::InvalidValue`], mirroring the Python `ValueError`
    /// — silently coercing them would mask corrupt or future data.
    pub fn from_json(data: &Value) -> Result<Self, ActivityPubError> {
        let interaction_type = match data.get("interaction_type") {
            Some(Value::String(s)) => InteractionType::from_raw(s).ok_or_else(|| {
                ActivityPubError::InvalidValue(format!("unknown interaction_type: {s}"))
            })?,
            _ => InteractionType::Mention,
        };
        let status = match data.get("status") {
            Some(Value::String(s)) => InteractionStatus::from_raw(s).ok_or_else(|| {
                ActivityPubError::InvalidValue(format!("unknown interaction status: {s}"))
            })?,
            _ => InteractionStatus::Confirmed,
        };
        Ok(Interaction {
            source_actor_id: get_string_coerced(data, "source_actor_id"),
            target_resource: get_string_coerced(data, "target_resource"),
            interaction_type,
            activity_id: get_string_coerced(data, "activity_id"),
            object_id: get_string_coerced(data, "object_id"),
            content: get_string_coerced(data, "content"),
            author_name: get_string_coerced(data, "author_name"),
            author_url: get_string_coerced(data, "author_url"),
            author_photo: get_string_coerced(data, "author_photo"),
            published: data.get("published").and_then(parse_dt),
            status,
            metadata: get_map(data, "metadata"),
            created_at: data.get("created_at").and_then(parse_dt),
            updated_at: data.get("updated_at").and_then(parse_dt),
            mentioned_actors: str_list(data.get("mentioned_actors")),
        })
    }
}

/// A pending follow request awaiting approval.
#[derive(Debug, Clone, Default)]
pub struct FollowRequest {
    /// The remote actor requesting the follow.
    pub actor_id: String,
    /// The local actor or object URL being followed.
    pub target_actor_id: String,
    /// The requester's inbox URL (Accept/Reject destination).
    pub inbox: String,
    /// The requester's shared inbox URL, if any.
    pub shared_inbox: String,
    /// The requester's cached actor document.
    pub actor_data: Map<String, Value>,
    /// The raw incoming `Follow` activity.
    pub activity: Map<String, Value>,
    /// When the request was received.
    pub requested_at: Option<DateTime<Utc>>,
}

impl FollowRequest {
    /// Return a JSON-serializable representation.
    pub fn to_json(&self) -> Value {
        json!({
            "actor_id": self.actor_id,
            "target_actor_id": self.target_actor_id,
            "inbox": self.inbox,
            "shared_inbox": self.shared_inbox,
            "actor_data": self.actor_data,
            "activity": self.activity,
            "requested_at": dt_value(self.requested_at.as_ref()),
        })
    }

    /// Build a `FollowRequest` from a dictionary.
    pub fn from_json(data: &Value) -> Self {
        FollowRequest {
            actor_id: get_string(data, "actor_id"),
            target_actor_id: get_string(data, "target_actor_id"),
            inbox: get_string(data, "inbox"),
            shared_inbox: get_string(data, "shared_inbox"),
            actor_data: get_map(data, "actor_data"),
            activity: get_map(data, "activity"),
            requested_at: data.get("requested_at").and_then(parse_dt),
        }
    }
}

/// A stored follower record.
#[derive(Debug, Clone, Default)]
pub struct Follower {
    /// The remote actor's ID (URL).
    pub actor_id: String,
    /// The remote actor's inbox URL.
    pub inbox: String,
    /// The remote actor's shared inbox URL, if any.
    pub shared_inbox: String,
    /// When the follow was accepted.
    pub followed_at: Option<DateTime<Utc>>,
    /// The follower's cached actor document.
    pub actor_data: Map<String, Value>,
    /// The local resource URL being followed (actor URL or object id).
    /// Empty means unassigned (legacy).
    pub target_actor_id: String,
}

impl Follower {
    /// Return a JSON-serializable representation.
    pub fn to_json(&self) -> Value {
        json!({
            "actor_id": self.actor_id,
            "inbox": self.inbox,
            "shared_inbox": self.shared_inbox,
            "followed_at": dt_value(self.followed_at.as_ref()),
            "actor_data": self.actor_data,
            "target_actor_id": self.target_actor_id,
        })
    }

    /// Build a `Follower` from a dictionary.
    pub fn from_json(data: &Value) -> Self {
        Follower {
            actor_id: get_string(data, "actor_id"),
            inbox: get_string(data, "inbox"),
            shared_inbox: get_string(data, "shared_inbox"),
            followed_at: data.get("followed_at").and_then(parse_dt),
            actor_data: get_map(data, "actor_data"),
            target_actor_id: get_string(data, "target_actor_id"),
        }
    }
}

/// Convenience type used by helper functions that accept actor data.
pub type JsonMap = HashMap<String, Value>;

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn parse_dt_negative_fraction() {
        let dt = parse_dt(&json!(-0.5)).unwrap();
        assert_eq!(dt_to_iso(&dt), "1969-12-31T23:59:59.500000+00:00");
    }

    #[test]
    fn dt_to_iso_omits_zero_fraction() {
        let dt = parse_dt(&json!("2024-01-02T03:04:05Z")).unwrap();
        assert_eq!(dt_to_iso(&dt), "2024-01-02T03:04:05+00:00");
    }

    #[test]
    fn interaction_unknown_enum_rejected() {
        let bad = json!({
            "source_actor_id": "a", "target_resource": "t",
            "interaction_type": "bogus", "status": "confirmed",
        });
        assert!(Interaction::from_json(&bad).is_err());
    }
}
