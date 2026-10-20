//! Audience helpers — parse ActivityPub addressing fields.
//! Mirrors `pubby.audience`.

use std::collections::HashSet;
use std::sync::LazyLock;

use serde_json::Value;

use crate::model::AS_PUBLIC;

/// Recognized identifiers for the ActivityStreams public audience. The
/// canonical URI is defined in the spec; `Public` and `as:Public` are
/// common shorthand aliases emitted by some implementations.
pub static PUBLIC_URIS: LazyLock<HashSet<&'static str>> =
    LazyLock::new(|| [AS_PUBLIC, "Public", "as:Public"].into_iter().collect());

/// Audience fields inspected by [`addressees`].
pub const AUDIENCE_FIELDS: &[&str] = &["to", "cc", "bto", "bcc"];

/// Return the union of string values in `to`, `cc`, `bto` and `bcc`.
///
/// Each field may be a string, a list of strings, or absent; non-string
/// collection members and non-string/non-list field values are ignored.
///
/// Only the supplied mapping is inspected — the function does not look
/// inside an activity's embedded `object`. Call it once per container
/// (activity and object) and combine the results if both scopes matter.
pub fn addressees(obj_or_activity: &Value) -> HashSet<String> {
    let mut urls = HashSet::new();
    let Some(map) = obj_or_activity.as_object() else {
        return urls;
    };
    for key in AUDIENCE_FIELDS {
        match map.get(*key) {
            Some(Value::String(s)) => {
                urls.insert(s.clone());
            }
            Some(Value::Array(items)) => {
                for v in items {
                    if let Some(s) = v.as_str() {
                        urls.insert(s.to_string());
                    }
                }
            }
            _ => {}
        }
    }
    urls
}

/// Return whether the mapping addresses the public collection.
///
/// `true` when any of [`PUBLIC_URIS`] appears in the mapping's audience
/// fields. Only the supplied mapping is inspected.
pub fn is_public(obj_or_activity: &Value) -> bool {
    addressees(obj_or_activity)
        .iter()
        .any(|u| PUBLIC_URIS.contains(u.as_str()))
}

/// Return actor URLs from the mapping's `Mention` tags.
///
/// Entries of the form `{"type": "Mention", "href": "<url>"}` in the `tag`
/// collection contribute their `href`; malformed entries, non-`Mention`
/// tags and empty `href` values are skipped. URLs are returned in
/// first-seen order with duplicates removed.
pub fn mentioned_actors(obj_data: &Value) -> Vec<String> {
    let mut mentioned = Vec::new();
    let mut seen = HashSet::new();
    if let Some(Value::Array(tags)) = obj_data.get("tag") {
        for tag in tags {
            if tag.get("type").and_then(|v| v.as_str()) != Some("Mention") {
                continue;
            }
            if let Some(href) = tag.get("href").and_then(|v| v.as_str()) {
                if !href.is_empty() && seen.insert(href.to_string()) {
                    mentioned.push(href.to_string());
                }
            }
        }
    }
    mentioned
}
