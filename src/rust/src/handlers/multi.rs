//! Multi-actor support — a registry of [`ActivityPubHandler`]s keyed by
//! local username, for Mastodon-style servers hosting many actors.
//!
//! [`MultiActorHandler`] pairs an instance-level handler (serving
//! `{prefix}/actor` and instance-signed requests) with an
//! [`ActorResolver`] callback that maps a username to its handler.
//! `server::dispatch_multi` and `server::axum::activitypub_router_multi`
//! expose the per-actor routes (`{actor_path_template}`,
//! `{actor_path_template}/inbox`, …) plus the well-known endpoints.

use std::sync::Arc;

use serde_json::Value;

use crate::error::Result;
use crate::model::str_list;

use super::discovery::build_webfinger_response;
use super::ActivityPubHandler;

/// Resolves a local actor by username — `Ok(None)` for unknown users.
/// The host decides caching/LRU; construct one [`ActivityPubHandler`]
/// per local account.
pub type ActorResolver =
    Arc<dyn Fn(&str) -> Result<Option<Arc<ActivityPubHandler>>> + Send + Sync>;

/// Registry of local actors for multi-actor deployments.
pub struct MultiActorHandler {
    /// Instance-level actor — serves `{prefix}/actor`, signs
    /// instance-level fetches, receives relay subscriptions, and acts as
    /// the shared-inbox fallback.
    pub instance: Arc<ActivityPubHandler>,
    /// Per-username resolver.
    pub resolver: ActorResolver,
    /// Actor path template containing `{username}`, e.g.
    /// `/ap/users/{username}` (the placeholder syntax matches axum's
    /// `{name}` route params).
    pub actor_path_template: String,
    /// Domain for WebFinger `acct:` resolution — defaults to the
    /// instance handler's `webfinger_domain`.
    pub webfinger_domain: String,
}

impl MultiActorHandler {
    /// Create a multi-actor handler.
    pub fn new(
        instance: Arc<ActivityPubHandler>,
        resolver: ActorResolver,
        actor_path_template: impl Into<String>,
    ) -> Self {
        let webfinger_domain = instance.webfinger_domain.clone();
        MultiActorHandler {
            instance,
            resolver,
            actor_path_template: actor_path_template.into(),
            webfinger_domain,
        }
    }

    /// The template's static prefix/suffix around `{username}`.
    fn template_parts(&self) -> (&str, &str) {
        match self.actor_path_template.split_once("{username}") {
            Some((pre, suf)) => (pre, suf),
            None => (self.actor_path_template.as_str(), ""),
        }
    }

    /// The actor's path for `username` (template with the placeholder
    /// substituted).
    pub fn actor_path(&self, username: &str) -> String {
        let (pre, suf) = self.template_parts();
        format!("{pre}{username}{suf}")
    }

    /// The actor's full URL for `username` under the instance base URL.
    pub fn actor_url(&self, username: &str) -> String {
        format!(
            "{}{}",
            self.instance.base_url.trim_end_matches('/'),
            self.actor_path(username)
        )
    }

    /// Extract the username from an actor path — `None` when `path`
    /// doesn't match the template or the segment contains a `/`.
    pub fn username_from_path<'a>(&self, path: &'a str) -> Option<(&'a str, &'a str)> {
        let (pre, suf) = self.template_parts();
        let rest = path.strip_prefix(pre)?;
        let (username, sub) = if suf.is_empty() {
            match rest.find('/') {
                Some(i) => (&rest[..i], &rest[i..]),
                None => (rest, ""),
            }
        } else {
            let i = rest.find(suf)?;
            (&rest[..i], &rest[i + suf.len()..])
        };
        if username.is_empty() || username.contains('/') {
            return None;
        }
        Some((username, sub))
    }

    /// Resolve the handler for `username`.
    pub fn for_username(&self, username: &str) -> Result<Option<Arc<ActivityPubHandler>>> {
        (self.resolver)(username)
    }

    /// Resolve the local handler an inbound activity addresses: scans
    /// `to`/`cc`/`audience` and `object.attributedTo` for URLs matching
    /// the actor template, then falls back to the instance actor.
    pub fn for_addressees(&self, activity_data: &Value) -> Result<Arc<ActivityPubHandler>> {
        let mut candidates: Vec<String> = Vec::new();
        for f in ["to", "cc", "audience"] {
            candidates.extend(str_list(activity_data.get(f)));
        }
        if let Some(m) = activity_data.get("object").and_then(|o| o.as_object()) {
            candidates.extend(str_list(m.get("attributedTo")));
            for f in ["to", "cc"] {
                candidates.extend(str_list(m.get(f)));
            }
        }
        if let Some(actor) = self.local_actor_from_urls(&candidates)? {
            return Ok(actor);
        }
        Ok(self.instance.clone())
    }

    /// First candidate URL under the actor template → its handler.
    fn local_actor_from_urls(
        &self,
        urls: &[String],
    ) -> Result<Option<Arc<ActivityPubHandler>>> {
        for url in urls {
            let base = self.instance.base_url.trim_end_matches('/');
            let Some(path) = url.strip_prefix(base) else {
                continue;
            };
            if let Some((username, sub)) = self.username_from_path(path) {
                if sub.is_empty() || sub.starts_with('/') {
                    if let Some(h) = self.for_username(username)? {
                        return Ok(Some(h));
                    }
                }
            }
        }
        Ok(None)
    }

    /// WebFinger for any local actor (instance actor included).
    ///
    /// Accepts `acct:user@domain`, `acct:@user@domain`, and the instance
    /// actor's `acct:` — all case-insensitive on the domain.
    pub fn get_webfinger_response(&self, resource: Option<&str>) -> Option<Value> {
        let resource = resource?;
        let normalized = crate::cache::normalize_webfinger_resource(resource);
        let (username, domain) = normalized.split_once('@')?;
        if !domain.eq_ignore_ascii_case(&self.webfinger_domain) {
            return None;
        }
        let handler = if username.eq_ignore_ascii_case(&self.instance.username) {
            Some(self.instance.clone())
        } else {
            self.for_username(username).ok().flatten()
        }?;
        Some(build_webfinger_response(
            &handler.username,
            &self.webfinger_domain,
            &handler.actor_id,
        ))
    }
}
