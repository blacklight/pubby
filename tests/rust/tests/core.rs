//! Integration tests for the pubby Rust crate — mirror the key Python
//! test coverage (model, collections, signatures, storage, handlers).

use std::collections::HashMap;
use std::sync::Arc;

use pubby_sdk::crypto::{
    export_private_key_pem, export_public_key_pem, generate_rsa_keypair, load_private_key,
    load_public_key, sign_request, verify_request,
};
use pubby_sdk::handlers::HandlerOptions;
use pubby_sdk::server::{dispatch, DispatchOptions, DispatchRequest};
use pubby_sdk::storage::file::FileActivityPubStorage;
use pubby_sdk::*;
use serde_json::{json, Value};

fn make_config() -> ActorConfig {
    ActorConfig::from_json(&json!({
        "base_url": "https://blog.example.com",
        "username": "blog",
        "name": "Test Blog",
        "summary": "A test blog",
        "icon_url": "https://blog.example.com/icon.png",
        "image_url": "https://blog.example.com/banner.png",
    }))
    .unwrap()
}

fn make_handler(dir: &tempfile::TempDir) -> Arc<ActivityPubHandler> {
    let storage = Arc::new(FileActivityPubStorage::new(dir.path(), true).unwrap());
    let (priv_key, _) = generate_rsa_keypair(2048).unwrap();
    let opts = HandlerOptions {
        async_delivery: false,
        ..HandlerOptions::default()
    };
    Arc::new(ActivityPubHandler::new(storage, make_config(), priv_key, opts).unwrap())
}

#[test]
fn test_ordered_collection() {
    let doc = ordered_collection(
        "https://e.com/users/a/following",
        &[json!("https://r.com/u/1")],
        None,
    );
    assert_eq!(doc["type"], "OrderedCollection");
    assert_eq!(doc["totalItems"], 1);
    assert_eq!(doc["orderedItems"][0], "https://r.com/u/1");
    assert_eq!(doc["@context"], *AP_CONTEXT);
}

#[test]
fn test_ordered_collection_index_and_page() {
    let idx = ordered_collection_index("https://e.com/u/a/outbox", 45, 20, None);
    assert_eq!(idx["first"], "https://e.com/u/a/outbox?page=1");
    assert_eq!(idx["last"], "https://e.com/u/a/outbox?page=3");
    let page = ordered_collection_page("https://e.com/u/a/outbox", 2, 45, &[json!("x")], 20, None);
    assert_eq!(page["type"], "OrderedCollectionPage");
    assert_eq!(page["prev"], "https://e.com/u/a/outbox?page=1");
    assert_eq!(page["next"], "https://e.com/u/a/outbox?page=3");
}

#[test]
fn test_activity_type_from_raw() {
    assert_eq!(ActivityType::from_raw("create"), Some(ActivityType::Create));
    assert_eq!(
        ActivityType::from_raw(" QUOTEREQUEST "),
        Some(ActivityType::QuoteRequest)
    );
    assert_eq!(ActivityType::from_raw("nope"), None);
}

#[test]
fn test_object_roundtrip() {
    let mut obj = Object::new("https://e.com/o/1");
    obj.content = "<p>hi</p>".into();
    obj.to = vec![AS_PUBLIC.to_string()];
    let doc = obj.to_json();
    assert_eq!(doc["type"], "Note");
    assert_eq!(doc["to"][0], AS_PUBLIC);
    let back = Object::from_json(&doc);
    assert_eq!(back.id, "https://e.com/o/1");
}

#[test]
fn test_signature_roundtrip() {
    let (priv_key, pub_key) = generate_rsa_keypair(2048).unwrap();
    let body = b"{\"hello\":\"world\"}";
    let headers = sign_request(
        &priv_key,
        "https://a.com/actor#main-key",
        "POST",
        "https://b.com/inbox",
        Some(body),
        &Default::default(),
    )
    .unwrap();
    assert!(headers.contains_key("Signature"));
    assert!(headers.contains_key("Digest"));
    assert!(verify_request(&pub_key, "POST", "/inbox", &headers, Some(body)).unwrap());
    // Tampered body must fail digest check
    assert!(verify_request(&pub_key, "POST", "/inbox", &headers, Some(b"bad")).is_err());
}

#[test]
fn test_key_pem_roundtrip() {
    let (priv_key, pub_key) = generate_rsa_keypair(2048).unwrap();
    let priv_pem = export_private_key_pem(&priv_key).unwrap();
    let pub_pem = export_public_key_pem(&pub_key).unwrap();
    assert!(priv_pem.contains("PRIVATE KEY"));
    assert!(pub_pem.contains("PUBLIC KEY"));
    let _ = load_private_key(&priv_pem).unwrap();
    let _ = load_public_key(&pub_pem).unwrap();
}

#[test]
fn test_key_pem_tolerates_envelope_noise() {
    let (priv_key, pub_key) = generate_rsa_keypair(2048).unwrap();
    let priv_pem = export_private_key_pem(&priv_key).unwrap();
    let pub_pem = export_public_key_pem(&pub_key).unwrap();
    // Akkoma-style `publicKeyPem` envelopes: trailing blank lines,
    // CRLF line endings, or text outside the encapsulation block.
    for variant in [
        format!("{pub_pem}\n"),
        pub_pem.replace('\n', "\r\n"),
        format!("preamble\n{pub_pem}\n\n"),
    ] {
        let parsed = load_public_key(&variant).expect("public key should parse");
        assert_eq!(export_public_key_pem(&parsed).unwrap(), pub_pem);
    }
    for variant in [format!("{priv_pem}\n"), priv_pem.replace('\n', "\r\n")] {
        let parsed = load_private_key(&variant).expect("private key should parse");
        assert_eq!(export_private_key_pem(&parsed).unwrap(), priv_pem);
    }
}

#[test]
fn test_moderation() {
    assert_eq!(normalize_domain("HTTPS://Example.COM:443/x"), "example.com");
    assert_eq!(extract_domain("https://a.b/users/alice"), "a.b");
    assert!(is_domain_blocked(
        "evil.com",
        None,
        Some(&["evil.com".into()])
    ));
    assert!(is_domain_blocked("x.com", Some(&["ok.com".into()]), None));
    assert!(!is_domain_blocked("ok.com", Some(&["ok.com".into()]), None));
}

#[test]
fn test_audience() {
    let doc = json!({"to": [AS_PUBLIC], "cc": "https://e.com/u/1", "bto": 42});
    assert!(is_public(&doc));
    let a = addressees(&doc);
    assert!(a.contains(AS_PUBLIC));
    assert!(a.contains("https://e.com/u/1"));
    let mentions = mentioned_actors(&json!({
        "tag": [{"type": "Mention", "href": "https://e.com/u/1"},
                {"type": "Hashtag", "name": "#x"}]
    }));
    assert_eq!(mentions, vec!["https://e.com/u/1"]);
}

#[test]
fn test_attribution() {
    assert!(validate_attribution(
        "https://a.com/u/1",
        &json!({
            "id": "https://a.com/o/1", "attributedTo": "https://a.com/u/1"
        })
    )
    .is_ok());
    assert!(validate_attribution(
        "https://a.com/u/1",
        &json!({
            "id": "https://b.com/o/1"
        })
    )
    .is_err());
    assert!(validate_attribution(
        "https://a.com/u/1",
        &json!({
            "attributedTo": "https://a.com/u/2"
        })
    )
    .is_err());
}

#[test]
fn test_content_rendering() {
    let rendered = render_post_html("Check https://example.com. And #Rust #lang!", |tag| {
        format!("https://e.com/tags/{tag}")
    });
    assert!(rendered
        .html
        .contains("<a href=\"https://example.com\">example.com</a>"));
    assert!(rendered.html.contains("rel=\"tag\""));
    assert_eq!(rendered.hashtags, vec!["rust", "lang"]);
    assert_eq!(format_duration(3723.0).unwrap(), "PT1H2M3S");
    assert!(format_duration(f64::NAN).is_err());
    assert_eq!(parse_duration(&json!("PT1H2M3S")), Some(3723.0));
    assert_eq!(parse_duration(&json!(63.9)), Some(63.9));
    assert_eq!(parse_duration(&json!("PT")), None);
    let link = render_verified_link("https://e.com/me", None);
    assert!(link.contains("rel=\"me\""));
}

#[test]
fn test_rate_limiter() {
    let rl = RateLimiter::new(2, std::time::Duration::from_secs(60));
    assert!(rl.check("1.2.3.4").is_ok());
    assert!(rl.check("1.2.3.4").is_ok());
    assert!(rl.check("1.2.3.4").is_err());
    assert!(rl.is_allowed("5.6.7.8"));
    rl.reset(Some("1.2.3.4"));
    assert!(rl.check("1.2.3.4").is_ok());
}

#[test]
fn test_quotes() {
    assert_eq!(
        extract_quote_target(&json!({"quoteUri": "https://e.com/o/1"})),
        Some("https://e.com/o/1".to_string())
    );
    let mut obj = json!({"id": "x"});
    set_quote_target(&mut obj, "https://e.com/o/1");
    assert_eq!(obj["quote"], "https://e.com/o/1");
    allow_public_quotes(&mut obj);
    assert!(obj["interactionPolicy"]["canQuote"]["automaticApproval"][0]
        .as_str()
        .unwrap()
        .contains("Public"));
}

#[test]
fn test_file_storage() {
    let dir = tempfile::tempdir().unwrap();
    let storage = FileActivityPubStorage::new(dir.path(), true).unwrap();
    assert_eq!(storage.schema_version(), 4);

    let follower = Follower {
        actor_id: "https://r.com/u/1".into(),
        inbox: "https://r.com/u/1/inbox".into(),
        shared_inbox: "https://r.com/inbox".into(),
        followed_at: Some(chrono::Utc::now()),
        actor_data: Default::default(),
        target_actor_id: "https://blog.example.com/ap/actor".into(),
    };
    storage.store_follower(&follower).unwrap();
    let followers = storage
        .get_followers(Some("https://blog.example.com/ap/actor"))
        .unwrap();
    assert_eq!(followers.len(), 1);
    assert_eq!(followers[0].inbox, "https://r.com/u/1/inbox");

    let mut interaction = Interaction::new(
        "https://r.com/u/1",
        "https://blog.example.com/posts/1",
        InteractionType::Like,
    );
    interaction.object_id = "https://r.com/status/9".into();
    storage.store_interaction(&interaction).unwrap();
    let found = storage
        .get_interaction_by_object_id("https://r.com/status/9", InteractionStatus::Confirmed)
        .unwrap();
    assert!(found.is_some());

    storage
        .store_activity("https://e.com/a/1", &json!({"type": "Create"}))
        .unwrap();
    let activities = storage.get_activities(10, 0).unwrap();
    assert_eq!(activities.len(), 1);

    storage
        .cache_remote_actor(
            "https://r.com/u/1",
            &json!({"id": "https://r.com/u/1"}),
            None,
        )
        .unwrap();
    let cached = storage
        .get_cached_actor("https://r.com/u/1", 86400.0)
        .unwrap();
    assert_eq!(cached.unwrap()["id"], "https://r.com/u/1");

    storage
        .remove_follower("https://r.com/u/1", "https://blog.example.com/ap/actor")
        .unwrap();
    assert!(storage.get_followers(None).unwrap().is_empty());
}

#[test]
fn test_sqlite_storage() {
    let storage = pubby_sdk::storage::db::SqliteActivityPubStorage::new(":memory:").unwrap();
    let follower = Follower {
        actor_id: "https://r.com/u/1".into(),
        inbox: "https://r.com/u/1/inbox".into(),
        shared_inbox: "".into(),
        followed_at: Some(chrono::Utc::now()),
        actor_data: Default::default(),
        target_actor_id: "https://blog.example.com/ap/actor".into(),
    };
    storage.store_follower(&follower).unwrap();
    assert_eq!(storage.get_followers(None).unwrap().len(), 1);
    let mut i = Interaction::new(
        "https://r.com/u/1",
        "https://e.com/p/1",
        InteractionType::Reply,
    );
    i.object_id = "https://r.com/s/1".into();
    i.mentioned_actors = vec!["https://blog.example.com/ap/actor".into()];
    storage.store_interaction(&i).unwrap();
    assert!(storage
        .get_interaction_by_object_id("https://r.com/s/1", InteractionStatus::Confirmed)
        .unwrap()
        .is_some());
    // exercise the interaction_mentions join (regression: renamed
    // `mentioned_actor_url` column).
    let mentioned = storage
        .get_interactions_mentioning(
            "https://blog.example.com/ap/actor",
            Some(InteractionType::Reply),
            InteractionStatus::Confirmed,
        )
        .unwrap();
    assert_eq!(mentioned.len(), 1);
    assert_eq!(mentioned[0].object_id, "https://r.com/s/1");
}

#[test]
fn test_document_cache() {
    let cache = DocumentCache::default();
    let calls = Arc::new(std::sync::atomic::AtomicUsize::new(0));
    let c2 = calls.clone();
    let v: Option<u32> = cache
        .get_or_render(
            vec!["a".into()],
            move || {
                c2.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                Ok(Some(42u32))
            },
            None,
            None,
        )
        .unwrap();
    assert_eq!(v, Some(42));
    // Second call is a cache hit
    let c2 = calls.clone();
    let v: Option<u32> = cache
        .get_or_render(
            vec!["a".into()],
            move || {
                c2.fetch_add(1, std::sync::atomic::Ordering::SeqCst);
                Ok(Some(7u32))
            },
            None,
            None,
        )
        .unwrap();
    assert_eq!(v, Some(42));
    assert_eq!(calls.load(std::sync::atomic::Ordering::SeqCst), 1);
}

#[test]
fn test_build_activities() {
    let like = build_like_activity(
        "https://a.com/ap/actor",
        "https://b.com/o/1",
        None,
        None,
        Some("https://a.com/a/1"),
        None,
        None,
    );
    assert_eq!(like["type"], "Like");
    assert_eq!(like["object"], "https://b.com/o/1");
    let del = build_delete_activity(
        "https://a.com/ap/actor",
        "https://a.com/o/9",
        None,
        None,
        None,
        None,
        None,
    );
    assert_eq!(del["object"]["type"], "Tombstone");
    assert_eq!(del["cc"][0], "https://a.com/ap/actor/followers");
    let undo_inner = json!({"id": "https://a.com/a/2", "type": "Like", "object": "x"});
    let undo = build_undo_activity(
        &undo_inner,
        "https://a.com/ap/actor",
        None,
        None,
        None,
        None,
        None,
    );
    assert_eq!(undo["type"], "Undo");
    let qa = build_quote_authorization(
        "https://a.com/ap/actor",
        "https://b.com/s/1",
        "https://a.com/o/1",
        None,
        None,
        None,
        None,
    );
    assert_eq!(qa["type"], "QuoteAuthorization");
    let followers = vec![Follower {
        actor_id: "a".into(),
        inbox: "https://r.com/u/1/inbox".into(),
        shared_inbox: "https://r.com/inbox".into(),
        followed_at: None,
        actor_data: Default::default(),
        target_actor_id: "".into(),
    }];
    assert_eq!(collect_inboxes(&followers), vec!["https://r.com/inbox"]);
}

#[test]
fn test_handler_actor_document_and_webfinger() {
    let dir = tempfile::tempdir().unwrap();
    let handler = make_handler(&dir);
    let doc = handler.get_actor_document();
    assert_eq!(doc["id"], "https://blog.example.com/ap/actor");
    assert_eq!(doc["preferredUsername"], "blog");
    assert!(doc["publicKey"]["publicKeyPem"]
        .as_str()
        .unwrap()
        .contains("PUBLIC KEY"));
    assert!(handler.is_valid_webfinger_resource(Some("acct:blog@blog.example.com")));
    assert!(handler.is_valid_webfinger_resource(Some("acct:@Blog@blog.example.com")));
    assert!(!handler.is_valid_webfinger_resource(Some("blog@blog.example.com")));
    assert!(!handler.is_valid_webfinger_resource(Some("acct:other@blog.example.com")));
    let wf = handler
        .get_webfinger_response(Some("acct:blog@blog.example.com"))
        .unwrap();
    assert_eq!(wf["subject"], "acct:blog@blog.example.com");
    assert!(handler
        .get_webfinger_response(Some("acct:other@blog.example.com"))
        .is_none());
    let ni = handler.get_nodeinfo_discovery();
    assert_eq!(
        ni["links"][0]["href"],
        "https://blog.example.com/nodeinfo/2.1"
    );
}

#[test]
fn test_actor_document_emits_config_tag() {
    let dir = tempfile::tempdir().unwrap();
    let mut cfg = make_config();
    cfg.tag = vec![
        json!({
            "type": "Hashtag",
            "name": "#rust",
            "href": "https://blog.example.com/tags/rust",
        }),
        json!({
            "type": "Emoji",
            "name": ":blobcat:",
            "icon": {"type": "Image", "mediaType": "image/png",
                     "url": "https://blog.example.com/emoji/blobcat.png"},
        }),
    ];
    let storage = Arc::new(FileActivityPubStorage::new(dir.path(), true).unwrap());
    let (priv_key, _) = generate_rsa_keypair(2048).unwrap();
    let h = ActivityPubHandler::new(storage, cfg, priv_key, HandlerOptions::default()).unwrap();
    let doc = h.get_actor_document();
    let tags = doc["tag"].as_array().unwrap();
    assert_eq!(tags.len(), 2);
    assert_eq!(tags[0]["type"], "Hashtag");
    assert_eq!(tags[1]["name"], ":blobcat:");
    // Round-trip: a parsed remote actor keeps its `tag` entries.
    let parsed = Actor::from_json(&doc);
    assert_eq!(parsed.tag.len(), 2);
}

#[test]
fn test_actor_document_omits_empty_tag() {
    let dir = tempfile::tempdir().unwrap();
    let handler = make_handler(&dir);
    let doc = handler.get_actor_document();
    assert!(doc.get("tag").is_none());
}

#[test]
fn test_dispatch_endpoints() {
    let dir = tempfile::tempdir().unwrap();
    let handler = make_handler(&dir);
    let opts = DispatchOptions::default();

    let req =
        |method: &str, path: &str, query: HashMap<String, String>, body: Vec<u8>| DispatchRequest {
            method: method.into(),
            path: path.into(),
            query,
            headers: HashMap::new(),
            body,
            remote_addr: "1.2.3.4".into(),
            request_target: None,
        };

    let resp = dispatch(
        &handler,
        &req("GET", "/ap/actor", HashMap::new(), vec![]),
        "/ap",
        &opts,
    );
    assert_eq!(resp.status, 200);
    let doc: Value = serde_json::from_slice(&resp.body).unwrap();
    assert_eq!(doc["preferredUsername"], "blog");

    let mut q = HashMap::new();
    q.insert("resource".into(), "acct:blog@blog.example.com".into());
    let resp = dispatch(
        &handler,
        &req("GET", "/.well-known/webfinger", q, vec![]),
        "/ap",
        &opts,
    );
    assert_eq!(resp.status, 200);
    let doc: Value = serde_json::from_slice(&resp.body).unwrap();
    assert_eq!(doc["subject"], "acct:blog@blog.example.com");

    // Bad JSON inbox body
    let resp = dispatch(
        &handler,
        &req("POST", "/ap/inbox", HashMap::new(), b"nope".to_vec()),
        "/ap",
        &opts,
    );
    assert_eq!(resp.status, 400);

    // Missing signature headers → 401
    let resp = dispatch(
        &handler,
        &req("POST", "/ap/inbox", HashMap::new(), b"{}".to_vec()),
        "/ap",
        &opts,
    );
    assert_eq!(resp.status, 401);

    // Followers collection
    let resp = dispatch(
        &handler,
        &req("GET", "/ap/followers", HashMap::new(), vec![]),
        "/ap",
        &opts,
    );
    assert_eq!(resp.status, 200);

    // Unknown path
    let resp = dispatch(
        &handler,
        &req("GET", "/nope", HashMap::new(), vec![]),
        "/ap",
        &opts,
    );
    assert_eq!(resp.status, 404);
}

#[test]
fn test_inbox_follow_skip_verification() {
    let dir = tempfile::tempdir().unwrap();
    let handler = make_handler(&dir);
    let follow = json!({
        "id": "https://r.com/f/1",
        "type": "Follow",
        "actor": "https://r.com/u/1",
        "object": "https://blog.example.com/ap/actor",
    });
    // Actor fetch fails (no network) → None result, no follower stored.
    let out = handler
        .process_inbox_activity(&follow, "POST", "/ap/inbox", None, None, true)
        .unwrap();
    assert!(out.is_none());
}

#[test]
fn test_outbox_publish_and_collection() {
    let dir = tempfile::tempdir().unwrap();
    let handler = make_handler(&dir);
    let mut obj = Object::new("https://blog.example.com/posts/1");
    obj.content = "Hello".into();
    obj.attributed_to = json!(handler.actor_id);
    obj.to = vec![AS_PUBLIC.to_string()];
    obj.cc = vec![handler.followers_url.clone()];
    let activity = handler.publish_object(&obj, None).unwrap();
    assert_eq!(activity["type"], "Create");
    let outbox = handler.get_outbox(None, None).unwrap();
    assert_eq!(outbox["totalItems"], 1);
}

#[test]
fn test_mastodon_api() {
    let dir = tempfile::tempdir().unwrap();
    let handler = make_handler(&dir);
    let api = pubby_sdk::server::mastodon::MastodonAPI::new(handler);
    let (body, status) = api.instance_v1();
    assert_eq!(status, 200);
    assert_eq!(body["uri"], "blog.example.com");
    let (body, status) = api.accounts_lookup(Some("blog@blog.example.com"));
    assert_eq!(status, 200);
    assert_eq!(body["username"], "blog");
    let (_, status) = api.accounts_lookup(Some("nope@elsewhere.com"));
    assert_eq!(status, 404);
    let (body, status) = api.accounts_get("1");
    assert_eq!(status, 200);
    assert_eq!(body["acct"], "blog@blog.example.com");
}

#[test]
fn test_renderer() {
    let mut i = Interaction::new(
        "https://r.com/users/alice",
        "https://blog.example.com/posts/1",
        InteractionType::Reply,
    );
    i.content = "<p>Nice post!</p><script>alert(1)</script>".into();
    i.author_name = "Alice".into();
    i.author_url = "https://r.com/users/alice".into();
    let renderer = pubby_sdk::render::InteractionsRenderer::new();
    let html = renderer.render_interaction(&i, None);
    assert!(html.contains("ap-interaction-reply"));
    assert!(html.contains("<p>Nice post!</p>"));
    assert!(!html.contains("script"));
    let list = renderer.render_interactions(&[i], None);
    assert!(list.contains("ap-interactions"));
}

// ---------- Review regression tests ----------

#[test]
fn test_handler_options_defaults_match_python() {
    let o = HandlerOptions::default();
    assert_eq!(o.http_timeout, 15.0);
    assert_eq!(o.max_retries, 3);
    assert_eq!(o.max_delivery_workers, 10);
    assert!(o.auto_approve_quotes);
    assert!(o.async_delivery);
    assert!(!o.store_local_only);
    assert_eq!(o.software_name, "pubby");
    assert!(!o.strict_attribution);
}

#[test]
fn test_safe_url_xss() {
    use pubby_sdk::render::template_utils::safe_url;
    assert_eq!(safe_url("https://e.com/x"), "https://e.com/x");
    assert_eq!(safe_url("javascript:alert(1)"), "");
    assert_eq!(safe_url("javascript&#58;alert(1)"), "");
    assert_eq!(safe_url("https://e.com/\" onclick=\"x"), "");
    assert_eq!(safe_url("https://e.com/<script>"), "");
    assert_eq!(safe_url("//evil.com/x"), "");
    assert_eq!(safe_url("data:text/html,<script>"), "");
}

#[test]
fn test_sanitize_html_href_xss() {
    use pubby_sdk::render::sanitize_html;
    // Entity-encoded scheme smuggling is rejected.
    let out = sanitize_html(r#"<a href="jav&#x61;script:alert(1)">x</a>"#);
    assert!(!out.contains("jav"), "got: {out}");
    let out = sanitize_html(r#"<a href="&#106;&#97;vascript:alert(1)">x</a>"#);
    assert!(!out.contains("href"), "got: {out}");
    let out = sanitize_html(r#"<a href="//evil.example.com">x</a>"#);
    assert!(!out.contains("href"), "got: {out}");
    // Event-handler attributes are still stripped.
    let out = sanitize_html(r#"<a href="https://e.com" onclick="alert(1)">x</a>"#);
    assert!(!out.contains("onclick"), "got: {out}");
    // Legitimate links survive.
    let out = sanitize_html(r#"<a href="https://e.com/p">x</a>"#);
    assert!(out.contains(r#"href="https://e.com/p""#), "got: {out}");
    let out = sanitize_html(r#"<a href="/relative">x</a>"#);
    assert!(out.contains(r#"href="/relative""#), "got: {out}");
}

#[test]
fn test_custom_template_autoescape() {
    let mut i = Interaction::new(
        "https://r.com/u/a",
        "https://blog.example.com/ap/actor",
        InteractionType::Mention,
    );
    i.author_name = "<img src=x onerror=alert(1)>".into();
    let renderer = pubby_sdk::render::InteractionsRenderer::new();
    let html = renderer.render_interaction(&i, Some("{{ interaction.author_name }}"));
    assert!(!html.contains("<img"), "got: {html}");
    assert!(html.contains("&lt;img"), "got: {html}");
    // safe_url output is auto-escaped inside attributes too.
    let mut i2 = i.clone();
    i2.author_url = "javascript:alert(1)".into();
    let html2 = renderer.render_interaction(
        &i2,
        Some(r#"<a href="{{ safe_url(interaction.author_url) }}">x</a>"#),
    );
    assert!(!html2.contains("javascript"), "got: {html2}");
}

#[test]
fn test_interaction_from_json_strict() {
    let data = json!({
        "source_actor_id": "a",
        "target_resource": "t",
        "interaction_type": "unknown-type",
        "status": "confirmed",
    });
    assert!(Interaction::from_json(&data).is_err());
    let data = json!({
        "source_actor_id": "a",
        "target_resource": "t",
        "interaction_type": "like",
        "status": "bogus",
    });
    assert!(Interaction::from_json(&data).is_err());
    let data = json!({
        "source_actor_id": "a",
        "target_resource": "t",
        "interaction_type": "like",
        "status": "confirmed",
    });
    assert!(Interaction::from_json(&data).is_ok());
}

#[test]
fn test_activity_embedded_actor() {
    let a = Activity::from_json(&json!({
        "type": "Delete",
        "actor": {"id": "https://r.com/users/alice", "type": "Person"},
        "object": "https://r.com/posts/1",
    }));
    assert_eq!(a.actor, "https://r.com/users/alice");
}

#[test]
fn test_attribution_scalar_bypass() {
    // Non-empty non-string/list/dict attributedTo must not slide.
    for v in [json!(false), json!(0), json!(0.0), json!(true), json!(1)] {
        let obj = json!({"attributedTo": v, "id": "https://a.com/o/1"});
        assert!(
            pubby_sdk::attribution::validate("https://a.com/u/1", &obj).is_err(),
            "attributedTo={v} should fail"
        );
    }
    // String match passes; mismatch fails.
    let obj = json!({"attributedTo": "https://a.com/u/1"});
    assert!(pubby_sdk::attribution::validate("https://a.com/u/1", &obj).is_ok());
    let obj = json!({"attributedTo": "https://evil.com/u/2"});
    assert!(pubby_sdk::attribution::validate("https://a.com/u/1", &obj).is_err());
    // Array containing the actor (as object) passes.
    let obj = json!({"attributedTo": [{"id": "https://a.com/u/1"}]});
    assert!(pubby_sdk::attribution::validate("https://a.com/u/1", &obj).is_ok());
}

#[test]
fn test_ssrf_outbound_url() {
    use pubby_sdk::http::is_safe_outbound_url;
    assert!(is_safe_outbound_url("https://example.com/ap/actor"));
    assert!(is_safe_outbound_url("https://mastodon.social/users/a"));
    for bad in [
        "http://localhost/x",
        "https://127.0.0.1/x",
        "http://169.254.169.254/latest/meta-data",
        "https://10.0.0.1/x",
        "https://192.168.1.1/x",
        "https://[::1]/x",
        "https://[fe80::1]/x",
        "https://metadata.google.internal/x",
        "https://printer.local/x",
        "ftp://example.com/x",
        "file:///etc/passwd",
        "not-a-url",
    ] {
        assert!(!is_safe_outbound_url(bad), "{bad} should be unsafe");
    }
}

#[test]
fn test_signature_digest_required() {
    let (priv_key, pub_key) = generate_rsa_keypair(2048).unwrap();
    let body = br#"{"type":"Follow"}"#;
    let url = "https://blog.example.com/ap/inbox";
    let key_id = "https://r.com/u/1#main-key";

    let no_digest = [
        "(request-target)".to_string(),
        "host".to_string(),
        "date".to_string(),
    ];
    fn opts<'a>(hdrs: &'a [String]) -> pubby_sdk::crypto::signatures::SignOptions<'a> {
        pubby_sdk::crypto::signatures::SignOptions {
            headers: HashMap::new(),
            signed_headers: Some(hdrs),
        }
    }

    // 1. Digest header entirely missing → reject.
    let headers = sign_request(
        &priv_key,
        key_id,
        "POST",
        url,
        Some(body),
        &opts(&no_digest),
    )
    .unwrap();
    assert!(verify_request(&pub_key, "POST", "/ap/inbox", &headers, Some(body)).is_err());

    // 2. Digest header present but not signed → reject.
    let mut headers = sign_request(
        &priv_key,
        key_id,
        "POST",
        url,
        Some(body),
        &opts(&no_digest),
    )
    .unwrap();
    headers.insert(
        "Digest".to_string(),
        pubby_sdk::crypto::signatures::build_digest(body),
    );
    assert!(verify_request(&pub_key, "POST", "/ap/inbox", &headers, Some(body)).is_err());

    // 3. Properly signed digest → ok.
    let all = [
        "(request-target)".to_string(),
        "host".to_string(),
        "date".to_string(),
        "digest".to_string(),
    ];
    let headers = sign_request(&priv_key, key_id, "POST", url, Some(body), &opts(&all)).unwrap();
    assert!(verify_request(&pub_key, "POST", "/ap/inbox", &headers, Some(body)).unwrap());
}

#[test]
fn test_signature_date_freshness() {
    let (priv_key, pub_key) = generate_rsa_keypair(2048).unwrap();
    let url = "https://blog.example.com/ap/inbox";
    let key_id = "https://r.com/u/1#main-key";
    let signed = [
        "(request-target)".to_string(),
        "host".to_string(),
        "date".to_string(),
    ];

    // Stale signed Date (over the 300s skew bound) → reject.
    let old = chrono::Utc::now() - chrono::Duration::hours(2);
    let mut custom = HashMap::new();
    custom.insert(
        "Date".to_string(),
        old.format("%a, %d %b %Y %H:%M:%S GMT").to_string(),
    );
    let headers = sign_request(
        &priv_key,
        key_id,
        "POST",
        url,
        None,
        &pubby_sdk::crypto::signatures::SignOptions {
            headers: custom,
            signed_headers: Some(&signed),
        },
    )
    .unwrap();
    assert!(verify_request(&pub_key, "POST", "/ap/inbox", &headers, None).is_err());

    // Fresh signed Date → ok.
    let headers = sign_request(
        &priv_key,
        key_id,
        "POST",
        url,
        None,
        &pubby_sdk::crypto::signatures::SignOptions {
            headers: HashMap::new(),
            signed_headers: Some(&signed),
        },
    )
    .unwrap();
    assert!(verify_request(&pub_key, "POST", "/ap/inbox", &headers, None).unwrap());
}

#[test]
fn test_parse_dt_negative_fraction() {
    let dt = pubby_sdk::model::parse_dt(&json!(-0.5)).unwrap();
    assert_eq!(
        pubby_sdk::model::dt_to_iso(&dt),
        "1969-12-31T23:59:59.500000+00:00"
    );
    let dt = pubby_sdk::model::parse_dt(&json!(-1.5)).unwrap();
    assert_eq!(
        pubby_sdk::model::dt_to_iso(&dt),
        "1969-12-31T23:59:58.500000+00:00"
    );
}

#[test]
fn test_normalize_domain() {
    use pubby_sdk::moderation::normalize_domain;
    assert_eq!(normalize_domain("https://[::1]:8080/x"), "::1");
    assert_eq!(normalize_domain("https://EXAMPLE.com/u"), "example.com");
    assert_eq!(
        normalize_domain("https://xn--e1afmkfd.xn--p1ai/u"),
        "пример.рф"
    );
}

#[test]
fn test_cache_single_flight() {
    use std::sync::atomic::{AtomicUsize, Ordering};
    let cache = Arc::new(pubby_sdk::cache::DocumentCache::default());
    let renders = Arc::new(AtomicUsize::new(0));
    let mut handles = Vec::new();
    for _ in 0..16 {
        let cache = cache.clone();
        let renders = renders.clone();
        handles.push(std::thread::spawn(move || {
            let key = vec!["k".to_string()];
            cache.get_or_render(
                key,
                || {
                    renders.fetch_add(1, Ordering::SeqCst);
                    std::thread::sleep(std::time::Duration::from_millis(30));
                    Ok(Some(json!({"v": 42})))
                },
                None,
                None,
            )
        }));
    }
    let results: Vec<_> = handles.into_iter().map(|h| h.join().unwrap()).collect();
    for r in results {
        assert_eq!(r.unwrap(), Some(json!({"v": 42})));
    }
    assert_eq!(renders.load(Ordering::SeqCst), 1);
}

#[test]
fn test_cache_invalidation_race() {
    let cache = pubby_sdk::cache::DocumentCache::default();
    let key = vec!["inv".to_string()];
    cache
        .get_or_render(key.clone(), || Ok(Some(json!({"v": 1}))), None, None)
        .unwrap();
    cache.invalidate_prefix(&[vec!["inv".to_string()]]);
    // After invalidation a second render must run (stale result dropped).
    let out = cache
        .get_or_render(key, || Ok(Some(json!({"v": 2}))), None, None)
        .unwrap();
    assert_eq!(out, Some(json!({"v": 2})));
}

#[test]
fn test_async_single_flight() {
    use std::sync::atomic::{AtomicUsize, Ordering};
    let cache = Arc::new(pubby_sdk::cache::DocumentCache::default());
    let renders = Arc::new(AtomicUsize::new(0));
    futures::executor::block_on(async {
        let key = vec!["async".to_string()];
        let c1 = cache.clone();
        let k1 = key.clone();
        let r1 = renders.clone();
        let f1 = c1.get_or_render_async::<serde_json::Value, _, _>(
            k1,
            move || async move {
                r1.fetch_add(1, Ordering::SeqCst);
                futures_timer::Delay::new(std::time::Duration::from_millis(20)).await;
                Ok(Some(json!({"a": 1})))
            },
            None,
            None,
        );
        let c2 = cache.clone();
        let f2 = c2.get_or_render_async::<serde_json::Value, _, _>(
            key,
            || async { Ok(None) },
            None,
            None,
        );
        let (o1, o2) = futures::join!(f1, f2);
        assert_eq!(o1.unwrap(), Some(json!({"a": 1})));
        // Waiter sees the owner's result, not its own None.
        assert_eq!(o2.unwrap(), Some(json!({"a": 1})));
    });
    assert_eq!(renders.load(Ordering::SeqCst), 1);
}

#[test]
fn test_mention_target_is_actor() {
    let dir = tempfile::tempdir().unwrap();
    let handler = make_handler(&dir);
    let actor_id = handler.actor_id.clone();
    let activity = json!({
        "id": "https://r.com/acts/1",
        "type": "Create",
        "actor": "https://r.com/users/alice",
        "to": [AS_PUBLIC],
        "object": {
            "id": "https://r.com/posts/1",
            "type": "Note",
            "content": "hello @blog",
            "to": [AS_PUBLIC],
            "tag": [{"type": "Mention", "href": actor_id}],
        },
    });
    handler
        .process_inbox_activity(&activity, "POST", "/ap/inbox", None, None, true)
        .unwrap();
    let interactions = handler
        .storage
        .get_interactions(
            &actor_id,
            Some(InteractionType::Mention),
            InteractionStatus::Confirmed,
        )
        .unwrap();
    assert_eq!(interactions.len(), 1);
    assert_eq!(interactions[0].target_resource, actor_id);
}

#[test]
fn test_follow_happy_path() {
    let dir = tempfile::tempdir().unwrap();
    let handler = make_handler(&dir);
    let actor_id = handler.actor_id.clone();
    // Seed the remote actor's cached document so no network is needed.
    handler
        .storage
        .cache_remote_actor(
            "https://r.com/users/alice",
            &json!({
                "id": "https://r.com/users/alice",
                "type": "Person",
                "preferredUsername": "alice",
                "inbox": "https://r.com/users/alice/inbox",
            }),
            None,
        )
        .unwrap();
    let activity = json!({
        "id": "https://r.com/acts/f1",
        "type": "Follow",
        "actor": "https://r.com/users/alice",
        "object": actor_id,
    });
    handler
        .process_inbox_activity(&activity, "POST", "/ap/inbox", None, None, true)
        .unwrap();
    let followers = handler.storage.get_followers(Some(&actor_id)).unwrap();
    assert_eq!(followers.len(), 1);
    assert_eq!(followers[0].actor_id, "https://r.com/users/alice");
}

#[test]
fn test_accept_reject_follow_request() {
    let dir = tempfile::tempdir().unwrap();
    let handler = make_handler(&dir);
    handler
        .storage
        .cache_remote_actor(
            "https://r.com/users/bob",
            &json!({
                "id": "https://r.com/users/bob",
                "inbox": "https://r.com/users/bob/inbox",
            }),
            None,
        )
        .unwrap();
    let options = pubby_sdk::follows::FollowResponseOptions {
        deliver: Some(Arc::new(|_: &str, _: &Value| {})),
        ..Default::default()
    };
    let request = |actor: &str| FollowRequest {
        actor_id: actor.into(),
        target_actor_id: handler.actor_id.clone(),
        inbox: format!("{actor}/inbox"),
        shared_inbox: String::new(),
        actor_data: serde_json::Map::new(),
        activity: json!({"type": "Follow", "actor": actor, "object": handler.actor_id})
            .as_object()
            .unwrap()
            .clone(),
        requested_at: Some(chrono::Utc::now()),
    };
    let bob = request("https://r.com/users/bob");
    handler.storage.store_follow_request(&bob).unwrap();
    pubby_sdk::follows::accept_follow_request(
        handler.storage.as_ref(),
        &bob,
        &handler.actor_id,
        &options,
        None,
    )
    .unwrap();
    assert_eq!(
        handler
            .storage
            .get_followers(Some(&handler.actor_id))
            .unwrap()
            .len(),
        1
    );

    let carol = request("https://r.com/users/carol");
    handler.storage.store_follow_request(&carol).unwrap();
    pubby_sdk::follows::reject_follow_request(
        handler.storage.as_ref(),
        &carol,
        &handler.actor_id,
        &options,
    )
    .unwrap();
    assert_eq!(
        handler
            .storage
            .get_followers(Some(&handler.actor_id))
            .unwrap()
            .len(),
        1
    );
}

#[test]
fn test_file_storage_migration() {
    use sha2::Digest as _;
    fn sanitize(v: &str) -> String {
        let h = format!("{:x}", sha2::Sha256::digest(v.as_bytes()));
        let safe: String = v
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
    let dir = tempfile::tempdir().unwrap();
    let data = dir.path().join("ap");
    let target = "https://blog.example.com/posts/1";
    let actor = "https://r.com/users/alice";
    std::fs::create_dir_all(data.join("followers")).unwrap();
    std::fs::create_dir_all(data.join("interactions").join(sanitize(target))).unwrap();
    // v1-style data: follower without target_actor_id, old-style reply filename.
    std::fs::write(
        data.join("followers").join("alice.json"),
        serde_json::to_string(&json!({
            "actor_id": "https://r.com/users/alice",
            "inbox": "https://r.com/inbox",
            "shared_inbox": "",
            "actor_data": {},
        }))
        .unwrap(),
    )
    .unwrap();
    std::fs::write(
        data.join("interactions")
            .join(sanitize(target))
            .join(format!("reply-{}.json", sanitize(actor))),
        serde_json::to_string(&json!({
            "source_actor_id": "https://r.com/users/alice",
            "target_resource": target,
            "interaction_type": "reply",
            "status": "confirmed",
            "object_id": "https://r.com/posts/9",
        }))
        .unwrap(),
    )
    .unwrap();
    std::fs::write(data.join(".schema_version"), "1").unwrap();

    let storage = FileActivityPubStorage::new(&data, true).unwrap();
    // Reply file renamed by object_id; version bumped to 4.
    // The old-style file was renamed to the object_id-keyed name.
    let target_dir = data.join("interactions").join(sanitize(target));
    let files: Vec<_> = std::fs::read_dir(&target_dir)
        .unwrap()
        .map(|e| e.unwrap().file_name().into_string().unwrap())
        .collect();
    let expected = format!("reply-{}.json", sanitize("https://r.com/posts/9"));
    assert!(files.contains(&expected), "files: {files:?}");
    assert_eq!(
        std::fs::read_to_string(data.join(".schema_version")).unwrap(),
        "4"
    );
    let followers = storage
        .get_followers(Some("https://blog.example.com/ap/actor"))
        .unwrap();
    assert_eq!(followers.len(), 1);
}

#[test]
fn test_file_storage_byte_compat() {
    let dir = tempfile::tempdir().unwrap();
    let data = dir.path().join("ap");
    let storage = FileActivityPubStorage::new(&data, true).unwrap();
    let mut interaction = Interaction::new(
        "https://r.com/u/å",
        "https://t.com/p/1",
        InteractionType::Like,
    );
    interaction.object_id = "https://r.com/o/1".into();
    storage.store_interaction(&interaction).unwrap();
    // ensure_ascii + indent=2 like Python's json.dumps.
    let files: Vec<_> = std::fs::read_dir(data.join("interactions"))
        .unwrap()
        .map(|e| e.unwrap().path())
        .collect();
    let mut hits = 0;
    for f in &files {
        if f.is_dir() {
            for entry in std::fs::read_dir(f).unwrap().flatten() {
                let text = std::fs::read_to_string(entry.path()).unwrap();
                assert!(!text.contains('å'), "must be ensure_ascii: {text}");
                assert!(text.contains("\\u00e5") || !text.chars().any(|c| c as u32 > 127));
                assert!(text.contains("{\n  "), "must be indent=2 pretty: {text}");
                hits += 1;
            }
        }
    }
    assert!(hits > 0);
}

#[test]
fn test_dispatch_extra_paths() {
    use pubby_sdk::server::{dispatch, DispatchOptions, DispatchRequest};
    let dir = tempfile::tempdir().unwrap();
    let handler = make_handler(&dir);
    let opts = DispatchOptions::default();
    let req = |method: &str, path: &str, query: HashMap<String, String>| DispatchRequest {
        method: method.into(),
        path: path.into(),
        query,
        headers: HashMap::new(),
        body: vec![],
        remote_addr: "1.2.3.4".into(),
        request_target: None,
    };
    // Empty webfinger resource → 400.
    let mut q = HashMap::new();
    q.insert("resource".to_string(), String::new());
    let resp = dispatch(
        &handler,
        &req("GET", "/.well-known/webfinger", q),
        "/ap",
        &opts,
    );
    assert_eq!(resp.status, 400);
    // NodeInfo aliases all 200.
    for p in [
        "/nodeinfo/2.1",
        "/nodeinfo/2.0",
        "/nodeinfo/2.0.json",
        "/nodeinfo/2.1.json",
    ] {
        let resp = dispatch(&handler, &req("GET", p, HashMap::new()), "/ap", &opts);
        assert_eq!(resp.status, 200, "{p}");
    }
}

#[test]
fn test_mastodon_mappers() {
    use pubby_sdk::server::mastodon;
    let dir = tempfile::tempdir().unwrap();
    let handler = make_handler(&dir);
    let account = mastodon::actor_to_account(&handler).unwrap();
    assert_eq!(account["acct"], "blog@blog.example.com");
    let activity = json!({
        "id": "https://blog.example.com/activities/1",
        "type": "Create",
        "object": {
            "id": "https://blog.example.com/posts/1",
            "type": "Note",
            "content": "<p>hi</p>",
        },
    });
    let status = mastodon::activity_to_status(&activity, &handler, None).unwrap();
    assert_eq!(status["content"], "<p>hi</p>");
    assert_eq!(status["account"]["username"], "blog");
    // MastodonAPI options override.
    let api = mastodon::MastodonAPI::with_options(
        handler.clone(),
        mastodon::MastodonAPIOptions {
            title: Some("Custom".into()),
            software_name: Some("myapp".into()),
            software_version: Some("9.9".into()),
            ..Default::default()
        },
    );
    let (info, status) = api.instance_v2();
    assert_eq!(status, 200);
    assert_eq!(info["title"], "Custom");
    assert!(info["version"].as_str().unwrap().contains("9.9"));
}

#[test]
fn test_file_storage_reads_python_fixture() {
    // `fixtures/python-filestorage` was produced by the Python
    // `FileActivityPubStorage` (schema v4) — verifies true
    // cross-implementation readability.
    let src = concat!(env!("CARGO_MANIFEST_DIR"), "/fixtures/python-filestorage");
    let dir = tempfile::tempdir().unwrap();
    let dst = dir.path().join("ap");
    // Copy the fixture so the Rust adapter can write/migrate freely.
    fn copy_dir(a: &std::path::Path, b: &std::path::Path) {
        std::fs::create_dir_all(b).unwrap();
        for e in std::fs::read_dir(a).unwrap().flatten() {
            let (fa, fb) = (e.path(), b.join(e.file_name()));
            if fa.is_dir() {
                copy_dir(&fa, &fb);
            } else {
                std::fs::copy(&fa, &fb).unwrap();
            }
        }
    }
    copy_dir(std::path::Path::new(src), &dst);

    let storage = FileActivityPubStorage::new(&dst, true).unwrap();
    assert_eq!(
        std::fs::read_to_string(dst.join(".schema_version")).unwrap(),
        "4"
    );

    let followers = storage
        .get_followers(Some("https://blog.example.com/ap/actor"))
        .unwrap();
    assert_eq!(followers.len(), 1);
    assert_eq!(followers[0].actor_id, "https://r.com/users/alice");

    let interactions = storage
        .get_interactions(
            "https://blog.example.com/posts/1",
            Some(InteractionType::Reply),
            InteractionStatus::Confirmed,
        )
        .unwrap();
    assert_eq!(interactions.len(), 1);
    assert_eq!(interactions[0].object_id, "https://r.com/posts/9");
    assert_eq!(interactions[0].content, "hello from Python");

    // Mention index written by Python reads back through Rust.
    let mentioned = storage
        .get_interactions_mentioning(
            "https://blog.example.com/ap/actor",
            None,
            InteractionStatus::Confirmed,
        )
        .unwrap();
    assert_eq!(mentioned.len(), 1);

    let activities = storage.get_activities(10, 0).unwrap();
    assert_eq!(activities.len(), 1);
    assert_eq!(activities[0]["type"], "Create");

    // Verify fixture compatibility regardless of when Python cached the actor.
    let actor = storage
        .get_cached_actor("https://r.com/users/alice", f64::INFINITY)
        .unwrap()
        .unwrap();
    assert_eq!(actor["id"], "https://r.com/users/alice");
}

// ---------- 0.5.1: per-actor endpoint paths, scoped outbox, following
// seam, WebFinger URL resources, InboxOptions ----------

#[test]
fn test_actor_config_endpoint_paths() {
    let cfg = ActorConfig::from_json(&json!({
        "base_url": "https://blog.example.com",
        "username": "alice",
        "actor_path": "/ap/users/alice",
        "inbox_path": "/ap/users/alice/inbox",
        "outbox_path": "/ap/users/alice/outbox",
        "followers_path": "/ap/users/alice/followers",
        "following_path": "/ap/users/alice/following",
    }))
    .unwrap();
    let dir = tempfile::tempdir().unwrap();
    let storage = Arc::new(FileActivityPubStorage::new(dir.path(), true).unwrap());
    let (priv_key, _) = generate_rsa_keypair(2048).unwrap();
    let h = ActivityPubHandler::new(storage, cfg, priv_key, HandlerOptions::default()).unwrap();

    assert_eq!(h.inbox_url, "https://blog.example.com/ap/users/alice/inbox");
    assert_eq!(
        h.outbox_url,
        "https://blog.example.com/ap/users/alice/outbox"
    );
    assert_eq!(
        h.followers_url,
        "https://blog.example.com/ap/users/alice/followers"
    );
    assert_eq!(
        h.following_url,
        "https://blog.example.com/ap/users/alice/following"
    );
    // The shared inbox always stays on the prefix route.
    assert_eq!(h.shared_inbox_url, "https://blog.example.com/ap/inbox");

    let doc = h.get_actor_document();
    assert_eq!(doc["inbox"], h.inbox_url);
    assert_eq!(doc["outbox"], h.outbox_url);
    assert_eq!(doc["followers"], h.followers_url);
    assert_eq!(doc["following"], h.following_url);
    assert_eq!(doc["endpoints"]["sharedInbox"], h.shared_inbox_url);
}

#[test]
fn test_actor_config_endpoint_paths_default_unchanged() {
    let dir = tempfile::tempdir().unwrap();
    let h = make_handler(&dir);
    assert_eq!(h.inbox_url, "https://blog.example.com/ap/inbox");
    assert_eq!(h.outbox_url, "https://blog.example.com/ap/outbox");
    assert_eq!(h.followers_url, "https://blog.example.com/ap/followers");
    assert_eq!(h.following_url, "https://blog.example.com/ap/following");
}

#[test]
fn test_outbox_is_actor_scoped() {
    let dir = tempfile::tempdir().unwrap();
    let h = make_handler(&dir);
    h.storage
        .store_activity(
            "https://blog.example.com/ap/actor/activities/1",
            &json!({
                "id": "https://blog.example.com/ap/actor/activities/1",
                "type": "Create",
                "actor": "https://blog.example.com/ap/actor",
            }),
        )
        .unwrap();
    h.storage
        .store_activity(
            "https://blog.example.com/ap/users/bob/activities/2",
            &json!({
                "id": "https://blog.example.com/ap/users/bob/activities/2",
                "type": "Create",
                "actor": "https://blog.example.com/ap/users/bob",
            }),
        )
        .unwrap();

    let outbox = h.get_outbox(None, None).unwrap();
    let items = outbox["orderedItems"].as_array().unwrap();
    assert_eq!(items.len(), 1);
    assert_eq!(
        items[0]["id"],
        "https://blog.example.com/ap/actor/activities/1"
    );
}

/// Storage wrapper that returns a populated `following` list — verifies
/// the handler renders the storage-provided items.
struct FollowingStorage {
    inner: FileActivityPubStorage,
    following: Vec<String>,
}

impl ActivityPubStorage for FollowingStorage {
    fn store_follower(&self, f: &Follower) -> Result<()> {
        self.inner.store_follower(f)
    }
    fn remove_follower(&self, actor_id: &str, target: &str) -> Result<()> {
        self.inner.remove_follower(actor_id, target)
    }
    fn get_followers(&self, actor_id: Option<&str>) -> Result<Vec<Follower>> {
        self.inner.get_followers(actor_id)
    }
    fn get_following(&self, _a: &str, _l: usize, _o: usize) -> Result<Vec<String>> {
        Ok(self.following.clone())
    }
    fn store_interaction(&self, i: &Interaction) -> Result<()> {
        self.inner.store_interaction(i)
    }
    fn delete_interaction(&self, a: &str, t: &str, ty: InteractionType) -> Result<()> {
        self.inner.delete_interaction(a, t, ty)
    }
    fn get_interactions(
        &self,
        t: &str,
        ty: Option<InteractionType>,
        s: InteractionStatus,
    ) -> Result<Vec<Interaction>> {
        self.inner.get_interactions(t, ty, s)
    }
    fn store_activity(&self, id: &str, data: &Value) -> Result<()> {
        self.inner.store_activity(id, data)
    }
    fn get_activities(&self, l: usize, o: usize) -> Result<Vec<Value>> {
        self.inner.get_activities(l, o)
    }
    fn cache_remote_actor(
        &self,
        id: &str,
        data: &Value,
        fetched: Option<chrono::DateTime<chrono::Utc>>,
    ) -> Result<()> {
        self.inner.cache_remote_actor(id, data, fetched)
    }
    fn get_cached_actor(&self, id: &str, age: f64) -> Result<Option<Value>> {
        self.inner.get_cached_actor(id, age)
    }
}

#[test]
fn test_following_collection() {
    let dir = tempfile::tempdir().unwrap();
    let h = make_handler(&dir);
    // Default storage: empty following collection (back-compat).
    let coll = h.get_following_collection().unwrap();
    assert_eq!(coll["type"], "OrderedCollection");
    assert_eq!(coll["totalItems"], 0);

    // Overridden storage: items flow into the collection.
    let storage = Arc::new(FollowingStorage {
        inner: FileActivityPubStorage::new(dir.path(), true).unwrap(),
        following: vec![
            "https://r.com/users/alice".to_string(),
            "https://r.com/users/bob".to_string(),
        ],
    });
    let (priv_key, _) = generate_rsa_keypair(2048).unwrap();
    let h2 = ActivityPubHandler::new(storage, make_config(), priv_key, HandlerOptions::default())
        .unwrap();
    let coll2 = h2.get_following_collection().unwrap();
    assert_eq!(coll2["totalItems"], 2);
    assert_eq!(coll2["orderedItems"][1], "https://r.com/users/bob");
}

#[test]
fn test_webfinger_url_resources() {
    let dir = tempfile::tempdir().unwrap();
    let h = make_handler(&dir);
    let actor_url = "https://blog.example.com/ap/actor";

    // acct: forms still work (unchanged).
    assert!(h.is_valid_webfinger_resource(Some("acct:blog@blog.example.com")));
    assert!(h.is_valid_webfinger_resource(Some("acct:@blog@blog.example.com")));

    // URL resource matching the actor document URL.
    assert!(h.is_valid_webfinger_resource(Some(actor_url)));
    let jrd = h.get_webfinger_response(Some(actor_url)).unwrap();
    assert_eq!(jrd["subject"], "acct:blog@blog.example.com");

    // Foreign URLs and unrelated local URLs are rejected.
    assert!(!h.is_valid_webfinger_resource(Some("https://other.example.com/ap/actor")));
    assert!(!h.is_valid_webfinger_resource(Some("https://blog.example.com/ap/users/bob")));
}

#[test]
fn test_multi_webfinger_url_resources() {
    let dir = tempfile::tempdir().unwrap();
    let instance = make_handler(&dir);
    let bob_storage = Arc::new(FileActivityPubStorage::new(dir.path(), true).unwrap());
    let (bob_key, _) = generate_rsa_keypair(2048).unwrap();
    let bob_cfg = ActorConfig::from_json(&json!({
        "base_url": "https://blog.example.com",
        "username": "bob",
        "actor_path": "/ap/users/bob",
    }))
    .unwrap();
    let bob = Arc::new(
        ActivityPubHandler::new(bob_storage, bob_cfg, bob_key, HandlerOptions::default()).unwrap(),
    );
    let bob_c = bob.clone();
    let multi = MultiActorHandler::new(
        instance,
        Arc::new(move |u: &str| {
            Ok(if u.eq_ignore_ascii_case("bob") {
                Some(bob_c.clone())
            } else {
                None
            })
        }),
        "/ap/users/{username}",
    );

    let jrd = multi
        .get_webfinger_response(Some("https://blog.example.com/ap/users/bob"))
        .unwrap();
    assert_eq!(jrd["subject"], "acct:bob@blog.example.com");

    // The instance actor's own URL resolves too.
    let jrd = multi
        .get_webfinger_response(Some("https://blog.example.com/ap/actor"))
        .unwrap();
    assert_eq!(jrd["subject"], "acct:blog@blog.example.com");

    // Unknown user / foreign URL / sub-path → None.
    assert!(multi
        .get_webfinger_response(Some("https://blog.example.com/ap/users/carol"))
        .is_none());
    assert!(multi
        .get_webfinger_response(Some("https://other.example.com/ap/users/bob"))
        .is_none());
    assert!(multi
        .get_webfinger_response(Some("https://blog.example.com/ap/users/bob/outbox"))
        .is_none());
}

#[test]
fn test_inbox_options_with_options() {
    use pubby_sdk::handlers::{InboxOptions, InboxProcessor};
    use std::time::Duration;

    let dir = tempfile::tempdir().unwrap();
    let storage = Arc::new(FileActivityPubStorage::new(dir.path(), true).unwrap());
    let (priv_key, _) = generate_rsa_keypair(2048).unwrap();

    let mut opts = InboxOptions::new(
        storage.clone(),
        "https://blog.example.com/ap/actor",
        priv_key.clone(),
        "https://blog.example.com/ap/actor#main-key",
    );
    opts.strict_attribution = true;
    opts.auto_approve_quotes = false;
    opts.blocked_instances = Some(vec!["bad.example".to_string()]);
    opts.http_client = pubby_sdk::http::HttpClient::new(Duration::from_secs(5));
    let proc = InboxProcessor::with_options(opts);
    assert_eq!(proc.actor_id, "https://blog.example.com/ap/actor");
    assert!(proc.strict_attribution);
    assert!(!proc.auto_approve_quotes);
    assert_eq!(
        proc.blocked_instances,
        Some(vec!["bad.example".to_string()])
    );
    // user_agent defaulted from the actor id.
    assert!(!proc.user_agent.is_empty());
}
