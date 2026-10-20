//! Cryptographic helpers — mirrors `pubby.crypto`.
//!
//! RSA key generation/PEM serialization ([`keys`]) and draft-cavage
//! HTTP Signatures ([`signatures`]).

pub mod keys;
pub mod signatures;

pub use keys::{
    ensure_private_key_file, export_private_key_pem, export_public_key_pem, generate_rsa_keypair,
    load_private_key, load_public_key,
};
pub use signatures::{sign_request, verify_request, DEFAULT_SIGNED_HEADERS};
