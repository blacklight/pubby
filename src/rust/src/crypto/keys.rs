//! RSA key generation and PEM import/export — mirrors
//! `pubby.crypto._keys`.

use std::path::{Path, PathBuf};

use rsa::pkcs1::{DecodeRsaPrivateKey, DecodeRsaPublicKey};
use rsa::pkcs8::{
    DecodePrivateKey, DecodePublicKey, EncodePrivateKey, EncodePublicKey, LineEnding,
};
use rsa::{RsaPrivateKey, RsaPublicKey};

use crate::error::{ActivityPubError, Result};

/// Generate a new RSA key pair.
///
/// :param key_size: RSA key size in bits (default 2048).
/// :return: A tuple of (private_key, public_key).
pub fn generate_rsa_keypair(key_size: usize) -> Result<(RsaPrivateKey, RsaPublicKey)> {
    let private_key = RsaPrivateKey::new(&mut rand::thread_rng(), key_size)?;
    let public_key = RsaPublicKey::from(&private_key);
    Ok((private_key, public_key))
}

/// Load an RSA private key from PEM-encoded data.
///
/// Accepts both PKCS#8 (`BEGIN PRIVATE KEY`) and the PKCS#1
/// (`BEGIN RSA PRIVATE KEY`) form many existing fediverse keys use.
///
/// :param pem_data: PEM-encoded private key string or bytes.
/// :return: The loaded RSA private key.
pub fn load_private_key(pem_data: impl AsRef<[u8]>) -> Result<RsaPrivateKey> {
    let pem = normalize_pem(&String::from_utf8_lossy(pem_data.as_ref()));
    RsaPrivateKey::from_pkcs8_pem(&pem)
        .or_else(|_| RsaPrivateKey::from_pkcs1_pem(&pem))
        .map_err(|e| ActivityPubError::InvalidValue(format!("invalid private key: {e}")))
}

/// Load an RSA public key from PEM-encoded data.
///
/// Accepts both SPKI (`BEGIN PUBLIC KEY`) and the PKCS#1
/// (`BEGIN RSA PUBLIC KEY`) form many existing fediverse keys use.
pub fn load_public_key(pem_data: impl AsRef<[u8]>) -> Result<RsaPublicKey> {
    let pem = normalize_pem(&String::from_utf8_lossy(pem_data.as_ref()));
    RsaPublicKey::from_public_key_pem(&pem)
        .or_else(|_| RsaPublicKey::from_pkcs1_pem(&pem))
        .map_err(|e| ActivityPubError::InvalidValue(format!("invalid public key: {e}")))
}

/// Normalize a PEM document for the strict RFC 7468 parser: line
/// endings become LF and everything outside the first
/// `BEGIN`/`END` encapsulation block is dropped. Some fediverse
/// software ships `publicKeyPem` values the strict parser rejects —
/// e.g. Akkoma appends a trailing blank line.
fn normalize_pem(pem: &str) -> String {
    let pem = pem.replace("\r\n", "\n").replace('\r', "\n");
    let Some(begin) = pem.find("-----BEGIN ") else {
        return pem.trim().to_string();
    };
    let block = &pem[begin..];
    match block.find("-----END ") {
        Some(i) => {
            let tail = &block[i..];
            let end = tail.find('\n').map(|n| i + n).unwrap_or(block.len());
            format!("{}\n", block[..end].trim_end())
        }
        None => block.trim_end().to_string(),
    }
}

/// Export an RSA public key as a PEM-encoded string (SPKI format).
pub fn export_public_key_pem(public_key: &RsaPublicKey) -> Result<String> {
    public_key
        .to_public_key_pem(LineEnding::LF)
        .map_err(|e| ActivityPubError::General(format!("public key export failed: {e}")))
}

/// Export an RSA private key as a PEM-encoded string (PKCS#8 format,
/// unencrypted).
pub fn export_private_key_pem(private_key: &RsaPrivateKey) -> Result<String> {
    private_key
        .to_pkcs8_pem(LineEnding::LF)
        .map(|doc| doc.to_string())
        .map_err(|e| ActivityPubError::General(format!("private key export failed: {e}")))
}

/// Return the resolved path to a PEM private key file, generating an
/// RSA-2048 keypair if the file is missing or empty.
///
/// Parent directories are created as needed and the key is written with
/// `0o600` permissions on Unix.
pub fn ensure_private_key_file(path: impl AsRef<Path>) -> Result<PathBuf> {
    let key_path = expanduser(path.as_ref());

    if let Some(parent) = key_path.parent() {
        std::fs::create_dir_all(parent)?;
    }

    // Refuse to follow a symlink at the key path — the file written
    // here must be a fresh private key, not an attacker-chosen target.
    if let Ok(meta) = std::fs::symlink_metadata(&key_path) {
        if meta.file_type().is_symlink() {
            return Err(ActivityPubError::InvalidValue(format!(
                "private key path is a symlink: {}",
                key_path.display()
            )));
        }
    }

    let needs_key = match std::fs::metadata(&key_path) {
        Ok(m) => m.len() == 0,
        Err(_) => true,
    };

    if needs_key {
        let (private_key, _) = generate_rsa_keypair(2048)?;
        let pem = export_private_key_pem(&private_key)?;
        // Created with 0600 up front — no world-readable window.
        let mut opts = std::fs::OpenOptions::new();
        opts.write(true).create_new(true);
        #[cfg(unix)]
        {
            use std::os::unix::fs::OpenOptionsExt;
            opts.mode(0o600);
        }
        match opts.open(&key_path) {
            Ok(mut f) => std::io::Write::write_all(&mut f, pem.as_bytes())?,
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
                // Raced with another creator — the existing file wins.
            }
            Err(e) => return Err(e.into()),
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&key_path, std::fs::Permissions::from_mode(0o600))?;
        }
    }

    Ok(key_path.canonicalize().unwrap_or(key_path))
}

/// Expand a leading `~` in a path to the user's home directory.
fn expanduser(path: &Path) -> PathBuf {
    let s = path.to_string_lossy();
    if let Some(rest) = s.strip_prefix("~/") {
        if let Some(home) = std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE")) {
            return PathBuf::from(home).join(rest);
        }
    } else if s == "~" {
        if let Some(home) = std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE")) {
            return PathBuf::from(home);
        }
    }
    path.to_path_buf()
}
