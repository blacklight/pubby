//! Quote helpers — FEP-0449 quote fields and FEP-044f interaction
//! policies. Mirrors `pubby.quotes`.
//!
//! `quote` is the FEP-0449 field; `quoteUri` and `quoteUrl` are emitted by
//! Fedibird, Akkoma and Mastodon; `_misskey_quote` is Misskey's variant.
//! Readers should accept all four spellings ([`extract_quote_target`]);
//! writers should emit all of them ([`set_quote_target`]) so every
//! compatible server recognizes the quote.

use std::sync::LazyLock;

use serde_json::{json, Value};

use crate::model::AS_PUBLIC;

/// Quote field spellings recognized on inbound objects, in precedence
/// order — the FEP-0449 `quote` field wins when several are present.
pub const QUOTE_FIELD_KEYS: &[&str] = &["quote", "quoteUri", "quoteUrl", "_misskey_quote"];

/// FEP-044f extension terms for `QuoteRequest` and `QuoteAuthorization`
/// documents.
pub static FEP_044F_TERMS: LazyLock<Value> = LazyLock::new(|| {
    json!({
        "QuoteAuthorization": "https://w3id.org/fep/044f#QuoteAuthorization",
        "QuoteRequest": "https://w3id.org/fep/044f#QuoteRequest",
        "gts": "https://gotosocial.org/ns#",
        "interactingObject": {
            "@id": "gts:interactingObject",
            "@type": "@id",
        },
        "interactionTarget": {
            "@id": "gts:interactionTarget",
            "@type": "@id",
        },
    })
});

/// JSON-LD `@context` for FEP-044f payloads (`QuoteRequest` activities,
/// `QuoteAuthorization` documents and their `Accept`).
pub static FEP_044F_CONTEXT: LazyLock<Value> = LazyLock::new(|| {
    json!([
        "https://www.w3.org/ns/activitystreams",
        FEP_044F_TERMS.clone(),
    ])
});

/// FEP-044f `interactionPolicy` value allowing anyone to quote without
/// manual approval.
pub static PUBLIC_QUOTE_POLICY: LazyLock<Value> = LazyLock::new(|| {
    json!({
        "canQuote": {
            "automaticApproval": [AS_PUBLIC],
            "manualApproval": [],
        }
    })
});

/// Extract the quoted object URL from an object, if present.
///
/// Checks every spelling in [`QUOTE_FIELD_KEYS`].
pub fn extract_quote_target(obj_data: &Value) -> Option<String> {
    for key in QUOTE_FIELD_KEYS {
        if let Some(s) = obj_data.get(*key).and_then(|v| v.as_str()) {
            if !s.is_empty() {
                return Some(s.to_string());
            }
        }
    }
    None
}

/// Stamp `quoted_uri` on `obj` under every recognized quote field.
///
/// Emits all spellings in [`QUOTE_FIELD_KEYS`] so FEP-0449, Mastodon,
/// Fedibird, Akkoma and Misskey readers all recognize the quote.
/// Mutates `obj` (no-op when `obj` is not a JSON object).
pub fn set_quote_target(obj: &mut Value, quoted_uri: &str) {
    if let Some(m) = obj.as_object_mut() {
        for key in QUOTE_FIELD_KEYS {
            m.insert(key.to_string(), json!(quoted_uri));
        }
    }
}

/// Stamp the public-quote `interactionPolicy` on an object document.
/// Mutates `obj` (no-op when `obj` is not a JSON object).
pub fn allow_public_quotes(obj: &mut Value) {
    if let Some(m) = obj.as_object_mut() {
        m.insert("interactionPolicy".to_string(), PUBLIC_QUOTE_POLICY.clone());
    }
}
