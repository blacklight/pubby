//! Pending follow-request resolution — mirrors `pubby.follows`.
//!
//! When an inbox `follow_policy` marks an incoming `Follow` as requiring
//! manual approval, pubby stores a [`FollowRequest`] instead of a follower
//! and sends no reply. The application later calls
//! [`accept_follow_request`] or [`reject_follow_request`] to promote or
//! discard the request and deliver the matching `Accept`/`Reject`
//! activity to the requester.

use serde_json::{json, Value};

use crate::cache::{self, DocumentCache};
use crate::error::{ActivityPubError, Result};
use crate::handlers::client::get_default_user_agent;
use crate::handlers::outbox::deliver_activity;
use crate::http::HttpClient;
use crate::model::{ap_context, FollowRequest, Follower};
use crate::storage::ActivityPubStorage;

/// Build the `Accept`/`Reject` activity answering a `Follow`.
///
/// The original Follow document is embedded as `object` so remote servers
/// can match the response to the pending request.
///
/// :param actor_id: The local actor answering the request.
/// :param follow_activity: The raw incoming `Follow` activity.
/// :param response_type: `"Accept"` or `"Reject"`.
pub fn build_follow_response(
    actor_id: &str,
    follow_activity: &Value,
    response_type: &str,
) -> Value {
    json!({
        "@context": ap_context(),
        "id": format!("{actor_id}#{}-{}", response_type.to_lowercase(), uuid::Uuid::new_v4()),
        "type": response_type,
        "actor": actor_id,
        "object": follow_activity,
    })
}

/// Options for [`accept_follow_request`]/[`reject_follow_request`].
#[derive(Default)]
pub struct FollowResponseOptions {
    /// Optional callback `(inbox_url, activity)` to hand the response to
    /// the application's delivery pipeline (e.g. a task queue). When
    /// unset, the activity is POSTed synchronously via
    /// [`deliver_activity`], which requires `key_id` and `private_key`.
    pub deliver: Option<crate::handlers::outbox::DeliverFn>,
    /// Key ID for the HTTP signature (`<actor_id>#main-key`).
    pub key_id: Option<String>,
    /// RSA private key used to sign the delivery.
    pub private_key: Option<rsa::RsaPrivateKey>,
    /// Optional `User-Agent` for the synchronous delivery.
    pub user_agent: Option<String>,
    /// HTTP client for the synchronous delivery.
    pub http_client: Option<HttpClient>,
}

fn deliver_response(
    request: &FollowRequest,
    activity: &Value,
    options: &FollowResponseOptions,
) -> Result<()> {
    let inbox_url = if !request.inbox.is_empty() {
        request.inbox.clone()
    } else {
        request.shared_inbox.clone()
    };
    if inbox_url.is_empty() {
        log::warn!(
            "Follow request from {} has no inbox; cannot deliver {}",
            request.actor_id,
            activity.get("type").and_then(|v| v.as_str()).unwrap_or("")
        );
        return Ok(());
    }
    if let Some(deliver) = &options.deliver {
        deliver(&inbox_url, activity);
        return Ok(());
    }
    let (Some(key_id), Some(private_key)) = (&options.key_id, &options.private_key) else {
        return Err(ActivityPubError::InvalidValue(
            "key_id and private_key are required when no deliver callback is provided".to_string(),
        ));
    };
    let client = options.http_client.clone().unwrap_or_default();
    deliver_activity(
        activity,
        &inbox_url,
        key_id,
        private_key,
        options.user_agent.as_deref(),
        &client,
    )?;
    Ok(())
}

/// Approve a pending follow request.
///
/// Promotes the request to a stored follower, removes the pending record
/// and delivers an `Accept` activity to the requester.
///
/// :param storage: The storage backend the request was stored in.
/// :param request: The pending [`FollowRequest`] to approve.
/// :param actor_id: The local actor answering the request.
/// :param document_cache: Optional [`DocumentCache`] whose
///     followers-collection entries are invalidated.
/// :return: The delivered `Accept` activity.
pub fn accept_follow_request(
    storage: &dyn ActivityPubStorage,
    request: &FollowRequest,
    actor_id: &str,
    options: &FollowResponseOptions,
    document_cache: Option<&DocumentCache>,
) -> Result<Value> {
    let follower = Follower {
        actor_id: request.actor_id.clone(),
        inbox: request.inbox.clone(),
        shared_inbox: request.shared_inbox.clone(),
        followed_at: request.requested_at,
        actor_data: request.actor_data.clone(),
        target_actor_id: request.target_actor_id.clone(),
    };
    storage.store_follower(&follower)?;
    storage.remove_follow_request(&request.actor_id, &request.target_actor_id)?;
    if let Some(cache) = document_cache {
        cache.invalidate_prefix(&[cache::followers_key()]);
    }

    let activity = build_follow_response(actor_id, &request.activity.clone().into(), "Accept");
    deliver_response(request, &activity, options)?;
    Ok(activity)
}

/// Decline a pending follow request.
///
/// Removes the pending record and delivers a `Reject` activity to the
/// requester so their server clears the pending state.
///
/// :return: The delivered `Reject` activity.
pub fn reject_follow_request(
    storage: &dyn ActivityPubStorage,
    request: &FollowRequest,
    actor_id: &str,
    options: &FollowResponseOptions,
) -> Result<Value> {
    storage.remove_follow_request(&request.actor_id, &request.target_actor_id)?;
    let activity = build_follow_response(actor_id, &request.activity.clone().into(), "Reject");
    deliver_response(request, &activity, options)?;
    Ok(activity)
}

/// Default User-Agent for follow-response deliveries.
pub fn default_user_agent(actor_id: &str) -> String {
    get_default_user_agent(actor_id)
}
