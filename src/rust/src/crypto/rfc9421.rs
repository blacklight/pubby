//! HTTP Message Signatures (RFC 9421) — `rsa-v1_5-sha256` over derived
//! components (`@method`, `@authority`, `@path`, `@query`) and header
//! fields, with body integrity via `Content-Digest` (RFC 9530).
//!
//! Self-contained like [`super::signatures`] — no HTTP-signature crate.
//! Covers the Structured Field subset that signatures use: inner lists
//! of quoted strings with integer/string parameters, and `Signature`
//! byte sequences.

use std::collections::HashMap;

use base64::Engine;
use chrono::Utc;
use rsa::pkcs1v15::{SigningKey as RsaSigningKey, VerifyingKey as RsaVerifyingKey};
use rsa::signature::{SignatureEncoding, Signer, Verifier};
use rsa::RsaPrivateKey;
use sha2::{Digest, Sha256};

use super::keys::AnyPublicKey;
use super::signatures::{SignOptions, MAX_SIGNED_DATE_SKEW_SECS};
use crate::error::{ActivityPubError, Result};

/// `alg` identifier for RSASSA-PKCS1-v1_5 with SHA-256 (RFC 9421 §3.3.2).
pub const ALG_RSA_V1_5_SHA256: &str = "rsa-v1_5-sha256";

/// `alg` identifier for Ed25519 (RFC 9421 Appendix B.2.6 — `ed25519`).
pub const ALG_ED25519: &str = "ed25519";

/// The private key an outgoing signature is made with.
#[derive(Clone, Copy)]
enum SignKey<'a> {
    Rsa(&'a RsaPrivateKey),
    Ed25519(&'a ed25519_dalek::SigningKey),
}

impl SignKey<'_> {
    fn alg(&self) -> &'static str {
        match self {
            SignKey::Rsa(_) => ALG_RSA_V1_5_SHA256,
            SignKey::Ed25519(_) => ALG_ED25519,
        }
    }

    fn sign(&self, data: &[u8]) -> Vec<u8> {
        match self {
            SignKey::Rsa(key) => RsaSigningKey::<Sha256>::new((*key).clone())
                .sign(data)
                .to_bytes()
                .into_vec(),
            SignKey::Ed25519(key) => key.sign(data).to_bytes().to_vec(),
        }
    }
}

/// Signature label used for outgoing signatures.
const LABEL: &str = "sig1";

/// Components covered by outgoing signatures. `content-digest` is added
/// when a body is sent.
pub const DEFAULT_COMPONENTS: &[&str] = &["@method", "@authority", "@path"];

/// Components every verified signature must cover.
const REQUIRED_COMPONENTS: &[&str] = &["@method", "@authority", "@path"];

/// A Structured Field bare item used in signature parameters.
#[derive(Debug, Clone, PartialEq, Eq)]
enum Item {
    Str(String),
    Int(i64),
}

/// One signature parsed out of `Signature-Input` + `Signature`.
#[derive(Debug, Clone)]
pub struct ParsedSignature {
    label: String,
    components: Vec<String>,
    params: Vec<(String, Item)>,
    value: Vec<u8>,
}

impl ParsedSignature {
    /// The `keyid` parameter, when present.
    pub fn key_id(&self) -> Option<&str> {
        self.params.iter().find_map(|(k, v)| match v {
            Item::Str(s) if k == "keyid" => Some(s.as_str()),
            _ => None,
        })
    }

    fn int_param(&self, name: &str) -> Option<i64> {
        self.params.iter().find_map(|(k, v)| match v {
            Item::Int(i) if k == name => Some(*i),
            _ => None,
        })
    }

    fn str_param(&self, name: &str) -> Option<&str> {
        self.params.iter().find_map(|(k, v)| match v {
            Item::Str(s) if k == name => Some(s.as_str()),
            _ => None,
        })
    }
}

fn bad(msg: impl Into<String>) -> ActivityPubError {
    ActivityPubError::SignatureVerification(msg.into())
}

fn header<'a>(headers: &'a HashMap<String, String>, name: &str) -> Option<&'a str> {
    headers
        .iter()
        .find(|(k, _)| k.eq_ignore_ascii_case(name))
        .map(|(_, v)| v.trim())
}

/// Build the `Content-Digest` value of a body (`sha-256=:<base64>:`).
pub fn build_content_digest(body: &[u8]) -> String {
    format!(
        "sha-256=:{}:",
        base64::engine::general_purpose::STANDARD.encode(Sha256::digest(body))
    )
}

fn quote(s: &str) -> String {
    format!("\"{}\"", s.replace('\\', "\\\\").replace('"', "\\\""))
}

fn unquote(s: &str) -> Option<String> {
    let inner = s.strip_prefix('"')?.strip_suffix('"')?;
    let mut out = String::with_capacity(inner.len());
    let mut escaped = false;
    for c in inner.chars() {
        match (escaped, c) {
            (true, c) => {
                out.push(c);
                escaped = false;
            }
            (false, '\\') => escaped = true,
            (false, '"') => return None,
            (false, c) => out.push(c),
        }
    }
    (!escaped).then_some(out)
}

/// Serialize the covered components and parameters as an inner list
/// with parameters — the `@signature-params` value.
fn serialize_params(components: &[String], params: &[(String, Item)]) -> String {
    let inner: Vec<String> = components.iter().map(|c| quote(c)).collect();
    let mut out = format!("({})", inner.join(" "));
    for (k, v) in params {
        match v {
            Item::Str(s) => out.push_str(&format!(";{k}={}", quote(s))),
            Item::Int(i) => out.push_str(&format!(";{k}={i}")),
        }
    }
    out
}

/// Split on `sep` outside double quotes and parentheses.
fn split_top(s: &str, sep: char) -> Vec<&str> {
    let (mut out, mut depth, mut in_quotes, mut escaped, mut start) =
        (Vec::new(), 0, false, false, 0);
    for (i, c) in s.char_indices() {
        if in_quotes {
            match (escaped, c) {
                (true, _) => escaped = false,
                (false, '\\') => escaped = true,
                (false, '"') => in_quotes = false,
                _ => {}
            }
        } else {
            match c {
                '"' => in_quotes = true,
                '(' => depth += 1,
                ')' => depth -= 1,
                c if c == sep && depth == 0 => {
                    out.push(&s[start..i]);
                    start = i + c.len_utf8();
                }
                _ => {}
            }
        }
    }
    out.push(&s[start..]);
    out
}

fn parse_item(raw: &str) -> Result<Item> {
    if raw.starts_with('"') {
        return unquote(raw)
            .map(Item::Str)
            .ok_or_else(|| bad(format!("malformed string parameter {raw}")));
    }
    raw.parse::<i64>()
        .map(Item::Int)
        .map_err(|_| bad(format!("unsupported signature parameter value {raw}")))
}

/// Parse one `Signature-Input` member (`label=("a" "b");k=v`), leaving
/// the signature bytes unset.
fn parse_input_member(member: &str) -> Result<ParsedSignature> {
    let (label, rest) = member
        .split_once('=')
        .ok_or_else(|| bad(format!("malformed Signature-Input member {member}")))?;
    let rest = rest.trim();
    let close = rest
        .find(')')
        .filter(|_| rest.starts_with('('))
        .ok_or_else(|| bad("Signature-Input value is not an inner list"))?;

    let components = rest[1..close]
        .split_whitespace()
        .map(|t| unquote(t).ok_or_else(|| bad(format!("unsupported covered component {t}"))))
        .collect::<Result<Vec<_>>>()?;

    let params = split_top(&rest[close + 1..], ';')
        .into_iter()
        .map(str::trim)
        .filter(|p| !p.is_empty())
        .map(|p| {
            let (k, v) = p
                .split_once('=')
                .ok_or_else(|| bad(format!("malformed signature parameter {p}")))?;
            Ok((k.trim().to_string(), parse_item(v.trim())?))
        })
        .collect::<Result<Vec<_>>>()?;

    Ok(ParsedSignature {
        label: label.trim().to_string(),
        components,
        params,
        value: Vec::new(),
    })
}

/// Parse the `Signature-Input` and `Signature` fields of a request.
///
/// :return: One entry per `Signature-Input` member, in order. Empty when
///     the request carries no RFC 9421 signature (`Signature-Input`
///     absent), so callers can fall back to draft-cavage verification.
pub fn parse_signatures(headers: &HashMap<String, String>) -> Result<Vec<ParsedSignature>> {
    let Some(input) = header(headers, "signature-input") else {
        return Ok(Vec::new());
    };
    let signature = header(headers, "signature")
        .ok_or_else(|| bad("Signature-Input present without a Signature field"))?;

    let mut values: HashMap<String, Vec<u8>> = HashMap::new();
    for member in split_top(signature, ',') {
        let (label, value) = member
            .trim()
            .split_once('=')
            .ok_or_else(|| bad("malformed Signature member"))?;
        let b64 = value
            .strip_prefix(':')
            .and_then(|v| v.strip_suffix(':'))
            .ok_or_else(|| bad("Signature value is not a byte sequence"))?;
        let bytes = base64::engine::general_purpose::STANDARD
            .decode(b64)
            .map_err(|e| bad(format!("invalid base64 signature: {e}")))?;
        values.insert(label.trim().to_string(), bytes);
    }

    split_top(input, ',')
        .into_iter()
        .map(str::trim)
        .filter(|m| !m.is_empty())
        .map(|m| {
            let mut sig = parse_input_member(m)?;
            sig.value = values
                .get(&sig.label)
                .cloned()
                .ok_or_else(|| bad(format!("no Signature for label {}", sig.label)))?;
            Ok(sig)
        })
        .collect()
}

/// Source of covered component values for one message.
struct Message<'a> {
    method: &'a str,
    authority: String,
    path_and_query: &'a str,
    headers: &'a HashMap<String, String>,
}

impl Message<'_> {
    fn value(&self, name: &str) -> Result<String> {
        let (path, query) = match self.path_and_query.split_once('?') {
            Some((p, q)) => (p, Some(q)),
            None => (self.path_and_query, None),
        };
        match name {
            "@method" => Ok(self.method.to_uppercase()),
            "@authority" => Ok(self.authority.clone()),
            "@path" => Ok(if path.is_empty() {
                "/".to_string()
            } else {
                path.to_string()
            }),
            "@query" => Ok(format!("?{}", query.unwrap_or_default())),
            n if n.starts_with('@') => Err(bad(format!("unsupported derived component {n}"))),
            n => header(self.headers, n)
                .map(str::to_string)
                .ok_or_else(|| bad(format!("covered component {n} is missing"))),
        }
    }
}

/// The signature base (RFC 9421 §2.5): one `"name": value` line per
/// covered component, then the `@signature-params` line.
fn signature_base(
    components: &[String],
    params: &[(String, Item)],
    message: &Message,
) -> Result<String> {
    let mut lines = Vec::with_capacity(components.len() + 1);
    for c in components {
        lines.push(format!("{}: {}", quote(c), message.value(c)?));
    }
    lines.push(format!(
        "{}: {}",
        quote("@signature-params"),
        serialize_params(components, params)
    ));
    Ok(lines.join("\n"))
}

/// Sign an outgoing request with RFC 9421 and return the headers to add:
/// `Signature-Input`, `Signature`, and `Content-Digest` when a body is
/// present.
///
/// `options.signed_headers` lists the covered components (defaults to
/// [`DEFAULT_COMPONENTS`]); `content-digest` is covered automatically
/// when a body is sent.
pub fn sign_request(
    private_key: &RsaPrivateKey,
    key_id: &str,
    method: &str,
    url: &str,
    body: Option<&[u8]>,
    options: &SignOptions,
) -> Result<HashMap<String, String>> {
    sign_at(
        SignKey::Rsa(private_key),
        key_id,
        method,
        url,
        body,
        options,
        Utc::now().timestamp(),
    )
}

/// Sign an outgoing request with an Ed25519 key (`alg="ed25519"`).
///
/// Same output as [`sign_request`]; the actor's own key stays RSA unless
/// the host supplies an Ed25519 signing key here.
pub fn sign_request_ed25519(
    signing_key: &ed25519_dalek::SigningKey,
    key_id: &str,
    method: &str,
    url: &str,
    body: Option<&[u8]>,
    options: &SignOptions,
) -> Result<HashMap<String, String>> {
    sign_at(
        SignKey::Ed25519(signing_key),
        key_id,
        method,
        url,
        body,
        options,
        Utc::now().timestamp(),
    )
}

fn sign_at(
    key: SignKey<'_>,
    key_id: &str,
    method: &str,
    url: &str,
    body: Option<&[u8]>,
    options: &SignOptions,
    created: i64,
) -> Result<HashMap<String, String>> {
    let parsed = url::Url::parse(url)
        .map_err(|e| ActivityPubError::InvalidValue(format!("invalid URL {url}: {e}")))?;
    let authority = match parsed.port() {
        Some(p) => format!("{}:{p}", parsed.host_str().unwrap_or_default()),
        None => parsed.host_str().unwrap_or_default().to_string(),
    }
    .to_lowercase();
    let path_and_query = match parsed.query() {
        Some(q) => format!("{}?{q}", parsed.path()),
        None => parsed.path().to_string(),
    };

    let mut components: Vec<String> = match options.signed_headers {
        Some(c) => c.iter().map(|s| s.to_lowercase()).collect(),
        None => DEFAULT_COMPONENTS.iter().map(|s| s.to_string()).collect(),
    };
    let mut headers = options.headers.clone();
    let body = body.filter(|b| !b.is_empty());
    if let Some(body) = body {
        if !components.iter().any(|c| c == "content-digest") {
            components.push("content-digest".to_string());
        }
        if header(&headers, "content-digest").is_none() {
            headers.insert("Content-Digest".to_string(), build_content_digest(body));
        }
    } else {
        components.retain(|c| c != "content-digest");
    }

    let params = vec![
        ("created".to_string(), Item::Int(created)),
        ("keyid".to_string(), Item::Str(key_id.to_string())),
        ("alg".to_string(), Item::Str(key.alg().to_string())),
    ];

    let base = signature_base(
        &components,
        &params,
        &Message {
            method,
            authority,
            path_and_query: &path_and_query,
            headers: &headers,
        },
    )?;
    let sig_b64 = base64::engine::general_purpose::STANDARD.encode(key.sign(base.as_bytes()));

    headers.insert(
        "Signature-Input".to_string(),
        format!("{LABEL}={}", serialize_params(&components, &params)),
    );
    headers.insert("Signature".to_string(), format!("{LABEL}=:{sig_b64}:"));
    Ok(headers)
}

/// Verify one parsed RFC 9421 signature against a request.
///
/// Checks the algorithm, the `created`/`expires` window, the required
/// covered components, `Content-Digest` when a body is present, and the
/// RSA signature over the rebuilt signature base.
///
/// :param path_and_query: The request target as received (path plus
///     optional `?query`).
/// :raises: [`ActivityPubError::SignatureVerification`] on any failure.
pub fn verify_request(
    public_key: &AnyPublicKey,
    signature: &ParsedSignature,
    method: &str,
    path_and_query: &str,
    headers: &HashMap<String, String>,
    body: Option<&[u8]>,
) -> Result<()> {
    verify_at(
        public_key,
        signature,
        method,
        path_and_query,
        headers,
        body,
        Utc::now().timestamp(),
    )
}

fn verify_at(
    public_key: &AnyPublicKey,
    signature: &ParsedSignature,
    method: &str,
    path_and_query: &str,
    headers: &HashMap<String, String>,
    body: Option<&[u8]>,
    now: i64,
) -> Result<()> {
    let key_alg = match public_key {
        AnyPublicKey::Rsa(_) => ALG_RSA_V1_5_SHA256,
        AnyPublicKey::Ed25519(_) => ALG_ED25519,
    };
    if let Some(alg) = signature.str_param("alg") {
        if alg != key_alg {
            return Err(bad(format!(
                "algorithm {alg} does not match the signing key"
            )));
        }
    }
    for required in REQUIRED_COMPONENTS {
        if !signature.components.iter().any(|c| c == required) {
            return Err(bad(format!("signature does not cover {required}")));
        }
    }

    let created = signature
        .int_param("created")
        .ok_or_else(|| bad("signature has no created parameter"))?;
    if (now - created).abs() > MAX_SIGNED_DATE_SKEW_SECS {
        return Err(bad(format!(
            "signature created {}s off (max {MAX_SIGNED_DATE_SKEW_SECS}s)",
            (now - created).abs()
        )));
    }
    if signature.int_param("expires").is_some_and(|e| now > e) {
        return Err(bad("signature has expired"));
    }

    if let Some(body) = body.filter(|b| !b.is_empty()) {
        if !signature.components.iter().any(|c| c == "content-digest") {
            return Err(bad("request has a body but content-digest is not covered"));
        }
        let expected = build_content_digest(body);
        match header(headers, "content-digest") {
            Some(actual) if actual == expected => {}
            actual => {
                return Err(bad(format!(
                    "Content-Digest mismatch: expected {expected}, got {actual:?}"
                )))
            }
        }
    }

    let authority = header(headers, "host")
        .ok_or_else(|| bad("request has no Host header for @authority"))?
        .to_lowercase();
    let base = signature_base(
        &signature.components,
        &signature.params,
        &Message {
            method,
            authority,
            path_and_query,
            headers,
        },
    )?;

    let verified = match public_key {
        AnyPublicKey::Rsa(key) => {
            let sig = rsa::pkcs1v15::Signature::try_from(signature.value.as_slice())
                .map_err(|e| bad(format!("malformed signature: {e}")))?;
            RsaVerifyingKey::<Sha256>::new(key.clone()).verify(base.as_bytes(), &sig)
        }
        AnyPublicKey::Ed25519(key) => {
            let sig = ed25519_dalek::Signature::from_slice(&signature.value)
                .map_err(|e| bad(format!("malformed signature: {e}")))?;
            key.verify(base.as_bytes(), &sig)
        }
    };
    verified.map_err(|e| bad(format!("Signature verification failed: {e}")))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::crypto::keys::generate_rsa_keypair;

    const URL: &str = "https://remote.example/ap/users/bob/inbox?x=1";
    const KEY_ID: &str = "https://remote.example/ap/actor#main-key";
    const BODY: &[u8] = br#"{"type":"Follow"}"#;

    fn keypair() -> (RsaPrivateKey, AnyPublicKey) {
        let (private, public) = generate_rsa_keypair(1024).unwrap();
        (private, AnyPublicKey::Rsa(public))
    }

    /// RFC 9421 Appendix B.2.6 — `test-key-ed25519`, SPKI PEM form.
    const RFC_ED25519_PUBLIC_KEY: &str = "-----BEGIN PUBLIC KEY-----
MCowBQYDK2VwAyEAJrQLj5P/89iXES9+vFgrIy29clF9CC/oPPsw3c5D0bs=
-----END PUBLIC KEY-----";

    #[test]
    fn verifies_rfc9421_appendix_b26_ed25519_example() {
        let key = crate::crypto::keys::load_any_public_key(RFC_ED25519_PUBLIC_KEY).unwrap();
        assert!(matches!(key, AnyPublicKey::Ed25519(_)));
        let headers = HashMap::from([
            ("signature-input".to_string(), "sig-b26=(\"date\" \"@method\" \"@path\" \"@authority\" \"content-type\" \"content-length\");created=1618884473;keyid=\"test-key-ed25519\"".to_string()),
            ("signature".to_string(), "sig-b26=:wqcAqbmYJ2ji2glfAMaRy4gruYYnx2nEFN2HN6jrnDnQCK1u02Gb04v9EDgwUPiu4A0w6vuQv5lIp5WPpBKRCw==:".to_string()),
            ("date".to_string(), "Tue, 20 Apr 2021 02:07:55 GMT".to_string()),
            ("host".to_string(), "example.com".to_string()),
            ("content-type".to_string(), "application/json".to_string()),
            ("content-length".to_string(), "18".to_string()),
        ]);
        let sig = &parse_signatures(&headers).unwrap()[0];
        verify_at(
            &key,
            sig,
            "POST",
            "/foo?param=value&pet=dog",
            &headers,
            None,
            1618884473,
        )
        .unwrap();
    }

    #[test]
    fn ed25519_round_trip() {
        let signing_key = ed25519_dalek::SigningKey::from_bytes(&[7u8; 32]);
        let public = AnyPublicKey::Ed25519(signing_key.verifying_key());
        let headers = sign_request_ed25519(
            &signing_key,
            KEY_ID,
            "POST",
            URL,
            Some(BODY),
            &SignOptions::default(),
        )
        .unwrap();
        let mut received = headers.clone();
        received.insert("Host".into(), "remote.example".into());
        let sig = &parse_signatures(&received).unwrap()[0];
        assert_eq!(sig.str_param("alg"), Some(ALG_ED25519));
        verify_request(
            &public,
            sig,
            "POST",
            "/ap/users/bob/inbox?x=1",
            &received,
            Some(BODY),
        )
        .unwrap();
    }

    #[test]
    fn rejects_algorithm_not_matching_key_type() {
        let (private, rsa_public) = keypair();
        let signing_key = ed25519_dalek::SigningKey::from_bytes(&[7u8; 32]);
        let headers = sign_request_ed25519(
            &signing_key,
            KEY_ID,
            "POST",
            URL,
            None,
            &SignOptions::default(),
        )
        .unwrap();
        let mut received = headers.clone();
        received.insert("Host".into(), "remote.example".into());
        let sig = &parse_signatures(&received).unwrap()[0];
        // An Ed25519-labelled signature must not verify under an RSA key.
        let err = verify_request(
            &rsa_public,
            sig,
            "POST",
            "/ap/users/bob/inbox?x=1",
            &received,
            None,
        )
        .unwrap_err();
        assert!(err.to_string().contains("does not match"), "{err}");
        let _ = private;
    }

    /// What a receiver sees: the signed headers plus the `Host` the
    /// HTTP stack adds.
    fn received(key: &RsaPrivateKey, body: Option<&[u8]>) -> HashMap<String, String> {
        let mut headers =
            sign_request(key, KEY_ID, "POST", URL, body, &SignOptions::default()).unwrap();
        headers.insert("Host".into(), "remote.example".into());
        headers
    }

    #[test]
    fn signed_post_round_trips_through_verify() {
        let (private, public) = keypair();
        let headers = received(&private, Some(BODY));
        let sigs = parse_signatures(&headers).unwrap();
        assert_eq!(sigs.len(), 1);
        assert_eq!(sigs[0].key_id(), Some(KEY_ID));
        verify_request(
            &public,
            &sigs[0],
            "POST",
            "/ap/users/bob/inbox?x=1",
            &headers,
            Some(BODY),
        )
        .unwrap();
    }

    #[test]
    fn signed_post_covers_content_digest_and_derived_components() {
        let (private, _) = keypair();
        let headers = received(&private, Some(BODY));
        assert_eq!(headers["Content-Digest"], build_content_digest(BODY));
        let sig = &parse_signatures(&headers).unwrap()[0];
        assert_eq!(
            sig.components,
            ["@method", "@authority", "@path", "content-digest"]
        );
    }

    #[test]
    fn rejects_tampered_body() {
        let (private, public) = keypair();
        let headers = received(&private, Some(BODY));
        let sig = &parse_signatures(&headers).unwrap()[0];
        let err = verify_request(
            &public,
            sig,
            "POST",
            "/ap/users/bob/inbox?x=1",
            &headers,
            Some(b"{}"),
        )
        .unwrap_err();
        assert!(err.to_string().contains("Content-Digest mismatch"), "{err}");
    }

    #[test]
    fn rejects_altered_target() {
        let (private, public) = keypair();
        let headers = received(&private, Some(BODY));
        let sig = &parse_signatures(&headers).unwrap()[0];
        assert!(verify_request(
            &public,
            sig,
            "POST",
            "/ap/users/eve/inbox?x=1",
            &headers,
            Some(BODY)
        )
        .is_err());
    }

    #[test]
    fn rejects_wrong_key() {
        let (private, _) = keypair();
        let (_, other_public) = keypair();
        let headers = received(&private, Some(BODY));
        let sig = &parse_signatures(&headers).unwrap()[0];
        assert!(verify_request(
            &other_public,
            sig,
            "POST",
            "/ap/users/bob/inbox?x=1",
            &headers,
            Some(BODY)
        )
        .is_err());
    }

    #[test]
    fn rejects_stale_created() {
        let (private, public) = keypair();
        let stale = Utc::now().timestamp() - 3600;
        let mut headers = sign_at(
            SignKey::Rsa(&private),
            KEY_ID,
            "POST",
            URL,
            Some(BODY),
            &SignOptions::default(),
            stale,
        )
        .unwrap();
        headers.insert("Host".into(), "remote.example".into());
        let sig = &parse_signatures(&headers).unwrap()[0];
        let err = verify_request(
            &public,
            sig,
            "POST",
            "/ap/users/bob/inbox?x=1",
            &headers,
            Some(BODY),
        )
        .unwrap_err();
        assert!(err.to_string().contains("created"), "{err}");
    }

    #[test]
    fn rejects_signature_missing_required_component() {
        let (private, public) = keypair();
        let options = SignOptions {
            headers: HashMap::new(),
            signed_headers: Some(&["@method".to_string(), "@path".to_string()]),
        };
        let mut headers = sign_request(&private, KEY_ID, "GET", URL, None, &options).unwrap();
        headers.insert("Host".into(), "remote.example".into());
        let sig = &parse_signatures(&headers).unwrap()[0];
        let err = verify_request(
            &public,
            sig,
            "GET",
            "/ap/users/bob/inbox?x=1",
            &headers,
            None,
        )
        .unwrap_err();
        assert!(err.to_string().contains("@authority"), "{err}");
    }

    #[test]
    fn signature_base_matches_rfc_serialization() {
        let headers = HashMap::from([("content-digest".to_string(), "sha-256=:X:".to_string())]);
        let components: Vec<String> = ["@method", "@authority", "@path", "content-digest"]
            .map(String::from)
            .to_vec();
        let params = vec![
            ("created".to_string(), Item::Int(1618884473)),
            ("keyid".to_string(), Item::Str("test-key-rsa-pss".into())),
        ];
        let base = signature_base(
            &components,
            &params,
            &Message {
                method: "post",
                authority: "example.com".into(),
                path_and_query: "/foo?param=value&pet=dog",
                headers: &headers,
            },
        )
        .unwrap();
        assert_eq!(
            base,
            "\"@method\": POST\n\
             \"@authority\": example.com\n\
             \"@path\": /foo\n\
             \"content-digest\": sha-256=:X:\n\
             \"@signature-params\": (\"@method\" \"@authority\" \"@path\" \"content-digest\");created=1618884473;keyid=\"test-key-rsa-pss\""
        );
    }

    #[test]
    fn query_component_keeps_question_mark() {
        let headers = HashMap::new();
        let message = |pq| Message {
            method: "GET",
            authority: "example.com".into(),
            path_and_query: pq,
            headers: &headers,
        };
        assert_eq!(message("/a?b=c").value("@query").unwrap(), "?b=c");
        assert_eq!(message("/a").value("@query").unwrap(), "?");
        assert_eq!(message("").value("@path").unwrap(), "/");
    }

    #[test]
    fn parses_signature_input_from_rfc_style_header() {
        let headers = HashMap::from([
            (
                "Signature-Input".to_string(),
                "sig-b22=(\"@authority\" \"@path\" \"content-digest\");created=1618884473;keyid=\"test-key-rsa-pss\";alg=\"rsa-v1_5-sha256\"".to_string(),
            ),
            ("Signature".to_string(), "sig-b22=:AAAA:".to_string()),
        ]);
        let sig = &parse_signatures(&headers).unwrap()[0];
        assert_eq!(sig.label, "sig-b22");
        assert_eq!(sig.components, ["@authority", "@path", "content-digest"]);
        assert_eq!(sig.key_id(), Some("test-key-rsa-pss"));
        assert_eq!(sig.int_param("created"), Some(1618884473));
        assert_eq!(sig.str_param("alg"), Some(ALG_RSA_V1_5_SHA256));
    }

    #[test]
    fn no_signature_input_means_not_rfc9421() {
        let headers = HashMap::from([("Signature".to_string(), "keyId=\"x\"".to_string())]);
        assert!(parse_signatures(&headers).unwrap().is_empty());
    }
}
