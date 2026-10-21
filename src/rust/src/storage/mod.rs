//! Storage layer — mirrors `pubby.storage`.
//!
//! [`ActivityPubStorage`] is the pluggable storage trait (the Rust
//! equivalent of the Python ABC). `Arc<dyn ActivityPubStorage>` is used
//! wherever the handler needs a shared, thread-safe backend.

pub mod file;
pub mod migrations;

#[cfg(feature = "db")]
pub mod db;

use std::collections::HashSet;

use chrono::{DateTime, Utc};
use serde_json::Value;

use crate::error::{ActivityPubError, Result};
use crate::model::{FollowRequest, Follower, Interaction, InteractionStatus, InteractionType};

/// Abstract storage backend for ActivityPub data.
///
/// Implementations must be `Send + Sync` — the handler delivers activities
/// from background threads.
///
/// Methods returning `Err(ActivityPubError::Unsupported)` mirror the
/// Python methods that raise `NotImplementedError` — callers (e.g. the
/// inbox processor for follow requests) fall back accordingly.
pub trait ActivityPubStorage: Send + Sync {
    // ---------- Followers ----------

    /// Store or update a follower record.
    fn store_follower(&self, follower: &Follower) -> Result<()>;

    /// Remove a follower by their actor ID. When `target_actor_id` is
    /// empty, all follow records from this remote actor are removed.
    fn remove_follower(&self, actor_id: &str, target_actor_id: &str) -> Result<()>;

    /// Retrieve stored followers. `actor_id` filters to a local target
    /// (plus unassigned/legacy followers); `None` returns all.
    fn get_followers(&self, actor_id: Option<&str>) -> Result<Vec<Follower>>;

    /// Retrieve followers of any of the given local targets (actor URLs or
    /// object ids). Unassigned followers are not included.
    fn get_followers_of_targets(&self, target_ids: &[String]) -> Result<Vec<Follower>> {
        let wanted: HashSet<&str> = target_ids.iter().map(String::as_str).collect();
        Ok(self
            .get_followers(None)?
            .into_iter()
            .filter(|f| wanted.contains(f.target_actor_id.as_str()))
            .collect())
    }

    // ---------- Follow requests (pending approvals) ----------

    /// Store or update a pending follow request. Backends without pending
    /// request support return `Err(Unsupported)`; callers fall back to
    /// auto-accepting.
    fn store_follow_request(&self, _request: &FollowRequest) -> Result<()> {
        Err(ActivityPubError::Unsupported(format!(
            "{} does not support follow requests",
            std::any::type_name::<Self>()
        )))
    }

    /// Retrieve pending follow requests; `target_actor_id` scopes the
    /// result to a local target, `None` returns all.
    fn get_follow_requests(&self, _target_actor_id: Option<&str>) -> Result<Vec<FollowRequest>> {
        Ok(Vec::new())
    }

    /// Retrieve the pending request from `actor_id` for `target_actor_id`.
    fn get_follow_request(
        &self,
        actor_id: &str,
        target_actor_id: &str,
    ) -> Result<Option<FollowRequest>> {
        Ok(self
            .get_follow_requests(Some(target_actor_id))?
            .into_iter()
            .find(|r| r.actor_id == actor_id))
    }

    /// Remove a pending follow request. Returns `true` if at least one
    /// request was removed.
    fn remove_follow_request(&self, _actor_id: &str, _target_actor_id: &str) -> Result<bool> {
        Ok(false)
    }

    // ---------- Interactions ----------

    /// Store or update an interaction.
    fn store_interaction(&self, interaction: &Interaction) -> Result<()>;

    /// Mark an interaction as deleted.
    fn delete_interaction(
        &self,
        source_actor_id: &str,
        target_resource: &str,
        interaction_type: InteractionType,
    ) -> Result<()>;

    /// Delete an interaction by its `object_id` (the remote object URL).
    /// Returns `true` if at least one was found.
    fn delete_interaction_by_object_id(
        &self,
        _source_actor_id: &str,
        _object_id: &str,
    ) -> Result<bool> {
        Ok(false)
    }

    /// Retrieve an interaction by its `object_id` (the remote object URL).
    fn get_interaction_by_object_id(
        &self,
        _object_id: &str,
        _status: InteractionStatus,
    ) -> Result<Option<Interaction>> {
        Ok(None)
    }

    /// Retrieve interactions for a resource.
    fn get_interactions(
        &self,
        target_resource: &str,
        interaction_type: Option<InteractionType>,
        status: InteractionStatus,
    ) -> Result<Vec<Interaction>>;

    /// Retrieve interactions that mention a given actor URL.
    fn get_interactions_mentioning(
        &self,
        _actor_url: &str,
        _interaction_type: Option<InteractionType>,
        _status: InteractionStatus,
    ) -> Result<Vec<Interaction>> {
        Ok(Vec::new())
    }

    // ---------- Activities (outbox records) ----------

    /// Store an outbound activity record.
    fn store_activity(&self, activity_id: &str, activity_data: &Value) -> Result<()>;

    /// Retrieve outbound activities (for the outbox collection), newest
    /// first.
    fn get_activities(&self, limit: usize, offset: usize) -> Result<Vec<Value>>;

    /// Retrieve outbound activities of a single actor, newest first.
    ///
    /// The default implementation fetches `limit + offset` entries and
    /// filters on the activity's `actor` field — correct but linear;
    /// backends should override with an indexed query.
    fn get_activities_for_actor(
        &self,
        actor_id: &str,
        limit: usize,
        offset: usize,
    ) -> Result<Vec<Value>> {
        let all = self.get_activities(limit.saturating_add(offset).max(limit), 0)?;
        Ok(all
            .into_iter()
            .filter(|a| match a.get("actor") {
                Some(Value::String(s)) => s == actor_id,
                Some(Value::Object(m)) => m.get("id").and_then(|v| v.as_str()) == Some(actor_id),
                _ => false,
            })
            .skip(offset)
            .take(limit)
            .collect())
    }

    // ---------- Actor cache ----------

    /// Cache a fetched remote actor's data.
    fn cache_remote_actor(
        &self,
        actor_id: &str,
        actor_data: &Value,
        fetched_at: Option<DateTime<Utc>>,
    ) -> Result<()>;

    /// Retrieve a cached remote actor if still fresh (`max_age_seconds`
    /// bounds the entry age; default callers use 24h).
    fn get_cached_actor(&self, actor_id: &str, max_age_seconds: f64) -> Result<Option<Value>>;

    // ---------- Quote authorizations ----------

    /// Store a QuoteAuthorization object so it can be served via HTTP GET.
    fn store_quote_authorization(
        &self,
        _authorization_id: &str,
        _authorization_data: &Value,
    ) -> Result<()> {
        Ok(())
    }

    /// Retrieve a stored QuoteAuthorization by its ID.
    fn get_quote_authorization(&self, _authorization_id: &str) -> Result<Option<Value>> {
        Ok(None)
    }
}
