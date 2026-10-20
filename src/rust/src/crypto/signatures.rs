//! HTTP Signatures implementation (draft-cavage-http-signatures-12) —
//! mirrors `pubby.crypto._signatures`.
//!
//! Signs and verifies HTTP requests using RSA-SHA256. Self-contained:
//! depends only on `rsa`, `sha2` and `base64`.

use std::collections::HashMap;

use base64::Engine;
use chrono::Utc;
use rsa::pkcs1v15::{SigningKey, VerifyingKey};
use rsa::signature::{SignatureEncoding, Signer, Verifier};
use rsa::{RsaPrivateKey, RsaPublicKey};
use sha2::{Digest, Sha256};

use crate::error::{ActivityPubError, Result};

/// Headers that must be signed for outgoing requests.
pub const DEFAULT_SIGNED_HEADERS: &[&str] = &["(request-target)", "host", "date", "digest"];

/// Maximum accepted clock skew (seconds) between the signer and this
/// server when a request's `date` header is signed. Requests with a
/// signed `date` outside this window are rejected as replays.
pub const MAX_SIGNED_DATE_SKEW_SECS: i64 = 300;

/// Build the SHA-256 `Digest` header value of a request body.
///
/// :return: The `Digest` header value (`SHA-256=<base64>`).
pub fn build_digest(body: &[u8]) -> String {
    let sha256 = Sha256::digest(body);
    format!(
        "SHA-256={}",
        base64::engine::general_purpose::STANDARD.encode(sha256)
    )
}

/// Build the signing string per draft-cavage-http-signatures-12.
///
/// :param method: HTTP method (lowercase).
/// :param path: Request path including query string.
/// :param headers: Header name → value map (case-insensitive keys).
/// :param signed_headers: Header names to include in the signature.
pub fn build_signing_string(
    method: &str,
    path: &str,
    headers: &HashMap<String, String>,
    signed_headers: &[String],
) -> String {
    let lower: HashMap<String, String> = headers
        .iter()
        .map(|(k, v)| (k.to_lowercase(), v.clone()))
        .collect();
    let mut parts = Vec::new();
    for h in signed_headers {
        let h_lower = h.to_lowercase();
        if h_lower == "(request-target)" {
            parts.push(format!(
                "(request-target): {} {}",
                method.to_lowercase(),
                path
            ));
        } else {
            let value = lower.get(&h_lower).cloned().unwrap_or_default();
            parts.push(format!("{h_lower}: {value}"));
        }
    }
    parts.join("\n")
}

fn split_url(url: &str) -> (String, String, String) {
    // Returns (path_with_query, host, host_header)
    let parsed = match url::Url::parse(url) {
        Ok(u) => u,
        Err(_) => {
            return (url.to_string(), String::new(), String::new());
        }
    };
    let mut path = parsed.path().to_string();
    if path.is_empty() {
        path = "/".to_string();
    }
    if let Some(q) = parsed.query() {
        path = format!("{path}?{q}");
    }
    let host = parsed.host_str().unwrap_or_default().to_string();
    let host_header = match parsed.port() {
        Some(p) if p != 80 && p != 443 => format!("{host}:{p}"),
        _ => host.clone(),
    };
    (path, host, host_header)
}

/// Options accepted by [`sign_request`].
#[derive(Debug, Default)]
pub struct SignOptions<'a> {
    /// Existing headers to include in signing. `Date`/`Host`/`Digest` are
    /// generated when not provided.
    pub headers: HashMap<String, String>,
    /// Header names to sign (defaults to [`DEFAULT_SIGNED_HEADERS`]).
    pub signed_headers: Option<&'a [String]>,
}

/// Sign an outgoing HTTP request and return the headers to include.
///
/// Returns a map with `Date`, `Digest` (if a body is present), `Host` and
/// `Signature` headers — plus any headers passed via `options`.
pub fn sign_request(
    private_key: &RsaPrivateKey,
    key_id: &str,
    method: &str,
    url: &str,
    body: Option<&[u8]>,
    options: &SignOptions,
) -> Result<HashMap<String, String>> {
    let mut signed_headers: Vec<String> = match options.signed_headers {
        Some(h) => h.to_vec(),
        None => DEFAULT_SIGNED_HEADERS
            .iter()
            .map(|s| s.to_string())
            .collect(),
    };

    // Remove digest from signed headers if there's no body
    if body.map(|b| b.is_empty()).unwrap_or(true) {
        signed_headers.retain(|h| !h.eq_ignore_ascii_case("digest"));
    }

    let (path, _host, host_header) = split_url(url);

    let mut out_headers: HashMap<String, String> = options.headers.clone();
    fn has_header(headers: &HashMap<String, String>, h: &str) -> bool {
        headers.keys().any(|k| k.eq_ignore_ascii_case(h))
    }

    if !has_header(&out_headers, "Host") {
        out_headers.insert("Host".to_string(), host_header);
    }
    if !has_header(&out_headers, "Date") {
        out_headers.insert(
            "Date".to_string(),
            Utc::now().format("%a, %d %b %Y %H:%M:%S GMT").to_string(),
        );
    }
    if let Some(b) = body {
        if !b.is_empty() && !has_header(&out_headers, "Digest") {
            out_headers.insert("Digest".to_string(), build_digest(b));
        }
    }

    let signing_string = build_signing_string(method, &path, &out_headers, &signed_headers);

    let signing_key = SigningKey::<Sha256>::new(private_key.clone());
    let signature = signing_key.sign(signing_string.as_bytes());
    let sig_b64 = base64::engine::general_purpose::STANDARD.encode(signature.to_bytes());

    let header_list = signed_headers.join(" ");
    out_headers.insert(
        "Signature".to_string(),
        format!(
            "keyId=\"{key_id}\",algorithm=\"rsa-sha256\",headers=\"{header_list}\",signature=\"{sig_b64}\""
        ),
    );

    Ok(out_headers)
}

/// Parse a Signature header value into its component fields.
pub fn parse_signature_header(header: &str) -> HashMap<String, String> {
    let mut result = HashMap::new();
    let mut remaining = header.trim().to_string();

    while !remaining.is_empty() {
        let eq_idx = match remaining.find('=') {
            Some(i) => i,
            None => break,
        };
        let key = remaining[..eq_idx]
            .trim()
            .trim_start_matches(',')
            .trim()
            .to_string();
        remaining = remaining[eq_idx + 1..].trim().to_string();

        if remaining.starts_with('"') {
            match remaining[1..].find('"') {
                Some(end_quote) => {
                    result.insert(key, remaining[1..end_quote + 1].to_string());
                    remaining = remaining[end_quote + 2..].trim().to_string();
                    if remaining.starts_with(',') {
                        remaining = remaining[1..].trim().to_string();
                    }
                }
                None => {
                    result.insert(key, remaining[1..].to_string());
                    break;
                }
            }
        } else {
            match remaining.find(',') {
                Some(comma_idx) => {
                    result.insert(key, remaining[..comma_idx].trim().to_string());
                    remaining = remaining[comma_idx + 1..].trim().to_string();
                }
                None => {
                    result.insert(key, remaining.trim().to_string());
                    break;
                }
            }
        }
    }

    result
}

/// Verify the HTTP signature on an incoming request.
///
/// :return: `Ok(true)` if the signature is valid.
/// :raises: [`ActivityPubError::SignatureVerification`] when the signature
///     is missing or invalid, or the digest doesn't match.
pub fn verify_request(
    public_key: &RsaPublicKey,
    method: &str,
    path: &str,
    headers: &HashMap<String, String>,
    body: Option<&[u8]>,
) -> Result<bool> {
    let lower: HashMap<String, String> = headers
        .iter()
        .map(|(k, v)| (k.to_lowercase(), v.clone()))
        .collect();

    let sig_header = lower.get("signature").ok_or_else(|| {
        ActivityPubError::SignatureVerification("Missing Signature header".into())
    })?;

    let parsed = parse_signature_header(sig_header);
    let sig_b64 = parsed.get("signature").ok_or_else(|| {
        ActivityPubError::SignatureVerification("Signature header has no signature field".into())
    })?;

    let signed_headers: Vec<String> = parsed
        .get("headers")
        .cloned()
        .unwrap_or_else(|| "date".to_string())
        .split_whitespace()
        .map(String::from)
        .collect();

    let sig_bytes = base64::engine::general_purpose::STANDARD
        .decode(sig_b64)
        .map_err(|e| {
            ActivityPubError::SignatureVerification(format!("invalid base64 signature: {e}"))
        })?;

    // A request carrying a body must authenticate it: require a
    // `Digest` header that is covered by the signature and matches.
    let body_present = body.map(|b| !b.is_empty()).unwrap_or(false);
    if body_present {
        let digest_header = lower.get("digest").ok_or_else(|| {
            ActivityPubError::SignatureVerification(
                "Request has a body but no Digest header".into(),
            )
        })?;
        let digest_signed = signed_headers
            .iter()
            .any(|h| h.eq_ignore_ascii_case("digest"));
        if !digest_signed {
            return Err(ActivityPubError::SignatureVerification(
                "Digest header is not covered by the signature".into(),
            ));
        }
        let expected_digest = build_digest(body.unwrap_or_default());
        if &expected_digest != digest_header {
            return Err(ActivityPubError::SignatureVerification(format!(
                "Digest mismatch: expected {expected_digest}, got {digest_header}"
            )));
        }
    }

    // Replay protection: when `date` is signed it must be close to now.
    let date_signed = signed_headers
        .iter()
        .any(|h| h.eq_ignore_ascii_case("date"));
    if date_signed {
        let date_header = lower.get("date").ok_or_else(|| {
            ActivityPubError::SignatureVerification("Signed date header is missing".into())
        })?;
        let signed_at = chrono::DateTime::parse_from_rfc2822(date_header)
            .map_err(|_| {
                ActivityPubError::SignatureVerification(format!(
                    "Unparseable signed Date: {date_header:?}"
                ))
            })?
            .with_timezone(&Utc);
        let skew = (Utc::now() - signed_at).num_seconds().abs();
        if skew > MAX_SIGNED_DATE_SKEW_SECS {
            return Err(ActivityPubError::SignatureVerification(format!(
                "Signed Date is {skew}s off (max {MAX_SIGNED_DATE_SKEW_SECS}s)"
            )));
        }
    }

    let signing_string = build_signing_string(method, path, headers, &signed_headers);
    let signature = rsa::pkcs1v15::Signature::try_from(sig_bytes.as_slice()).map_err(|e| {
        ActivityPubError::SignatureVerification(format!("malformed signature: {e}"))
    })?;

    let verifying_key = VerifyingKey::<Sha256>::new(public_key.clone());
    verifying_key
        .verify(signing_string.as_bytes(), &signature)
        .map_err(|e| {
            ActivityPubError::SignatureVerification(format!("Signature verification failed: {e}"))
        })?;

    Ok(true)
}

/// Extract the `keyId` from a Signature header (helper used by the inbox
/// processor).
pub fn signature_key_id(sig_header: &str) -> Option<String> {
    for part in sig_header.split(',') {
        let part = part.trim();
        if let Some(rest) = part.strip_prefix("keyId=") {
            return Some(rest.trim_matches('"').to_string());
        }
    }
    None
}
