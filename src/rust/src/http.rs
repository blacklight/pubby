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
//! - Response bodies are capped at [`MAX_BODY_BYTES`] unless the client
//!   overrides the cap via [`HttpClient::max_body_bytes`].

use std::collections::HashMap;
use std::net::IpAddr;
use std::time::Duration;

use serde_json::Value;

use crate::error::{ActivityPubError, Result};

/// Default maximum response body size accepted (16 MiB).
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
    /// Allow loopback/private targets — development and tests only.
    allow_private: bool,
    /// Maximum response body size accepted.
    max_body_bytes: u64,
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
            allow_private: false,
            max_body_bytes: MAX_BODY_BYTES,
        }
    }

    /// Create a client that also accepts loopback/private targets.
    ///
    /// For development and tests only — a public URL must never be
    /// redirected into a private one, so per-hop validation still
    /// applies to redirects when the original target was public.
    pub fn new_relaxed(timeout: Duration) -> Self {
        let mut client = Self::new(timeout);
        client.allow_private = true;
        client
    }

    /// Allow (or disallow) loopback/private targets on this client.
    ///
    /// For development and tests only.
    pub fn allow_private_targets(&mut self, allow: bool) -> &mut Self {
        self.allow_private = allow;
        self
    }

    /// Override the response body cap for this client (default:
    /// [`MAX_BODY_BYTES`]). Raising it is appropriate only for endpoints
    /// known to serve bulk media; federation documents stay small.
    pub fn max_body_bytes(&mut self, limit: u64) -> &mut Self {
        self.max_body_bytes = limit;
        self
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
        let initial_safe = is_safe_outbound_url(&url);
        if !initial_safe && !self.allow_private {
            return Err(ActivityPubError::General(format!(
                "refusing unsafe outbound URL: {url}"
            )));
        }
        // Relaxed mode only clears the *initial* check when the target is
        // private; a public target still gets per-hop validation so it can
        // never be bounced to an internal address.
        let check_hops = !self.allow_private || initial_safe;
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
                    if check_hops && !is_safe_outbound_url(&next) {
                        log::warn!("Refusing redirect to unsafe URL: {next}");
                        return read_response(resp, self.max_body_bytes);
                    }
                    url = next;
                    continue;
                }
            }
            return read_response(resp, self.max_body_bytes);
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
        if !is_safe_outbound_url(url) && !self.allow_private {
            return Err(ActivityPubError::General(format!(
                "refusing unsafe outbound URL: {url}"
            )));
        }
        let mut req = self.agent.post(url);
        for (k, v) in headers {
            req = req.header(k, v);
        }
        let resp = req.send(body)?;
        read_response(resp, self.max_body_bytes)
    }
}

fn read_response(resp: ureq::http::Response<ureq::Body>, limit: u64) -> Result<HttpResponse> {
    let status = resp.status().as_u16();
    let mut resp = resp;
    let body = resp
        .body_mut()
        .with_config()
        .limit(limit)
        .read_to_vec()
        .map_err(|e| ActivityPubError::General(format!("failed to read response body: {e}")))?;
    Ok(HttpResponse { status, body })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::thread::JoinHandle;

    /// Spawn a one-shot loopback origin that replies with the given raw
    /// response bytes once the request headers have been read. Returns
    /// the URL to request and the server thread handle.
    fn serve_once(response: Vec<u8>) -> (String, JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        let handle = std::thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            let mut received = Vec::new();
            let mut buf = [0u8; 4096];
            // Consume the request headers before writing the response.
            while !received.windows(4).any(|w| w == b"\r\n\r\n") {
                match stream.read(&mut buf) {
                    Ok(0) | Err(_) => return,
                    Ok(n) => received.extend_from_slice(&buf[..n]),
                }
            }
            // An over-limit body is abandoned by the client, so a
            // late write can hit a closed socket — not a test failure.
            let _ = stream.write_all(&response);
            let _ = stream.flush();
        });
        (format!("http://127.0.0.1:{port}/"), handle)
    }

    fn fixed_length_response(body: &[u8]) -> Vec<u8> {
        let head = format!(
            "HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\nContent-Length: {}\r\n\r\n",
            body.len()
        );
        let mut response = head.into_bytes();
        response.extend_from_slice(body);
        response
    }

    fn chunked_response(body: &[u8]) -> Vec<u8> {
        let mut response =
            b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\nTransfer-Encoding: chunked\r\n\r\n"
                .to_vec();
        for chunk in body.chunks(1024 * 1024) {
            response.extend_from_slice(format!("{:x}\r\n", chunk.len()).as_bytes());
            response.extend_from_slice(chunk);
            response.extend_from_slice(b"\r\n");
        }
        response.extend_from_slice(b"0\r\n\r\n");
        response
    }

    fn relaxed_client() -> HttpClient {
        let mut client = HttpClient::new(Duration::from_secs(30));
        client.allow_private_targets(true);
        client
    }

    fn get(client: &HttpClient, response: Vec<u8>) -> (Result<HttpResponse>, JoinHandle<()>) {
        let (url, handle) = serve_once(response);
        (client.get(&url, &HashMap::new()), handle)
    }

    #[test]
    fn body_within_default_cap_succeeds() {
        let body = vec![42u8; 4096];
        let (result, handle) = get(&relaxed_client(), fixed_length_response(&body));
        handle.join().unwrap();
        let resp = result.unwrap();
        assert!(resp.is_success());
        assert_eq!(resp.body, body);
    }

    #[test]
    fn default_cap_rejects_oversized_content_length() {
        let body = vec![1u8; MAX_BODY_BYTES as usize + 1];
        let (result, handle) = get(&relaxed_client(), fixed_length_response(&body));
        handle.join().unwrap();
        let err = result.unwrap_err().to_string();
        assert!(
            err.contains("larger than request limit"),
            "unexpected error: {err}"
        );
    }

    #[test]
    fn default_cap_rejects_oversized_chunked() {
        let body = vec![1u8; MAX_BODY_BYTES as usize + 1];
        let (result, handle) = get(&relaxed_client(), chunked_response(&body));
        handle.join().unwrap();
        let err = result.unwrap_err().to_string();
        assert!(
            err.contains("larger than request limit"),
            "unexpected error: {err}"
        );
    }

    #[test]
    fn raised_cap_accepts_large_body() {
        let mut client = relaxed_client();
        client.max_body_bytes(64 * 1024 * 1024);
        let body = vec![7u8; MAX_BODY_BYTES as usize + 1];
        let (result, handle) = get(&client, fixed_length_response(&body));
        handle.join().unwrap();
        assert_eq!(result.unwrap().body, body);
    }

    #[test]
    fn lowered_cap_rejects_small_body() {
        let mut client = relaxed_client();
        client.max_body_bytes(8);
        let (result, handle) = get(&client, fixed_length_response(&vec![0u8; 64]));
        handle.join().unwrap();
        let err = result.unwrap_err().to_string();
        assert!(
            err.contains("larger than request limit"),
            "unexpected error: {err}"
        );
    }
}
