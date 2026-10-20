//! Outbox processing — build activities, fan-out delivery to follower
//! inboxes. Mirrors `pubby.handlers._outbox`.

use std::collections::HashSet;
use std::sync::Arc;
use std::time::Duration;

use chrono::Utc;
use serde_json::{json, Value};

use crate::cache::{self, DocumentCache};
use crate::crypto::signatures::{sign_request, SignOptions};
use crate::error::Result;
use crate::http::HttpClient;
use crate::model::{ap_context, Actor, Follower, Object, AS_PUBLIC};
use crate::moderation::is_domain_blocked;
use crate::quotes::FEP_044F_CONTEXT;
use crate::storage::ActivityPubStorage;

/// URL patterns that are not actor URLs (collections, public addressing).
const NON_ACTOR_URL_PATTERNS: &[&str] = &["/followers", "/following", "/outbox", "/inbox"];

/// Collect unique inbox URLs from followers, preferring shared inboxes.
///
/// Shared inboxes are preferred over per-actor inboxes to reduce the
/// number of delivery requests; followers without any inbox are skipped.
pub fn collect_inboxes(followers: &[Follower]) -> Vec<String> {
    let mut seen = HashSet::new();
    let mut inboxes = Vec::new();
    for follower in followers {
        let inbox = if follower.shared_inbox.is_empty() {
            &follower.inbox
        } else {
            &follower.shared_inbox
        };
        if !inbox.is_empty() && seen.insert(inbox.clone()) {
            inboxes.push(inbox.clone());
        }
    }
    inboxes
}

/// Deliver an activity to a single inbox with one signed POST request.
///
/// Unlike the built-in fan-out, this performs no retries and does not
/// raise on HTTP error statuses: it returns the response status code so
/// callers (e.g. a task-queue worker) can decide their own retry policy.
/// Network-level failures still surface as errors.
///
/// :return: The HTTP response status code.
pub fn deliver_activity(
    activity: &Value,
    inbox_url: &str,
    key_id: &str,
    private_key: &rsa::RsaPrivateKey,
    user_agent: Option<&str>,
    client: &HttpClient,
) -> Result<u16> {
    let body = serde_json::to_vec(activity)?;
    let content_type = "application/activity+json";
    let content_length = body.len().to_string();

    let signed_headers: Vec<String> = [
        "(request-target)",
        "host",
        "date",
        "digest",
        "content-type",
        "content-length",
    ]
    .iter()
    .map(|s| s.to_string())
    .collect();

    let opts = SignOptions {
        headers: [
            ("Content-Type".to_string(), content_type.to_string()),
            ("Content-Length".to_string(), content_length.clone()),
        ]
        .into_iter()
        .collect(),
        signed_headers: Some(&signed_headers),
    };
    let mut headers = sign_request(private_key, key_id, "POST", inbox_url, Some(&body), &opts)?;
    headers.insert("Content-Type".into(), content_type.into());
    headers.insert("Content-Length".into(), content_length);
    headers.insert(
        "User-Agent".into(),
        user_agent
            .map(String::from)
            .unwrap_or_else(|| format!("pubby/{}", crate::VERSION)),
    );

    let resp = client.post(inbox_url, &headers, &body)?;
    if resp.is_success() {
        log::info!("Delivered to {inbox_url} (status {})", resp.status);
    } else {
        log::warn!(
            "Delivery to {inbox_url} returned status {}: {}",
            resp.status,
            &resp.text()[..resp.text().len().min(200)]
        );
    }
    Ok(resp.status)
}

fn new_activity_id(actor_id: &str) -> String {
    format!("{actor_id}/activities/{}", uuid::Uuid::new_v4())
}

fn format_published(published: Option<&Value>) -> String {
    match published {
        Some(Value::String(s)) => s.clone(),
        _ => crate::model::dt_to_iso(&Utc::now()),
    }
}

fn ts_now() -> String {
    crate::model::dt_to_iso(&Utc::now())
}

/// Build a `Like` activity targeting a remote object.
///
/// Audience is caller-provided; when `to`/`cc` are `None`, the activity
/// defaults to `to=[AS_PUBLIC]` and `cc=[]`.
pub fn build_like_activity(
    actor_id: &str,
    object_id: &str,
    to: Option<Vec<String>>,
    cc: Option<Vec<String>>,
    activity_id: Option<&str>,
    published: Option<&Value>,
    context: Option<&Value>,
) -> Value {
    json!({
        "@context": context.cloned().unwrap_or_else(ap_context),
        "id": activity_id.map(String::from).unwrap_or_else(|| new_activity_id(actor_id)),
        "type": "Like",
        "actor": actor_id,
        "published": format_published(published),
        "object": object_id,
        "to": to.unwrap_or_else(|| vec![AS_PUBLIC.to_string()]),
        "cc": cc.unwrap_or_default(),
    })
}

/// Build an `Announce` (boost) activity targeting a remote object.
pub fn build_announce_activity(
    actor_id: &str,
    object_id: &str,
    to: Option<Vec<String>>,
    cc: Option<Vec<String>>,
    activity_id: Option<&str>,
    published: Option<&Value>,
    context: Option<&Value>,
) -> Value {
    json!({
        "@context": context.cloned().unwrap_or_else(ap_context),
        "id": activity_id.map(String::from).unwrap_or_else(|| new_activity_id(actor_id)),
        "type": "Announce",
        "actor": actor_id,
        "published": format_published(published),
        "object": object_id,
        "to": to.unwrap_or_else(|| vec![AS_PUBLIC.to_string()]),
        "cc": cc.unwrap_or_default(),
    })
}

/// Build an `Undo` activity wrapping another activity.
///
/// When `to`/`cc` are `None`, the addressing is inherited from
/// `inner_activity`; if the inner activity has no addressing, it defaults
/// to `to=[AS_PUBLIC]` and `cc=[]`.
pub fn build_undo_activity(
    inner_activity: &Value,
    actor_id: &str,
    activity_id: Option<&str>,
    published: Option<&Value>,
    to: Option<Vec<String>>,
    cc: Option<Vec<String>>,
    context: Option<&Value>,
) -> Value {
    let default_to = crate::model::str_list(inner_activity.get("to"));
    let to = to.unwrap_or(if default_to.is_empty() {
        vec![AS_PUBLIC.to_string()]
    } else {
        default_to
    });
    let cc = cc.unwrap_or_else(|| crate::model::str_list(inner_activity.get("cc")));
    json!({
        "@context": context.cloned().unwrap_or_else(ap_context),
        "id": activity_id.map(String::from).unwrap_or_else(|| new_activity_id(actor_id)),
        "type": "Undo",
        "actor": actor_id,
        "published": format_published(published),
        "object": inner_activity,
        "to": to,
        "cc": cc,
    })
}

/// Build a `Delete` activity wrapping a Tombstone for `object_id`.
///
/// When `to` is omitted the activity is addressed to the public
/// ActivityStreams collection. When `cc` is omitted it defaults to the
/// actor's followers collection via `{actor_id}/followers`.
pub fn build_delete_activity(
    actor_id: &str,
    object_id: &str,
    to: Option<Vec<String>>,
    cc: Option<Vec<String>>,
    activity_id: Option<&str>,
    published: Option<&Value>,
    context: Option<&Value>,
) -> Value {
    json!({
        "@context": context.cloned().unwrap_or_else(ap_context),
        "id": activity_id.map(String::from).unwrap_or_else(|| new_activity_id(actor_id)),
        "type": "Delete",
        "actor": actor_id,
        "published": format_published(published),
        "to": to.unwrap_or_else(|| vec![AS_PUBLIC.to_string()]),
        "cc": cc.unwrap_or_else(|| vec![format!("{actor_id}/followers")]),
        "object": {
            "id": object_id,
            "type": "Tombstone",
        },
    })
}

/// Build an `Update` activity wrapping an updated object document.
///
/// The embedded object is `object_doc` with its `to`/`cc` rewritten to
/// the activity's audience and `updated` stamped; `object_doc` itself is
/// not mutated.
#[allow(clippy::too_many_arguments)]
pub fn build_update_activity(
    actor_id: &str,
    object_doc: &Value,
    to: Option<Vec<String>>,
    cc: Option<Vec<String>>,
    activity_id: Option<&str>,
    published: Option<&Value>,
    updated: Option<&Value>,
    context: Option<&Value>,
) -> Value {
    let to = to.unwrap_or_else(|| vec![AS_PUBLIC.to_string()]);
    let cc = cc.unwrap_or_else(|| vec![format!("{actor_id}/followers")]);
    let published_str = format_published(published);
    let mut obj = object_doc.clone();
    if let Some(m) = obj.as_object_mut() {
        m.insert("to".into(), json!(to));
        m.insert("cc".into(), json!(cc));
        m.insert(
            "updated".into(),
            match updated {
                Some(u) => Value::String(format_published(Some(u))),
                None => Value::String(published_str.clone()),
            },
        );
    }
    json!({
        "@context": context.cloned().unwrap_or_else(ap_context),
        "id": activity_id.map(String::from).unwrap_or_else(|| new_activity_id(actor_id)),
        "type": "Update",
        "actor": actor_id,
        "published": published_str,
        "to": to,
        "cc": cc,
        "object": obj,
    })
}

/// Build a FEP-044f `QuoteRequest` activity for a quoting object.
///
/// `instrument` is the quoting object — pass the full Note document
/// (embedded) so the target server can approve without dereferencing it;
/// a bare object URL also works when the document is publicly fetchable.
/// `target_actor_id` is the quoted object's author: the request is
/// addressed to them alone and delivered to their inbox.
#[allow(clippy::too_many_arguments)]
pub fn build_quote_request_activity(
    actor_id: &str,
    quoted_object_id: &str,
    instrument: &Value,
    target_actor_id: &str,
    to: Option<Vec<String>>,
    cc: Option<Vec<String>>,
    activity_id: Option<&str>,
    published: Option<&Value>,
    context: Option<&Value>,
) -> Value {
    json!({
        "@context": context.cloned().unwrap_or_else(|| FEP_044F_CONTEXT.clone()),
        "id": activity_id.map(String::from).unwrap_or_else(|| new_activity_id(actor_id)),
        "type": "QuoteRequest",
        "actor": actor_id,
        "published": format_published(published),
        "object": quoted_object_id,
        "instrument": instrument,
        "to": to.unwrap_or_else(|| vec![target_actor_id.to_string()]),
        "cc": cc.unwrap_or_default(),
    })
}

/// Build a FEP-044f `QuoteAuthorization` document.
///
/// `author_actor_id` is the quoted object's author — the only party
/// allowed to approve the quote — and becomes the document's
/// `attributedTo`. `interacting_object` is the quoting post and
/// `interaction_target` the quoted post.
pub fn build_quote_authorization(
    author_actor_id: &str,
    interacting_object: &str,
    interaction_target: &str,
    authorization_id: Option<&str>,
    to: Option<Vec<String>>,
    cc: Option<Vec<String>>,
    context: Option<&Value>,
) -> Value {
    let mut authorization = json!({
        "@context": context.cloned().unwrap_or_else(|| FEP_044F_CONTEXT.clone()),
        "id": authorization_id.map(String::from).unwrap_or_else(|| {
            format!("{author_actor_id}/quote_authorizations/{}", uuid::Uuid::new_v4())
        }),
        "type": "QuoteAuthorization",
        "attributedTo": author_actor_id,
        "interactingObject": interacting_object,
        "interactionTarget": interaction_target,
    });
    if let Some(m) = authorization.as_object_mut() {
        if let Some(to) = to {
            m.insert("to".into(), json!(to));
        }
        if let Some(cc) = cc {
            m.insert("cc".into(), json!(cc));
        }
    }
    authorization
}

/// Callback invoked once per collected inbox instead of the built-in
/// fan-out: `deliver(inbox_url, activity)`.
pub type DeliverFn = Arc<dyn Fn(&str, &Value) + Send + Sync>;

/// Handles outbound activity creation and delivery.
///
/// Mirrors `OutboxProcessor`.
pub struct OutboxProcessor {
    /// Storage backend.
    pub storage: Arc<dyn ActivityPubStorage>,
    /// This server's actor ID (URL).
    pub actor_id: String,
    /// Document cache for dereference endpoints.
    pub document_cache: Option<Arc<DocumentCache>>,
    /// RSA private key for signing outgoing requests.
    pub private_key: rsa::RsaPrivateKey,
    /// Key ID for HTTP signatures.
    pub key_id: String,
    /// URL of the followers collection.
    pub followers_collection_url: String,
    /// Maximum delivery retry attempts.
    pub max_retries: usize,
    /// Base delay (seconds) for exponential backoff.
    pub retry_base_delay: f64,
    /// Maximum threads for concurrent fan-out delivery.
    pub max_delivery_workers: usize,
    /// User-Agent for outgoing HTTP requests.
    pub user_agent: String,
    /// HTTP client for outgoing requests.
    pub http_client: HttpClient,
    /// Whether fan-out delivery runs in a background thread.
    pub async_delivery: bool,
    /// Allow-list of remote instance domains.
    pub allowed_instances: Option<Vec<String>>,
    /// Block-list of remote instance domains.
    pub blocked_instances: Option<Vec<String>>,
    /// Optional custom delivery callable.
    pub deliver: Option<DeliverFn>,
}

impl OutboxProcessor {
    fn new_activity_id(&self) -> String {
        new_activity_id(&self.actor_id)
    }

    /// Build a `Create` activity wrapping an [`Object`].
    pub fn build_create_activity(&self, obj: &Object) -> Value {
        let activity_id = self.new_activity_id();
        let has_explicit_addressing = !obj.to.is_empty() || !obj.cc.is_empty();
        let to_field = if has_explicit_addressing {
            obj.to.clone()
        } else {
            vec![AS_PUBLIC.to_string()]
        };
        let cc_field = if has_explicit_addressing {
            obj.cc.clone()
        } else if self.followers_collection_url.is_empty() {
            vec![]
        } else {
            vec![self.followers_collection_url.clone()]
        };
        json!({
            "@context": ap_context(),
            "id": activity_id,
            "type": "Create",
            "actor": self.actor_id,
            "published": ts_now(),
            "to": to_field,
            "cc": cc_field,
            "object": obj.to_json(),
        })
    }

    /// Build an `Update` activity wrapping an [`Object`].
    pub fn build_update_activity_obj(&self, obj: &Object) -> Value {
        let has_explicit_addressing = !obj.to.is_empty() || !obj.cc.is_empty();
        build_update_activity(
            &self.actor_id,
            &obj.to_json(),
            if has_explicit_addressing {
                Some(obj.to.clone())
            } else {
                None
            },
            if has_explicit_addressing {
                Some(obj.cc.clone())
            } else {
                Some(if self.followers_collection_url.is_empty() {
                    vec![]
                } else {
                    vec![self.followers_collection_url.clone()]
                })
            },
            None,
            None,
            None,
            Some(&ap_context()),
        )
    }

    /// Build a `Delete` activity for an object.
    pub fn build_delete_activity_obj(&self, object_id: &str) -> Value {
        build_delete_activity(
            &self.actor_id,
            object_id,
            None,
            Some(if self.followers_collection_url.is_empty() {
                vec![]
            } else {
                vec![self.followers_collection_url.clone()]
            }),
            None,
            None,
            Some(&ap_context()),
        )
    }

    /// Build a `Like` activity targeting a remote object.
    pub fn build_like_activity(
        &self,
        object_url: &str,
        activity_id: Option<&str>,
        published: Option<&Value>,
    ) -> Value {
        build_like_activity(
            &self.actor_id,
            object_url,
            None,
            Some(if self.followers_collection_url.is_empty() {
                vec![]
            } else {
                vec![self.followers_collection_url.clone()]
            }),
            activity_id,
            published,
            None,
        )
    }

    /// Build an `Announce` (boost) activity targeting a remote object.
    pub fn build_announce_activity(
        &self,
        object_url: &str,
        activity_id: Option<&str>,
        published: Option<&Value>,
    ) -> Value {
        build_announce_activity(
            &self.actor_id,
            object_url,
            None,
            Some(if self.followers_collection_url.is_empty() {
                vec![]
            } else {
                vec![self.followers_collection_url.clone()]
            }),
            activity_id,
            published,
            None,
        )
    }

    /// Build an `Undo` activity wrapping another activity.
    pub fn build_undo_activity(&self, inner_activity: &Value) -> Value {
        build_undo_activity(inner_activity, &self.actor_id, None, None, None, None, None)
    }

    /// Build a FEP-044f `QuoteRequest` activity for a quoting object.
    #[allow(clippy::too_many_arguments)]
    pub fn build_quote_request_activity(
        &self,
        quoted_object_id: &str,
        instrument: &Value,
        target_actor_id: &str,
        activity_id: Option<&str>,
        published: Option<&Value>,
    ) -> Value {
        build_quote_request_activity(
            &self.actor_id,
            quoted_object_id,
            instrument,
            target_actor_id,
            None,
            None,
            activity_id,
            published,
            None,
        )
    }

    /// Publish an activity: store in outbox and fan out to followers and
    /// mentioned actors.
    ///
    /// :return: The stored activity.
    pub fn publish(&self, activity: &Value) -> Result<Value> {
        let activity_id = activity
            .get("id")
            .and_then(|v| v.as_str())
            .map(String::from)
            .unwrap_or_else(|| self.new_activity_id());
        let mut activity = activity.clone();
        if activity.get("id").is_none() {
            let Some(m) = activity.as_object_mut() else {
                return Err(crate::error::ActivityPubError::InvalidValue(
                    "activity must be a JSON object".to_string(),
                ));
            };
            m.insert("id".to_string(), json!(activity_id));
        }
        self.storage.store_activity(&activity_id, &activity)?;

        if let Some(c) = &self.document_cache {
            c.invalidate_prefix(&[cache::route_key(&["outbox"]), cache::nodeinfo_key()]);
        }

        let addressed_to_followers = self.is_addressed_to_followers(&activity);
        let follower_inboxes = if addressed_to_followers {
            let followers = self.storage.get_followers(Some(&self.actor_id))?;
            collect_inboxes(&followers)
        } else {
            Vec::new()
        };

        let recipient_inboxes = self.collect_recipient_inboxes(&activity);

        let mut seen: HashSet<String> = follower_inboxes.iter().cloned().collect();
        let mut inboxes = follower_inboxes;
        for inbox in recipient_inboxes {
            if seen.insert(inbox.clone()) {
                inboxes.push(inbox);
            }
        }

        if self.allowed_instances.is_some() || self.blocked_instances.is_some() {
            let unfiltered = inboxes.len();
            inboxes.retain(|url| {
                !is_domain_blocked(
                    url,
                    self.allowed_instances.as_deref(),
                    self.blocked_instances.as_deref(),
                )
            });
            let skipped = unfiltered - inboxes.len();
            if skipped > 0 {
                log::info!("Skipped {skipped} inbox(es) on blocked/non-allowed instances for activity {activity_id}");
            }
        }

        log::info!(
            "Delivering activity {activity_id} to {} inboxes",
            inboxes.len()
        );

        if let Some(deliver) = &self.deliver {
            for inbox_url in &inboxes {
                let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
                    deliver(inbox_url, &activity)
                }));
            }
        } else if self.async_delivery {
            let proc = self.clone_parts();
            let inboxes_c = inboxes.clone();
            let activity_c = activity.clone();
            std::thread::spawn(move || proc.fan_out_delivery(&inboxes_c, &activity_c));
        } else {
            self.fan_out_delivery(&inboxes, &activity);
        }

        Ok(activity)
    }

    fn clone_parts(&self) -> DeliveryContext {
        DeliveryContext {
            private_key: self.private_key.clone(),
            key_id: self.key_id.clone(),
            max_retries: self.max_retries,
            retry_base_delay: self.retry_base_delay,
            max_delivery_workers: self.max_delivery_workers,
            user_agent: self.user_agent.clone(),
            http_client: self.http_client.clone(),
        }
    }

    /// Deliver an activity to multiple inboxes concurrently.
    pub fn fan_out_delivery(&self, inboxes: &[String], activity: &Value) {
        let ctx = self.clone_parts();
        ctx.fan_out_delivery(inboxes, activity);
    }

    fn is_addressed_to_followers(&self, activity: &Value) -> bool {
        if self.followers_collection_url.is_empty() {
            let mut to_cc = crate::model::str_list(activity.get("to"));
            to_cc.extend(crate::model::str_list(activity.get("cc")));
            return to_cc.iter().any(|v| v == AS_PUBLIC);
        }
        for field in ["to", "cc"] {
            for url in crate::model::str_list(activity.get(field)) {
                if url == self.followers_collection_url || url == AS_PUBLIC {
                    return true;
                }
            }
        }
        false
    }

    fn is_actor_url(&self, url: &str) -> bool {
        if url.is_empty() || !url.starts_with("http") {
            return false;
        }
        if url == AS_PUBLIC {
            return false;
        }
        if !self.followers_collection_url.is_empty() && url == self.followers_collection_url {
            return false;
        }
        !NON_ACTOR_URL_PATTERNS.iter().any(|p| url.ends_with(p))
    }

    fn extract_recipient_actors(&self, activity: &Value) -> Vec<String> {
        let mut actors = Vec::new();
        let mut seen = HashSet::new();
        for field in ["to", "cc"] {
            for url in crate::model::str_list(activity.get(field)) {
                if self.is_actor_url(&url) && seen.insert(url.clone()) {
                    actors.push(url);
                }
            }
        }
        actors
    }

    /// Fetch a remote actor document (cache first).
    pub fn fetch_actor(&self, actor_url: &str) -> Option<Value> {
        if let Ok(Some(cached)) = self.storage.get_cached_actor(actor_url, 86400.0) {
            return Some(cached);
        }
        let base_headers: std::collections::HashMap<String, String> = [
            (
                "Accept".to_string(),
                "application/activity+json, application/ld+json".to_string(),
            ),
            ("User-Agent".to_string(), self.user_agent.clone()),
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
            actor_url,
            None,
            &opts,
        ) {
            Ok(mut h) => {
                h.extend(base_headers);
                h
            }
            Err(e) => {
                log::warn!("Failed to sign actor fetch {actor_url}: {e}");
                return None;
            }
        };
        match self.http_client.get(actor_url, &headers) {
            Ok(resp) if resp.is_success() => {
                let data = resp.json().ok()?;
                let _ = self.storage.cache_remote_actor(actor_url, &data, None);
                Some(data)
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

    /// Collect inbox URLs for actors in the activity's `to`/`cc` fields.
    fn collect_recipient_inboxes(&self, activity: &Value) -> Vec<String> {
        let actor_urls = self.extract_recipient_actors(activity);
        let mut inboxes = Vec::new();
        let mut seen = HashSet::new();
        for actor_url in actor_urls {
            if is_domain_blocked(
                &actor_url,
                self.allowed_instances.as_deref(),
                self.blocked_instances.as_deref(),
            ) {
                continue;
            }
            let Some(actor_data) = self.fetch_actor(&actor_url) else {
                continue;
            };
            let actor = Actor::from_json(&actor_data);
            let inbox = actor
                .endpoints
                .get("sharedInbox")
                .and_then(|v| v.as_str())
                .filter(|s| !s.is_empty())
                .map(String::from)
                .unwrap_or(actor.inbox);
            if !inbox.is_empty() && seen.insert(inbox.clone()) {
                inboxes.push(inbox);
            }
        }
        inboxes
    }
}

/// Detachable delivery context used for `async_delivery` fan-out.
pub struct DeliveryContext {
    private_key: rsa::RsaPrivateKey,
    key_id: String,
    max_retries: usize,
    retry_base_delay: f64,
    max_delivery_workers: usize,
    user_agent: String,
    http_client: HttpClient,
}

impl DeliveryContext {
    /// Deliver `activity` to every inbox in `inboxes`, concurrently.
    pub fn fan_out_delivery(&self, inboxes: &[String], activity: &Value) {
        if inboxes.is_empty() {
            return;
        }
        let workers = self.max_delivery_workers.max(1).min(inboxes.len());
        let queue: Arc<std::sync::Mutex<std::collections::VecDeque<String>>> =
            Arc::new(std::sync::Mutex::new(
                inboxes
                    .iter()
                    .cloned()
                    .collect::<std::collections::VecDeque<_>>(),
            ));
        let mut handles = Vec::new();
        for _ in 0..workers {
            let queue = queue.clone();
            let ctx = Self {
                private_key: self.private_key.clone(),
                key_id: self.key_id.clone(),
                max_retries: self.max_retries,
                retry_base_delay: self.retry_base_delay,
                max_delivery_workers: self.max_delivery_workers,
                user_agent: self.user_agent.clone(),
                http_client: self.http_client.clone(),
            };
            let activity = activity.clone();
            handles.push(std::thread::spawn(move || loop {
                let url = {
                    let mut q = queue.lock().unwrap_or_else(|e| e.into_inner());
                    q.pop_front()
                };
                let Some(url) = url else { break };
                ctx.deliver_with_retry(&url, &activity);
            }));
        }
        for h in handles {
            let _ = h.join();
        }
    }

    /// Deliver an activity to a remote inbox with exponential backoff.
    pub fn deliver_with_retry(&self, inbox_url: &str, activity: &Value) -> bool {
        for attempt in 0..self.max_retries.max(1) {
            match self.deliver(inbox_url, activity) {
                Ok(true) => return true,
                Ok(false) | Err(_) => {
                    log::warn!(
                        "Delivery attempt {}/{} to {inbox_url} failed",
                        attempt + 1,
                        self.max_retries,
                    );
                }
            }
            if attempt + 1 < self.max_retries {
                let delay = self.retry_base_delay * 2f64.powi(attempt as i32);
                std::thread::sleep(Duration::from_secs_f64(delay));
            }
        }
        log::error!(
            "Delivery to {inbox_url} failed after {} attempts",
            self.max_retries
        );
        false
    }

    /// Deliver an activity to a single inbox.
    pub fn deliver(&self, inbox_url: &str, activity: &Value) -> Result<bool> {
        let status = deliver_activity(
            activity,
            inbox_url,
            &self.key_id,
            &self.private_key,
            Some(&self.user_agent),
            &self.http_client,
        )?;
        Ok((200..300).contains(&status))
    }
}

/// Build the outbox `OrderedCollection` response.
pub fn get_outbox_collection(
    storage: &dyn ActivityPubStorage,
    collection_url: &str,
    limit: usize,
    offset: usize,
) -> Result<Value> {
    let activities = storage.get_activities(limit, offset)?;
    Ok(json!({
        "@context": ap_context(),
        "id": collection_url,
        "type": "OrderedCollection",
        "totalItems": activities.len(),
        "orderedItems": activities,
    }))
}
