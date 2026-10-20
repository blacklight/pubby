//! Migration utilities for pubby storage backends — mirrors
//! `pubby.storage._migrations`.

use std::path::Path;

use serde_json::{Map, Value};

use crate::model::Interaction;

use super::file::FileActivityPubStorage;
use super::ActivityPubStorage;

/// Extract actor URLs from Mention tags in an ActivityPub object.
///
/// Same logic as the inbox processor's mention extraction, duplicated to
/// avoid circular imports (mirrors the Python module).
pub fn extract_mentions_from_tags(obj_data: &Value) -> Vec<String> {
    let mut mentioned = Vec::new();
    if let Some(Value::Array(tags)) = obj_data.get("tag") {
        for tag in tags {
            if tag.get("type").and_then(|v| v.as_str()) == Some("Mention") {
                if let Some(href) = tag.get("href").and_then(|v| v.as_str()) {
                    if !href.is_empty() {
                        mentioned.push(href.to_string());
                    }
                }
            }
        }
    }
    mentioned
}

/// Get all interactions from a `FileActivityPubStorage` data directory.
pub fn get_all_file_interactions(data_dir: &Path) -> Vec<Interaction> {
    let mut interactions = Vec::new();
    let interactions_dir = data_dir.join("interactions");
    let Ok(entries) = std::fs::read_dir(&interactions_dir) else {
        return interactions;
    };

    for entry in entries.flatten() {
        let target_dir = entry.path();
        if !target_dir.is_dir() {
            continue;
        }
        let Ok(files) = std::fs::read_dir(&target_dir) else {
            continue;
        };
        for file in files.flatten() {
            let path = file.path();
            if path.extension().map(|e| e == "json").unwrap_or(false) {
                if let Ok(text) = std::fs::read_to_string(&path) {
                    if let Ok(data) = serde_json::from_str::<Value>(&text) {
                        if let Ok(i) = Interaction::from_json(&data) {
                            interactions.push(i);
                        }
                    }
                }
            }
        }
    }
    interactions
}

fn empty_stats(keys: &[&str]) -> Map<String, Value> {
    keys.iter()
        .map(|k| (k.to_string(), Value::from(0)))
        .collect()
}

/// Backfill `mentioned_actors` for existing interactions.
///
/// Scans all interactions, extracts mentions from the `raw_object` stored
/// in metadata, and updates the `mentioned_actors` field.
///
/// Only supports `FileActivityPubStorage` (mirrors the Python
/// implementation); other backends report zero stats.
pub fn backfill_mentions(
    storage: &dyn ActivityPubStorage,
    file_storage: Option<&FileActivityPubStorage>,
    dry_run: bool,
) -> Map<String, Value> {
    let mut stats = empty_stats(&[
        "scanned",
        "updated",
        "skipped_no_metadata",
        "skipped_no_raw_object",
        "skipped_already_has_mentions",
        "errors",
    ]);

    let Some(file_storage) = file_storage else {
        log::warn!("backfill_mentions currently only supports FileActivityPubStorage.");
        return stats;
    };

    for interaction in get_all_file_interactions(&file_storage.data_dir) {
        inc(&mut stats, "scanned");

        if !interaction.mentioned_actors.is_empty() {
            inc(&mut stats, "skipped_already_has_mentions");
            continue;
        }
        if interaction.metadata.is_empty() {
            inc(&mut stats, "skipped_no_metadata");
            continue;
        }
        let Some(raw_object) = interaction.metadata.get("raw_object") else {
            inc(&mut stats, "skipped_no_raw_object");
            continue;
        };
        let mentions = extract_mentions_from_tags(raw_object);
        if mentions.is_empty() {
            continue;
        }

        inc(&mut stats, "updated");
        if !dry_run {
            let mut updated = interaction.clone();
            updated.mentioned_actors = mentions;
            if let Err(e) = storage.store_interaction(&updated) {
                log::error!("Failed to update interaction {}: {e}", updated.object_id);
                inc(&mut stats, "errors");
            }
        }
    }
    stats
}

/// Backfill the `object_id` index for existing interactions.
pub fn backfill_object_id_index(
    file_storage: &FileActivityPubStorage,
    dry_run: bool,
) -> Map<String, Value> {
    let mut stats = empty_stats(&[
        "scanned",
        "indexed",
        "skipped_no_object_id",
        "skipped_already_indexed",
        "errors",
    ]);

    for interaction in get_all_file_interactions(&file_storage.data_dir) {
        inc(&mut stats, "scanned");

        if interaction.object_id.is_empty() {
            inc(&mut stats, "skipped_no_object_id");
            continue;
        }
        if file_storage
            .object_id_index_path(&interaction.object_id)
            .exists()
        {
            inc(&mut stats, "skipped_already_indexed");
            continue;
        }

        inc(&mut stats, "indexed");
        if !dry_run {
            if let Err(e) = file_storage.update_object_id_index(&interaction, true) {
                log::error!("Failed to index interaction {}: {e}", interaction.object_id);
                inc(&mut stats, "errors");
            }
        }
    }
    stats
}

fn inc(stats: &mut Map<String, Value>, key: &str) {
    let cur = stats.get(key).and_then(|v| v.as_i64()).unwrap_or(0);
    stats.insert(key.to_string(), Value::from(cur + 1));
}
