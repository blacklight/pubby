//! Mastodon-compatible API layer — mirrors `pubby.server.mastodon`.
//!
//! Framework-agnostic mappers and route handlers exposing a read-only
//! subset of the Mastodon REST API backed by the ActivityPub handler and
//! storage.

use base64::Engine;
use serde_json::{json, Map, Value};

use crate::handlers::ActivityPubHandler;
use crate::model::{Follower, InteractionType};

/// Local account ID used by the Mastodon API surface.
pub const ACCOUNT_LOCAL_ID: &str = "1";

/// Derive a URL-safe, deterministic, reversible identifier from a URL.
pub fn stable_id(url: &str) -> String {
    base64::engine::general_purpose::URL_SAFE
        .encode(url.as_bytes())
        .trim_end_matches('=')
        .to_string()
}

/// Reverse a [`stable_id`] encoding back to the original URL.
pub fn id_to_url(encoded: &str) -> Result<String, String> {
    let padded = format!("{}{}", encoded, "=".repeat((4 - encoded.len() % 4) % 4));
    base64::engine::general_purpose::URL_SAFE
        .decode(padded.as_bytes())
        .map_err(|e| e.to_string())
        .and_then(|b| String::from_utf8(b).map_err(|e| e.to_string()))
}

fn follower_domains(handler: &ActivityPubHandler) -> crate::error::Result<Vec<String>> {
    let followers = handler.storage.get_followers(Some(&handler.actor_id))?;
    let mut domains: Vec<String> = followers
        .iter()
        .filter_map(|f| {
            url::Url::parse(&f.actor_id)
                .ok()
                .and_then(|u| u.host_str().map(String::from))
        })
        .collect();
    domains.sort();
    domains.dedup();
    Ok(domains)
}

/// Map the local actor to a Mastodon Account entity.
pub fn actor_to_account(handler: &ActivityPubHandler) -> crate::error::Result<Value> {
    let followers = handler.storage.get_followers(Some(&handler.actor_id))?;
    let activities = handler.storage.get_activities(10000, 0)?;

    let fields: Vec<Value> = handler
        .actor_attachment
        .iter()
        .filter(|a| a.get("type").and_then(|v| v.as_str()) == Some("PropertyValue"))
        .map(|att| {
            json!({
                "name": att.get("name").and_then(|v| v.as_str()).unwrap_or_default(),
                "value": att.get("value").and_then(|v| v.as_str()).unwrap_or_default(),
                "verified_at": Value::Null,
            })
        })
        .collect();

    Ok(json!({
        "id": ACCOUNT_LOCAL_ID,
        "username": handler.username,
        "acct": format!("{}@{}", handler.username, handler.webfinger_domain),
        "display_name": handler.actor_name.clone().unwrap_or_else(|| handler.username.clone()),
        "note": handler.actor_summary,
        "url": if handler.actor_url.is_empty() { &handler.base_url } else { &handler.actor_url },
        "uri": handler.actor_id,
        "avatar": handler.icon_url,
        "avatar_static": handler.icon_url,
        "header": "",
        "header_static": "",
        "locked": handler.manually_approves,
        "created_at": "1970-01-01T00:00:00.000Z",
        "last_status_at": Value::Null,
        "followers_count": followers.len(),
        "following_count": 0,
        "statuses_count": activities.len(),
        "fields": fields,
        "emojis": [],
        "bot": false,
        "group": false,
        "discoverable": true,
        "noindex": false,
        "source": Value::Null,
    }))
}

fn parse_published(value: Option<&Value>) -> String {
    match value {
        Some(Value::String(s)) if !s.trim().is_empty() => s.clone(),
        _ => {
            let s = crate::model::dt_to_iso(&chrono::Utc::now());
            s.replace("+00:00", "Z")
        }
    }
}

fn extract_object(activity: &Value) -> Map<String, Value> {
    match activity.get("object") {
        Some(Value::String(s)) => {
            let mut m = Map::new();
            m.insert("id".into(), json!(s));
            m
        }
        Some(Value::Object(m)) => m.clone(),
        _ => Map::new(),
    }
}

/// Extract a single displayable URL from an object's `url` field.
fn object_url(obj: &Map<String, Value>, fallback: &str) -> String {
    let entries: Vec<Value> = match obj.get("url") {
        Some(Value::Array(a)) => a.clone(),
        Some(v) => vec![v.clone()],
        None => vec![],
    };
    let mut first_href = String::new();
    for entry in entries {
        let (href, preferred) = match &entry {
            Value::String(s) => (s.clone(), true),
            Value::Object(m) => {
                let href = m
                    .get("href")
                    .and_then(|v| v.as_str())
                    .unwrap_or_default()
                    .to_string();
                let preferred = m.get("mediaType").and_then(|v| v.as_str()) == Some("text/html")
                    || m.get("rel").and_then(|v| v.as_str()) == Some("alternate");
                (href, preferred)
            }
            _ => continue,
        };
        if href.is_empty() {
            continue;
        }
        if preferred {
            return href;
        }
        if first_href.is_empty() {
            first_href = href;
        }
    }
    if first_href.is_empty() {
        fallback.to_string()
    } else {
        first_href
    }
}

fn map_media_attachments(attachments: &[Value]) -> Vec<Value> {
    attachments
        .iter()
        .map(|att| {
            let media_type = att
                .get("mediaType")
                .and_then(|v| v.as_str())
                .unwrap_or_default();
            let att_type = if media_type.starts_with("image/") {
                "image"
            } else if media_type.starts_with("video/") {
                "video"
            } else if media_type.starts_with("audio/") {
                "audio"
            } else {
                "unknown"
            };
            let url = att
                .get("url")
                .or_else(|| att.get("href"))
                .and_then(|v| v.as_str())
                .unwrap_or_default();
            json!({
                "id": stable_id(url),
                "type": att_type,
                "url": url,
                "preview_url": url,
                "remote_url": Value::Null,
                "meta": Value::Null,
                "description": att.get("name").cloned().unwrap_or(Value::Null),
                "blurhash": Value::Null,
            })
        })
        .collect()
}

fn map_tags(tags: &[Value], base_url: &str) -> Vec<Value> {
    tags.iter()
        .filter_map(|tag| {
            match tag.get("type").and_then(|v| v.as_str())? {
                "Hashtag" => {
                    let name = tag
                        .get("name")
                        .and_then(|v| v.as_str())
                        .unwrap_or_default()
                        .trim_start_matches('#')
                        .to_string();
                    Some(json!({
                        "name": name,
                        "url": tag.get("href").and_then(|v| v.as_str()).map(String::from)
                            .unwrap_or_else(|| format!("{base_url}/tags/{name}")),
                    }))
                }
                "Mention" => Some(json!({
                    "id": tag.get("href").and_then(|v| v.as_str()).unwrap_or_default(),
                    "username": tag.get("name").and_then(|v| v.as_str()).unwrap_or_default().trim_start_matches('@'),
                    "url": tag.get("href").and_then(|v| v.as_str()).unwrap_or_default(),
                    "acct": tag.get("name").and_then(|v| v.as_str()).unwrap_or_default().trim_start_matches('@'),
                })),
                _ => None,
            }
        })
        .collect()
}

fn count_interactions(
    handler: &ActivityPubHandler,
    object_url: &str,
    interaction_type: InteractionType,
) -> crate::error::Result<usize> {
    Ok(handler
        .storage
        .get_interactions(
            object_url,
            Some(interaction_type),
            crate::model::InteractionStatus::Confirmed,
        )?
        .len())
}

/// Map an outbox activity (Create wrapping a Note/Article) to a Mastodon
/// Status entity.
pub fn activity_to_status(
    activity: &Value,
    handler: &ActivityPubHandler,
    account: Option<Value>,
) -> crate::error::Result<Value> {
    let account = match account {
        Some(a) => a,
        None => actor_to_account(handler)?,
    };

    let obj = extract_object(activity);
    let object_id = obj
        .get("id")
        .and_then(|v| v.as_str())
        .or_else(|| activity.get("id").and_then(|v| v.as_str()))
        .unwrap_or_default()
        .to_string();
    let object_url = object_url(&obj, &object_id);

    let content = obj
        .get("content")
        .and_then(|v| v.as_str())
        .unwrap_or_default();
    let language = obj
        .get("contentMap")
        .and_then(|v| v.as_object())
        .and_then(|m| m.keys().next().cloned());

    let tags = map_tags(
        obj.get("tag")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default()
            .as_slice(),
        &handler.base_url,
    );
    let media = map_media_attachments(
        obj.get("attachment")
            .and_then(|v| v.as_array())
            .cloned()
            .unwrap_or_default()
            .as_slice(),
    );

    let tags_filtered: Vec<Value> = tags
        .into_iter()
        .filter(|t| t.get("name").is_some() && t.get("url").is_some() && t.get("id").is_none())
        .collect();

    Ok(json!({
        "id": stable_id(&object_id),
        "created_at": parse_published(obj.get("published").or_else(|| activity.get("published"))),
        "in_reply_to_id": Value::Null,
        "in_reply_to_account_id": Value::Null,
        "sensitive": obj.get("sensitive").and_then(|v| v.as_bool()).unwrap_or(false),
        "spoiler_text": obj.get("summary").and_then(|v| v.as_str()).unwrap_or_default(),
        "visibility": "public",
        "language": language,
        "uri": object_id,
        "url": object_url,
        "replies_count": count_interactions(handler, &object_url, InteractionType::Reply)?,
        "reblogs_count": count_interactions(handler, &object_url, InteractionType::Boost)?,
        "favourites_count": count_interactions(handler, &object_url, InteractionType::Like)?,
        "favourited": false,
        "reblogged": false,
        "muted": false,
        "bookmarked": false,
        "pinned": false,
        "content": content,
        "reblog": Value::Null,
        "application": {
            "name": handler.software_name,
            "website": handler.base_url,
        },
        "account": account,
        "media_attachments": media,
        "mentions": [],
        "tags": tags_filtered,
        "emojis": [],
        "card": Value::Null,
        "poll": Value::Null,
    }))
}

/// Map a stored [`Follower`] to a minimal Mastodon Account entity.
pub fn follower_to_account(follower: &Follower) -> Value {
    let data = Value::Object(follower.actor_data.clone());
    let actor_url = data
        .get("url")
        .and_then(|v| v.as_str())
        .unwrap_or(&follower.actor_id);
    let username = data
        .get("preferredUsername")
        .and_then(|v| v.as_str())
        .unwrap_or_default();
    let name = data
        .get("name")
        .and_then(|v| v.as_str())
        .unwrap_or(username);
    let avatar = match data.get("icon") {
        Some(Value::Object(m)) => m.get("url").and_then(|v| v.as_str()).unwrap_or_default(),
        Some(Value::String(s)) => s.as_str(),
        _ => "",
    };
    let domain = url::Url::parse(&follower.actor_id)
        .ok()
        .and_then(|u| u.host_str().map(String::from))
        .unwrap_or_default();
    json!({
        "id": stable_id(&follower.actor_id),
        "username": username,
        "acct": if username.is_empty() { follower.actor_id.clone() } else { format!("{username}@{domain}") },
        "display_name": name,
        "note": data.get("summary").and_then(|v| v.as_str()).unwrap_or_default(),
        "url": actor_url,
        "uri": follower.actor_id,
        "avatar": avatar,
        "avatar_static": avatar,
        "header": "",
        "header_static": "",
        "locked": data.get("manuallyApprovesFollowers").and_then(|v| v.as_bool()).unwrap_or(false),
        "created_at": "1970-01-01T00:00:00.000Z",
        "last_status_at": Value::Null,
        "followers_count": 0,
        "following_count": 0,
        "statuses_count": 0,
        "fields": [],
        "emojis": [],
        "bot": false,
        "group": false,
        "discoverable": data.get("discoverable").and_then(|v| v.as_bool()).unwrap_or(true),
        "noindex": false,
    })
}

/// Build a Mastodon Tag entity.
pub fn tag_to_mastodon_tag(name: &str, base_url: &str, history: Option<Vec<Value>>) -> Value {
    let name = name.to_lowercase();
    json!({
        "name": name,
        "url": format!("{base_url}/tags/{name}"),
        "history": history.unwrap_or_default(),
    })
}

/// Stateless request handler for the Mastodon-compatible API surface.
///
/// Each method returns `(body, status_code)` that the framework adapter
/// turns into an HTTP response.
pub struct MastodonAPI {
    /// The Pubby handler.
    pub handler: std::sync::Arc<ActivityPubHandler>,
    /// Instance title (defaults to the actor name).
    pub title: String,
    /// Instance description.
    pub description: String,
    /// Contact e-mail shown in instance info.
    pub contact_email: String,
    /// Software name for the `version` field.
    pub software_name: String,
    /// Software version string.
    pub software_version: String,
}

/// Optional overrides for [`MastodonAPI::with_options`] — mirrors the
/// Python `MastodonAPI` constructor kwargs.
#[derive(Default)]
pub struct MastodonAPIOptions {
    /// Instance title (defaults to the actor name).
    pub title: Option<String>,
    /// Instance description (defaults to the actor summary).
    pub description: Option<String>,
    /// Contact e-mail shown in instance info.
    pub contact_email: String,
    /// Software name for the `version` field.
    pub software_name: Option<String>,
    /// Software version string.
    pub software_version: Option<String>,
}

impl MastodonAPI {
    /// Create a Mastodon API handler with default instance metadata.
    pub fn new(handler: std::sync::Arc<ActivityPubHandler>) -> Self {
        Self::with_options(handler, MastodonAPIOptions::default())
    }

    /// Create a Mastodon API handler with Python-parity overrides.
    pub fn with_options(
        handler: std::sync::Arc<ActivityPubHandler>,
        options: MastodonAPIOptions,
    ) -> Self {
        MastodonAPI {
            title: options
                .title
                .or_else(|| handler.actor_name.clone())
                .unwrap_or_else(|| handler.username.clone()),
            description: options
                .description
                .unwrap_or_else(|| handler.actor_summary.clone()),
            contact_email: options.contact_email,
            software_name: options
                .software_name
                .unwrap_or_else(|| handler.software_name.clone()),
            software_version: options
                .software_version
                .unwrap_or_else(|| handler.software_version.clone()),
            handler,
        }
    }

    fn account(&self) -> crate::error::Result<Value> {
        actor_to_account(&self.handler)
    }

    fn storage_error(e: crate::error::ActivityPubError) -> (Value, u16) {
        (json!({"error": e.to_string()}), 500)
    }

    /// `GET /api/v1/instance`
    pub fn instance_v1(&self) -> (Value, u16) {
        let account = match self.account() {
            Ok(a) => a,
            Err(e) => return Self::storage_error(e),
        };
        let activities = match self.handler.storage.get_activities(10000, 0) {
            Ok(a) => a,
            Err(e) => return Self::storage_error(e),
        };
        let peers = match follower_domains(&self.handler) {
            Ok(p) => p,
            Err(e) => return Self::storage_error(e),
        };
        (
            json!({
                "uri": self.handler.webfinger_domain,
                "title": self.title,
                "description": self.description,
                "short_description": self.description,
                "email": self.contact_email,
                "version": format!("{} {} (Mastodon-compatible)", self.software_name, self.software_version),
                "urls": {"streaming_api": ""},
                "stats": {
                    "user_count": 1,
                    "status_count": activities.len(),
                    "domain_count": peers.len(),
                },
                "thumbnail": if self.handler.icon_url.is_empty() { Value::Null } else { json!(self.handler.icon_url) },
                "languages": ["en"],
                "registrations": false,
                "approval_required": false,
                "invites_enabled": false,
                "configuration": {
                    "statuses": {"max_characters": 500, "max_media_attachments": 4},
                    "media_attachments": {
                        "supported_mime_types": ["image/jpeg","image/png","image/gif","image/webp","video/mp4","audio/mpeg"],
                        "image_size_limit": 10485760,
                        "video_size_limit": 41943040,
                    },
                    "polls": {"max_options":4,"max_characters_per_option":50,"min_expiration":300,"max_expiration":2629746},
                },
                "contact_account": account,
                "rules": [],
            }),
            200,
        )
    }

    /// `GET /api/v2/instance`
    pub fn instance_v2(&self) -> (Value, u16) {
        let account = match self.account() {
            Ok(a) => a,
            Err(e) => return Self::storage_error(e),
        };
        (
            json!({
                "domain": self.handler.webfinger_domain,
                "title": self.title,
                "version": format!("{} {} (Mastodon-compatible)", self.software_name, self.software_version),
                "source_url": "",
                "description": self.description,
                "usage": {"users": {"active_month": 1}},
                "thumbnail": {"url": self.handler.icon_url},
                "languages": ["en"],
                "configuration": {
                    "urls": {"streaming": ""},
                    "accounts": {"max_featured_tags": 0},
                    "statuses": {"max_characters":500,"max_media_attachments":4,"characters_reserved_per_url":23},
                    "media_attachments": {
                        "supported_mime_types": ["image/jpeg","image/png","image/gif","image/webp","video/mp4","audio/mpeg"],
                        "image_size_limit": 10485760,
                        "video_size_limit": 41943040,
                    },
                    "polls": {"max_options":4,"max_characters_per_option":50,"min_expiration":300,"max_expiration":2629746},
                    "translation": {"enabled": false},
                },
                "registrations": {"enabled": false, "approval_required": false, "message": Value::Null},
                "contact": {"email": self.contact_email, "account": account},
                "rules": [],
            }),
            200,
        )
    }

    /// `GET /api/v1/instance/peers`
    pub fn instance_peers(&self) -> (Value, u16) {
        match follower_domains(&self.handler) {
            Ok(peers) => (json!(peers), 200),
            Err(e) => Self::storage_error(e),
        }
    }

    /// `GET /api/v1/accounts/lookup?acct=...`
    pub fn accounts_lookup(&self, acct: Option<&str>) -> (Value, u16) {
        let Some(acct) = acct.filter(|a| !a.is_empty()) else {
            return (json!({"error": "Missing acct parameter"}), 400);
        };
        let normalized = acct.trim_start_matches('@');
        let expected = format!(
            "{}@{}",
            self.handler.username, self.handler.webfinger_domain
        );
        if !normalized.eq_ignore_ascii_case(&expected)
            && !normalized.eq_ignore_ascii_case(&self.handler.username)
        {
            return (json!({"error": "Record not found"}), 404);
        }
        match self.account() {
            Ok(a) => (a, 200),
            Err(e) => Self::storage_error(e),
        }
    }

    /// `GET /api/v1/accounts/:id`
    pub fn accounts_get(&self, account_id: &str) -> (Value, u16) {
        if account_id != ACCOUNT_LOCAL_ID {
            return (json!({"error": "Record not found"}), 404);
        }
        match self.account() {
            Ok(a) => (a, 200),
            Err(e) => Self::storage_error(e),
        }
    }

    /// `GET /api/v1/accounts/:id/statuses`
    pub fn accounts_statuses(
        &self,
        account_id: &str,
        limit: usize,
        max_id: Option<&str>,
        since_id: Option<&str>,
        only_media: bool,
        tagged: Option<&str>,
    ) -> (Value, u16) {
        if account_id != ACCOUNT_LOCAL_ID {
            return (json!({"error": "Record not found"}), 404);
        }
        let limit = limit.clamp(1, 40);
        let all = match self.handler.storage.get_activities(10000, 0) {
            Ok(a) => a,
            Err(e) => return Self::storage_error(e),
        };
        let account = match self.account() {
            Ok(a) => a,
            Err(e) => return Self::storage_error(e),
        };
        let mut statuses = Vec::with_capacity(all.len());
        for a in &all {
            match activity_to_status(a, &self.handler, Some(account.clone())) {
                Ok(s) => statuses.push(s),
                Err(e) => return Self::storage_error(e),
            }
        }
        if let Some(max_id) = max_id {
            if let Some(idx) = statuses
                .iter()
                .position(|s| s.get("id").and_then(|v| v.as_str()) == Some(max_id))
            {
                statuses = statuses.split_off(idx + 1);
            }
        }
        if let Some(since_id) = since_id {
            if let Some(idx) = statuses
                .iter()
                .position(|s| s.get("id").and_then(|v| v.as_str()) == Some(since_id))
            {
                statuses.truncate(idx);
            }
        }
        if only_media {
            statuses.retain(|s| {
                s.get("media_attachments")
                    .and_then(|v| v.as_array())
                    .map(|a| !a.is_empty())
                    .unwrap_or(false)
            });
        }
        if let Some(tagged) = tagged {
            let tag_lower = tagged.to_lowercase();
            statuses.retain(|s| {
                s.get("tags")
                    .and_then(|v| v.as_array())
                    .map(|tags| {
                        tags.iter().any(|t| {
                            t.get("name")
                                .and_then(|v| v.as_str())
                                .map(|n| n.to_lowercase() == tag_lower)
                                .unwrap_or(false)
                        })
                    })
                    .unwrap_or(false)
            });
        }
        statuses.truncate(limit);
        (json!(statuses), 200)
    }

    /// `GET /api/v1/accounts/:id/followers`
    pub fn accounts_followers(
        &self,
        account_id: &str,
        limit: usize,
        max_id: Option<&str>,
        since_id: Option<&str>,
    ) -> (Value, u16) {
        if account_id != ACCOUNT_LOCAL_ID {
            return (json!({"error": "Record not found"}), 404);
        }
        let limit = limit.clamp(1, 80);
        let followers = match self
            .handler
            .storage
            .get_followers(Some(&self.handler.actor_id))
        {
            Ok(f) => f,
            Err(e) => return Self::storage_error(e),
        };
        let mut accounts: Vec<Value> = followers.iter().map(follower_to_account).collect();
        if let Some(max_id) = max_id {
            if let Some(idx) = accounts
                .iter()
                .position(|a| a.get("id").and_then(|v| v.as_str()) == Some(max_id))
            {
                accounts = accounts.split_off(idx + 1);
            }
        }
        if let Some(since_id) = since_id {
            if let Some(idx) = accounts
                .iter()
                .position(|a| a.get("id").and_then(|v| v.as_str()) == Some(since_id))
            {
                accounts.truncate(idx);
            }
        }
        accounts.truncate(limit);
        (json!(accounts), 200)
    }

    /// `GET /api/v1/statuses/:id`
    pub fn statuses_get(&self, status_id: &str) -> (Value, u16) {
        let Ok(object_url) = id_to_url(status_id) else {
            return (json!({"error": "Record not found"}), 404);
        };
        let activities = match self.handler.storage.get_activities(10000, 0) {
            Ok(a) => a,
            Err(e) => return Self::storage_error(e),
        };
        for act in &activities {
            let obj_id = match act.get("object") {
                Some(Value::Object(m)) => m.get("id").and_then(|v| v.as_str()),
                Some(Value::String(s)) => Some(s.as_str()),
                _ => None,
            };
            if obj_id == Some(object_url.as_str()) {
                return match activity_to_status(act, &self.handler, None) {
                    Ok(s) => (s, 200),
                    Err(e) => Self::storage_error(e),
                };
            }
        }
        (json!({"error": "Record not found"}), 404)
    }
}
