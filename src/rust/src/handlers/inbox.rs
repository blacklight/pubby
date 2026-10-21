//! Inbox processing — dispatch incoming activities by type. Mirrors
//! `pubby.handlers._inbox`.

use std::collections::HashMap;
use std::sync::Arc;

use chrono::Utc;
use serde_json::{json, Value};

use crate::attribution::validate as validate_attribution;
use crate::audience::{is_public, mentioned_actors};
use crate::cache::{self, DocumentCache};
use crate::crypto::keys::load_public_key;
use crate::crypto::signatures::{sign_request, signature_key_id, verify_request, SignOptions};
use crate::error::{ActivityPubError, Result};
use crate::http::HttpClient;
use crate::model::{
    ap_context, Activity, ActivityType, Actor, FollowPolicy, FollowRequest, Follower, Interaction,
    InteractionStatus, InteractionType, Object,
};
use crate::moderation::{extract_domain, is_domain_blocked};
use crate::quotes::{extract_quote_target, FEP_044F_CONTEXT};
use crate::storage::ActivityPubStorage;

use super::client::get_default_user_agent;
use super::outbox::build_quote_authorization;

/// Callback invoked when an interaction is received.
pub type OnInteractionReceived = Arc<dyn Fn(&Interaction) + Send + Sync>;

/// What the inbox dispatcher should do after an [`OnActivityReceived`]
/// hook ran.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ActivityDisposition {
    /// The hook fully handled the activity; skip built-in processing.
    Handled,
    /// Continue into the built-in per-type handlers.
    PassThrough,
}

/// Hook invoked for every verified inbound activity before the
/// built-in dispatch — the host seam for activity types Pubby does
/// not handle natively (`Move`, `Add`, `Remove`, `Block`, `Flag`, …).
/// `Err` behaves like [`ActivityDisposition::PassThrough`].
pub type OnActivityReceived =
    Arc<dyn Fn(&Activity, &Value) -> Result<ActivityDisposition> + Send + Sync>;

/// Callback invoked when an inbound `Accept`/`Reject` settles one of
/// this actor's outbound `Follow`s — `(target_actor_id, accepted)`.
pub type OnFollowResponse = Arc<dyn Fn(&str, bool) + Send + Sync>;

/// Callback deciding how an incoming `Follow` is handled.
/// Called with `(target_actor_id, requester_actor_id)` and returns an
/// optional [`FollowPolicy`]; `None`/unknown resolve to `Accept`.
/// A callback that fails resolves to `Manual` — a policy failure must not
/// silently grant a follow.
pub type FollowPolicyFn = Arc<dyn Fn(&str, &str) -> Option<FollowPolicy> + Send + Sync>;

/// Processes incoming ActivityPub activities.
///
/// Dispatches by activity type and performs HTTP signature verification.
pub struct InboxProcessor {
    /// Storage backend.
    pub storage: Arc<dyn ActivityPubStorage>,
    /// This server's actor ID (URL).
    pub actor_id: String,
    /// Document cache for dereference endpoints.
    pub document_cache: Option<Arc<DocumentCache>>,
    /// RSA private key for signing outgoing Accept responses.
    pub private_key: rsa::RsaPrivateKey,
    /// Key ID for HTTP signatures (e.g. `actor_id#main-key`).
    pub key_id: String,
    /// Optional callback when an interaction is stored.
    pub on_interaction_received: Option<OnInteractionReceived>,
    /// Timeout for outgoing HTTP requests.
    pub http_client: HttpClient,
    /// User-Agent for outgoing HTTP requests.
    pub user_agent: String,
    /// Auto-approve quote requests targeting local objects.
    pub auto_approve_quotes: bool,
    /// Only store interactions targeting local resources.
    pub store_local_only: bool,
    /// Base URLs considered "local". Defaults to the actor's base URL.
    pub local_base_urls: Vec<String>,
    /// Allow-list of remote instance domains.
    pub allowed_instances: Option<Vec<String>>,
    /// Block-list of remote instance domains.
    pub blocked_instances: Option<Vec<String>>,
    /// Validate inbound `Create`/`Update` object attribution.
    pub strict_attribution: bool,
    /// Callback deciding how an incoming `Follow` is handled.
    pub follow_policy: Option<FollowPolicyFn>,
    /// Hook invoked for every verified inbound activity before the
    /// built-in dispatch (see [`OnActivityReceived`]).
    pub on_activity: Option<OnActivityReceived>,
    /// Callback for `Accept`/`Reject` of this actor's outbound
    /// `Follow`s — `(target_actor_id, accepted)`.
    pub on_follow_response: Option<OnFollowResponse>,
}

impl InboxProcessor {
    #[allow(clippy::too_many_arguments)]
    /// Create an inbox processor.
    pub fn new(
        storage: Arc<dyn ActivityPubStorage>,
        actor_id: impl Into<String>,
        private_key: rsa::RsaPrivateKey,
        key_id: impl Into<String>,
        on_interaction_received: Option<OnInteractionReceived>,
        user_agent: Option<String>,
        http_client: HttpClient,
        auto_approve_quotes: bool,
        store_local_only: bool,
        local_base_urls: Vec<String>,
        allowed_instances: Option<Vec<String>>,
        blocked_instances: Option<Vec<String>>,
        strict_attribution: bool,
        follow_policy: Option<FollowPolicyFn>,
        document_cache: Option<Arc<DocumentCache>>,
    ) -> Self {
        let actor_id = actor_id.into();
        InboxProcessor {
            storage,
            actor_id: actor_id.clone(),
            document_cache,
            private_key,
            key_id: key_id.into(),
            on_interaction_received,
            http_client,
            user_agent: user_agent.unwrap_or_else(|| get_default_user_agent(&actor_id)),
            auto_approve_quotes,
            store_local_only,
            local_base_urls,
            allowed_instances,
            blocked_instances,
            strict_attribution,
            follow_policy,
            on_activity: None,
            on_follow_response: None,
        }
    }

    fn invalidate_cached_documents(&self, prefixes: &[Vec<String>]) {
        if let Some(cache) = &self.document_cache {
            cache.invalidate_prefix(prefixes);
        }
    }

    fn is_local_target(&self, target_resource: &str) -> bool {
        let base_urls = if self.local_base_urls.is_empty() {
            vec![self
                .actor_id
                .rsplit_once('/')
                .map(|(b, _)| b.to_string())
                .unwrap_or_else(|| self.actor_id.clone())]
        } else {
            self.local_base_urls.clone()
        };
        base_urls.iter().any(|b| target_resource.starts_with(b))
    }

    /// Whether a `Follow` target is a local actor or object.
    fn is_local_follow_target(&self, target_actor_id: &str) -> bool {
        if target_actor_id == self.actor_id {
            return true;
        }
        // Compare the full authority (host + port), like Python's
        // `urlparse(...).netloc` — same hostname on a different port is
        // a different instance.
        let netloc =
            |u: &url::Url| u[url::Position::BeforeHost..url::Position::AfterPort].to_lowercase();
        let same_host = url::Url::parse(target_actor_id)
            .ok()
            .zip(url::Url::parse(&self.actor_id).ok())
            .map(|(a, b)| a.host().is_some() && netloc(&a) == netloc(&b))
            .unwrap_or(false);
        same_host || self.is_local_target(target_actor_id)
    }

    fn should_store_interaction(&self, target_resource: &str, mentions_actor: bool) -> bool {
        if !self.store_local_only {
            return true;
        }
        self.is_local_target(target_resource) || mentions_actor
    }

    /// Fetch a remote actor document, using cache if available.
    pub fn fetch_actor(&self, actor_id: &str) -> Option<Value> {
        if let Ok(Some(cached)) = self.storage.get_cached_actor(actor_id, 86400.0) {
            return Some(cached);
        }
        let base_headers: HashMap<String, String> = [
            (
                "Accept".into(),
                "application/activity+json, application/ld+json".into(),
            ),
            ("User-Agent".into(), self.user_agent.clone()),
        ]
        .into_iter()
        .collect();
        let opts = SignOptions {
            headers: base_headers.clone(),
            signed_headers: None,
        };
        let headers = match sign_request(
            &self.private_key,
            &self.key_id,
            "GET",
            actor_id,
            None,
            &opts,
        ) {
            Ok(mut h) => {
                h.extend(base_headers);
                h
            }
            Err(_) => return None,
        };
        match self.http_client.get(actor_id, &headers) {
            Ok(resp) if resp.is_success() => {
                let data = resp.json().ok()?;
                let _ = self.storage.cache_remote_actor(actor_id, &data, None);
                Some(data)
            }
            Ok(resp) => {
                if resp.status == 410 {
                    log::debug!("Actor gone (deleted): {actor_id}");
                } else {
                    log::warn!("Failed to fetch actor {actor_id}: status {}", resp.status);
                }
                None
            }
            Err(e) => {
                log::warn!("Failed to fetch actor {actor_id}: {e}");
                None
            }
        }
    }

    /// Verify the HTTP signature on an incoming request.
    ///
    /// `expected_actor`, when given, is the actor the request claims to act
    /// as (`activity.actor`). The verified key owner must equal it, or the
    /// claimed actor's document must advertise the signing key.
    ///
    /// :return: The actor ID (key owner) from the signature.
    pub fn verify_signature(
        &self,
        method: &str,
        path: &str,
        headers: &HashMap<String, String>,
        body: Option<&[u8]>,
        expected_actor: Option<&str>,
    ) -> Result<String> {
        let lower: HashMap<String, String> = headers
            .iter()
            .map(|(k, v)| (k.to_lowercase(), v.clone()))
            .collect();
        let sig_header = lower.get("signature").ok_or_else(|| {
            ActivityPubError::SignatureVerification("Missing Signature header".into())
        })?;

        let key_id = signature_key_id(sig_header).ok_or_else(|| {
            ActivityPubError::SignatureVerification("No keyId in Signature header".into())
        })?;

        let actor_url = key_id.split('#').next().unwrap_or_default().to_string();

        let actor_data = self.fetch_actor(&actor_url).ok_or_else(|| {
            ActivityPubError::SignatureVerification(format!(
                "Cannot fetch actor for key verification: {actor_url}"
            ))
        })?;

        let public_key_pem = actor_data
            .get("publicKey")
            .and_then(|pk| pk.as_object())
            .and_then(|pk| pk.get("publicKeyPem"))
            .and_then(|v| v.as_str())
            .unwrap_or_default()
            .to_string();
        if public_key_pem.is_empty() {
            return Err(ActivityPubError::SignatureVerification(format!(
                "No public key found for actor: {actor_url}"
            )));
        }

        let public_key = load_public_key(&public_key_pem)?;
        verify_request(&public_key, method, path, headers, body)?;

        if let Some(expected) = expected_actor {
            if expected != actor_url {
                self.verify_key_ownership(&key_id, expected)?;
            }
        }

        Ok(actor_url)
    }

    /// Check that `expected_actor`'s document delegates to `key_id`.
    fn verify_key_ownership(&self, key_id: &str, expected_actor: &str) -> Result<()> {
        let actor_data = self.fetch_actor(expected_actor).ok_or_else(|| {
            ActivityPubError::SignatureVerification(format!(
                "Cannot fetch expected actor for key ownership check: {expected_actor}"
            ))
        })?;
        let pk = actor_data.get("publicKey").cloned().unwrap_or(Value::Null);
        let keys: Vec<Value> = match pk {
            Value::Array(a) => a,
            other => vec![other],
        };
        let owned = keys
            .iter()
            .any(|k| k.get("id").and_then(|v| v.as_str()) == Some(key_id));
        if owned {
            return Ok(());
        }
        Err(ActivityPubError::SignatureVerification(format!(
            "Signature key {key_id} is not owned by activity actor {expected_actor}"
        )))
    }

    /// Process an incoming activity.
    ///
    /// :param activity_data: The parsed activity JSON-LD document.
    /// :param method: HTTP method of the incoming request.
    /// :param path: Request path.
    /// :param headers: Request headers (for signature verification). Required
    ///     unless `skip_verification` is set.
    /// :param body: Raw request body.
    /// :param skip_verification: Skip HTTP signature verification (testing).
    /// :return: Response value or `None`.
    pub fn process(
        &self,
        activity_data: &Value,
        method: &str,
        path: &str,
        headers: Option<&HashMap<String, String>>,
        body: Option<&[u8]>,
        skip_verification: bool,
    ) -> Result<Option<Value>> {
        // Instance allow/block check — runs before signature verification
        // so rejected instances are dropped without fetching the key.
        let actor_value = activity_data.get("actor").and_then(|v| {
            if v.is_object() {
                v.get("id").and_then(|i| i.as_str())
            } else {
                v.as_str()
            }
        });
        if let Some(actor) = actor_value {
            if is_domain_blocked(
                actor,
                self.allowed_instances.as_deref(),
                self.blocked_instances.as_deref(),
            ) {
                log::info!(
                    "Dropping inbound activity {} from blocked instance {}",
                    activity_data
                        .get("id")
                        .and_then(|v| v.as_str())
                        .unwrap_or(""),
                    extract_domain(actor),
                );
                return Ok(None);
            }
        }

        if !skip_verification {
            let Some(headers) = headers else {
                return Err(ActivityPubError::SignatureVerification(
                    "Cannot verify signature: no request headers provided. \
                     Pass skip_verification=true to process unauthenticated \
                     activities."
                        .into(),
                ));
            };
            self.verify_signature(method, path, headers, body, actor_value)?;
        }

        if !activity_data.is_object() {
            return Err(ActivityPubError::General(
                "Invalid activity: expected object, got array".into(),
            ));
        }

        let activity = Activity::from_json(activity_data);
        let activity_type_str = activity.activity_type.trim().to_string();

        // Host hook — runs after signature verification and the
        // domain-block gate, before the built-in dispatch.
        if let Some(cb) = &self.on_activity {
            match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                cb(&activity, activity_data)
            })) {
                Ok(Ok(ActivityDisposition::Handled)) => return Ok(None),
                Ok(Err(e)) => {
                    log::warn!("on_activity hook failed for {activity_type_str}: {e}")
                }
                Err(_) => log::warn!("on_activity hook panicked for {activity_type_str}"),
                _ => {}
            }
        }

        let activity_type = match ActivityType::from_raw(&activity_type_str) {
            Some(t) => t,
            None => {
                log::info!("Ignoring unknown activity type: {activity_type_str}");
                return Ok(None);
            }
        };

        match activity_type {
            ActivityType::Follow => self.handle_follow(&activity, activity_data),
            ActivityType::Undo => self.handle_undo(&activity, activity_data),
            ActivityType::Create => self.handle_create(&activity, activity_data),
            ActivityType::Like => self.handle_like(&activity),
            ActivityType::Announce => self.handle_announce(&activity),
            ActivityType::Delete => self.handle_delete(&activity),
            ActivityType::Update => self.handle_update(&activity),
            ActivityType::QuoteRequest => self.handle_quote_request(&activity, activity_data),
            ActivityType::Accept => self.handle_follow_response(&activity, true),
            ActivityType::Reject => self.handle_follow_response(&activity, false),
        }
    }

    /// Handle `Accept`/`Reject` of a `Follow` this actor sent.
    ///
    /// Only settles when the embedded Follow's `actor` is this actor —
    /// i.e. the response targets one of *our* outbound follows. A pending
    /// follow request left by the host (if it records outgoing follows
    /// through `store_follow_request`) is removed, then
    /// `on_follow_response(target, accepted)` fires.
    fn handle_follow_response(&self, activity: &Activity, accepted: bool) -> Result<Option<Value>> {
        let inner = match &activity.object {
            Value::Object(m) => m,
            _ => return Ok(None),
        };
        if inner
            .get("type")
            .and_then(|v| v.as_str())
            .map(|t| !t.eq_ignore_ascii_case("follow"))
            .unwrap_or(true)
        {
            return Ok(None);
        }
        let inner_actor = match inner.get("actor") {
            Some(Value::String(s)) => s.clone(),
            Some(Value::Object(m)) => m
                .get("id")
                .and_then(|v| v.as_str())
                .unwrap_or_default()
                .to_string(),
            _ => String::new(),
        };
        if inner_actor != self.actor_id {
            log::debug!(
                "Ignoring {} of a Follow not issued by this actor",
                activity.activity_type
            );
            return Ok(None);
        }
        let target = match inner.get("object") {
            Some(Value::String(s)) => s.clone(),
            Some(Value::Object(m)) => m
                .get("id")
                .and_then(|v| v.as_str())
                .unwrap_or_default()
                .to_string(),
            _ => String::new(),
        };
        if target.is_empty() {
            return Ok(None);
        }
        let _ = self.storage.remove_follow_request(&self.actor_id, &target);
        if let Some(cb) = &self.on_follow_response {
            let _ =
                std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| cb(&target, accepted)));
        }
        log::info!(
            "{} Follow of {target} (from {})",
            if accepted { "Accepted" } else { "Rejected" },
            activity.actor
        );
        Ok(None)
    }

    fn handle_follow(&self, activity: &Activity, raw: &Value) -> Result<Option<Value>> {
        let actor_id = &activity.actor;
        log::info!("Processing Follow from {actor_id}");

        let target_actor_id = match &activity.object {
            Value::Object(m) => m
                .get("id")
                .and_then(|v| v.as_str())
                .unwrap_or_default()
                .to_string(),
            Value::String(s) => s.clone(),
            _ => String::new(),
        };
        let target_actor_id = if target_actor_id.is_empty() {
            self.actor_id.clone()
        } else {
            target_actor_id
        };

        if !self.is_local_follow_target(&target_actor_id) {
            log::info!("Ignoring Follow of non-local target {target_actor_id} from {actor_id}");
            return Ok(None);
        }

        let Some(actor_data) = self.fetch_actor(actor_id) else {
            log::warn!("Cannot fetch actor for Follow: {actor_id}");
            return Ok(None);
        };

        let actor = Actor::from_json(&actor_data);
        let inbox = actor.inbox.clone();
        let shared_inbox = actor
            .endpoints
            .get("sharedInbox")
            .and_then(|v| v.as_str())
            .unwrap_or_default()
            .to_string();

        let policy = self.resolve_follow_policy(&target_actor_id, actor_id);

        if policy == FollowPolicy::Reject {
            let reject_activity = json!({
                "@context": ap_context(),
                "id": format!("{}#reject-{}", self.actor_id, activity.id),
                "type": "Reject",
                "actor": self.actor_id,
                "object": raw,
            });
            let _ = self.deliver_to_inbox(&inbox, &reject_activity);
            return Ok(Some(reject_activity));
        }

        if policy == FollowPolicy::Manual {
            let request = FollowRequest {
                actor_id: actor_id.clone(),
                target_actor_id: target_actor_id.clone(),
                inbox: inbox.clone(),
                shared_inbox: shared_inbox.clone(),
                actor_data: actor_data.as_object().cloned().unwrap_or_default(),
                activity: raw.as_object().cloned().unwrap_or_default(),
                requested_at: Some(Utc::now()),
            };
            match self.storage.store_follow_request(&request) {
                Err(ActivityPubError::Unsupported(_)) => {
                    log::warn!(
                        "Storage does not support follow requests; auto-accepting Follow from {actor_id}"
                    );
                }
                Err(e) => return Err(e),
                Ok(()) => {
                    log::info!("Follow from {actor_id} to {target_actor_id} is pending approval");
                    return Ok(None);
                }
            }
        }

        let follower = Follower {
            actor_id: actor_id.clone(),
            inbox: inbox.clone(),
            shared_inbox,
            followed_at: Some(Utc::now()),
            actor_data: actor_data.as_object().cloned().unwrap_or_default(),
            target_actor_id,
        };
        self.storage.store_follower(&follower)?;
        self.invalidate_cached_documents(&[cache::followers_key()]);

        let accept_activity = json!({
            "@context": ap_context(),
            "id": format!("{}#accept-{}", self.actor_id, activity.id),
            "type": "Accept",
            "actor": self.actor_id,
            "object": raw,
        });
        let _ = self.deliver_to_inbox(&inbox, &accept_activity);
        Ok(Some(accept_activity))
    }

    /// Evaluate the configured `follow_policy` callback.
    fn resolve_follow_policy(
        &self,
        target_actor_id: &str,
        requester_actor_id: &str,
    ) -> FollowPolicy {
        let Some(cb) = &self.follow_policy else {
            return FollowPolicy::Accept;
        };
        match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            cb(target_actor_id, requester_actor_id)
        })) {
            Ok(Some(policy)) => policy,
            Ok(None) => FollowPolicy::Accept,
            Err(_) => {
                log::warn!(
                    "follow_policy callback failed for {requester_actor_id} -> {target_actor_id}; marking the request pending"
                );
                FollowPolicy::Manual
            }
        }
    }

    fn handle_undo(&self, activity: &Activity, _: &Value) -> Result<Option<Value>> {
        let Some(inner) = activity.object.as_object() else {
            return Ok(None);
        };
        let inner_type = inner.get("type").and_then(|v| v.as_str()).unwrap_or("");

        if inner_type == "Follow" {
            let actor_id = &activity.actor;
            log::info!("Processing Undo Follow from {actor_id}");

            let target_actor_id = match inner.get("object") {
                Some(Value::Object(m)) => m
                    .get("id")
                    .and_then(|v| v.as_str())
                    .unwrap_or_default()
                    .to_string(),
                Some(Value::String(s)) => s.clone(),
                _ => String::new(),
            };
            let target_actor_id = if target_actor_id.is_empty() {
                self.actor_id.clone()
            } else {
                target_actor_id
            };

            self.storage.remove_follower(actor_id, &target_actor_id)?;
            self.invalidate_cached_documents(&[cache::followers_key()]);
            // A withdrawn request may still be awaiting approval.
            let _ = self
                .storage
                .remove_follow_request(actor_id, &target_actor_id);
        } else if matches!(inner_type, "Like" | "Announce") {
            self.handle_undo_interaction(activity, inner)?;
        } else {
            log::info!("Ignoring Undo of type: {inner_type}");
        }
        Ok(None)
    }

    /// Handle Undo of a Like or Announce.
    fn handle_undo_interaction(
        &self,
        activity: &Activity,
        inner: &serde_json::Map<String, Value>,
    ) -> Result<()> {
        let actor_id = &activity.actor;
        let inner_type = inner.get("type").and_then(|v| v.as_str()).unwrap_or("");
        let interaction_type = match inner_type {
            "Like" => InteractionType::Like,
            "Announce" => InteractionType::Boost,
            _ => return Ok(()),
        };
        let target = match inner.get("object") {
            Some(Value::String(s)) => s.clone(),
            Some(Value::Object(m)) => m
                .get("id")
                .and_then(|v| v.as_str())
                .unwrap_or_default()
                .to_string(),
            _ => String::new(),
        };
        if !target.is_empty() {
            self.storage
                .delete_interaction(actor_id, &target, interaction_type)?;
            log::info!("Removed {inner_type} from {actor_id} on {target}");
        }
        Ok(())
    }

    /// Check if a Create activity is a direct mention of our actor.
    fn is_mention_of_actor(&self, activity: &Activity, obj_data: &Value) -> bool {
        if activity
            .to
            .iter()
            .chain(activity.cc.iter())
            .any(|r| r == &self.actor_id)
        {
            return true;
        }
        if let Some(Value::Array(tags)) = obj_data.get("tag") {
            for tag in tags {
                if tag.get("type").and_then(|v| v.as_str()) == Some("Mention")
                    && tag.get("href").and_then(|v| v.as_str()) == Some(&self.actor_id)
                {
                    return true;
                }
            }
        }
        false
    }

    fn author_fields(&self, actor_id: &str) -> (Option<Value>, String, String, String) {
        let actor_data = self.fetch_actor(actor_id);
        let mut name = String::new();
        let mut url = String::new();
        let mut photo = String::new();
        if let Some(data) = &actor_data {
            name = data
                .get("name")
                .and_then(|v| v.as_str())
                .filter(|s| !s.is_empty())
                .or_else(|| data.get("preferredUsername").and_then(|v| v.as_str()))
                .unwrap_or_default()
                .to_string();
            url = data
                .get("url")
                .and_then(|v| v.as_str())
                .filter(|s| !s.is_empty())
                .or_else(|| data.get("id").and_then(|v| v.as_str()))
                .unwrap_or_default()
                .to_string();
            match data.get("icon") {
                Some(Value::Object(m)) => {
                    photo = m
                        .get("url")
                        .and_then(|v| v.as_str())
                        .unwrap_or_default()
                        .to_string()
                }
                Some(Value::String(s)) => photo = s.clone(),
                _ => {}
            }
        }
        (actor_data, name, url, photo)
    }

    #[allow(clippy::too_many_arguments)]
    fn make_interaction(
        &self,
        activity: &Activity,
        obj: &Object,
        obj_data: &Value,
        interaction_type: InteractionType,
        effective_target: &str,
        actor_name: &str,
        actor_url_f: &str,
        actor_photo: &str,
    ) -> Interaction {
        let mut metadata = serde_json::Map::new();
        metadata.insert("raw_object".to_string(), obj_data.clone());
        Interaction {
            source_actor_id: activity.actor.clone(),
            target_resource: effective_target.to_string(),
            interaction_type,
            activity_id: activity.id.clone(),
            object_id: obj.id.clone(),
            content: obj.content.clone(),
            author_name: actor_name.to_string(),
            author_url: actor_url_f.to_string(),
            author_photo: actor_photo.to_string(),
            published: obj.published.or(Some(Utc::now())),
            status: InteractionStatus::Confirmed,
            metadata,
            mentioned_actors: mentioned_actors(obj_data),
            created_at: Some(Utc::now()),
            updated_at: Some(Utc::now()),
        }
    }

    fn handle_create(&self, activity: &Activity, _: &Value) -> Result<Option<Value>> {
        if !activity.object.is_object() {
            return Ok(None);
        }
        let obj_data_v = activity.object.clone();

        if self.strict_attribution {
            if let Err(e) = validate_attribution(&activity.actor, &obj_data_v) {
                log::warn!("Dropping Create from {}: {e}", activity.actor);
                return Ok(None);
            }
        }

        let obj = Object::from_json(&obj_data_v);
        let quote_target = extract_quote_target(&obj_data_v);
        let target = obj.in_reply_to.clone();

        // Determine interaction type: quote > reply > mention
        let (interaction_type, effective_target) = if let Some(qt) = quote_target {
            (InteractionType::Quote, qt)
        } else if let Some(t) = &target {
            (InteractionType::Reply, t.clone())
        } else if self.is_mention_of_actor(activity, &obj_data_v) {
            // Direct mention to the actor (guestbook entry): keyed to
            // the local actor, not the remote object's own URL.
            (InteractionType::Mention, self.actor_id.clone())
        } else {
            log::debug!(
                "Ignoring Create from {} — not a reply, quote or mention",
                activity.actor
            );
            return Ok(None);
        };

        let (_, author_name, author_url, author_photo) = self.author_fields(&activity.actor);

        let interaction = self.make_interaction(
            activity,
            &obj,
            &obj_data_v,
            interaction_type,
            &effective_target,
            &author_name,
            &author_url,
            &author_photo,
        );

        if let Some(cb) = &self.on_interaction_received {
            cb(&interaction);
        }

        let mentions_local_actor = interaction.mentioned_actors.contains(&self.actor_id);
        if !is_public(&obj_data_v) {
            log::debug!(
                "Skipped storing {} from {} on {effective_target} (not publicly addressed)",
                interaction_type.as_str(),
                activity.actor
            );
        } else if self.should_store_interaction(&effective_target, mentions_local_actor) {
            self.storage.store_interaction(&interaction)?;
            log::info!(
                "Stored {} from {} on {effective_target}",
                interaction_type.as_str(),
                activity.actor
            );
        }

        Ok(None)
    }

    fn handle_like(&self, activity: &Activity) -> Result<Option<Value>> {
        let target = match &activity.object {
            Value::String(s) => s.clone(),
            Value::Object(m) => m
                .get("id")
                .and_then(|v| v.as_str())
                .unwrap_or_default()
                .to_string(),
            _ => String::new(),
        };
        if target.is_empty() {
            return Ok(None);
        }

        let (_, author_name, author_url, author_photo) = self.author_fields(&activity.actor);
        let interaction = Interaction {
            source_actor_id: activity.actor.clone(),
            target_resource: target.clone(),
            interaction_type: InteractionType::Like,
            activity_id: activity.id.clone(),
            author_name,
            author_url,
            author_photo,
            published: activity.published.or(Some(Utc::now())),
            status: InteractionStatus::Confirmed,
            created_at: Some(Utc::now()),
            updated_at: Some(Utc::now()),
            ..Default::default()
        };

        if let Some(cb) = &self.on_interaction_received {
            cb(&interaction);
        }

        if self.should_store_interaction(&target, false) {
            self.storage.store_interaction(&interaction)?;
            log::info!("Stored like from {} on {target}", activity.actor);
        }
        Ok(None)
    }

    fn handle_announce(&self, activity: &Activity) -> Result<Option<Value>> {
        let target = match &activity.object {
            Value::String(s) => s.clone(),
            Value::Object(m) => m
                .get("id")
                .and_then(|v| v.as_str())
                .unwrap_or_default()
                .to_string(),
            _ => String::new(),
        };
        if target.is_empty() {
            return Ok(None);
        }

        let (_, author_name, author_url, author_photo) = self.author_fields(&activity.actor);
        let interaction = Interaction {
            source_actor_id: activity.actor.clone(),
            target_resource: target.clone(),
            interaction_type: InteractionType::Boost,
            activity_id: activity.id.clone(),
            author_name,
            author_url,
            author_photo,
            published: activity.published.or(Some(Utc::now())),
            status: InteractionStatus::Confirmed,
            created_at: Some(Utc::now()),
            updated_at: Some(Utc::now()),
            ..Default::default()
        };

        if let Some(cb) = &self.on_interaction_received {
            cb(&interaction);
        }

        if self.should_store_interaction(&target, false) {
            self.storage.store_interaction(&interaction)?;
            log::info!("Stored boost from {} on {target}", activity.actor);
        }
        Ok(None)
    }

    fn handle_delete(&self, activity: &Activity) -> Result<Option<Value>> {
        let target = match &activity.object {
            Value::Object(m) => m
                .get("id")
                .and_then(|v| v.as_str())
                .unwrap_or_default()
                .to_string(),
            Value::String(s) => s.clone(),
            _ => return Ok(None),
        };

        if target == activity.actor {
            // The remote actor deleted its own actor document — it can no
            // longer follow this local actor, so drop the follow record.
            self.storage
                .remove_follower(&activity.actor, &self.actor_id)?;
            self.invalidate_cached_documents(&[cache::followers_key()]);
            log::info!(
                "Removed follower {} for actor {} (actor deleted)",
                activity.actor,
                self.actor_id
            );
        }

        let found = self
            .storage
            .delete_interaction_by_object_id(&activity.actor, &target)?;

        if !found {
            for itype in InteractionType::variants() {
                self.storage
                    .delete_interaction(&activity.actor, &target, *itype)?;
            }
        }

        log::info!("Processed Delete from {} for {target}", activity.actor);
        Ok(None)
    }

    fn handle_update(&self, activity: &Activity) -> Result<Option<Value>> {
        if !activity.object.is_object() {
            return Ok(None);
        }
        let obj_data_v = activity.object.clone();

        if self.strict_attribution {
            if let Err(e) = validate_attribution(&activity.actor, &obj_data_v) {
                log::warn!("Dropping Update from {}: {e}", activity.actor);
                return Ok(None);
            }
        }

        let obj = Object::from_json(&obj_data_v);
        let Some(target) = obj.in_reply_to.clone() else {
            return Ok(None);
        };

        let (_, author_name, author_url, author_photo) = self.author_fields(&activity.actor);
        let mut metadata = serde_json::Map::new();
        metadata.insert("raw_object".to_string(), obj_data_v);

        let interaction = Interaction {
            source_actor_id: activity.actor.clone(),
            target_resource: target.clone(),
            interaction_type: InteractionType::Reply,
            activity_id: activity.id.clone(),
            object_id: obj.id.clone(),
            content: obj.content.clone(),
            author_name,
            author_url,
            author_photo,
            published: obj.published.or(Some(Utc::now())),
            status: InteractionStatus::Confirmed,
            metadata,
            created_at: Some(Utc::now()),
            updated_at: Some(Utc::now()),
            ..Default::default()
        };

        self.storage.store_interaction(&interaction)?;
        log::info!("Updated reply from {} on {target}", activity.actor);
        Ok(None)
    }

    /// Handle an incoming `QuoteRequest` activity (FEP-044f).
    fn handle_quote_request(&self, activity: &Activity, raw: &Value) -> Result<Option<Value>> {
        if !self.auto_approve_quotes {
            log::info!(
                "QuoteRequest from {} ignored (auto_approve disabled)",
                activity.actor
            );
            return Ok(None);
        }

        let quoted_uri = match &activity.object {
            Value::String(s) => s.clone(),
            Value::Object(m) => m
                .get("id")
                .and_then(|v| v.as_str())
                .unwrap_or_default()
                .to_string(),
            _ => String::new(),
        };
        let quoting_uri = match raw.get("instrument") {
            Some(Value::String(s)) => s.clone(),
            Some(Value::Object(m)) => m
                .get("id")
                .and_then(|v| v.as_str())
                .unwrap_or_default()
                .to_string(),
            _ => String::new(),
        };

        if quoted_uri.is_empty() || quoting_uri.is_empty() {
            log::info!("QuoteRequest missing object or instrument — ignoring");
            return Ok(None);
        }

        let Some(actor_data) = self.fetch_actor(&activity.actor) else {
            log::warn!("Cannot fetch actor for QuoteRequest: {}", activity.actor);
            return Ok(None);
        };

        let actor = Actor::from_json(&actor_data);
        let inbox_url = actor.inbox;
        if inbox_url.is_empty() {
            log::warn!("Cannot send Accept: no inbox for {}", activity.actor);
            return Ok(None);
        }

        let authorization = build_quote_authorization(
            &self.actor_id,
            &quoting_uri,
            &quoted_uri,
            None,
            None,
            None,
            None,
        );
        let auth_id = authorization
            .get("id")
            .and_then(|v| v.as_str())
            .unwrap_or_default()
            .to_string();

        self.storage
            .store_quote_authorization(&auth_id, &authorization)?;

        let accept_activity = json!({
            "@context": FEP_044F_CONTEXT.clone(),
            "type": "Accept",
            "id": format!("{}/activities/{}", self.actor_id, uuid::Uuid::new_v4()),
            "actor": self.actor_id,
            "to": activity.actor,
            "object": raw,
            "result": auth_id,
        });

        log::info!(
            "Accepting QuoteRequest {} with authorization {auth_id} → {inbox_url}",
            activity.id
        );
        let _ = self.deliver_to_inbox(&inbox_url, &accept_activity);
        Ok(Some(accept_activity))
    }

    /// Deliver an activity to a remote inbox.
    pub fn deliver_to_inbox(&self, inbox_url: &str, activity: &Value) -> bool {
        let body = match serde_json::to_vec(activity) {
            Ok(b) => b,
            Err(_) => return false,
        };
        let opts = SignOptions {
            headers: [(
                "Content-Type".to_string(),
                "application/activity+json".to_string(),
            )]
            .into_iter()
            .collect(),
            signed_headers: None,
        };
        let result = (|| -> Result<bool> {
            let mut headers = sign_request(
                &self.private_key,
                &self.key_id,
                "POST",
                inbox_url,
                Some(&body),
                &opts,
            )?;
            headers.insert("Content-Type".into(), "application/activity+json".into());
            headers.insert("User-Agent".into(), self.user_agent.clone());
            let resp = self.http_client.post(inbox_url, &headers, &body)?;
            if !resp.is_success() {
                log::warn!("Delivery to {inbox_url} returned status {}", resp.status);
                return Ok(false);
            }
            Ok(true)
        })();
        match result {
            Ok(b) => b,
            Err(e) => {
                log::warn!("Failed to deliver to {inbox_url}: {e}");
                false
            }
        }
    }
}
