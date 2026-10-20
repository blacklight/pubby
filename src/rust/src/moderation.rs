//! Instance-level moderation helpers — domain allow/block lists.
//! Mirrors `pubby.moderation`.

/// Normalize a domain or URL for allow/block comparisons.
///
/// Strips the scheme, path, port and credentials, then lower-cases the
/// hostname. Returns `""` for empty or unparseable input.
pub fn normalize_domain(value: &str) -> String {
    let candidate = value.trim().to_lowercase();
    if candidate.is_empty() {
        return String::new();
    }

    // Prefix bare hosts with a dummy scheme so they parse with the host
    // in the netloc position (`url` requires a scheme, unlike urlparse).
    let candidate = if candidate.contains("://") {
        candidate
    } else {
        format!("scheme://{candidate}")
    };

    url::Url::parse(&candidate)
        .ok()
        .and_then(|u| {
            // Match Python's `urlparse().hostname`: bare IPv6 (no
            // brackets) and raw-unicode IDN (no punycode).
            match u.host()? {
                url::Host::Ipv6(ip) => Some(ip.to_string()),
                url::Host::Ipv4(ip) => Some(ip.to_string()),
                url::Host::Domain(d) => Some(idna::domain_to_unicode(d).0.to_lowercase()),
            }
        })
        .map(|h| h.trim().to_string())
        .unwrap_or_default()
}

/// Extract the normalized domain from an actor URL, inbox URL, or bare
/// domain.
pub fn extract_domain(url_or_actor: &str) -> String {
    normalize_domain(url_or_actor)
}

/// Return `true` when `domain` is blocked or excluded by an allow-list.
///
/// - An empty or absent allow-list means "allow all" (except explicit
///   blocks).
/// - Blocked domains take precedence over allowed domains.
/// - Comparisons are case-insensitive and ignore URL schemes, paths and
///   ports on both sides.
pub fn is_domain_blocked(
    domain: &str,
    allowed: Option<&[String]>,
    blocked: Option<&[String]>,
) -> bool {
    let allowed_empty = allowed.map(|a| a.is_empty()).unwrap_or(true);
    let blocked_empty = blocked.map(|b| b.is_empty()).unwrap_or(true);
    if allowed_empty && blocked_empty {
        return false;
    }

    let normalized = normalize_domain(domain);
    if normalized.is_empty() {
        return false;
    }

    if let Some(blocked) = blocked {
        if blocked.iter().any(|d| normalize_domain(d) == normalized) {
            return true;
        }
    }

    match allowed {
        Some(list) if !list.is_empty() => !list.iter().any(|d| normalize_domain(d) == normalized),
        _ => false,
    }
}
