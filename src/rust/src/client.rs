//! One-off actor/inbox resolution helpers — mirrors `pubby.client`.

use serde_json::Value;

use crate::crypto::signatures::{sign_request, SignOptions};
use crate::handlers::client::get_default_user_agent;
use crate::http::HttpClient;
use crate::moderation::is_domain_blocked;
use crate::storage::ActivityPubStorage;

/// Return the preferred inbox URL from an actor document.
///
/// Prefers `endpoints.sharedInbox` over `inbox`.
pub fn extract_actor_inbox(actor_data: &Value) -> Option<String> {
    let map = actor_data.as_object()?;
    let shared = map
        .get("endpoints")
        .and_then(|e| e.as_object())
        .and_then(|e| e.get("sharedInbox"))
        .and_then(|v| v.as_str())
        .filter(|s| !s.is_empty());
    match shared {
        Some(s) => Some(s.to_string()),
        None => map
            .get("inbox")
            .and_then(|v| v.as_str())
            .filter(|s| !s.is_empty())
            .map(String::from),
    }
}

/// Options for [`resolve_actor_inbox`].
#[derive(Default)]
pub struct ResolveInboxOptions<'a> {
    /// RSA private key (PEM or key object) for a signed fetch.
    pub private_key: Option<rsa::RsaPrivateKey>,
    /// Key ID for the HTTP signature (`<actor_id>#main-key`).
    pub key_id: Option<&'a str>,
    /// Allow-list of remote instance domains.
    pub allowed_instances: Option<&'a [String]>,
    /// Block-list of remote instance domains.
    pub blocked_instances: Option<&'a [String]>,
    /// `User-Agent` header; defaults to `pubby/{version} (+{actor_url})`.
    pub user_agent: Option<String>,
}

/// Resolve a remote actor's inbox, using the cache and an optional signed
/// fetch.
///
/// - Returns `None` for non-HTTP(S) actor IDs.
/// - Applies allow/block domain filtering before any network call.
/// - Consults `storage.get_cached_actor` first.
/// - Fetches the actor document with `Accept: application/activity+json`
///   on cache miss; signs the GET when `key_id` and `private_key` are set.
/// - Caches successful fetches via `storage.cache_remote_actor`.
/// - Returns `endpoints.sharedInbox` when available, otherwise `inbox`.
pub fn resolve_actor_inbox(
    actor_url: &str,
    storage: &dyn ActivityPubStorage,
    options: &ResolveInboxOptions,
    client: &HttpClient,
) -> Option<String> {
    let parsed = url::Url::parse(actor_url).ok()?;
    if !matches!(parsed.scheme(), "http" | "https") {
        log::debug!("Refusing to resolve non-HTTP(S) actor ID: {actor_url}");
        return None;
    }

    if is_domain_blocked(
        actor_url,
        options.allowed_instances,
        options.blocked_instances,
    ) {
        log::debug!("Skipping actor on blocked/non-allowed instance: {actor_url}");
        return None;
    }

    if let Ok(Some(cached)) = storage.get_cached_actor(actor_url, 86400.0) {
        return extract_actor_inbox(&cached);
    }

    let mut headers: std::collections::HashMap<String, String> = [
        (
            "Accept".to_string(),
            "application/activity+json, application/ld+json".to_string(),
        ),
        (
            "User-Agent".to_string(),
            options
                .user_agent
                .clone()
                .unwrap_or_else(|| get_default_user_agent(actor_url)),
        ),
    ]
    .into_iter()
    .collect();

    if let (Some(key), Some(key_id)) = (&options.private_key, options.key_id) {
        let opts = SignOptions {
            headers: headers.clone(),
            signed_headers: None,
        };
        match sign_request(key, key_id, "GET", actor_url, None, &opts) {
            Ok(signed) => headers = signed,
            Err(e) => {
                log::warn!("Failed to sign actor fetch for {actor_url}: {e}");
            }
        }
    }

    match client.get(actor_url, &headers) {
        Ok(resp) if resp.is_success() => {
            let actor_data = resp.json().ok()?;
            let _ = storage.cache_remote_actor(actor_url, &actor_data, None);
            extract_actor_inbox(&actor_data)
        }
        Ok(resp) => {
            if resp.status == 410 {
                log::debug!("Actor gone (deleted): {actor_url}");
            } else {
                log::warn!("Failed to fetch actor {actor_url}: status {}", resp.status);
            }
            None
        }
        Err(e) => {
            log::warn!("Failed to fetch actor {actor_url}: {e}");
            None
        }
    }
}
