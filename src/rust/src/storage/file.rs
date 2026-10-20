//! File-based ActivityPub storage adapter — mirrors
//! `pubby.storage.adapters.file`.
//!
//! Stores data as JSON files organized by type, byte-compatible with the
//! Python layout (same `_sanitize` filename scheme, same `.schema_version`
//! file, same `_mentions`/`_object_ids` indexes):
//!
//! ```text
//! data_dir/
//! ├── followers/
//! ├── follow_requests/
//! ├── interactions/
//! │   ├── {sanitized_target}/
//! │   └── _mentions/
//! │   └── _object_ids/
//! ├── activities/
//! ├── cache/actors/
//! └── quote_authorizations/
//! ```

use std::path::{Path, PathBuf};
use std::sync::Mutex;

use chrono::{DateTime, Utc};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};

use crate::error::{ActivityPubError, Result};
use crate::model::{
    parse_dt, FollowRequest, Follower, Interaction, InteractionStatus, InteractionType,
};

use super::ActivityPubStorage;

/// Current file-storage schema version (kept in lockstep with Python).
/// History:
/// 1: Initial version (no version file = version 0)
/// 2: Added `_object_ids/` index for `get_interaction_by_object_id()`
/// 3: Reply/quote files keyed by object_id to allow multiple per actor
/// 4: Added `target_actor_id` to followers for per-actor isolation
pub const SCHEMA_VERSION: u32 = 4;

/// Interaction types that allow multiple interactions from the same actor
/// to the same target resource (e.g. multiple replies to the same post).
fn is_multi_interaction_type(t: InteractionType) -> bool {
    matches!(t, InteractionType::Reply | InteractionType::Quote)
}

/// Create a filesystem-safe name from a URL or ID — byte-compatible with
/// the Python `_sanitize` (sha256-16 hex suffix + readable prefix).
pub(crate) fn sanitize(value: &str) -> String {
    let h = format!("{:x}", Sha256::digest(value.as_bytes()));
    let safe: String = value
        .chars()
        .map(|c| {
            if c.is_alphanumeric() || matches!(c, '-' | '_' | '.') {
                c
            } else {
                '-'
            }
        })
        .collect();
    let safe: String = safe.trim_matches('-').chars().take(80).collect();
    format!("{safe}-{}", &h[..16])
}

/// File-based storage backend for ActivityPub data.
///
/// All data is stored as JSON files, one per entity, organized in
/// subdirectories. Thread-safe via a `Mutex` per resource path.
pub struct FileActivityPubStorage {
    /// Root directory for storing data.
    pub data_dir: PathBuf,
    /// Fixed striped lock table (bounded memory — no per-path growth).
    locks: Vec<Mutex<()>>,
}

const LOCK_SHARDS: usize = 256;

impl FileActivityPubStorage {
    /// Create a file storage rooted at `data_dir`, running schema
    /// migrations when `auto_migrate` is set.
    pub fn new(data_dir: impl AsRef<Path>, auto_migrate: bool) -> Result<Self> {
        let storage = FileActivityPubStorage {
            data_dir: data_dir.as_ref().to_path_buf(),
            locks: (0..LOCK_SHARDS).map(|_| Mutex::new(())).collect(),
        };
        if auto_migrate {
            storage.run_migrations()?;
        }
        Ok(storage)
    }

    // ---------- Schema versioning ----------

    fn version_path(&self) -> PathBuf {
        self.data_dir.join(".schema_version")
    }

    /// The current schema version recorded on disk (0 when absent).
    pub fn schema_version(&self) -> u32 {
        std::fs::read_to_string(self.version_path())
            .ok()
            .and_then(|s| s.trim().parse().ok())
            .unwrap_or(0)
    }

    fn set_schema_version(&self, version: u32) -> Result<()> {
        if let Some(parent) = self.version_path().parent() {
            std::fs::create_dir_all(parent)?;
        }
        std::fs::write(self.version_path(), version.to_string())?;
        Ok(())
    }

    /// Run any pending migrations to bring the schema up to date.
    pub fn run_migrations(&self) -> Result<()> {
        let current = self.schema_version();
        if current >= SCHEMA_VERSION {
            return Ok(());
        }

        log::info!("Migrating file storage schema from version {current} to {SCHEMA_VERSION}");

        for version in (current + 1)..=SCHEMA_VERSION {
            match version {
                2 => self.migrate_to_v2_object_id_index()?,
                3 => self.migrate_to_v3_multi_reply_files()?,
                4 => log::info!("No-op migration to v4: target_actor_id field added to followers"),
                _ => {}
            }
        }

        self.set_schema_version(SCHEMA_VERSION)?;
        Ok(())
    }

    /// v2: backfill the object_id index.
    fn migrate_to_v2_object_id_index(&self) -> Result<()> {
        let interactions = super::migrations::get_all_file_interactions(&self.data_dir);
        let mut indexed = 0;
        for interaction in &interactions {
            if !interaction.object_id.is_empty() {
                self.update_object_id_index(interaction, true)?;
                indexed += 1;
            }
        }
        log::info!("Indexed {indexed} interactions by object_id");
        Ok(())
    }

    /// v3: rename reply/quote files to include object_id.
    fn migrate_to_v3_multi_reply_files(&self) -> Result<()> {
        let interactions = super::migrations::get_all_file_interactions(&self.data_dir);
        let mut renamed = 0;

        for interaction in &interactions {
            if !is_multi_interaction_type(interaction.interaction_type)
                || interaction.object_id.is_empty()
            {
                continue;
            }

            let old_path = self
                .interaction_dir(&interaction.target_resource)
                .join(format!(
                    "{}-{}.json",
                    interaction.interaction_type.as_str(),
                    sanitize(&interaction.source_actor_id)
                ));
            let new_path = self.interaction_path(
                &interaction.source_actor_id,
                &interaction.target_resource,
                interaction.interaction_type,
                Some(&interaction.object_id),
            );

            if old_path == new_path {
                continue;
            }

            if old_path.exists() {
                let data = self.read_json(&old_path)?;
                if data
                    .as_ref()
                    .and_then(|d| d.get("object_id"))
                    .and_then(|v| v.as_str())
                    == Some(interaction.object_id.as_str())
                {
                    if let Some(parent) = new_path.parent() {
                        std::fs::create_dir_all(parent)?;
                    }
                    std::fs::rename(&old_path, &new_path)?;
                    renamed += 1;
                } else {
                    if let Some(parent) = new_path.parent() {
                        std::fs::create_dir_all(parent)?;
                    }
                    self.write_json(&new_path, &interaction.to_json())?;
                    renamed += 1;
                }
            } else if !new_path.exists() {
                if let Some(parent) = new_path.parent() {
                    std::fs::create_dir_all(parent)?;
                }
                self.write_json(&new_path, &interaction.to_json())?;
                renamed += 1;
            }
        }

        log::info!("Renamed/created {renamed} reply/quote files with object_id key");
        Ok(())
    }

    // ---------- File helpers ----------

    fn lock_for(&self, path: &Path) -> &Mutex<()> {
        use std::hash::{Hash, Hasher};
        let mut h = std::collections::hash_map::DefaultHasher::new();
        path.hash(&mut h);
        &self.locks[(h.finish() as usize) % LOCK_SHARDS]
    }

    /// Write data as JSON to a file, creating directories as needed.
    /// Uses a tmp file + fsync + rename for atomicity (same as Python,
    /// plus durability on power loss).
    pub fn write_json(&self, path: &Path, data: &Value) -> Result<()> {
        let _guard = self
            .lock_for(path)
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        self.write_json_locked(path, data)
    }

    /// [`write_json`](Self::write_json) without taking the per-path lock —
    /// for callers already holding it.
    fn write_json_locked(&self, path: &Path, data: &Value) -> Result<()> {
        if let Some(parent) = path.parent() {
            std::fs::create_dir_all(parent)?;
        }
        let tmp = path.with_extension("tmp");
        let result = (|| -> Result<()> {
            {
                let mut f = std::fs::File::create(&tmp)?;
                std::io::Write::write_all(
                    &mut f,
                    crate::model::dumps_python_pretty(data).as_bytes(),
                )?;
                f.sync_all()?;
            }
            std::fs::rename(&tmp, path)?;
            if let Some(parent) = path.parent() {
                if let Ok(dir) = std::fs::File::open(parent) {
                    let _ = dir.sync_all();
                }
            }
            Ok(())
        })();
        if result.is_err() {
            let _ = std::fs::remove_file(&tmp);
        }
        result
    }

    /// Read JSON data from a file. Returns `Ok(None)` when absent.
    pub fn read_json(&self, path: &Path) -> Result<Option<Value>> {
        let _guard = self
            .lock_for(path)
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        self.read_json_locked(path)
    }

    /// [`read_json`](Self::read_json) without taking the per-path lock.
    fn read_json_locked(&self, path: &Path) -> Result<Option<Value>> {
        if !path.exists() {
            return Ok(None);
        }
        let text = std::fs::read_to_string(path)?;
        Ok(Some(serde_json::from_str(&text)?))
    }

    fn delete_file(&self, path: &Path) -> Result<bool> {
        let _guard = self
            .lock_for(path)
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        if path.exists() {
            std::fs::remove_file(path)?;
            return Ok(true);
        }
        Ok(false)
    }

    /// List all `.json` files in a directory (sorted).
    pub fn list_json_files(&self, directory: &Path) -> Vec<PathBuf> {
        let mut files: Vec<PathBuf> = match std::fs::read_dir(directory) {
            Ok(rd) => rd
                .filter_map(|e| e.ok())
                .map(|e| e.path())
                .filter(|p| p.extension().map(|e| e == "json").unwrap_or(false))
                .collect(),
            Err(_) => return Vec::new(),
        };
        files.sort();
        files
    }

    // ---------- Followers ----------

    fn follower_path(&self, actor_id: &str, target_actor_id: &str) -> PathBuf {
        let filename = if !target_actor_id.is_empty() {
            format!("{}-{}.json", sanitize(target_actor_id), sanitize(actor_id))
        } else {
            format!("{}.json", sanitize(actor_id))
        };
        self.data_dir.join("followers").join(filename)
    }

    // ---------- Follow requests ----------

    fn follow_request_path(&self, actor_id: &str, target_actor_id: &str) -> PathBuf {
        let filename = if !target_actor_id.is_empty() {
            format!("{}-{}.json", sanitize(target_actor_id), sanitize(actor_id))
        } else {
            format!("{}.json", sanitize(actor_id))
        };
        self.data_dir.join("follow_requests").join(filename)
    }

    // ---------- Interactions ----------

    fn interaction_dir(&self, target_resource: &str) -> PathBuf {
        self.data_dir
            .join("interactions")
            .join(sanitize(target_resource))
    }

    fn interaction_path(
        &self,
        source_actor_id: &str,
        target_resource: &str,
        interaction_type: InteractionType,
        object_id: Option<&str>,
    ) -> PathBuf {
        let dirname = self.interaction_dir(target_resource);
        let filename = if object_id.map(|s| !s.is_empty()).unwrap_or(false)
            && is_multi_interaction_type(interaction_type)
        {
            format!(
                "{}-{}.json",
                interaction_type.as_str(),
                sanitize(object_id.unwrap())
            )
        } else {
            format!(
                "{}-{}.json",
                interaction_type.as_str(),
                sanitize(source_actor_id)
            )
        };
        dirname.join(filename)
    }

    // ---------- Mention index ----------

    fn mention_index_path(&self, actor_url: &str) -> PathBuf {
        self.data_dir
            .join("interactions")
            .join("_mentions")
            .join(format!("{}.json", sanitize(actor_url)))
    }

    fn update_mention_index(&self, interaction: &Interaction, add: bool) -> Result<()> {
        for actor_url in &interaction.mentioned_actors {
            let path = self.mention_index_path(actor_url);
            let _guard = self
                .lock_for(&path)
                .lock()
                .unwrap_or_else(|e| e.into_inner());

            let mut index: Vec<Value> = match self.read_json_locked(&path)? {
                Some(v) => v.as_array().cloned().unwrap_or_default(),
                None => Vec::new(),
            };

            let mut entry = json!({
                "target_resource": interaction.target_resource,
                "interaction_type": interaction.interaction_type.as_str(),
                "source_actor_id": interaction.source_actor_id,
            });
            if !interaction.object_id.is_empty() {
                entry["object_id"] = json!(interaction.object_id);
            }

            let match_keys = [
                "target_resource",
                "interaction_type",
                "source_actor_id",
                "object_id",
            ];
            if add {
                let dup = index
                    .iter()
                    .any(|e| match_keys.iter().all(|k| e.get(*k) == entry.get(*k)));
                if !dup {
                    index.push(entry);
                }
            } else {
                index.retain(|e| {
                    !(e.get("target_resource") == entry.get("target_resource")
                        && e.get("interaction_type") == entry.get("interaction_type")
                        && e.get("source_actor_id") == entry.get("source_actor_id")
                        && e.get("object_id") == entry.get("object_id"))
                });
            }

            // Atomic index write via the tmp+rename helper (the lock is
            // already held for this path).
            self.write_json_locked(&path, &Value::Array(index))?;
        }
        Ok(())
    }

    // ---------- Object ID index ----------

    pub(crate) fn object_id_index_path(&self, object_id: &str) -> PathBuf {
        self.data_dir
            .join("interactions")
            .join("_object_ids")
            .join(format!("{}.json", sanitize(object_id)))
    }

    pub(crate) fn update_object_id_index(
        &self,
        interaction: &Interaction,
        add: bool,
    ) -> Result<()> {
        if interaction.object_id.is_empty() {
            return Ok(());
        }
        let path = self.object_id_index_path(&interaction.object_id);
        let _guard = self
            .lock_for(&path)
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        if add {
            let entry = json!({
                "target_resource": interaction.target_resource,
                "interaction_type": interaction.interaction_type.as_str(),
                "source_actor_id": interaction.source_actor_id,
            });
            self.write_json_locked(&path, &entry)?;
        } else if path.exists() {
            std::fs::remove_file(&path)?;
        }
        Ok(())
    }

    // ---------- Activities / actor cache / quote auths ----------

    fn activity_path(&self, activity_id: &str) -> PathBuf {
        self.data_dir
            .join("activities")
            .join(format!("{}.json", sanitize(activity_id)))
    }

    fn actor_cache_path(&self, actor_id: &str) -> PathBuf {
        self.data_dir
            .join("cache")
            .join("actors")
            .join(format!("{}.json", sanitize(actor_id)))
    }

    fn quote_auth_path(&self, authorization_id: &str) -> PathBuf {
        self.data_dir
            .join("quote_authorizations")
            .join(format!("{}.json", sanitize(authorization_id)))
    }
}

impl ActivityPubStorage for FileActivityPubStorage {
    // ---------- Followers ----------

    fn store_follower(&self, follower: &Follower) -> Result<()> {
        let path = self.follower_path(&follower.actor_id, &follower.target_actor_id);
        self.write_json(&path, &follower.to_json())
    }

    fn remove_follower(&self, actor_id: &str, target_actor_id: &str) -> Result<()> {
        if target_actor_id.is_empty() {
            log::warn!(
                "remove_follower called without target_actor_id — \
                 removing all follows from {actor_id}"
            );
            let followers_dir = self.data_dir.join("followers");
            let mut removed = 0;
            for fpath in self.list_json_files(&followers_dir) {
                if let Ok(Some(data)) = self.read_json(&fpath) {
                    if data.get("actor_id").and_then(|v| v.as_str()) == Some(actor_id) {
                        self.delete_file(&fpath)?;
                        removed += 1;
                    }
                }
            }
            if removed > 0 {
                return Ok(());
            }
            // Fall back to the legacy path in case it uses a different layout
        } else {
            // Try the target-specific path first
            let path = self.follower_path(actor_id, target_actor_id);
            if self.delete_file(&path)? {
                return Ok(());
            }
        }
        let path = self.follower_path(actor_id, "");
        self.delete_file(&path)?;
        Ok(())
    }

    fn get_followers(&self, actor_id: Option<&str>) -> Result<Vec<Follower>> {
        let followers_dir = self.data_dir.join("followers");
        let mut result = Vec::new();
        for fpath in self.list_json_files(&followers_dir) {
            if let Some(data) = self.read_json(&fpath)? {
                let follower = Follower::from_json(&data);
                match actor_id {
                    None => result.push(follower),
                    Some(aid) if follower.target_actor_id == aid => result.push(follower),
                    Some(_) if follower.target_actor_id.is_empty() => result.push(follower),
                    _ => {}
                }
            }
        }
        Ok(result)
    }

    fn get_followers_of_targets(&self, target_ids: &[String]) -> Result<Vec<Follower>> {
        let wanted: std::collections::HashSet<&str> =
            target_ids.iter().map(String::as_str).collect();
        let mut result = Vec::new();
        let mut seen: std::collections::HashSet<PathBuf> = std::collections::HashSet::new();
        let followers_dir = self.data_dir.join("followers");
        for target in &wanted {
            let prefix = format!("{}-", sanitize(target));
            for fpath in self.list_json_files(&followers_dir) {
                if seen.contains(&fpath) {
                    continue;
                }
                let fname = fpath
                    .file_name()
                    .map(|f| f.to_string_lossy().to_string())
                    .unwrap_or_default();
                if !fname.starts_with(&prefix) {
                    continue;
                }
                if let Some(data) = self.read_json(&fpath)? {
                    let follower = Follower::from_json(&data);
                    if wanted.contains(follower.target_actor_id.as_str()) {
                        seen.insert(fpath);
                        result.push(follower);
                    }
                }
            }
        }
        Ok(result)
    }

    // ---------- Follow requests ----------

    fn store_follow_request(&self, request: &FollowRequest) -> Result<()> {
        let path = self.follow_request_path(&request.actor_id, &request.target_actor_id);
        self.write_json(&path, &request.to_json())
    }

    fn get_follow_requests(&self, target_actor_id: Option<&str>) -> Result<Vec<FollowRequest>> {
        let requests_dir = self.data_dir.join("follow_requests");
        let mut result = Vec::new();
        for fpath in self.list_json_files(&requests_dir) {
            if let Some(data) = self.read_json(&fpath)? {
                let request = FollowRequest::from_json(&data);
                if target_actor_id.is_none()
                    || request.target_actor_id == target_actor_id.unwrap_or_default()
                {
                    result.push(request);
                }
            }
        }
        Ok(result)
    }

    fn get_follow_request(
        &self,
        actor_id: &str,
        target_actor_id: &str,
    ) -> Result<Option<FollowRequest>> {
        let path = self.follow_request_path(actor_id, target_actor_id);
        Ok(self.read_json(&path)?.map(|d| FollowRequest::from_json(&d)))
    }

    fn remove_follow_request(&self, actor_id: &str, target_actor_id: &str) -> Result<bool> {
        if !target_actor_id.is_empty() {
            return self.delete_file(&self.follow_request_path(actor_id, target_actor_id));
        }
        let mut removed = false;
        let requests_dir = self.data_dir.join("follow_requests");
        for fpath in self.list_json_files(&requests_dir) {
            if let Ok(Some(data)) = self.read_json(&fpath) {
                if data.get("actor_id").and_then(|v| v.as_str()) == Some(actor_id) {
                    removed = self.delete_file(&fpath)? || removed;
                }
            }
        }
        Ok(removed)
    }

    // ---------- Interactions ----------

    fn store_interaction(&self, interaction: &Interaction) -> Result<()> {
        let path = self.interaction_path(
            &interaction.source_actor_id,
            &interaction.target_resource,
            interaction.interaction_type,
            Some(&interaction.object_id),
        );
        self.write_json(&path, &interaction.to_json())?;

        if !interaction.mentioned_actors.is_empty() {
            self.update_mention_index(interaction, true)?;
        }
        if !interaction.object_id.is_empty() {
            self.update_object_id_index(interaction, true)?;
        }
        Ok(())
    }

    fn delete_interaction(
        &self,
        source_actor_id: &str,
        target_resource: &str,
        interaction_type: InteractionType,
    ) -> Result<()> {
        let path = self.interaction_path(source_actor_id, target_resource, interaction_type, None);

        let mut paths_to_delete = Vec::new();
        if path.exists() {
            paths_to_delete.push(path);
        }

        // For multi-interaction types, also scan the directory for files
        // from this actor (they are keyed by object_id, not actor).
        if is_multi_interaction_type(interaction_type) {
            let interaction_dir = self.interaction_dir(target_resource);
            let prefix = format!("{}-", interaction_type.as_str());
            for fpath in self.list_json_files(&interaction_dir) {
                if paths_to_delete.contains(&fpath) {
                    continue;
                }
                let fname = fpath
                    .file_name()
                    .map(|f| f.to_string_lossy().to_string())
                    .unwrap_or_default();
                if !fname.starts_with(&prefix) {
                    continue;
                }
                if let Some(data) = self.read_json(&fpath)? {
                    if data.get("source_actor_id").and_then(|v| v.as_str()) == Some(source_actor_id)
                        && data.get("status").and_then(|v| v.as_str())
                            != Some(InteractionStatus::Deleted.as_str())
                    {
                        paths_to_delete.push(fpath);
                    }
                }
            }
        }

        for p in paths_to_delete {
            if let Some(mut data) = self.read_json(&p)? {
                let interaction = Interaction::from_json(&data)?;
                if !interaction.mentioned_actors.is_empty() {
                    self.update_mention_index(&interaction, false)?;
                }
                // Note: the object_id index is kept for deleted
                // interactions so they can still be looked up with
                // status=DELETED.
                data["status"] = json!(InteractionStatus::Deleted.as_str());
                data["updated_at"] = json!(crate::model::dt_to_iso(&Utc::now()));
                self.write_json(&p, &data)?;
            }
        }
        Ok(())
    }

    fn delete_interaction_by_object_id(
        &self,
        source_actor_id: &str,
        object_id: &str,
    ) -> Result<bool> {
        let index_path = self.object_id_index_path(object_id);
        if !index_path.exists() {
            return Ok(false);
        }

        let Some(index_data) = self.read_json(&index_path)? else {
            return Ok(false);
        };
        if index_data.get("source_actor_id").and_then(|v| v.as_str()) != Some(source_actor_id) {
            return Ok(false);
        }

        let itype = InteractionType::from_raw(
            index_data
                .get("interaction_type")
                .and_then(|v| v.as_str())
                .unwrap_or_default(),
        )
        .ok_or_else(|| ActivityPubError::InvalidValue("bad interaction_type in index".into()))?;
        let interaction_path = self.interaction_path(
            index_data
                .get("source_actor_id")
                .and_then(|v| v.as_str())
                .unwrap_or_default(),
            index_data
                .get("target_resource")
                .and_then(|v| v.as_str())
                .unwrap_or_default(),
            itype,
            Some(object_id),
        );

        let Some(mut data) = self.read_json(&interaction_path)? else {
            return Ok(false);
        };
        if data.get("status").and_then(|v| v.as_str()) == Some(InteractionStatus::Deleted.as_str())
        {
            return Ok(false);
        }

        let interaction = Interaction::from_json(&data)?;
        if !interaction.mentioned_actors.is_empty() {
            self.update_mention_index(&interaction, false)?;
        }

        data["status"] = json!(InteractionStatus::Deleted.as_str());
        data["updated_at"] = json!(crate::model::dt_to_iso(&Utc::now()));
        self.write_json(&interaction_path, &data)?;
        Ok(true)
    }

    fn get_interaction_by_object_id(
        &self,
        object_id: &str,
        status: InteractionStatus,
    ) -> Result<Option<Interaction>> {
        let index_path = self.object_id_index_path(object_id);
        if !index_path.exists() {
            return Ok(None);
        }

        let Some(index_data) = self.read_json(&index_path)? else {
            return Ok(None);
        };

        let itype = match InteractionType::from_raw(
            index_data
                .get("interaction_type")
                .and_then(|v| v.as_str())
                .unwrap_or_default(),
        ) {
            Some(t) => t,
            None => return Ok(None),
        };
        let interaction_path = self.interaction_path(
            index_data
                .get("source_actor_id")
                .and_then(|v| v.as_str())
                .unwrap_or_default(),
            index_data
                .get("target_resource")
                .and_then(|v| v.as_str())
                .unwrap_or_default(),
            itype,
            Some(object_id),
        );

        let Some(data) = self.read_json(&interaction_path)? else {
            return Ok(None);
        };

        let interaction = Interaction::from_json(&data)?;
        if interaction.status != status {
            return Ok(None);
        }
        Ok(Some(interaction))
    }

    fn get_interactions(
        &self,
        target_resource: &str,
        interaction_type: Option<InteractionType>,
        status: InteractionStatus,
    ) -> Result<Vec<Interaction>> {
        let interaction_dir = self.interaction_dir(target_resource);
        let mut result = Vec::new();
        for fpath in self.list_json_files(&interaction_dir) {
            let Some(data) = self.read_json(&fpath)? else {
                continue;
            };
            let interaction = Interaction::from_json(&data)?;
            if interaction.status != status {
                continue;
            }
            if let Some(t) = interaction_type {
                if interaction.interaction_type != t {
                    continue;
                }
            }
            result.push(interaction);
        }
        Ok(result)
    }

    fn get_interactions_mentioning(
        &self,
        actor_url: &str,
        interaction_type: Option<InteractionType>,
        status: InteractionStatus,
    ) -> Result<Vec<Interaction>> {
        let path = self.mention_index_path(actor_url);
        if !path.exists() {
            return Ok(Vec::new());
        }

        let Some(index) = self.read_json(&path)? else {
            return Ok(Vec::new());
        };
        let Some(entries) = index.as_array() else {
            return Ok(Vec::new());
        };

        let mut result = Vec::new();
        for entry in entries {
            let etype = entry.get("interaction_type").and_then(|v| v.as_str());
            if let Some(t) = interaction_type {
                if etype != Some(t.as_str()) {
                    continue;
                }
            }
            let Some(itype) = etype.and_then(InteractionType::from_raw) else {
                continue;
            };
            let interaction_path = self.interaction_path(
                entry
                    .get("source_actor_id")
                    .and_then(|v| v.as_str())
                    .unwrap_or_default(),
                entry
                    .get("target_resource")
                    .and_then(|v| v.as_str())
                    .unwrap_or_default(),
                itype,
                entry.get("object_id").and_then(|v| v.as_str()),
            );
            let Some(data) = self.read_json(&interaction_path)? else {
                continue;
            };
            let interaction = Interaction::from_json(&data)?;
            if interaction.status != status {
                continue;
            }
            result.push(interaction);
        }
        Ok(result)
    }

    // ---------- Activities ----------

    fn store_activity(&self, activity_id: &str, activity_data: &Value) -> Result<()> {
        let path = self.activity_path(activity_id);
        let record = json!({
            "activity_id": activity_id,
            "activity_data": activity_data,
            "created_at": crate::model::dt_to_iso(&Utc::now()),
        });
        self.write_json(&path, &record)
    }

    fn get_activities(&self, limit: usize, offset: usize) -> Result<Vec<Value>> {
        let activities_dir = self.data_dir.join("activities");
        let mut files = self.list_json_files(&activities_dir);
        // Sort by modification time, newest first
        files.sort_by_key(|p| {
            std::cmp::Reverse(std::fs::metadata(p).and_then(|m| m.modified()).ok())
        });
        let mut result = Vec::new();
        for fpath in files.iter().skip(offset).take(limit) {
            if let Some(data) = self.read_json(fpath)? {
                if let Some(ad) = data.get("activity_data") {
                    result.push(ad.clone());
                }
            }
        }
        Ok(result)
    }

    // ---------- Actor cache ----------

    fn cache_remote_actor(
        &self,
        actor_id: &str,
        actor_data: &Value,
        fetched_at: Option<DateTime<Utc>>,
    ) -> Result<()> {
        let path = self.actor_cache_path(actor_id);
        let record = json!({
            "actor_id": actor_id,
            "actor_data": actor_data,
            "fetched_at": crate::model::dt_to_iso(&fetched_at.unwrap_or_else(Utc::now)),
        });
        self.write_json(&path, &record)
    }

    fn get_cached_actor(&self, actor_id: &str, max_age_seconds: f64) -> Result<Option<Value>> {
        let path = self.actor_cache_path(actor_id);
        let Some(data) = self.read_json(&path)? else {
            return Ok(None);
        };

        if let Some(fetched_at) = data.get("fetched_at").and_then(parse_dt) {
            let age = (Utc::now() - fetched_at).num_milliseconds() as f64 / 1000.0;
            if age > max_age_seconds {
                return Ok(None);
            }
        }

        Ok(data.get("actor_data").cloned())
    }

    // ---------- Quote authorizations ----------

    fn store_quote_authorization(
        &self,
        authorization_id: &str,
        authorization_data: &Value,
    ) -> Result<()> {
        let path = self.quote_auth_path(authorization_id);
        self.write_json(&path, authorization_data)
    }

    fn get_quote_authorization(&self, authorization_id: &str) -> Result<Option<Value>> {
        self.read_json(&self.quote_auth_path(authorization_id))
    }
}
