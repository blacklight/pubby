//! SQLite-backed storage adapter (feature `db`) — the Rust equivalent of
//! `pubby.storage.adapters.db`.
//!
//! The Python adapter is SQLAlchemy-based and dialect-agnostic; this port
//! targets SQLite via `rusqlite` — the most common embedded deployment —
//! with the same table layout and semantics.

use std::sync::Mutex;

use chrono::{DateTime, Utc};
use rusqlite::{params, Connection};
use serde_json::Value;

use crate::error::{ActivityPubError, Result};
use crate::model::{FollowRequest, Follower, Interaction, InteractionStatus, InteractionType};

use super::ActivityPubStorage;

const SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS followers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id TEXT NOT NULL,
    inbox TEXT NOT NULL,
    shared_inbox TEXT NOT NULL DEFAULT '',
    followed_at TEXT NOT NULL,
    actor_data TEXT NOT NULL DEFAULT '{}',
    target_actor_id TEXT NOT NULL DEFAULT '',
    UNIQUE(actor_id, target_actor_id)
);
CREATE TABLE IF NOT EXISTS follow_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor_id TEXT NOT NULL,
    target_actor_id TEXT NOT NULL DEFAULT '',
    inbox TEXT NOT NULL,
    shared_inbox TEXT NOT NULL DEFAULT '',
    actor_data TEXT NOT NULL DEFAULT '{}',
    activity TEXT NOT NULL DEFAULT '{}',
    requested_at TEXT NOT NULL,
    UNIQUE(actor_id, target_actor_id)
);
CREATE TABLE IF NOT EXISTS interactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_actor_id TEXT NOT NULL,
    target_resource TEXT NOT NULL,
    interaction_type TEXT NOT NULL,
    activity_id TEXT NOT NULL DEFAULT '',
    object_id TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL DEFAULT '',
    author_name TEXT NOT NULL DEFAULT '',
    author_url TEXT NOT NULL DEFAULT '',
    author_photo TEXT NOT NULL DEFAULT '',
    published TEXT,
    status TEXT NOT NULL DEFAULT 'confirmed',
    meta TEXT NOT NULL DEFAULT '{}',
    created_at TEXT,
    updated_at TEXT,
    UNIQUE(source_actor_id, target_resource, interaction_type, object_id)
);
CREATE TABLE IF NOT EXISTS interaction_mentions (
    interaction_id INTEGER NOT NULL,
    mentioned_actor_url TEXT NOT NULL,
    UNIQUE(interaction_id, mentioned_actor_url)
);
CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_id TEXT NOT NULL UNIQUE,
    activity_data TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS actor_cache (
    actor_id TEXT PRIMARY KEY,
    actor_data TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_interactions_target ON interactions(target_resource);
CREATE INDEX IF NOT EXISTS ix_interactions_object ON interactions(object_id);
CREATE INDEX IF NOT EXISTS ix_interaction_mentions_actor ON interaction_mentions(mentioned_actor_url);
CREATE INDEX IF NOT EXISTS ix_followers_target ON followers(target_actor_id);
CREATE INDEX IF NOT EXISTS ix_follow_requests_target ON follow_requests(target_actor_id);
";

fn dt_str(dt: Option<DateTime<Utc>>) -> Option<String> {
    dt.as_ref().map(crate::model::dt_to_iso)
}

fn to_json_text(v: &Value) -> String {
    serde_json::to_string(v).unwrap_or_else(|_| "{}".to_string())
}

fn map_to_json(m: &serde_json::Map<String, Value>) -> String {
    serde_json::to_string(m).unwrap_or_else(|_| "{}".to_string())
}

fn parse_json_map(s: &str) -> serde_json::Map<String, Value> {
    serde_json::from_str::<Value>(s)
        .ok()
        .and_then(|v| v.as_object().cloned())
        .unwrap_or_default()
}

fn parse_json_value(s: &str) -> Value {
    serde_json::from_str(s).unwrap_or(Value::Null)
}

fn err(e: impl std::fmt::Display) -> ActivityPubError {
    ActivityPubError::General(format!("sqlite error: {e}"))
}

/// SQLite storage backend for ActivityPub data.
///
/// Mirrors the Python `DbActivityPubStorage` semantics over `rusqlite`.
pub struct SqliteActivityPubStorage {
    conn: Mutex<Connection>,
}

impl SqliteActivityPubStorage {
    /// Open (and initialize) a SQLite database at `path`. Pass `":memory:"`
    /// for an in-memory database.
    pub fn new(path: impl AsRef<std::path::Path>) -> Result<Self> {
        let conn = if path.as_ref().as_os_str() == ":memory:" {
            Connection::open_in_memory().map_err(err)?
        } else {
            Connection::open(path).map_err(err)?
        };
        conn.execute_batch(SCHEMA).map_err(err)?;
        // Migrate schemas created by earlier Rust releases (column
        // renames) so old databases keep working.
        for (table, from, to) in [
            ("interactions", "metadata", "meta"),
            ("interaction_mentions", "actor_url", "mentioned_actor_url"),
        ] {
            let cols: Vec<String> = conn
                .prepare(&format!("PRAGMA table_info({table})"))
                .and_then(|mut st| {
                    st.query_map([], |r| r.get::<_, String>(1))
                        .map(|rows| rows.flatten().collect())
                })
                .unwrap_or_default();
            if cols.contains(&from.to_string()) && !cols.contains(&to.to_string()) {
                conn.execute_batch(&format!("ALTER TABLE {table} RENAME COLUMN {from} TO {to}"))
                    .map_err(err)?;
            }
        }
        // Enforce the Python UNIQUE constraint on DBs that predate it.
        conn.execute_batch(
            "CREATE UNIQUE INDEX IF NOT EXISTS uix_actor_resource_type_object \
             ON interactions(source_actor_id, target_resource, interaction_type, object_id)",
        )
        .map_err(err)?;
        Ok(SqliteActivityPubStorage {
            conn: Mutex::new(conn),
        })
    }

    #[allow(clippy::type_complexity)]
    fn interaction_from_row(
        &self,
        conn: &Connection,
        row: (
            i64,
            String,
            String,
            String,
            String,
            String,
            String,
            String,
            String,
            String,
            Option<String>,
            String,
            String,
            Option<String>,
            Option<String>,
        ),
    ) -> Result<Interaction> {
        let (
            id,
            source_actor_id,
            target_resource,
            itype,
            activity_id,
            object_id,
            content,
            author_name,
            author_url,
            author_photo,
            published,
            status,
            metadata,
            created_at,
            updated_at,
        ) = row;
        let mut mentioned_actors = Vec::new();
        let mut stmt = conn
            .prepare(
                "SELECT mentioned_actor_url FROM interaction_mentions WHERE interaction_id = ?1",
            )
            .map_err(err)?;
        let rows = stmt
            .query_map(params![id], |r| r.get::<_, String>(0))
            .map_err(err)?;
        for r in rows.flatten() {
            mentioned_actors.push(r);
        }
        Ok(Interaction {
            source_actor_id,
            target_resource,
            interaction_type: InteractionType::from_raw(&itype)
                .ok_or_else(|| err(format!("unknown interaction_type: {itype}")))?,
            activity_id,
            object_id,
            content,
            author_name,
            author_url,
            author_photo,
            published: published.as_deref().and_then(parse_dt_str_opt),
            status: InteractionStatus::from_raw(&status)
                .ok_or_else(|| err(format!("unknown interaction status: {status}")))?,
            metadata: parse_json_map(&metadata),
            created_at: created_at.as_deref().and_then(parse_dt_str_opt),
            updated_at: updated_at.as_deref().and_then(parse_dt_str_opt),
            mentioned_actors,
        })
    }
}

fn parse_dt_str_opt(s: &str) -> Option<DateTime<Utc>> {
    crate::model::parse_dt_str(s)
}

type InteractionRow = (
    i64,
    String,
    String,
    String,
    String,
    String,
    String,
    String,
    String,
    String,
    Option<String>,
    String,
    String,
    Option<String>,
    Option<String>,
);

const INTERACTION_COLS: &str = "id, source_actor_id, target_resource, interaction_type, activity_id, object_id, content, author_name, author_url, author_photo, published, status, meta, created_at, updated_at";

fn read_interaction_row(row: &rusqlite::Row) -> rusqlite::Result<InteractionRow> {
    Ok((
        row.get(0)?,
        row.get(1)?,
        row.get(2)?,
        row.get(3)?,
        row.get(4)?,
        row.get(5)?,
        row.get(6)?,
        row.get(7)?,
        row.get(8)?,
        row.get(9)?,
        row.get(10)?,
        row.get(11)?,
        row.get(12)?,
        row.get(13)?,
        row.get(14)?,
    ))
}

impl ActivityPubStorage for SqliteActivityPubStorage {
    fn store_follower(&self, follower: &Follower) -> Result<()> {
        let conn = self.conn.lock().unwrap_or_else(|e| e.into_inner());
        conn.execute(
            "INSERT INTO followers (actor_id, inbox, shared_inbox, followed_at, actor_data, target_actor_id)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6)
             ON CONFLICT(actor_id, target_actor_id) DO UPDATE SET
               inbox = excluded.inbox, shared_inbox = excluded.shared_inbox,
               followed_at = excluded.followed_at, actor_data = excluded.actor_data",
            params![
                follower.actor_id,
                follower.inbox,
                follower.shared_inbox,
                dt_str(follower.followed_at).unwrap_or_else(|| crate::model::dt_to_iso(&Utc::now())),
                map_to_json(&follower.actor_data),
                follower.target_actor_id,
            ],
        )
        .map_err(err)?;
        Ok(())
    }

    fn remove_follower(&self, actor_id: &str, target_actor_id: &str) -> Result<()> {
        let conn = self.conn.lock().unwrap_or_else(|e| e.into_inner());
        if target_actor_id.is_empty() {
            conn.execute(
                "DELETE FROM followers WHERE actor_id = ?1",
                params![actor_id],
            )
            .map_err(err)?;
        } else {
            conn.execute(
                "DELETE FROM followers WHERE actor_id = ?1 AND target_actor_id = ?2",
                params![actor_id, target_actor_id],
            )
            .map_err(err)?;
        }
        Ok(())
    }

    fn get_followers(&self, actor_id: Option<&str>) -> Result<Vec<Follower>> {
        let conn = self.conn.lock().unwrap_or_else(|e| e.into_inner());
        let (sql, params_vec): (&str, Vec<String>) = match actor_id {
            Some(aid) => (
                "SELECT actor_id, inbox, shared_inbox, followed_at, actor_data, target_actor_id
                 FROM followers WHERE target_actor_id = ?1 OR target_actor_id = ''",
                vec![aid.to_string()],
            ),
            None => (
                "SELECT actor_id, inbox, shared_inbox, followed_at, actor_data, target_actor_id
                 FROM followers",
                vec![],
            ),
        };
        let mut stmt = conn.prepare(sql).map_err(err)?;
        let rows = stmt
            .query_map(rusqlite::params_from_iter(params_vec.iter()), |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, String>(3)?,
                    r.get::<_, String>(4)?,
                    r.get::<_, String>(5)?,
                ))
            })
            .map_err(err)?;
        let mut out = Vec::new();
        for r in rows.flatten() {
            out.push(Follower {
                actor_id: r.0,
                inbox: r.1,
                shared_inbox: r.2,
                followed_at: parse_dt_str_opt(&r.3),
                actor_data: parse_json_map(&r.4),
                target_actor_id: r.5,
            });
        }
        Ok(out)
    }

    fn store_follow_request(&self, request: &FollowRequest) -> Result<()> {
        let conn = self.conn.lock().unwrap_or_else(|e| e.into_inner());
        conn.execute(
            "INSERT INTO follow_requests (actor_id, target_actor_id, inbox, shared_inbox, actor_data, activity, requested_at)
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7)
             ON CONFLICT(actor_id, target_actor_id) DO UPDATE SET
               inbox = excluded.inbox, shared_inbox = excluded.shared_inbox,
               actor_data = excluded.actor_data, activity = excluded.activity,
               requested_at = excluded.requested_at",
            params![
                request.actor_id,
                request.target_actor_id,
                request.inbox,
                request.shared_inbox,
                map_to_json(&request.actor_data),
                map_to_json(&request.activity),
                dt_str(request.requested_at).unwrap_or_else(|| crate::model::dt_to_iso(&Utc::now())),
            ],
        )
        .map_err(err)?;
        Ok(())
    }

    fn get_follow_requests(&self, target_actor_id: Option<&str>) -> Result<Vec<FollowRequest>> {
        let conn = self.conn.lock().unwrap_or_else(|e| e.into_inner());
        let (sql, params_vec): (&str, Vec<String>) = match target_actor_id {
            Some(t) => (
                "SELECT actor_id, target_actor_id, inbox, shared_inbox, actor_data, activity, requested_at
                 FROM follow_requests WHERE target_actor_id = ?1",
                vec![t.to_string()],
            ),
            None => (
                "SELECT actor_id, target_actor_id, inbox, shared_inbox, actor_data, activity, requested_at
                 FROM follow_requests",
                vec![],
            ),
        };
        let mut stmt = conn.prepare(sql).map_err(err)?;
        let rows = stmt
            .query_map(rusqlite::params_from_iter(params_vec.iter()), |r| {
                Ok((
                    r.get::<_, String>(0)?,
                    r.get::<_, String>(1)?,
                    r.get::<_, String>(2)?,
                    r.get::<_, String>(3)?,
                    r.get::<_, String>(4)?,
                    r.get::<_, String>(5)?,
                    r.get::<_, String>(6)?,
                ))
            })
            .map_err(err)?;
        let mut out = Vec::new();
        for r in rows.flatten() {
            out.push(FollowRequest {
                actor_id: r.0,
                target_actor_id: r.1,
                inbox: r.2,
                shared_inbox: r.3,
                actor_data: parse_json_map(&r.4),
                activity: parse_json_map(&r.5),
                requested_at: parse_dt_str_opt(&r.6),
            });
        }
        Ok(out)
    }

    fn remove_follow_request(&self, actor_id: &str, target_actor_id: &str) -> Result<bool> {
        let conn = self.conn.lock().unwrap_or_else(|e| e.into_inner());
        let n = if target_actor_id.is_empty() {
            conn.execute(
                "DELETE FROM follow_requests WHERE actor_id = ?1",
                params![actor_id],
            )
        } else {
            conn.execute(
                "DELETE FROM follow_requests WHERE actor_id = ?1 AND target_actor_id = ?2",
                params![actor_id, target_actor_id],
            )
        }
        .map_err(err)?;
        Ok(n > 0)
    }

    fn store_interaction(&self, interaction: &Interaction) -> Result<()> {
        let conn = self.conn.lock().unwrap_or_else(|e| e.into_inner());
        // Match Python semantics: multi-interaction types (reply/quote)
        // allow several rows per (actor, target); others upsert by them.
        let is_multi = matches!(
            interaction.interaction_type,
            InteractionType::Reply | InteractionType::Quote
        );
        let existing_id: Option<i64> = if is_multi && !interaction.object_id.is_empty() {
            conn.query_row(
                "SELECT id FROM interactions WHERE source_actor_id = ?1 AND target_resource = ?2 AND interaction_type = ?3 AND object_id = ?4",
                params![
                    interaction.source_actor_id,
                    interaction.target_resource,
                    interaction.interaction_type.as_str(),
                    interaction.object_id
                ],
                |r| r.get(0),
            )
            .ok()
        } else if is_multi {
            None
        } else {
            conn.query_row(
                "SELECT id FROM interactions WHERE source_actor_id = ?1 AND target_resource = ?2 AND interaction_type = ?3",
                params![
                    interaction.source_actor_id,
                    interaction.target_resource,
                    interaction.interaction_type.as_str()
                ],
                |r| r.get(0),
            )
            .ok()
        };

        let id = match existing_id {
            Some(id) => {
                conn.execute(
                    "UPDATE interactions SET activity_id=?1, object_id=?2, content=?3, author_name=?4, author_url=?5, author_photo=?6, published=?7, status=?8, meta=?9, updated_at=?10 WHERE id=?11",
                    params![
                        interaction.activity_id,
                        interaction.object_id,
                        interaction.content,
                        interaction.author_name,
                        interaction.author_url,
                        interaction.author_photo,
                        dt_str(interaction.published),
                        interaction.status.as_str(),
                        map_to_json(&interaction.metadata),
                        dt_str(Some(Utc::now())),
                        id,
                    ],
                )
                .map_err(err)?;
                conn.execute(
                    "DELETE FROM interaction_mentions WHERE interaction_id = ?1",
                    params![id],
                )
                .map_err(err)?;
                id
            }
            None => {
                conn.execute(
                    "INSERT INTO interactions (source_actor_id, target_resource, interaction_type, activity_id, object_id, content, author_name, author_url, author_photo, published, status, meta, created_at, updated_at)
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, ?8, ?9, ?10, ?11, ?12, ?13, ?14)",
                    params![
                        interaction.source_actor_id,
                        interaction.target_resource,
                        interaction.interaction_type.as_str(),
                        interaction.activity_id,
                        interaction.object_id,
                        interaction.content,
                        interaction.author_name,
                        interaction.author_url,
                        interaction.author_photo,
                        dt_str(interaction.published),
                        interaction.status.as_str(),
                        map_to_json(&interaction.metadata),
                        dt_str(interaction.created_at.or(Some(Utc::now()))),
                        dt_str(interaction.updated_at.or(Some(Utc::now()))),
                    ],
                )
                .map_err(err)?;
                conn.last_insert_rowid()
            }
        };

        for actor_url in &interaction.mentioned_actors {
            conn.execute(
                "INSERT OR IGNORE INTO interaction_mentions (interaction_id, mentioned_actor_url) VALUES (?1, ?2)",
                params![id, actor_url],
            )
            .map_err(err)?;
        }
        Ok(())
    }

    fn delete_interaction(
        &self,
        source_actor_id: &str,
        target_resource: &str,
        interaction_type: InteractionType,
    ) -> Result<()> {
        let conn = self.conn.lock().unwrap_or_else(|e| e.into_inner());
        conn.execute(
            "UPDATE interactions SET status = ?1, updated_at = ?2
             WHERE source_actor_id = ?3 AND target_resource = ?4 AND interaction_type = ?5",
            params![
                InteractionStatus::Deleted.as_str(),
                crate::model::dt_to_iso(&Utc::now()),
                source_actor_id,
                target_resource,
                interaction_type.as_str(),
            ],
        )
        .map_err(err)?;
        Ok(())
    }

    fn delete_interaction_by_object_id(
        &self,
        source_actor_id: &str,
        object_id: &str,
    ) -> Result<bool> {
        let conn = self.conn.lock().unwrap_or_else(|e| e.into_inner());
        let n = conn
            .execute(
                "UPDATE interactions SET status = ?1, updated_at = ?2
                 WHERE source_actor_id = ?3 AND object_id = ?4 AND status != ?1",
                params![
                    InteractionStatus::Deleted.as_str(),
                    crate::model::dt_to_iso(&Utc::now()),
                    source_actor_id,
                    object_id,
                ],
            )
            .map_err(err)?;
        Ok(n > 0)
    }

    fn get_interaction_by_object_id(
        &self,
        object_id: &str,
        status: InteractionStatus,
    ) -> Result<Option<Interaction>> {
        let conn = self.conn.lock().unwrap_or_else(|e| e.into_inner());
        let mut stmt = conn
            .prepare(&format!(
                "SELECT {INTERACTION_COLS} FROM interactions WHERE object_id = ?1 AND status = ?2 LIMIT 1"
            ))
            .map_err(err)?;
        let mut rows = stmt
            .query_map(params![object_id, status.as_str()], read_interaction_row)
            .map_err(err)?;
        match rows.next() {
            Some(Ok(r)) => Ok(Some(self.interaction_from_row(&conn, r)?)),
            _ => Ok(None),
        }
    }

    fn get_interactions(
        &self,
        target_resource: &str,
        interaction_type: Option<InteractionType>,
        status: InteractionStatus,
    ) -> Result<Vec<Interaction>> {
        let conn = self.conn.lock().unwrap_or_else(|e| e.into_inner());
        let mut sql = format!(
            "SELECT {INTERACTION_COLS} FROM interactions WHERE target_resource = ?1 AND status = ?2"
        );
        let mut params_vec: Vec<String> =
            vec![target_resource.to_string(), status.as_str().to_string()];
        if let Some(t) = interaction_type {
            sql += " AND interaction_type = ?3";
            params_vec.push(t.as_str().to_string());
        }
        let mut stmt = conn.prepare(&sql).map_err(err)?;
        let rows = stmt
            .query_map(
                rusqlite::params_from_iter(params_vec.iter()),
                read_interaction_row,
            )
            .map_err(err)?;
        let mut out = Vec::new();
        for r in rows.flatten() {
            out.push(self.interaction_from_row(&conn, r)?);
        }
        Ok(out)
    }

    fn get_interactions_mentioning(
        &self,
        actor_url: &str,
        interaction_type: Option<InteractionType>,
        status: InteractionStatus,
    ) -> Result<Vec<Interaction>> {
        let conn = self.conn.lock().unwrap_or_else(|e| e.into_inner());
        let mut sql = format!(
            "SELECT DISTINCT {cols} FROM interactions i JOIN interaction_mentions m ON m.interaction_id = i.id WHERE m.mentioned_actor_url = ?1 AND i.status = ?2",
            cols = INTERACTION_COLS
                .split(", ")
                .map(|c| format!("i.{c}"))
                .collect::<Vec<_>>()
                .join(", ")
        );
        let mut params_vec: Vec<String> = vec![actor_url.to_string(), status.as_str().to_string()];
        if let Some(t) = interaction_type {
            sql += " AND i.interaction_type = ?3";
            params_vec.push(t.as_str().to_string());
        }
        let mut stmt = conn.prepare(&sql).map_err(err)?;
        let rows = stmt
            .query_map(
                rusqlite::params_from_iter(params_vec.iter()),
                read_interaction_row,
            )
            .map_err(err)?;
        let mut out = Vec::new();
        for r in rows.flatten() {
            out.push(self.interaction_from_row(&conn, r)?);
        }
        Ok(out)
    }

    fn store_activity(&self, activity_id: &str, activity_data: &Value) -> Result<()> {
        let conn = self.conn.lock().unwrap_or_else(|e| e.into_inner());
        conn.execute(
            "INSERT INTO activities (activity_id, activity_data, created_at) VALUES (?1, ?2, ?3)
             ON CONFLICT(activity_id) DO UPDATE SET activity_data = excluded.activity_data",
            params![
                activity_id,
                to_json_text(activity_data),
                crate::model::dt_to_iso(&Utc::now())
            ],
        )
        .map_err(err)?;
        Ok(())
    }

    fn get_activities(&self, limit: usize, offset: usize) -> Result<Vec<Value>> {
        let conn = self.conn.lock().unwrap_or_else(|e| e.into_inner());
        let mut stmt = conn
            .prepare(
                "SELECT activity_data FROM activities ORDER BY created_at DESC LIMIT ?1 OFFSET ?2",
            )
            .map_err(err)?;
        let rows = stmt
            .query_map(params![limit as i64, offset as i64], |r| {
                r.get::<_, String>(0)
            })
            .map_err(err)?;
        Ok(rows.flatten().map(|s| parse_json_value(&s)).collect())
    }

    fn cache_remote_actor(
        &self,
        actor_id: &str,
        actor_data: &Value,
        fetched_at: Option<DateTime<Utc>>,
    ) -> Result<()> {
        let conn = self.conn.lock().unwrap_or_else(|e| e.into_inner());
        conn.execute(
            "INSERT INTO actor_cache (actor_id, actor_data, fetched_at) VALUES (?1, ?2, ?3)
             ON CONFLICT(actor_id) DO UPDATE SET actor_data = excluded.actor_data, fetched_at = excluded.fetched_at",
            params![
                actor_id,
                to_json_text(actor_data),
                crate::model::dt_to_iso(&fetched_at.unwrap_or_else(Utc::now))
            ],
        )
        .map_err(err)?;
        Ok(())
    }

    fn get_cached_actor(&self, actor_id: &str, max_age_seconds: f64) -> Result<Option<Value>> {
        let conn = self.conn.lock().unwrap_or_else(|e| e.into_inner());
        let row: Option<(String, String)> = conn
            .query_row(
                "SELECT actor_data, fetched_at FROM actor_cache WHERE actor_id = ?1",
                params![actor_id],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .ok();
        let Some((data, fetched_at)) = row else {
            return Ok(None);
        };
        if let Some(dt) = parse_dt_str_opt(&fetched_at) {
            let age = (Utc::now() - dt).num_milliseconds() as f64 / 1000.0;
            if age > max_age_seconds {
                return Ok(None);
            }
        }
        Ok(Some(parse_json_value(&data)))
    }

    fn store_quote_authorization(
        &self,
        authorization_id: &str,
        authorization_data: &Value,
    ) -> Result<()> {
        // Python stores quote authorizations in the `activities` table.
        self.store_activity(authorization_id, authorization_data)
    }

    fn get_quote_authorization(&self, authorization_id: &str) -> Result<Option<Value>> {
        let conn = self.conn.lock().unwrap_or_else(|e| e.into_inner());
        // Python reads authorizations back from `activities`.
        if let Ok(data) = conn.query_row(
            "SELECT activity_data FROM activities WHERE activity_id = ?1",
            params![authorization_id],
            |r| r.get::<_, String>(0),
        ) {
            return Ok(Some(parse_json_value(&data)));
        }
        // Fall back to the dedicated table used by earlier Rust releases.
        let has_legacy = conn
            .query_row::<i64, _, _>(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='quote_authorizations'",
                [],
                |r| r.get(0),
            )
            .map(|n| n > 0)
            .unwrap_or(false);
        if !has_legacy {
            return Ok(None);
        }
        Ok(conn
            .query_row(
                "SELECT authorization_data FROM quote_authorizations WHERE authorization_id = ?1",
                params![authorization_id],
                |r| r.get::<_, String>(0),
            )
            .ok()
            .map(|s| parse_json_value(&s)))
    }
}
