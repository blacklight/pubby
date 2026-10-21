//! axum adapter — mirrors `pubby.server.adapters.*`.
//!
//! [`activitypub_router`] builds an `axum::Router` exposing all pubby
//! endpoints; merge or nest it on your application router. Serve it with
//! `into_make_service_with_connect_info::<SocketAddr>()` so inbox rate
//! limiting buckets by the real client address.

use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::Arc;

use axum::body::{to_bytes, Body};
use axum::extract::{ConnectInfo, OriginalUri, Query, Request, State};
use axum::http::HeaderMap;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::Router;

use crate::handlers::ActivityPubHandler;
use crate::rate_limit::RateLimiter;

use super::mastodon::MastodonAPI;
use super::{dispatch, DispatchOptions, DispatchRequest, HttpResp};

/// Maximum accepted inbox body (8 MiB).
const MAX_INBOX_BODY: usize = 8 * 1024 * 1024;

/// Shared state for the axum adapter.
#[derive(Clone)]
struct ApState {
    handler: Arc<ActivityPubHandler>,
    prefix: Arc<String>,
    options: Arc<DispatchOptions>,
}

/// Normalize a route prefix to `/seg/seg` form (no trailing slash).
fn normalize_prefix(prefix: &str) -> String {
    let trimmed = prefix.trim_matches('/');
    if trimmed.is_empty() {
        "/ap".to_string()
    } else {
        format!("/{trimmed}")
    }
}

fn headers_to_map(headers: &HeaderMap) -> HashMap<String, String> {
    headers
        .iter()
        .map(|(k, v)| {
            (
                k.as_str().to_lowercase(),
                v.to_str().unwrap_or_default().to_string(),
            )
        })
        .collect()
}

fn into_response(resp: HttpResp) -> Response {
    let mut builder = axum::http::Response::builder().status(resp.status);
    for (k, v) in &resp.headers {
        builder = builder.header(k, v);
    }
    builder
        .body(Body::from(resp.body))
        .unwrap_or_else(|_| axum::http::StatusCode::INTERNAL_SERVER_ERROR.into_response())
}

/// Build a `DispatchRequest` from an axum `Request`.
///
/// `path` is the (nest-relative) request path used for routing;
/// `request_target` is the original `path + query` the client sent —
/// what remote servers actually signed.
async fn make_request(req: Request) -> std::result::Result<DispatchRequest, Box<Response>> {
    let (parts, body) = req.into_parts();
    // `nest` rewrites `req.uri` to the inner path — `OriginalUri` holds
    // the full path + query that dispatch routes on and clients signed.
    let uri = parts.extensions.get::<OriginalUri>().map(|o| o.0.clone());
    let uri = uri.unwrap_or(parts.uri);
    let path = uri.path().to_string();
    let query: HashMap<String, String> = Query::try_from_uri(&uri)
        .map(|Query(q)| q)
        .unwrap_or_default();
    let request_target = uri
        .path_and_query()
        .map(|pq| pq.as_str().to_string())
        .unwrap_or_else(|| uri.path().to_string());
    let remote_addr = parts
        .extensions
        .get::<ConnectInfo<SocketAddr>>()
        .map(|ci| ci.0.ip().to_string())
        .unwrap_or_default();
    let headers = headers_to_map(&parts.headers);
    let body = to_bytes(body, MAX_INBOX_BODY)
        .await
        .map_err(|_| Box::new(axum::http::StatusCode::PAYLOAD_TOO_LARGE.into_response()))?;
    Ok(DispatchRequest {
        method: parts.method.as_str().to_string(),
        path,
        query,
        headers,
        body: body.to_vec(),
        remote_addr,
        request_target: Some(request_target),
    })
}

async fn serve_req(State(s): State<ApState>, req: Request) -> Response {
    let resp = match make_request(req).await {
        Ok(dreq) => dispatch(&s.handler, &dreq, &s.prefix, &s.options),
        Err(e) => return *e,
    };
    into_response(resp)
}

/// Bind ActivityPub routes to an axum `Router`.
///
/// Registers:
/// - `GET /.well-known/webfinger`
/// - `GET /.well-known/nodeinfo`
/// - `GET /nodeinfo/2.1` (+ `/nodeinfo/2.0`, `/nodeinfo/2.0.json`,
///   `/nodeinfo/2.1.json` aliases)
/// - `GET <prefix>/actor`, `POST <prefix>/inbox`, `GET <prefix>/outbox`,
///   `GET <prefix>/followers`, `GET <prefix>/following`
/// - `GET {handler.actor_path}/quote_authorizations/*auth_id`
///
/// :param handler: Shared `ActivityPubHandler`.
/// :param prefix: URL prefix for AP routes (default `/ap`).
/// :param rate_limiter: Optional rate limiter for the inbox endpoint.
/// :param document_cache: Optional `DocumentCache` shared by dereference
///     endpoints (falls back to `handler.document_cache`).
/// :param rate_limit_key: Optional request → bucket-key extractor.
/// :param authorized_fetch: Require a valid HTTP signature on AP
///     dereference GET routes (`/.well-known` and `/nodeinfo` stay open).
pub fn activitypub_router(
    handler: Arc<ActivityPubHandler>,
    prefix: &str,
    rate_limiter: Option<Arc<RateLimiter>>,
    document_cache: Option<Arc<crate::cache::DocumentCache>>,
    rate_limit_key: Option<super::RateLimitKeyFn>,
    authorized_fetch: bool,
) -> Router {
    let prefix = normalize_prefix(prefix);
    let state = ApState {
        handler,
        prefix: Arc::new(prefix),
        options: Arc::new(DispatchOptions {
            rate_limiter,
            document_cache,
            rate_limit_key,
            authorized_fetch,
        }),
    };

    let well_known = Router::new()
        .route("/.well-known/webfinger", get(serve_req))
        .route("/.well-known/nodeinfo", get(serve_req))
        .route("/.well-known/host-meta", get(serve_req))
        .route("/.well-known/host-meta.json", get(serve_req))
        .route("/nodeinfo/2.1", get(serve_req))
        .route("/nodeinfo/2.0", get(serve_req))
        .route("/nodeinfo/2.0.json", get(serve_req))
        .route("/nodeinfo/2.1.json", get(serve_req));

    let ap = Router::new()
        .route("/actor", get(serve_req))
        .route("/inbox", post(serve_req))
        .route("/outbox", get(serve_req))
        .route("/followers", get(serve_req))
        .route("/following", get(serve_req));

    let qa_path = format!(
        "{}/quote_authorizations/{{*auth_id}}",
        state.handler.actor_path
    );
    let qa = Router::new().route(&qa_path, get(serve_req));

    Router::new()
        .merge(well_known)
        .nest(state.prefix.as_str(), ap)
        .merge(qa)
        .with_state(state)
}

/// Shared state for the multi-actor adapter.
#[derive(Clone)]
struct ApMultiState {
    multi: Arc<crate::handlers::MultiActorHandler>,
    prefix: Arc<String>,
    options: Arc<DispatchOptions>,
}

async fn serve_req_multi(State(s): State<ApMultiState>, req: Request) -> Response {
    let resp = match make_request(req).await {
        Ok(dreq) => super::dispatch_multi(&s.multi, &dreq, &s.prefix, &s.options),
        Err(e) => return *e,
    };
    into_response(resp)
}

/// Bind the multi-actor ActivityPub routes to an axum `Router`.
///
/// Registers the same well-known endpoints as [`activitypub_router`]
/// plus, derived from `handler.actor_path_template` (e.g.
/// `/ap/users/{username}` — the placeholder is a literal axum path
/// param): the actor document, `inbox`/`outbox`/`followers`/`following`/
/// `liked`/`collections/featured`, `activities/{id}` and
/// `quote_authorizations/{id}` per actor, plus `{prefix}/actor` and the
/// shared `POST {prefix}/inbox`.
pub fn activitypub_router_multi(
    handler: Arc<crate::handlers::MultiActorHandler>,
    prefix: &str,
    rate_limiter: Option<Arc<RateLimiter>>,
    document_cache: Option<Arc<crate::cache::DocumentCache>>,
    rate_limit_key: Option<super::RateLimitKeyFn>,
    authorized_fetch: bool,
) -> Router {
    let prefix = normalize_prefix(prefix);
    let state = ApMultiState {
        multi: handler,
        prefix: Arc::new(prefix.clone()),
        options: Arc::new(DispatchOptions {
            rate_limiter,
            document_cache,
            rate_limit_key,
            authorized_fetch,
        }),
    };

    let well_known = Router::new()
        .route("/.well-known/webfinger", get(serve_req_multi))
        .route("/.well-known/nodeinfo", get(serve_req_multi))
        .route("/.well-known/host-meta", get(serve_req_multi))
        .route("/.well-known/host-meta.json", get(serve_req_multi))
        .route("/nodeinfo/2.1", get(serve_req_multi))
        .route("/nodeinfo/2.0", get(serve_req_multi))
        .route("/nodeinfo/2.0.json", get(serve_req_multi))
        .route("/nodeinfo/2.1.json", get(serve_req_multi));

    // The template contains a literal `{username}` segment — already
    // axum's `{name}` path-param syntax.
    let tpl = state.multi.actor_path_template.clone();
    let mut ap = Router::new()
        .route("/actor", get(serve_req_multi))
        .route("/inbox", post(serve_req_multi))
        .route("/outbox", get(serve_req_multi))
        .route("/followers", get(serve_req_multi))
        .route("/following", get(serve_req_multi));
    // Per-actor routes live outside the prefix — the template carries
    // its own leading path (e.g. "/ap/users/{username}").
    ap = ap.merge(
        Router::new()
            .route(&tpl, get(serve_req_multi))
            .route(&format!("{tpl}/inbox"), post(serve_req_multi))
            .route(&format!("{tpl}/outbox"), get(serve_req_multi))
            .route(&format!("{tpl}/followers"), get(serve_req_multi))
            .route(&format!("{tpl}/following"), get(serve_req_multi))
            .route(&format!("{tpl}/liked"), get(serve_req_multi))
            .route(&format!("{tpl}/collections/featured"), get(serve_req_multi))
            .route(&format!("{tpl}/activities/{{*rest}}"), get(serve_req_multi))
            .route(
                &format!("{tpl}/quote_authorizations/{{*rest}}"),
                get(serve_req_multi),
            ),
    );

    Router::new()
        .merge(well_known)
        .nest(&prefix, ap)
        .with_state(state)
}

fn json_resp((body, status): (serde_json::Value, u16)) -> Response {
    let b = axum::http::Response::builder()
        .status(status)
        .header("Content-Type", "application/json");
    match b.body(Body::from(serde_json::to_vec(&body).unwrap_or_default())) {
        Ok(r) => r,
        Err(_) => axum::http::StatusCode::INTERNAL_SERVER_ERROR.into_response(),
    }
}

/// Bind the Mastodon-compatible API routes to an axum `Router`.
///
/// Registers the read-only subset implemented by [`MastodonAPI`]:
/// `/api/v1/instance`, `/api/v2/instance`, `/api/v1/instance/peers`,
/// `/api/v1/accounts/lookup`, `/api/v1/accounts/:id`,
/// `/api/v1/accounts/:id/statuses`, `/api/v1/accounts/:id/followers`,
/// `/api/v1/statuses/:id`.
pub fn mastodon_router(api: Arc<MastodonAPI>) -> Router {
    let api_lookup = api.clone();
    let api_get = api.clone();
    let api_statuses = api.clone();
    let api_followers = api.clone();
    let api_peers = api.clone();
    Router::new()
        .route(
            "/api/v1/instance",
            get({
                let api = api.clone();
                move || async move { json_resp(api.instance_v1()) }
            }),
        )
        .route(
            "/api/v2/instance",
            get({
                let api = api.clone();
                move || async move { json_resp(api.instance_v2()) }
            }),
        )
        .route(
            "/api/v1/instance/peers",
            get(move || async move { json_resp(api_peers.instance_peers()) }),
        )
        .route(
            "/api/v1/accounts/lookup",
            get(move |Query(q): Query<HashMap<String, String>>| async move {
                json_resp(api_lookup.accounts_lookup(q.get("acct").map(String::as_str)))
            }),
        )
        .route(
            "/api/v1/accounts/{id}",
            get(
                move |axum::extract::Path(id): axum::extract::Path<String>| async move {
                    json_resp(api_get.accounts_get(&id))
                },
            ),
        )
        .route(
            "/api/v1/accounts/{id}/statuses",
            get(
                move |axum::extract::Path(id): axum::extract::Path<String>,
                      Query(q): Query<HashMap<String, String>>| async move {
                    json_resp(
                        api_statuses.accounts_statuses(
                            &id,
                            q.get("limit").and_then(|v| v.parse().ok()).unwrap_or(20),
                            q.get("max_id").map(String::as_str),
                            q.get("since_id").map(String::as_str),
                            q.get("only_media")
                                .map(|v| v.eq_ignore_ascii_case("true"))
                                .unwrap_or(false),
                            q.get("tagged").map(String::as_str),
                        ),
                    )
                },
            ),
        )
        .route(
            "/api/v1/accounts/{id}/followers",
            get(
                move |axum::extract::Path(id): axum::extract::Path<String>,
                      Query(q): Query<HashMap<String, String>>| async move {
                    json_resp(api_followers.accounts_followers(
                        &id,
                        q.get("limit").and_then(|v| v.parse().ok()).unwrap_or(40),
                        q.get("max_id").map(String::as_str),
                        q.get("since_id").map(String::as_str),
                    ))
                },
            ),
        )
        .route(
            "/api/v1/statuses/{id}",
            get({
                let api = api.clone();
                move |axum::extract::Path(id): axum::extract::Path<String>| async move {
                    json_resp(api.statuses_get(&id))
                }
            }),
        )
        .with_state(())
}
