//! Attribution validation — sanity-check inbound object authorship.
//! Mirrors `pubby.attribution`.
//!
//! HTTP signature verification proves *who delivered* an activity; it does
//! not prove the delivered object was authored by that actor.
//! [`validate`] enforces two generic rules:
//!
//! - a non-empty `attributedTo` must name the delivering actor;
//! - the object `id`, when present, must be hosted on the same authority
//!   as the delivering actor.
//!
//! Objects lacking comparable fields pass — absence of evidence is not
//! treated as a mismatch.

use serde_json::Value;

use crate::error::{ActivityPubError, Result};
use crate::moderation::extract_domain;

fn attribution_matches(attributed_to: &Value, actor: &str) -> bool {
    match attributed_to {
        Value::String(s) => s == actor,
        Value::Object(m) => m.get("id").and_then(|v| v.as_str()) == Some(actor),
        Value::Array(items) => items.iter().any(|i| attribution_matches(i, actor)),
        _ => false,
    }
}

/// Check that `obj` can plausibly be attributed to `actor`.
///
/// Rules:
///
/// - `attributedTo` absent or empty: accepted (nothing to compare).
/// - `attributedTo` string: must equal `actor`.
/// - `attributedTo` list: the actor must appear among its string
///   members; a dict member is expanded through its `id`.
/// - `attributedTo` of any other type: rejected — an unverifiable
///   attribution must not bypass the scalar-mismatch check.
/// - `id` absent: the host check is skipped.
/// - `id` present: its domain (per [`crate::moderation::extract_domain`])
///   must match the actor's. Unparseable ids yield an empty domain and
///   are rejected.
///
/// Returns [`ActivityPubError::AttributionMismatch`] on failure.
pub fn validate(actor: &str, obj: &Value) -> Result<()> {
    if let Some(attributed_to) = obj.get("attributedTo") {
        // Python truthiness: falsy values skip the match check entirely.
        let truthy = match attributed_to {
            Value::Null => false,
            Value::Bool(b) => *b,
            Value::Number(n) => n.as_f64().map(|f| f != 0.0).unwrap_or(true),
            Value::String(s) => !s.is_empty(),
            Value::Array(a) => !a.is_empty(),
            Value::Object(m) => !m.is_empty(),
        };
        if truthy {
            if !attribution_matches(attributed_to, actor) {
                return Err(ActivityPubError::AttributionMismatch(format!(
                    "attributedTo {attributed_to} does not match actor {actor:?}"
                )));
            }
        } else if !attributed_to.is_null()
            && !matches!(
                attributed_to,
                Value::String(_) | Value::Array(_) | Value::Object(_)
            )
        {
            // A non-empty non-string/list/dict attributedTo cannot be
            // verified — treat it as a mismatch rather than letting it
            // slide (e.g. `false`, `0`, `0.0`).
            return Err(ActivityPubError::AttributionMismatch(format!(
                "attributedTo of unsupported type for actor {actor:?}"
            )));
        }
    }

    if let Some(object_id) = obj.get("id").and_then(|v| v.as_str()) {
        if !object_id.is_empty() {
            let obj_domain = extract_domain(object_id);
            let actor_domain = extract_domain(actor);
            if obj_domain.is_empty() || actor_domain.is_empty() || obj_domain != actor_domain {
                return Err(ActivityPubError::AttributionMismatch(format!(
                    "object id {object_id:?} is hosted on a different authority \
                     than actor {actor:?}"
                )));
            }
        }
    }

    Ok(())
}
