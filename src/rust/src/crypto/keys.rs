//! RSA key generation and PEM import/export — mirrors
//! `pubby.crypto._keys`.

use std::path::{Path, PathBuf};

use rsa::pkcs1::{DecodeRsaPrivateKey, DecodeRsaPublicKey};
use rsa::pkcs8::{
    DecodePrivateKey, DecodePublicKey, EncodePrivateKey, EncodePublicKey, LineEnding,
};
use rsa::{RsaPrivateKey, RsaPublicKey};

use crate::error::{ActivityPubError, Result};

/// Generate a new Ed25519 signing key.
pub fn generate_ed25519_key() -> ed25519_dalek::SigningKey {
    let mut seed = [0u8; 32];
    rand::RngCore::fill_bytes(&mut rand::thread_rng(), &mut seed);
    ed25519_dalek::SigningKey::from_bytes(&seed)
}

/// Load an Ed25519 private key from PKCS#8 PEM data.
pub fn load_ed25519_private_key(pem_data: impl AsRef<[u8]>) -> Result<ed25519_dalek::SigningKey> {
    let pem = normalize_pem(&String::from_utf8_lossy(pem_data.as_ref()));
    <ed25519_dalek::SigningKey as ed25519_dalek::pkcs8::DecodePrivateKey>::from_pkcs8_pem(&pem)
        .map_err(|e| ActivityPubError::InvalidValue(format!("invalid Ed25519 private key: {e}")))
}

/// Export an Ed25519 private key as PKCS#8 PEM.
pub fn export_ed25519_private_key_pem(key: &ed25519_dalek::SigningKey) -> Result<String> {
    use ed25519_dalek::pkcs8::EncodePrivateKey;
    key.to_pkcs8_pem(LineEnding::LF)
        .map(|pem| pem.to_string())
        .map_err(|e| ActivityPubError::InvalidValue(format!("Ed25519 key export failed: {e}")))
}

/// Export an Ed25519 public key as SPKI PEM (the `publicKeyPem` form).
pub fn export_ed25519_public_key_pem(key: &ed25519_dalek::VerifyingKey) -> Result<String> {
    use ed25519_dalek::pkcs8::EncodePublicKey;
    key.to_public_key_pem(LineEnding::LF)
        .map_err(|e| ActivityPubError::InvalidValue(format!("Ed25519 key export failed: {e}")))
}

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

/// A public key of a supported signature family.
#[derive(Debug, Clone)]
pub enum AnyPublicKey {
    /// RSA — `rsa-v1_5-sha256` in RFC 9421, `rsa-sha256` in draft-cavage.
    Rsa(RsaPublicKey),
    /// Ed25519 (RFC 8032) — RFC 9421 `ed25519`.
    Ed25519(ed25519_dalek::VerifyingKey),
}

/// Load an RSA or Ed25519 public key from SPKI PEM data.
pub fn load_any_public_key(pem_data: impl AsRef<[u8]>) -> Result<AnyPublicKey> {
    let pem = normalize_pem(&String::from_utf8_lossy(pem_data.as_ref()));
    if let Ok(key) = load_public_key(&pem) {
        return Ok(AnyPublicKey::Rsa(key));
    }
    <ed25519_dalek::VerifyingKey as ed25519_dalek::pkcs8::DecodePublicKey>::from_public_key_pem(
        &pem,
    )
    .map(AnyPublicKey::Ed25519)
    .map_err(|e| ActivityPubError::InvalidValue(format!("invalid public key: {e}")))
}

/// Normalize a PEM document for the strict RFC 7468 parser: line
/// endings become LF, everything outside the first `BEGIN`/`END`
/// encapsulation block is dropped, and the base64 body is re-wrapped
/// to 64 columns. Some fediverse software ships `publicKeyPem` values
/// the strict parser rejects — e.g. Akkoma appends a trailing blank
/// line and BirdsiteLive emits 72-column base64.
fn normalize_pem(pem: &str) -> String {
    let pem = pem.replace("\r\n", "\n").replace('\r', "\n");
    let Some(begin) = pem.find("-----BEGIN ") else {
        return pem.trim().to_string();
    };
    let block = &pem[begin..];
    let Some(end) = block.find("-----END ") else {
        return block.trim_end().to_string();
    };
    let tail = &block[end..];
    let block_end = end + tail.find('\n').map(|n| n + 1).unwrap_or(tail.len());
    canonicalize_pem_block(&block[..block_end])
}

/// Rebuild one `BEGIN`/`END` encapsulation with canonical 64-column
/// base64. PEM headers (e.g. `Proc-Type`/`DEK-Info` on legacy
/// encrypted keys) are preserved ahead of the body.
fn canonicalize_pem_block(block: &str) -> String {
    let lines: Vec<&str> = block.lines().collect();
    let Some(end_idx) = lines.iter().rposition(|l| l.contains("-----END ")) else {
        return block.trim_end().to_string();
    };
    let begin_line = lines.first().map(|l| l.trim_end()).unwrap_or_default();
    let end_line = lines[end_idx].trim_end();

    let mut headers = Vec::new();
    let mut body = String::new();
    let mut in_body = false;
    for line in &lines[1..end_idx] {
        let line = line.trim();
        if !in_body {
            if line.is_empty() {
                if !headers.is_empty() {
                    in_body = true;
                }
                continue;
            }
            if line.contains(':') {
                headers.push(line.to_string());
                continue;
            }
            in_body = true;
        }
        if line.is_empty() {
            continue;
        }
        body.extend(line.chars().filter(|c| !c.is_whitespace()));
    }

    let mut out = String::with_capacity(block.len() + 16);
    out.push_str(begin_line);
    out.push('\n');
    for header in &headers {
        out.push_str(header);
        out.push('\n');
    }
    if !headers.is_empty() {
        out.push('\n');
    }
    let mut col = 0usize;
    for ch in body.chars() {
        if col == 64 {
            out.push('\n');
            col = 0;
        }
        out.push(ch);
        col += 1;
    }
    if col > 0 {
        out.push('\n');
    }
    out.push_str(end_line);
    out.push('\n');
    out
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

#[cfg(test)]
mod tests {
    use super::*;

    fn rewrap_pem(pem: &str, width: usize) -> String {
        let lines: Vec<&str> = pem.lines().collect();
        let begin = lines
            .iter()
            .find(|l| l.starts_with("-----BEGIN "))
            .copied()
            .unwrap();
        let end = lines
            .iter()
            .find(|l| l.starts_with("-----END "))
            .copied()
            .unwrap();
        let body: String = lines
            .iter()
            .filter(|l| !l.starts_with("-----"))
            .flat_map(|l| l.chars())
            .collect();
        let mut wrapped = String::new();
        for (i, ch) in body.chars().enumerate() {
            if i > 0 && i % width == 0 {
                wrapped.push('\n');
            }
            wrapped.push(ch);
        }
        format!("{begin}\n{wrapped}\n{end}\n")
    }

    #[test]
    fn load_public_key_accepts_wide_base64_lines() {
        let (_, public_key) = generate_rsa_keypair(2048).unwrap();
        let pem = export_public_key_pem(&public_key).unwrap();
        let wide = rewrap_pem(&pem, 72);
        assert_ne!(wide, pem);

        let loaded = load_public_key(&wide).unwrap();
        assert_eq!(export_public_key_pem(&loaded).unwrap(), pem);
    }
}
