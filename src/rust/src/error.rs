//! Error types for the pubby crate — mirrors `pubby._exceptions`.

/// Result type alias for pubby operations.
pub type Result<T> = std::result::Result<T, ActivityPubError>;

/// Base error type for ActivityPub operations.
///
/// Mirrors the Python exception hierarchy: the variants correspond to
/// `ActivityPubError`, `SignatureVerificationError`, `DeliveryError`,
/// `AttributionMismatch`, and `RateLimitError`.
#[derive(Debug, thiserror::Error)]
pub enum ActivityPubError {
    /// Generic ActivityPub error (mirrors `ActivityPubError`).
    #[error("{0}")]
    General(String),

    /// Raised when an HTTP signature cannot be verified.
    #[error("HTTP signature verification failed: {0}")]
    SignatureVerification(String),

    /// Raised when activity delivery to a remote inbox fails.
    #[error("Delivery to {inbox_url} failed: {message}")]
    Delivery {
        /// The remote inbox URL that delivery failed for.
        inbox_url: String,
        /// Description of the failure.
        message: String,
    },

    /// Raised when an incoming object's attribution cannot be trusted.
    #[error("Object attribution mismatch: {0}")]
    AttributionMismatch(String),

    /// Raised when a rate limit is exceeded.
    #[error("Rate limit exceeded: {0}")]
    RateLimit(String),

    /// Raised when a storage backend does not support an operation
    /// (mirrors Python's `NotImplementedError` fallbacks).
    #[error("Operation not supported: {0}")]
    Unsupported(String),

    /// Raised for invalid values (mirrors Python's `ValueError`).
    #[error("{0}")]
    InvalidValue(String),

    /// Wraps an I/O error.
    #[error("I/O error: {0}")]
    Io(#[from] std::io::Error),

    /// Wraps a JSON serialization/deserialization error.
    #[error("JSON error: {0}")]
    Json(#[from] serde_json::Error),
}

impl ActivityPubError {
    /// Construct a [`SignatureVerification`] error with the default message
    /// used when verification fails.
    pub fn signature_failed() -> Self {
        ActivityPubError::SignatureVerification("signature did not verify".to_string())
    }

    /// Whether this error is a signature verification failure.
    pub fn is_signature_verification(&self) -> bool {
        matches!(self, ActivityPubError::SignatureVerification(_))
    }
}

impl From<rsa::Error> for ActivityPubError {
    fn from(e: rsa::Error) -> Self {
        ActivityPubError::General(format!("RSA error: {e}"))
    }
}

impl From<rsa::pkcs8::Error> for ActivityPubError {
    fn from(e: rsa::pkcs8::Error) -> Self {
        ActivityPubError::General(format!("PKCS#8 error: {e}"))
    }
}

impl From<ureq::Error> for ActivityPubError {
    fn from(e: ureq::Error) -> Self {
        ActivityPubError::General(format!("HTTP error: {e}"))
    }
}
