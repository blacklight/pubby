//! Framework-agnostic HTTP dispatch for the pubby endpoints — the Rust
//! equivalent of the Flask/FastAPI/Tornado adapters' shared logic.
//!
//! [`dispatch`] maps `(method, path, query, headers, body)` to a plain
//! [`HttpResp`]. Any Rust web framework can adapt it; the `axum` feature
//! provides a ready-made adapter (see `server::axum`).

pub mod mastodon;

#[cfg(feature = "axum")]
pub mod axum;

use std::collections::HashMap;
use std::sync::Arc;

use serde_json::{json, Value};

use crate::cache::{self, CachedResponse, DocumentCache};
use crate::handlers::ActivityPubHandler;
use crate::rate_limit::RateLimiter;

/// `application/activity+json` media type.
pub const ACTIVITY_JSON: &str = "application/activity+json";
/// `application/jrd+json` media type (WebFinger).
pub const JRD_JSON: &str = "application/jrd+json";

/// A framework-agnostic HTTP response.
#[derive(Debug, Clone)]
pub struct HttpResp {
    /// HTTP status code.
    pub status: u16,
    /// Response headers.
    pub headers: HashMap<String, String>,
    /// Response body bytes.
    pub body: Vec<u8>,
}

impl HttpResp {
    /// Build a JSON response.
    pub fn json(status: u16, body: &Value) -> Self {
        HttpResp {
            status,
            headers: [("Content-Type".to_string(), "application/json".to_string())]
                .into_iter()
                .collect(),
            body: serde_json::to_vec(body).unwrap_or_default(),
        }
    }

    /// Build a JSON response with a specific media type.
    pub fn json_typed(status: u16, body: &Value, media_type: &str) -> Self {
        let mut r = Self::json(status, body);
        r.headers
            .insert("Content-Type".to_string(), media_type.to_string());
        r
    }
}

/// Request → rate-limit bucket key extractor.
pub type RateLimitKeyFn = Arc<dyn Fn(&DispatchRequest) -> String + Send + Sync>;

/// Options for [`dispatch`] — mirrors the adapters' `bind_activitypub`
/// kwargs.
#[derive(Default)]
pub struct DispatchOptions {
    /// Optional rate limiter for the inbox endpoint.
    pub rate_limiter: Option<Arc<RateLimiter>>,
    /// Optional document cache shared by dereference GET endpoints.
    /// Falls back to `handler.document_cache`.
    pub document_cache: Option<Arc<DocumentCache>>,
    /// Custom key extractor for the rate-limit bucket (e.g. a
    /// trusted-proxy-aware resolver).
    pub rate_limit_key: Option<RateLimitKeyFn>,
}

/// A framework-agnostic incoming request passed to [`dispatch`].
#[derive(Debug, Clone, Default)]
pub struct DispatchRequest {
    /// HTTP method (`GET`, `POST`, …).
    pub method: String,
    /// Request path (e.g. `/ap/inbox`).
    pub path: String,
    /// Query parameters (first value wins).
    pub query: HashMap<String, String>,
    /// Request headers.
    pub headers: HashMap<String, String>,
    /// Raw request body.
    pub body: Vec<u8>,
    /// Client IP / address for the default rate-limit bucket.
    pub remote_addr: String,
    /// Raw `(request-target)` (path + query, as the client sent it) used
    /// for HTTP-signature verification. Falls back to `path` when unset —
    /// adapters behind mounts/proxies should fill this in.
    pub request_target: Option<String>,
}

/// Serve a dereference document through the shared document cache.
///
/// `render` returns the JSON document or `None` for a 404.
pub fn serve_document(
    handler: &ActivityPubHandler,
    options: &DispatchOptions,
    request: &DispatchRequest,
    key: &[String],
    media_type: &str,
    render: impl FnOnce() -> Option<Value>,
) -> HttpResp {
    let document_cache = options
        .document_cache
        .clone()
        .or_else(|| handler.document_cache.clone());

    let Some(cache) = document_cache else {
        return match render() {
            Some(doc) => HttpResp::json_typed(200, &doc, media_type),
            None => HttpResp::json(404, &json!({"error": "not found"})),
        };
    };

    let rendered = cache.get_or_render::<CachedResponse, _>(
        key.to_vec(),
        || Ok(render().map(|doc| CachedResponse::document(&doc, media_type, 200, HashMap::new()))),
        None,
        None,
    );

    let cached = match rendered {
        Ok(Some(c)) => c,
        Ok(None) => return HttpResp::json(404, &json!({"error": "not found"})),
        Err(e) => {
            log::warn!("Document cache render failed for {key:?}: {e}");
            return HttpResp::json(500, &json!({"error": "internal error"}));
        }
    };

    // Emit the entry's remaining freshness rather than the full TTL.
    // Stored response headers sit *under* the computed cache headers
    // (Python merges `{**cached.headers, **headers}`).
    let remaining = cache.ttl_remaining(&key.to_vec());
    let mut headers = cached.headers.clone();
    headers.extend(cache::cache_headers(
        remaining,
        &cached.etag,
        true,
        &["Accept"],
        cache.default_ttl > 0.0,
    ));
    let if_none_match = request
        .headers
        .iter()
        .find(|(k, _)| k.eq_ignore_ascii_case("if-none-match"))
        .map(|(_, v)| v.as_str());
    if !cached.etag.is_empty() && cache::etag_matches(if_none_match, &cached.etag) {
        return HttpResp {
            status: 304,
            headers,
            body: Vec::new(),
        };
    }
    headers.insert("Content-Type".to_string(), cached.media_type.clone());
    HttpResp {
        status: cached.status,
        headers,
        body: cached.body.clone(),
    }
}

/// Route a request to the matching pubby endpoint.
///
/// `prefix` is the ActivityPub route prefix (default `/ap` when empty).
/// WebFinger/NodeInfo well-known routes are served at their canonical
/// paths regardless of `prefix`; the quote-authorization route lives
/// under `{handler.actor_path}`.
pub fn dispatch(
    handler: &ActivityPubHandler,
    request: &DispatchRequest,
    prefix: &str,
    options: &DispatchOptions,
) -> HttpResp {
    let prefix = if prefix.is_empty() {
        "/ap"
    } else {
        prefix.trim_end_matches('/')
    };
    let method = request.method.to_uppercase();
    let path = request.path.as_str();

    // -- WebFinger --
    if method == "GET" && path == "/.well-known/webfinger" {
        let resource = request.query.get("resource").cloned();
        let Some(resource) = resource.filter(|r| !r.is_empty()) else {
            return HttpResp::json(400, &json!({"error": "resource parameter is required"}));
        };
        if !handler.is_valid_webfinger_resource(Some(&resource)) {
            return HttpResp::json(404, &json!({"error": "not found"}));
        }
        let key = cache::webfinger_key(&resource);
        return serve_document(handler, options, request, &key, JRD_JSON, || {
            handler.get_webfinger_response(Some(&resource))
        });
    }

    // -- NodeInfo discovery --
    if method == "GET" && path == "/.well-known/nodeinfo" {
        let key = cache::nodeinfo_discovery_key();
        return serve_document(handler, options, request, &key, "application/json", || {
            Some(handler.get_nodeinfo_discovery())
        });
    }

    // -- NodeInfo document (canonical + aliases) --
    if method == "GET"
        && matches!(
            path,
            "/nodeinfo/2.1" | "/nodeinfo/2.0" | "/nodeinfo/2.0.json" | "/nodeinfo/2.1.json"
        )
    {
        let key = cache::nodeinfo_key();
        return serve_document(handler, options, request, &key, "application/json", || {
            handler.get_nodeinfo_document().ok()
        });
    }

    // -- Actor --
    if method == "GET" && path == format!("{prefix}/actor") {
        let key = cache::actor_document_key();
        return serve_document(handler, options, request, &key, ACTIVITY_JSON, || {
            Some(handler.get_actor_document())
        });
    }

    // -- Inbox --
    if method == "POST" && path == format!("{prefix}/inbox") {
        if let Some(rl) = &options.rate_limiter {
            let bucket = options
                .rate_limit_key
                .as_ref()
                .map(|f| f(request))
                .unwrap_or_else(|| {
                    if request.remote_addr.is_empty() {
                        "unknown".to_string()
                    } else {
                        request.remote_addr.clone()
                    }
                });
            if rl.check(&bucket).is_err() {
                return HttpResp::json(429, &json!({"error": "rate limit exceeded"}));
            }
        }
        let activity_data: Value = match serde_json::from_slice(&request.body) {
            Ok(v) => v,
            Err(_) => return HttpResp::json(400, &json!({"error": "invalid JSON"})),
        };
        let target = request.request_target.as_deref().unwrap_or(path);
        return match handler.process_inbox_activity(
            &activity_data,
            "POST",
            target,
            Some(&request.headers),
            Some(&request.body),
            false,
        ) {
            Ok(_) => HttpResp::json(202, &json!({"status": "ok"})),
            Err(e) if e.is_signature_verification() => {
                HttpResp::json(401, &json!({"error": e.to_string()}))
            }
            Err(e) => HttpResp::json(400, &json!({"error": e.to_string()})),
        };
    }

    // -- Outbox --
    if method == "GET" && path == format!("{prefix}/outbox") {
        let limit = request
            .query
            .get("limit")
            .and_then(|v| v.parse::<i64>().ok())
            .unwrap_or(20);
        let offset = request
            .query
            .get("offset")
            .and_then(|v| v.parse::<i64>().ok())
            .unwrap_or(0);
        let key = cache::outbox_key(limit.max(0), offset.max(0));
        return serve_document(handler, options, request, &key, ACTIVITY_JSON, || {
            handler
                .get_outbox(Some(limit.max(0) as usize), Some(offset.max(0) as usize))
                .ok()
        });
    }

    // -- Followers --
    if method == "GET" && path == format!("{prefix}/followers") {
        let key = cache::followers_key();
        return serve_document(handler, options, request, &key, ACTIVITY_JSON, || {
            handler.get_followers_collection(None).ok()
        });
    }

    // -- Following --
    if method == "GET" && path == format!("{prefix}/following") {
        let key = cache::following_key();
        return serve_document(handler, options, request, &key, ACTIVITY_JSON, || {
            handler.get_following_collection().ok()
        });
    }

    // -- Quote authorizations --
    let qa_prefix = format!("{}/quote_authorizations/", handler.actor_path);
    if method == "GET" && path.starts_with(&qa_prefix) {
        let auth_id = &path[qa_prefix.len()..];
        if auth_id.is_empty() || auth_id.contains('/') || auth_id.contains("..") {
            return HttpResp::json(404, &json!({"error": "not found"}));
        }
        let full_id = format!("{}/quote_authorizations/{auth_id}", handler.actor_id);
        let key = cache::quote_authorization_key(&full_id);
        return serve_document(handler, options, request, &key, ACTIVITY_JSON, || {
            handler.get_quote_authorization(&full_id).ok().flatten()
        });
    }

    HttpResp::json(404, &json!({"error": "not found"}))
}
