//! WebFinger client utilities for resolving ActivityPub actor URLs.
//! Mirrors `pubby.webfinger`.

use std::collections::HashSet;
use std::sync::LazyLock;
use std::time::Duration;

use regex::Regex;
use serde_json::Value;

use crate::http::HttpClient;

/// Matches `@user@domain` handles in free text (candidate matches — the
/// `(?<!\w)` lookbehind is applied separately in [`mention_start_ok`]).
static MENTION_RE: LazyLock<Regex> =
    LazyLock::new(|| Regex::new(r"@([a-zA-Z0-9_.-]+)@([a-zA-Z0-9.-]+\.[a-zA-Z]{2,})").unwrap());

/// A resolved `@user@domain` mention.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Mention {
    /// The username part of the handle.
    pub username: String,
    /// The domain part of the handle.
    pub domain: String,
    /// The resolved ActivityPub actor URL.
    pub actor_url: String,
}

impl Mention {
    /// The canonical `@user@domain` form.
    pub fn acct(&self) -> String {
        format!("@{}@{}", self.username, self.domain)
    }

    /// Return an ActivityPub `Mention` tag dict.
    pub fn to_tag(&self) -> Value {
        serde_json::json!({
            "type": "Mention",
            "href": self.actor_url,
            "name": self.acct(),
        })
    }
}

fn mention_start_ok(text: &str, start: usize) -> bool {
    match text[..start].chars().last() {
        None => true,
        Some(c) => !(c.is_alphanumeric() || c == '_'),
    }
}

/// Resolve the ActivityPub actor URL for `@username@domain` via WebFinger.
///
/// Falls back to `https://{domain}/@{username}` when the lookup fails or
/// no `self` link is present.
pub fn resolve_actor_url(username: &str, domain: &str, timeout: Option<Duration>) -> String {
    resolve_actor_url_with_client(
        username,
        domain,
        &HttpClient::new(timeout.unwrap_or(Duration::from_secs(10))),
    )
}

/// [`resolve_actor_url`] variant taking an explicit HTTP client (useful
/// for tests and shared agents).
pub fn resolve_actor_url_with_client(username: &str, domain: &str, client: &HttpClient) -> String {
    let fallback = format!("https://{domain}/@{username}");
    let url = format!("https://{domain}/.well-known/webfinger?resource=acct:{username}@{domain}");

    // `domain` is caller/user-influenced — refuse loopback/private/literal-IP
    // targets up front (the client's initial-hop check covers this too).
    if !crate::http::is_safe_outbound_url(&url) {
        log::warn!("Refusing WebFinger lookup on unsafe domain: {domain}");
        return fallback;
    }

    let headers: std::collections::HashMap<String, String> =
        [("Accept".to_string(), "application/jrd+json".to_string())]
            .into_iter()
            .collect();

    match client.get(&url, &headers) {
        Ok(resp) if resp.is_success() => {
            if let Ok(data) = resp.json() {
                if let Some(links) = data.get("links").and_then(|v| v.as_array()) {
                    for link in links {
                        let is_self = link.get("rel").and_then(|v| v.as_str()) == Some("self");
                        let is_ap = link
                            .get("type")
                            .and_then(|v| v.as_str())
                            .map(|t| t.starts_with("application/"))
                            .unwrap_or(false);
                        if is_self && is_ap {
                            if let Some(href) = link.get("href").and_then(|v| v.as_str()) {
                                return href.to_string();
                            }
                        }
                    }
                }
            }
            fallback
        }
        _ => {
            log::warn!("WebFinger lookup failed for @{username}@{domain}, using fallback");
            fallback
        }
    }
}

/// Find all `@user@domain` patterns in `text` and resolve each via
/// WebFinger.
pub fn extract_mentions(text: &str, timeout: Option<Duration>) -> Vec<Mention> {
    extract_mentions_with_client(
        text,
        &HttpClient::new(timeout.unwrap_or(Duration::from_secs(10))),
    )
}

/// [`extract_mentions`] variant taking an explicit HTTP client.
pub fn extract_mentions_with_client(text: &str, client: &HttpClient) -> Vec<Mention> {
    let mut seen: HashSet<(String, String)> = HashSet::new();
    let mut mentions = Vec::new();
    for m in MENTION_RE.find_iter(text) {
        if !mention_start_ok(text, m.start()) {
            continue;
        }
        let caps = MENTION_RE.captures(m.as_str()).unwrap();
        let username = caps[1].to_string();
        let domain = caps[2].to_string();
        let key = (username.to_lowercase(), domain.to_lowercase());
        if !seen.insert(key) {
            continue;
        }
        let actor_url = resolve_actor_url_with_client(&username, &domain, client);
        mentions.push(Mention {
            username,
            domain,
            actor_url,
        });
    }
    mentions
}
