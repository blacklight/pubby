//! Thin synchronous HTTP helpers built on `ureq` — the Rust equivalent
//! of the `requests` calls used by the Python implementation.
//!
//! Non-2xx responses are not turned into errors at the transport level —
//! callers inspect the status code, matching the places where the Python
//! code calls `raise_for_status` explicitly or checks status ranges.
//!
//! Outbound safety:
//! - Redirects are followed manually (GET only, bounded hops) and every
//!   hop is re-validated by [`is_safe_outbound_url`], which rejects
//!   loopback/link-local/private/unspecified IP literals and common
//!   internal hostnames — an actor document or WebFinger response cannot
//!   bounce a request to `169.254.169.254` or `localhost`.
//! - Response bodies are capped at [`MAX_BODY_BYTES`].

use std::collections::HashMap;
use std::net::IpAddr;
use std::time::Duration;

use serde_json::Value;

use crate::error::{ActivityPubError, Result};

/// Maximum response body size accepted (16 MiB).
pub const MAX_BODY_BYTES: u64 = 16 * 1024 * 1024;

/// Maximum redirect hops followed for a GET request.
pub const MAX_REDIRECTS: u32 = 5;

/// A synchronous HTTP response (status + headers + body).
#[derive(Debug)]
pub struct HttpResponse {
    /// HTTP status code.
    pub status: u16,
    /// Response body bytes.
    pub body: Vec<u8>,
}

impl HttpResponse {
    /// Whether the status is in the 2xx range.
    pub fn is_success(&self) -> bool {
        (200..300).contains(&self.status)
    }

    /// Parse the body as JSON.
    pub fn json(&self) -> Result<Value> {
        Ok(serde_json::from_slice(&self.body)?)
    }

    /// The body as UTF-8 text.
    pub fn text(&self) -> String {
        String::from_utf8_lossy(&self.body).to_string()
    }
}

fn ip_is_public(ip: &IpAddr) -> bool {
    match ip {
        IpAddr::V4(v4) => {
            !(v4.is_private()
                || v4.is_loopback()
                || v4.is_link_local()
                || v4.is_broadcast()
                || v4.is_documentation()
                || v4.is_unspecified()
                // Shared address space (CGNAT) and other reserved ranges.
                || v4.octets()[0] == 0
                || (v4.octets()[0] == 100 && (v4.octets()[1] & 0xc0) == 64)
                || v4.octets()[0] >= 224)
        }
        IpAddr::V6(v6) => {
            !(v6.is_loopback()
                || v6.is_unspecified()
                || v6.is_multicast()
                || (v6.segments()[0] & 0xffc0) == 0xfe80 // link-local fe80::/10
                || (v6.segments()[0] & 0xfe00) == 0xfc00) // unique-local fc00::/7
        }
    }
}

/// Whether `url` is a safe outbound federation target.
///
/// Requires an `http`/`https` scheme and rejects literal hosts that are
/// loopback, link-local, private, CGNAT, multicast, unspecified, or
/// well-known internal hostnames (`localhost`, `*.local`, `*.internal`,
/// `*.localhost`, the cloud metadata address). DNS-level rebinding is
/// out of scope — see the crate docs.
pub fn is_safe_outbound_url(url: &str) -> bool {
    let Ok(parsed) = url::Url::parse(url) else {
        return false;
    };
    if !matches!(parsed.scheme().to_lowercase().as_str(), "http" | "https") {
        return false;
    }
    let Some(host) = parsed.host() else {
        return false;
    };
    match host {
        url::Host::Ipv4(v4) => ip_is_public(&IpAddr::V4(v4)),
        url::Host::Ipv6(v6) => ip_is_public(&IpAddr::V6(v6)),
        url::Host::Domain(domain) => {
            let d = domain.to_lowercase();
            !(d == "localhost"
                || d.ends_with(".localhost")
                || d.ends_with(".local")
                || d.ends_with(".internal")
                || d.ends_with(".lan")
                || d == "metadata.google.internal")
        }
    }
}

/// A synchronous HTTP client wrapping a `ureq` agent.
#[derive(Debug, Clone)]
pub struct HttpClient {
    agent: ureq::Agent,
    timeout: Duration,
}

impl Default for HttpClient {
    fn default() -> Self {
        HttpClient::new(Duration::from_secs(15))
    }
}

impl HttpClient {
    /// Create a client with the given per-request timeout.
    ///
    /// Automatic redirects are disabled; GET requests follow redirects
    /// manually with per-hop [`is_safe_outbound_url`] validation.
    pub fn new(timeout: Duration) -> Self {
        let config = ureq::Agent::config_builder()
            .timeout_global(Some(timeout))
            .http_status_as_error(false)
            .max_redirects(0)
            .build();
        HttpClient {
            agent: config.into(),
            timeout,
        }
    }

    /// The configured per-request timeout.
    pub fn timeout(&self) -> Duration {
        self.timeout
    }

    /// Perform a GET request, following validated redirects.
    ///
    /// The initial target is screened by [`is_safe_outbound_url`]
    /// exactly like every redirect hop.
    pub fn get(&self, url: &str, headers: &HashMap<String, String>) -> Result<HttpResponse> {
        let mut url = url.to_string();
        if !is_safe_outbound_url(&url) {
            return Err(ActivityPubError::General(format!(
                "refusing unsafe outbound URL: {url}"
            )));
        }
        for hop in 0..=MAX_REDIRECTS {
            let mut req = self.agent.get(&url);
            for (k, v) in headers {
                req = req.header(k, v);
            }
            let resp = req.call()?;
            let status = resp.status().as_u16();
            if (300..400).contains(&status) && hop < MAX_REDIRECTS {
                let location = resp
                    .headers()
                    .get("location")
                    .and_then(|v| v.to_str().ok())
                    .map(|s| s.to_string());
                if let Some(loc) = location {
                    let next = url::Url::parse(&url)
                        .ok()
                        .and_then(|base| base.join(&loc).ok())
                        .map(|u| u.to_string())
                        .unwrap_or(loc);
                    if !is_safe_outbound_url(&next) {
                        log::warn!("Refusing redirect to unsafe URL: {next}");
                        return read_response(resp);
                    }
                    url = next;
                    continue;
                }
            }
            return read_response(resp);
        }
        unreachable!("loop is bounded")
    }

    /// Perform a POST request with a body.
    ///
    /// Redirects are *not* followed for POSTs — re-posting a signed body
    /// to a different authority is unsafe; the 3xx status is returned to
    /// the caller.
    pub fn post(
        &self,
        url: &str,
        headers: &HashMap<String, String>,
        body: &[u8],
    ) -> Result<HttpResponse> {
        if !is_safe_outbound_url(url) {
            return Err(ActivityPubError::General(format!(
                "refusing unsafe outbound URL: {url}"
            )));
        }
        let mut req = self.agent.post(url);
        for (k, v) in headers {
            req = req.header(k, v);
        }
        let resp = req.send(body)?;
        read_response(resp)
    }
}

fn read_response(resp: ureq::http::Response<ureq::Body>) -> Result<HttpResponse> {
    let status = resp.status().as_u16();
    let mut resp = resp;
    let body = resp
        .body_mut()
        .with_config()
        .limit(MAX_BODY_BYTES)
        .read_to_vec()
        .map_err(|e| ActivityPubError::General(format!("failed to read response body: {e}")))?;
    Ok(HttpResponse { status, body })
}
