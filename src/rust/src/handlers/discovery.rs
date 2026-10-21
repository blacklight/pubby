//! WebFinger and NodeInfo response builders — mirrors
//! `pubby.handlers._discovery`.

use serde_json::{json, Value};

/// Build a WebFinger JRD response (RFC 7033).
pub fn build_webfinger_response(username: &str, domain: &str, actor_url: &str) -> Value {
    json!({
        "subject": format!("acct:{username}@{domain}"),
        "aliases": [actor_url],
        "links": [
            {
                "rel": "self",
                "type": "application/activity+json",
                "href": actor_url,
            },
            {
                "rel": "http://webfinger.net/rel/profile-page",
                "type": "text/html",
                "href": actor_url,
            },
        ],
    })
}

/// Build the NodeInfo well-known discovery document.
pub fn build_nodeinfo_discovery(base_url: &str) -> Value {
    json!({
        "links": [
            {
                "rel": "http://nodeinfo.diaspora.software/ns/schema/2.1",
                "href": format!("{}/nodeinfo/2.1", base_url.trim_end_matches('/')),
            }
        ]
    })
}

/// Usage statistics reported in the NodeInfo document.
#[derive(Debug, Clone, Copy, Default)]
pub struct NodeInfoStats {
    /// Total local users.
    pub users: u64,
    /// Total local posts/activities.
    pub posts: u64,
    /// Whether new registrations are accepted.
    pub open_registrations: bool,
}

/// Callback producing [`NodeInfoStats`] for the NodeInfo document.
pub type NodeInfoStatsFn = std::sync::Arc<dyn Fn() -> NodeInfoStats + Send + Sync>;

/// Build the `/.well-known/host-meta` XRD document advertising the
/// WebFinger `lrdd` template (XML serialization).
pub fn build_host_meta_xrd(base_url: &str) -> String {
    let base = base_url.trim_end_matches('/');
    format!(
        concat!(
            r#"<?xml version="1.0" encoding="UTF-8"?>"#,
            "\n",
            r#"<XRD xmlns="http://docs.oasis-open.org/ns/xri/xrd-1.0">"#,
            "\n",
            r#"  <Link rel="lrdd" type="application/jrd+json" "#,
            r#"template="{base}/.well-known/webfinger?resource={{uri}}"/>"#,
            "\n",
            "</XRD>\n",
        ),
        base = base,
    )
}

/// Build the `/.well-known/host-meta` document as a JRD (JSON) value —
/// same `lrdd` template, for clients that prefer JSON.
pub fn build_host_meta(base_url: &str) -> Value {
    let base = base_url.trim_end_matches('/');
    json!({
        "links": [
            {
                "rel": "lrdd",
                "type": "application/jrd+json",
                "template": format!("{base}/.well-known/webfinger?resource={{uri}}"),
            }
        ]
    })
}

/// Build the NodeInfo 2.1 document.
pub fn build_nodeinfo_document(
    software_name: &str,
    software_version: Option<&str>,
    total_users: u64,
    total_posts: u64,
    open_registrations: bool,
) -> Value {
    let software_version = software_version.unwrap_or(crate::VERSION);
    json!({
        "version": "2.1",
        "software": {
            "name": software_name,
            "version": software_version,
        },
        "protocols": ["activitypub"],
        "usage": {
            "users": {
                "total": total_users,
                "activeMonth": total_users,
                "activeHalfyear": total_users,
            },
            "localPosts": total_posts,
        },
        "openRegistrations": open_registrations,
        "services": {
            "inbound": [],
            "outbound": [],
        },
        "metadata": {},
    })
}
