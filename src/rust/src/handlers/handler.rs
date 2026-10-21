//! Main ActivityPub handler — mirrors `pubby.handlers._handler`.
//!
//! Rust has no keyword arguments: construct the handler with
//! [`ActivityPubHandler::new`] and an [`HandlerOptions`] built via
//! [`HandlerOptions::builder`] (or `HandlerOptions::default()`).

use std::sync::Arc;

use chrono::Utc;
use serde_json::{json, Value};

use crate::cache::{self, DocumentCache};
use crate::collections::ordered_collection;
use crate::crypto::keys::export_public_key_pem;
use crate::error::Result;
use crate::model::{ap_context, Actor, ActorConfig, FollowPolicy, Interaction, Object};
use crate::render::InteractionsRenderer;
use crate::storage::ActivityPubStorage;

use super::client::get_default_user_agent;
use super::discovery::{
    build_nodeinfo_discovery, build_nodeinfo_document, build_webfinger_response, NodeInfoStatsFn,
};
use super::inbox::{
    FollowPolicyFn, InboxProcessor, OnActivityReceived, OnFollowResponse, OnInteractionReceived,
};
use super::outbox::{DeliverFn, OnDeliveryResult, OutboxProcessor};

/// Options accepted by [`ActivityPubHandler::new`] — mirrors the
/// Python kwargs, including their defaults.
pub struct HandlerOptions {
    /// Path to a PEM-encoded private key file (alternative to
    /// `private_key` on [`ActivityPubHandler::new`]).
    pub private_key_path: Option<std::path::PathBuf>,
    /// Optional callback when an interaction is received.
    pub on_interaction_received: Option<OnInteractionReceived>,
    /// Domain for WebFinger `acct:` URIs. Defaults to the domain from
    /// `base_url`.
    pub webfinger_domain: Option<String>,
    /// User-Agent string for outgoing HTTP requests.
    pub user_agent: Option<String>,
    /// Timeout in seconds for outgoing HTTP requests.
    pub http_timeout: f64,
    /// Maximum delivery retry attempts.
    pub max_retries: usize,
    /// Maximum threads for concurrent delivery fan-out.
    pub max_delivery_workers: usize,
    /// Automatically send a `QuoteAuthorization` when an incoming quote
    /// targets a local object. Defaults to `true`.
    pub auto_approve_quotes: bool,
    /// Only store interactions whose `target_resource` is local or that
    /// mention the local actor.
    pub store_local_only: bool,
    /// Base URLs considered "local". Defaults to the actor's base URL.
    pub local_base_urls: Vec<String>,
    /// Software name for NodeInfo.
    pub software_name: String,
    /// Software version for NodeInfo. Defaults to pubby's version.
    pub software_version: Option<String>,
    /// Run delivery fan-out in a background thread. Defaults to `true`.
    pub async_delivery: bool,
    /// Allow-list of remote instance domains.
    pub allowed_instances: Option<Vec<String>>,
    /// Block-list of remote instance domains.
    pub blocked_instances: Option<Vec<String>>,
    /// Custom delivery callable invoked once per collected inbox.
    pub deliver: Option<DeliverFn>,
    /// Validate inbound `Create`/`Update` object attribution.
    pub strict_attribution: bool,
    /// Callback deciding how an incoming `Follow` is handled. When unset
    /// and `actor_config.manually_approves_followers` is set, every local
    /// target defaults to [`FollowPolicy::Manual`].
    pub follow_policy: Option<FollowPolicyFn>,
    /// Document cache used by framework adapters for dereference
    /// endpoints.
    pub document_cache: Option<Arc<DocumentCache>>,
    /// Allow outbound fetches/deliveries to loopback or private targets.
    /// For development and tests only. Defaults to `false`.
    pub allow_private_targets: bool,
    /// Hook invoked for every verified inbound activity before the
    /// built-in dispatch. Returning [`ActivityDisposition::Handled`]
    /// short-circuits Pubby's own processing.
    pub on_activity: Option<OnActivityReceived>,
    /// Callback invoked when an `Accept`/`Reject` settles one of this
    /// actor's outbound `Follow`s — `(target_actor_id, accepted)`.
    pub on_follow_response: Option<OnFollowResponse>,
    /// Callback invoked after every delivery attempt — observe-only seam
    /// for delivery ledgers.
    pub on_delivery_result: Option<OnDeliveryResult>,
    /// Callback producing real NodeInfo usage stats. When unset,
    /// `users = 1` and the stored activity count are reported.
    pub nodeinfo_stats: Option<NodeInfoStatsFn>,
}

impl Default for HandlerOptions {
    /// The same defaults as the Python kwargs
    /// (`http_timeout=15`, `max_retries=3`, `max_delivery_workers=10`,
    /// `auto_approve_quotes=True`, `async_delivery=True`,
    /// `software_name="pubby"`).
    fn default() -> Self {
        HandlerOptions {
            private_key_path: None,
            on_interaction_received: None,
            webfinger_domain: None,
            user_agent: None,
            http_timeout: 15.0,
            max_retries: 3,
            max_delivery_workers: 10,
            auto_approve_quotes: true,
            store_local_only: false,
            local_base_urls: Vec::new(),
            software_name: "pubby".to_string(),
            software_version: None,
            async_delivery: true,
            allowed_instances: None,
            blocked_instances: None,
            deliver: None,
            strict_attribution: false,
            follow_policy: None,
            document_cache: None,
            allow_private_targets: false,
            on_activity: None,
            on_follow_response: None,
            on_delivery_result: None,
            nodeinfo_stats: None,
        }
    }
}

/// Main ActivityPub handler — façade tying together the inbox processor,
/// outbox processor, discovery builders and renderer.
///
/// Mirrors `ActivityPubHandler`.
pub struct ActivityPubHandler {
    /// The storage backend.
    pub storage: Arc<dyn ActivityPubStorage>,
    /// Document cache for dereference endpoints.
    pub document_cache: Option<Arc<DocumentCache>>,
    /// The actor's base URL.
    pub base_url: String,
    /// The actor's username.
    pub username: String,
    /// The actor's display name.
    pub actor_name: Option<String>,
    /// The actor's bio/summary.
    pub actor_summary: String,
    /// Avatar URL.
    pub icon_url: String,
    /// URL path to the actor endpoint.
    pub actor_path: String,
    /// Actor type (`Person`, `Application`, …).
    pub actor_type: String,
    /// Whether follows require manual approval.
    pub manually_approves: bool,
    /// Actor `PropertyValue` attachments.
    pub actor_attachment: Vec<Value>,
    /// Human-readable profile URL.
    pub actor_url: String,
    /// `liked` collection URL from the actor config.
    pub actor_liked: Option<String>,
    /// `featured` collection URL from the actor config.
    pub actor_featured: Option<String>,
    /// `movedTo` actor URL from the actor config.
    pub actor_moved_to: Option<String>,
    /// `alsoKnownAs` actor URLs from the actor config.
    pub actor_also_known_as: Vec<String>,
    /// The full actor document ID.
    pub actor_id: String,
    /// Inbox URL.
    pub inbox_url: String,
    /// Outbox URL.
    pub outbox_url: String,
    /// Followers collection URL.
    pub followers_url: String,
    /// Following collection URL.
    pub following_url: String,
    /// Shared inbox URL.
    pub shared_inbox_url: String,
    /// Key ID (`actor_id#main-key`).
    pub key_id: String,
    /// WebFinger `acct:` domain.
    pub webfinger_domain: String,
    /// Public key PEM.
    pub public_key_pem: String,
    /// Software name for NodeInfo.
    pub software_name: String,
    /// Software version for NodeInfo.
    pub software_version: String,
    /// The inbox processor.
    pub inbox: InboxProcessor,
    /// The outbox processor.
    pub outbox: OutboxProcessor,
    /// The interactions renderer.
    pub renderer: InteractionsRenderer,
    /// NodeInfo stats callback.
    pub nodeinfo_stats: Option<NodeInfoStatsFn>,
}

impl ActivityPubHandler {
    /// Create a handler.
    ///
    /// :param storage: The storage backend.
    /// :param actor_config: Actor configuration (`ActorConfig`, or parse
    ///     a JSON dict via [`ActorConfig::from_json`]).
    /// :param private_key: RSA private key (object or PEM bytes/string).
    /// :param options: Additional [`HandlerOptions`].
    pub fn new(
        storage: Arc<dyn ActivityPubStorage>,
        actor_config: ActorConfig,
        private_key: rsa::RsaPrivateKey,
        options: HandlerOptions,
    ) -> Result<Self> {
        let base_url = actor_config.base_url.clone();
        let username = actor_config.username.clone();
        let actor_name = actor_config.name.clone();
        let actor_summary = actor_config.summary.clone();
        let icon_url = actor_config.icon_url.clone();
        let actor_path = actor_config.actor_path.clone();
        let actor_type = actor_config.actor_type.clone();
        let manually_approves = actor_config.manually_approves_followers;
        let actor_attachment = actor_config.attachment.clone();
        let actor_url = actor_config.url.clone().unwrap_or_else(|| base_url.clone());
        let actor_liked = actor_config.liked.clone();
        let actor_featured = actor_config.featured.clone();
        let actor_moved_to = actor_config.moved_to.clone();
        let actor_also_known_as = actor_config.also_known_as.clone();

        let actor_id = format!("{base_url}{actor_path}");
        let inbox_url = format!("{base_url}/ap/inbox");
        let outbox_url = format!("{base_url}/ap/outbox");
        let followers_url = format!("{base_url}/ap/followers");
        let following_url = format!("{base_url}/ap/following");
        let shared_inbox_url = inbox_url.clone();
        let key_id = format!("{actor_id}#main-key");

        let webfinger_domain = options
            .webfinger_domain
            .clone()
            .or_else(|| {
                url::Url::parse(&base_url)
                    .ok()
                    .and_then(|u| u.host_str().map(String::from))
            })
            .unwrap_or_default();

        let public_key_pem = export_public_key_pem(&rsa::RsaPublicKey::from(&private_key))?;

        let software_version = options
            .software_version
            .clone()
            .unwrap_or_else(|| crate::VERSION.to_string());

        let mut http_client = crate::http::HttpClient::new(std::time::Duration::from_secs_f64(
            options.http_timeout.max(1.0),
        ));
        http_client.allow_private_targets(options.allow_private_targets);

        let follow_policy = options.follow_policy.clone().or_else(|| {
            if manually_approves {
                Some(Arc::new(|_: &str, _: &str| Some(FollowPolicy::Manual)) as FollowPolicyFn)
            } else {
                None
            }
        });

        let mut inbox = InboxProcessor::new(
            storage.clone(),
            actor_id.clone(),
            private_key.clone(),
            key_id.clone(),
            options.on_interaction_received.clone(),
            options.user_agent.clone(),
            http_client.clone(),
            options.auto_approve_quotes,
            options.store_local_only,
            options.local_base_urls.clone(),
            options.allowed_instances.clone(),
            options.blocked_instances.clone(),
            options.strict_attribution,
            follow_policy,
            options.document_cache.clone(),
        );
        inbox.on_activity = options.on_activity.clone();
        inbox.on_follow_response = options.on_follow_response.clone();

        let outbox = OutboxProcessor {
            storage: storage.clone(),
            actor_id: actor_id.clone(),
            document_cache: options.document_cache.clone(),
            private_key: private_key.clone(),
            key_id: key_id.clone(),
            followers_collection_url: followers_url.clone(),
            max_retries: options.max_retries.max(1),
            retry_base_delay: 10.0,
            max_delivery_workers: options.max_delivery_workers.max(1),
            user_agent: options
                .user_agent
                .clone()
                .unwrap_or_else(|| get_default_user_agent(&actor_id)),
            http_client,
            async_delivery: options.async_delivery,
            allowed_instances: options.allowed_instances.clone(),
            blocked_instances: options.blocked_instances.clone(),
            deliver: options.deliver.clone(),
            on_delivery_result: options.on_delivery_result.clone(),
        };
        let nodeinfo_stats = options.nodeinfo_stats.clone();

        Ok(ActivityPubHandler {
            storage,
            document_cache: options.document_cache,
            base_url,
            username,
            actor_name,
            actor_summary,
            icon_url,
            actor_path,
            actor_type,
            manually_approves,
            actor_attachment,
            actor_url,
            actor_liked,
            actor_featured,
            actor_moved_to,
            actor_also_known_as,
            actor_id,
            inbox_url,
            outbox_url,
            followers_url,
            following_url,
            shared_inbox_url,
            key_id,
            webfinger_domain,
            public_key_pem,
            software_name: if options.software_name.is_empty() {
                "pubby".to_string()
            } else {
                options.software_name
            },
            software_version,
            inbox,
            outbox,
            renderer: InteractionsRenderer::new(),
            nodeinfo_stats,
        })
    }

    /// Attach (or replace) the document cache, propagating it to the
    /// sub-processors.
    pub fn set_document_cache(&mut self, cache: Option<Arc<DocumentCache>>) {
        self.document_cache = cache.clone();
        self.inbox.document_cache = cache.clone();
        self.outbox.document_cache = cache;
    }

    fn invalidate_cached_documents(&self, prefixes: &[Vec<String>]) {
        if let Some(cache) = &self.document_cache {
            cache.invalidate_prefix(prefixes);
        }
    }

    // ---------- Inbox ----------

    /// Process an incoming activity on the inbox.
    pub fn process_inbox_activity(
        &self,
        activity_data: &Value,
        method: &str,
        path: &str,
        headers: Option<&std::collections::HashMap<String, String>>,
        body: Option<&[u8]>,
        skip_verification: bool,
    ) -> Result<Option<Value>> {
        self.inbox.process(
            activity_data,
            method,
            path,
            headers,
            body,
            skip_verification,
        )
    }

    // ---------- Outbox ----------

    /// Publish an object to followers.
    ///
    /// :param activity_type: `"Create"`, `"Update"`, or `"Delete"`
    ///     (defaults to `"Create"` when `None`).
    /// :return: The published activity.
    pub fn publish_object(&self, obj: &Object, activity_type: Option<&str>) -> Result<Value> {
        let activity = match activity_type.unwrap_or("Create") {
            "Create" => self.outbox.build_create_activity(obj),
            "Update" => self.outbox.build_update_activity_obj(obj),
            "Delete" => self.outbox.build_delete_activity_obj(&obj.id),
            other => {
                return Err(crate::error::ActivityPubError::InvalidValue(format!(
                    "Unsupported activity type: {other}"
                )))
            }
        };
        self.outbox.publish(&activity)
    }

    /// Publish a pre-built activity to followers.
    ///
    /// Unlike [`publish_object`](Self::publish_object), this does not wrap
    /// the payload in a Create/Update envelope — it publishes the activity
    /// dict as-is (for `Like`, `Announce`, `Undo`, `Follow`, …).
    pub fn publish_activity(&self, activity: &Value) -> Result<Value> {
        self.outbox.publish(activity)
    }

    /// Get the outbox collection (defaults: `limit=20`, `offset=0`).
    pub fn get_outbox(&self, limit: Option<usize>, offset: Option<usize>) -> Result<Value> {
        super::outbox::get_outbox_collection(
            self.storage.as_ref(),
            &self.outbox_url,
            limit.unwrap_or(20),
            offset.unwrap_or(0),
        )
    }

    // ---------- Actor ----------

    /// Build the actor's JSON-LD document.
    pub fn get_actor_document(&self) -> Value {
        let actor = Actor {
            id: self.actor_id.clone(),
            actor_type: self.actor_type.clone(),
            preferred_username: self.username.clone(),
            name: self.actor_name.clone().unwrap_or_default(),
            summary: self.actor_summary.clone(),
            inbox: self.inbox_url.clone(),
            outbox: self.outbox_url.clone(),
            followers: self.followers_url.clone(),
            following: self.following_url.clone(),
            icon: if self.icon_url.is_empty() {
                None
            } else {
                Some(json!({"type": "Image", "url": self.icon_url}))
            },
            public_key_pem: self.public_key_pem.clone(),
            manually_approves_followers: self.manually_approves,
            discoverable: true,
            url: self.actor_url.clone(),
            endpoints: [("sharedInbox".to_string(), json!(self.shared_inbox_url))]
                .into_iter()
                .collect(),
            attachment: self.actor_attachment.clone(),
            liked: self.actor_liked.clone(),
            featured: self.actor_featured.clone(),
            moved_to: self.actor_moved_to.clone(),
            also_known_as: self.actor_also_known_as.clone(),
        };
        actor.to_json()
    }

    /// Publish an `Update` activity for the actor itself.
    pub fn publish_actor_update(&self, document: Option<&Value>) -> Result<Value> {
        let actor_doc = document
            .cloned()
            .unwrap_or_else(|| self.get_actor_document());
        self.invalidate_cached_documents(&[
            cache::actor_document_key(),
            cache::route_key(&["webfinger"]),
            cache::route_key(&["outbox"]),
            cache::followers_key(),
            cache::following_key(),
        ]);
        let activity = json!({
            "@context": ap_context(),
            "id": format!("{}#update-profile-{}", self.actor_id, uuid::Uuid::new_v4()),
            "type": "Update",
            "actor": self.actor_id,
            "published": crate::model::dt_to_iso(&Utc::now()),
            "to": [crate::model::AS_PUBLIC],
            "cc": [self.followers_url],
            "object": actor_doc,
        });
        self.outbox.publish(&activity)
    }

    // ---------- Collections ----------

    /// Build the followers OrderedCollection.
    ///
    /// :param actor_id: If provided, followers of this actor; otherwise
    ///     the handler's configured actor.
    pub fn get_followers_collection(&self, actor_id: Option<&str>) -> Result<Value> {
        let target = actor_id.unwrap_or(&self.actor_id);
        let followers = self.storage.get_followers(Some(target))?;
        let items: Vec<Value> = followers.iter().map(|f| json!(f.actor_id)).collect();
        Ok(ordered_collection(&self.followers_url, &items, None))
    }

    /// Build the following OrderedCollection (empty for blogs).
    pub fn get_following_collection(&self) -> Result<Value> {
        Ok(ordered_collection(&self.following_url, &[], None))
    }

    // ---------- Discovery ----------

    /// Whether `resource` is a spelling of this actor's `acct:` URI that
    /// [`get_webfinger_response`](Self::get_webfinger_response) accepts.
    pub fn is_valid_webfinger_resource(&self, resource: Option<&str>) -> bool {
        let Some(resource) = resource else {
            return true;
        };
        let mut normalized = resource;
        if !normalized.to_lowercase().starts_with("acct:") {
            return false;
        }
        normalized = &normalized[5..];
        if let Some(rest) = normalized.strip_prefix('@') {
            normalized = rest;
        }
        let expected = format!("{}@{}", self.username, self.webfinger_domain);
        normalized.eq_ignore_ascii_case(&expected)
    }

    /// Build the WebFinger response for the configured actor.
    pub fn get_webfinger_response(&self, resource: Option<&str>) -> Option<Value> {
        if resource.is_some() && !self.is_valid_webfinger_resource(resource) {
            return None;
        }
        Some(build_webfinger_response(
            &self.username,
            &self.webfinger_domain,
            &self.actor_id,
        ))
    }

    /// Build the NodeInfo well-known discovery document.
    pub fn get_nodeinfo_discovery(&self) -> Value {
        build_nodeinfo_discovery(&self.base_url)
    }

    /// Build the NodeInfo 2.1 document.
    ///
    /// When `HandlerOptions::nodeinfo_stats` is set the callback supplies
    /// real usage numbers; otherwise `users = 1` and the stored activity
    /// count are reported (legacy single-actor behavior).
    pub fn get_nodeinfo_document(&self) -> Result<Value> {
        let (users, posts, open_reg) = match &self.nodeinfo_stats {
            Some(f) => {
                let s = f();
                (s.users, s.posts, s.open_registrations)
            }
            None => {
                let activities = self.storage.get_activities(10000, 0)?;
                (1, activities.len() as u64, false)
            }
        };
        Ok(build_nodeinfo_document(
            &self.software_name,
            Some(&self.software_version),
            users,
            posts,
            open_reg,
        ))
    }

    /// Verify the HTTP signature on an inbound dereference request.
    ///
    /// Used by the authorized-fetch gate in `server::dispatch` (and by
    /// hosts' own middleware); returns the signer's actor URL.
    pub fn verify_fetch_signature(
        &self,
        method: &str,
        request_target: &str,
        headers: &std::collections::HashMap<String, String>,
    ) -> Result<String> {
        self.inbox
            .verify_signature(method, request_target, headers, None, None)
    }

    // ---------- Quote authorizations ----------

    /// Retrieve a stored QuoteAuthorization by its full ID/URL.
    pub fn get_quote_authorization(&self, authorization_id: &str) -> Result<Option<Value>> {
        self.storage.get_quote_authorization(authorization_id)
    }

    // ---------- Rendering ----------

    /// Render a single interaction as HTML.
    pub fn render_interaction(&self, interaction: &Interaction) -> String {
        self.renderer.render_interaction(interaction, None)
    }

    /// Render a list of interactions as HTML.
    pub fn render_interactions(&self, interactions: &[Interaction]) -> String {
        self.renderer.render_interactions(interactions, None)
    }
}
