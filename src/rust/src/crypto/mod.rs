//! Cryptographic helpers — mirrors `pubby.crypto`.
//!
//! RSA key generation/PEM serialization ([`keys`]), draft-cavage
//! HTTP Signatures ([`signatures`]) and RFC 9421 HTTP Message
//! Signatures ([`rfc9421`]).

use std::collections::HashMap;

use rsa::RsaPrivateKey;

use crate::error::Result;

pub mod keys;
pub mod rfc9421;
pub mod signatures;

pub use keys::{
    ensure_private_key_file, export_ed25519_private_key_pem, export_ed25519_public_key_pem,
    export_private_key_pem, export_public_key_pem, generate_ed25519_key, generate_rsa_keypair,
    load_ed25519_private_key, load_private_key, load_public_key,
};
pub use signatures::{sign_request, verify_request, DEFAULT_SIGNED_HEADERS};

use signatures::SignOptions;

/// Wire format used for outgoing HTTP signatures.
///
/// Inbound requests are never configured: verification detects the
/// format from the request (`Signature-Input` present → RFC 9421).
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub enum SignatureScheme {
    /// draft-cavage-http-signatures-12 (`Signature` with `keyId`).
    #[default]
    DraftCavage,
    /// RFC 9421 (`Signature-Input` + `Signature`, `Content-Digest`).
    Rfc9421,
}

/// An Ed25519 signing key and the `keyId` it is advertised under
/// (`<actor_id>#ed25519-key`).
#[derive(Clone)]
pub struct Ed25519Identity {
    /// The `keyId` written into `Signature-Input`.
    pub key_id: String,
    /// The Ed25519 private key.
    pub key: ed25519_dalek::SigningKey,
}

/// Sign an outgoing request with the given [`SignatureScheme`].
///
/// Under [`SignatureScheme::Rfc9421`] an available `ed25519` identity signs
/// instead of the RSA key; draft-cavage is RSA-only.
///
/// `options.signed_headers` means header names for [`SignatureScheme::DraftCavage`]
/// and covered components for [`SignatureScheme::Rfc9421`]; pass `None`
/// to get each scheme's defaults.
#[allow(clippy::too_many_arguments)]
pub fn sign_outgoing(
    scheme: SignatureScheme,
    private_key: &RsaPrivateKey,
    key_id: &str,
    ed25519: Option<&Ed25519Identity>,
    method: &str,
    url: &str,
    body: Option<&[u8]>,
    options: &SignOptions,
) -> Result<HashMap<String, String>> {
    match (scheme, ed25519) {
        (SignatureScheme::Rfc9421, Some(identity)) => rfc9421::sign_request_ed25519(
            &identity.key,
            &identity.key_id,
            method,
            url,
            body,
            options,
        ),
        (SignatureScheme::DraftCavage, _) => {
            sign_request(private_key, key_id, method, url, body, options)
        }
        (SignatureScheme::Rfc9421, None) => {
            rfc9421::sign_request(private_key, key_id, method, url, body, options)
        }
    }
}
