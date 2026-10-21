//! Feature-gated axum adapter smoke tests.

use std::sync::Arc;

use axum::body::Body;
use axum::http::{Request, StatusCode};
use pubby_sdk::handlers::HandlerOptions;
use pubby_sdk::server::axum::{activitypub_router, mastodon_router};
use pubby_sdk::server::mastodon::MastodonAPI;
use pubby_sdk::*;
use serde_json::{json, Value};
use tower::ServiceExt;

fn make_handler(dir: &tempfile::TempDir) -> Arc<ActivityPubHandler> {
    let storage = Arc::new(FileActivityPubStorage::new(dir.path().join("ap"), true).unwrap());
    let (private_key, _) = crypto::generate_rsa_keypair(2048).unwrap();
    let config = ActorConfig::from_json(&json!({
        "base_url": "https://blog.example.com",
        "username": "blog",
        "prefix": "/ap",
    }))
    .unwrap();
    Arc::new(
        ActivityPubHandler::new(storage, config, private_key, HandlerOptions::default()).unwrap(),
    )
}

async fn call(router: axum::Router, uri: &str) -> (StatusCode, Value) {
    let resp = router
        .oneshot(Request::builder().uri(uri).body(Body::empty()).unwrap())
        .await
        .unwrap();
    let status = resp.status();
    let body = axum::body::to_bytes(resp.into_body(), usize::MAX)
        .await
        .unwrap();
    let doc = serde_json::from_slice(&body).unwrap_or(Value::Null);
    (status, doc)
}

#[test]
fn test_axum_activitypub_routes() {
    futures::executor::block_on(test_axum_activitypub_routes_async());
}

async fn test_axum_activitypub_routes_async() {
    let dir = tempfile::tempdir().unwrap();
    let handler = make_handler(&dir);
    let router = activitypub_router(handler, "/ap", None, None, None, false);

    let (status, doc) = call(router.clone(), "/ap/actor").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(doc["preferredUsername"], "blog");

    let (status, _) = call(router.clone(), "/.well-known/webfinger").await;
    assert_eq!(status, StatusCode::BAD_REQUEST);
    let (status, _) = call(
        router.clone(),
        "/.well-known/webfinger?resource=acct:blog@blog.example.com",
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    // Empty resource → 400.
    let (status, _) = call(router.clone(), "/.well-known/webfinger?resource=").await;
    assert_eq!(status, StatusCode::BAD_REQUEST);

    let (status, doc) = call(router.clone(), "/nodeinfo/2.1").await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(doc["software"]["name"], "pubby");
    for alias in ["/nodeinfo/2.0", "/nodeinfo/2.0.json", "/nodeinfo/2.1.json"] {
        let (status, _) = call(router.clone(), alias).await;
        assert_eq!(status, StatusCode::OK, "{alias}");
    }

    // Unsigned inbox POST → 401.
    let resp = router
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/ap/inbox")
                .header("content-type", "application/activity+json")
                .body(Body::from(r#"{"type":"Like","actor":"x"}"#))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);
}

#[test]
fn test_axum_mastodon_routes() {
    futures::executor::block_on(test_axum_mastodon_routes_async());
}

async fn test_axum_mastodon_routes_async() {
    let dir = tempfile::tempdir().unwrap();
    let handler = make_handler(&dir);
    let api = Arc::new(MastodonAPI::new(handler));
    let router = mastodon_router(api);

    let (status, doc) = call(router.clone(), "/api/v1/instance").await;
    assert_eq!(status, StatusCode::OK);
    assert!(doc["version"].as_str().unwrap().contains("pubby"));

    let (status, doc) = call(
        router.clone(),
        "/api/v1/accounts/lookup?acct=blog@blog.example.com",
    )
    .await;
    assert_eq!(status, StatusCode::OK);
    assert_eq!(doc["acct"], "blog@blog.example.com");

    // only_media=TRUE must coerce case-insensitively (Python parity).
    let (status, _) = call(
        router.clone(),
        "/api/v1/accounts/1/statuses?only_media=TRUE",
    )
    .await;
    assert_eq!(status, StatusCode::OK);
}
