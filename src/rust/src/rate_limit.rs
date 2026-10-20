//! In-memory sliding-window rate limiter — mirrors `pubby._rate_limit`.

use std::collections::HashMap;
use std::sync::Mutex;
use std::time::{Duration, Instant};

use crate::error::{ActivityPubError, Result};

/// Simple in-memory per-key sliding window rate limiter.
///
/// `max_requests` requests are allowed within `window` per key (typically
/// an IP address).
pub struct RateLimiter {
    /// Maximum number of requests allowed within the window.
    pub max_requests: usize,
    /// Length of the sliding window.
    pub window: Duration,
    requests: Mutex<HashMap<String, Vec<Instant>>>,
}

impl Default for RateLimiter {
    fn default() -> Self {
        RateLimiter::new(60, Duration::from_secs_f64(60.0))
    }
}

impl RateLimiter {
    /// Create a rate limiter allowing `max_requests` per `window` per key.
    pub fn new(max_requests: usize, window: Duration) -> Self {
        RateLimiter {
            max_requests,
            window,
            requests: Mutex::new(HashMap::new()),
        }
    }

    /// The window length in seconds (Python `window_seconds` parity).
    pub fn window_seconds(&self) -> f64 {
        self.window.as_secs_f64()
    }

    fn cleanup(&self, map: &mut HashMap<String, Vec<Instant>>, key: &str, now: Instant) {
        let cutoff = now - self.window;
        if let Some(timestamps) = map.get_mut(key) {
            timestamps.retain(|t| *t >= cutoff);
            if timestamps.is_empty() {
                map.remove(key);
            }
        }
    }

    /// Check whether a request from `key` is allowed, recording it.
    ///
    /// Returns [`ActivityPubError::RateLimit`] when the limit is exceeded.
    pub fn check(&self, key: &str) -> Result<()> {
        let now = Instant::now();
        let mut map = self.requests.lock().unwrap_or_else(|e| e.into_inner());
        self.cleanup(&mut map, key, now);
        let timestamps = map.entry(key.to_string()).or_default();
        if timestamps.len() >= self.max_requests {
            return Err(ActivityPubError::RateLimit(format!(
                "Rate limit exceeded for {key}: {} requests per {:.1}s",
                self.max_requests,
                self.window_seconds()
            )));
        }
        timestamps.push(now);
        Ok(())
    }

    /// Check whether a request from `key` would be allowed without
    /// recording it.
    pub fn is_allowed(&self, key: &str) -> bool {
        let now = Instant::now();
        let mut map = self.requests.lock().unwrap_or_else(|e| e.into_inner());
        self.cleanup(&mut map, key, now);
        map.get(key).map(|v| v.len()).unwrap_or(0) < self.max_requests
    }

    /// Reset rate limit state: `Some(key)` resets only that key, `None`
    /// resets all keys.
    pub fn reset(&self, key: Option<&str>) {
        let mut map = self.requests.lock().unwrap_or_else(|e| e.into_inner());
        match key {
            None => map.clear(),
            Some(k) => {
                map.remove(k);
            }
        }
    }
}
