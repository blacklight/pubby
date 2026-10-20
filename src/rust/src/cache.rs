//! Short-TTL document cache with request coalescing (single-flight) —
//! mirrors `pubby.cache`.
//!
//! - [`DocumentCache`] — the public façade. [`DocumentCache::get_or_render`]
//!   returns a cached document or invokes the render once: concurrent
//!   callers share a single in-flight render. `None` render results are
//!   cached as misses for a short TTL. When `stale_factor` is non-zero a
//!   slightly expired entry is served on render errors (stale-if-error).
//! - [`DocumentStore`] — the pluggable backend trait.
//!   [`InMemoryDocumentStore`] (default) is a bounded LRU per process;
//!   `RedisDocumentStore` (feature `redis`) shares entries across processes.
//! - [`CachedResponse`] — an immutable, serializable response fragment
//!   (status, serialized body, media type, headers, ETag).
//! - [`cache_headers`]/[`etag_matches`] — HTTP emission helpers.
//!
//! Cache keys are vectors of string elements (Python: tuples). Element-wise
//! prefix invalidation avoids the substring collisions flat `a:b:c` keys
//! produce.

use std::any::Any;
use std::collections::{HashMap, HashSet};
use std::sync::{Arc, Condvar, Mutex};
use std::time::{Duration, Instant, SystemTime};

use base64::Engine;
use serde_json::{json, Value};
use sha1::Digest as _;

use crate::error::ActivityPubError;

/// A cache key — a list of string elements.
pub type CacheKey = Vec<String>;

/// Marker stored in place of a value for cached misses.
#[derive(Debug)]
struct CachedMiss;

/// Type-erased cached payload.
pub type CacheValue = Arc<dyn Any + Send + Sync>;

/// Sentinel distinguishing "no entry" from a cached miss.
#[derive(Debug)]
pub enum Lookup {
    /// Nothing stored.
    Absent,
    /// A cached miss (`None` render result).
    Miss,
    /// A cached value.
    Hit(CacheValue),
}

/// Default cap on the TTL of cached misses.
pub const DEFAULT_MISS_TTL: f64 = 15.0;

/// Default maximum number of entries in [`InMemoryDocumentStore`].
pub const DEFAULT_MAX_ENTRIES: usize = 10_000;

/// Default stale-if-error factor.
pub const DEFAULT_STALE_FACTOR: f64 = 5.0;

/// An immutable, serializable HTTP response fragment stored in the cache.
#[derive(Debug, Clone)]
pub struct CachedResponse {
    /// HTTP status code.
    pub status: u16,
    /// Serialized body bytes.
    pub body: Vec<u8>,
    /// Media type of the body.
    pub media_type: String,
    /// Extra headers (e.g. `Location` for redirects).
    pub headers: HashMap<String, String>,
    /// Precomputed ETag (unquoted).
    pub etag: String,
}

impl CachedResponse {
    /// Wrap a JSON-serializable document (or pre-serialized bytes).
    pub fn document(
        doc: &Value,
        media_type: &str,
        status: u16,
        headers: HashMap<String, String>,
    ) -> Self {
        // Python-compatible payload: `json.dumps` body + SHA-1 ETag, so
        // Rust and Python processes sharing a document store produce
        // identical bytes and validators.
        let body = crate::model::dumps_python(doc).into_bytes();
        let etag = format!("{:x}", sha1::Sha1::digest(&body));
        CachedResponse {
            status,
            body,
            media_type: media_type.to_string(),
            headers,
            etag,
        }
    }

    /// Wrap a redirect target (stored as a `Location` header).
    pub fn redirect(url: &str, status: u16) -> Self {
        CachedResponse {
            status,
            body: Vec::new(),
            media_type: "application/json".to_string(),
            headers: [("Location".to_string(), url.to_string())]
                .into_iter()
                .collect(),
            etag: String::new(),
        }
    }

    /// Serialize to a JSON-compatible value (for cross-process stores).
    pub fn to_jsonable(&self) -> Value {
        json!({
            "__cached_response__": true,
            "status": self.status,
            "media_type": self.media_type,
            "headers": self.headers,
            "etag": self.etag,
            "body_b64": base64::engine::general_purpose::STANDARD.encode(&self.body),
        })
    }

    /// Inverse of [`to_jsonable`](Self::to_jsonable).
    pub fn from_jsonable(data: &Value) -> Self {
        let body = data
            .get("body_b64")
            .and_then(|v| v.as_str())
            .and_then(|s| base64::engine::general_purpose::STANDARD.decode(s).ok())
            .unwrap_or_default();
        let headers = data
            .get("headers")
            .and_then(|v| serde_json::from_value(v.clone()).ok())
            .unwrap_or_default();
        CachedResponse {
            status: data.get("status").and_then(|v| v.as_u64()).unwrap_or(200) as u16,
            body,
            media_type: data
                .get("media_type")
                .and_then(|v| v.as_str())
                .unwrap_or("application/json")
                .to_string(),
            headers,
            etag: data
                .get("etag")
                .and_then(|v| v.as_str())
                .unwrap_or_default()
                .to_string(),
        }
    }
}

/// HTTP headers for a cached dereference response.
///
/// Emits `Cache-Control` matching the cache TTL, `Vary: Accept` and, when
/// `etag` is given, an `ETag` for conditional requests.
pub fn cache_headers(
    ttl: f64,
    etag: &str,
    public: bool,
    vary: &[&str],
    stale: bool,
) -> HashMap<String, String> {
    let cache_control = if ttl > 0.0 {
        format!(
            "{}, max-age={}",
            if public { "public" } else { "private" },
            ttl as i64
        )
    } else if stale {
        "max-age=0, must-revalidate".to_string()
    } else {
        "no-store".to_string()
    };
    let mut headers = HashMap::new();
    headers.insert("Cache-Control".to_string(), cache_control);
    if !vary.is_empty() {
        headers.insert("Vary".to_string(), vary.join(", "));
    }
    if !etag.is_empty() {
        headers.insert("ETag".to_string(), format!("\"{etag}\""));
    }
    headers
}

/// Check an `If-None-Match` header against an ETag (weak comparison).
pub fn etag_matches(if_none_match: Option<&str>, etag: &str) -> bool {
    let (Some(header), false) = (if_none_match, etag.is_empty()) else {
        return false;
    };
    if header.is_empty() {
        return false;
    }
    let quoted = format!("\"{etag}\"");
    header
        .split(',')
        .map(|c| c.trim())
        .any(|c| c == "*" || c == quoted || c == format!("W/{quoted}"))
}

/// A stored document with freshness bookkeeping.
///
/// Deadlines are wall-clock (unix seconds) so entries can be shared across
/// processes through a common store.
#[derive(Debug, Clone)]
pub struct CacheEntry {
    /// The stored payload.
    pub value: CacheValue,
    /// `SystemTime` deadline for fresh hits (unix seconds).
    pub fresh_until: f64,
    /// Hard eviction deadline (unix seconds).
    pub drop_after: f64,
}

fn now_unix() -> f64 {
    SystemTime::now()
        .duration_since(SystemTime::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

impl CacheEntry {
    /// Whether the entry is still fresh.
    pub fn fresh(&self) -> bool {
        self.fresh_until > now_unix()
    }

    /// Expired but still inside the stale-if-error grace window.
    pub fn usable_stale(&self) -> bool {
        let now = now_unix();
        self.fresh_until <= now && now < self.drop_after
    }
}

/// Storage backend interface for [`DocumentCache`].
pub trait DocumentStore: Send + Sync {
    /// Read an entry — which may be expired but retained for
    /// stale-if-error serving — or `None` when nothing is stored.
    fn get(&self, key: &CacheKey) -> Option<CacheEntry>;
    /// Store `value` under `key` (`ttl` seconds fresh, `stale_ttl` extra
    /// seconds retained for stale-if-error).
    fn set(&self, key: &CacheKey, value: CacheValue, ttl: f64, stale_ttl: f64);
    /// Drop the given keys exactly; returns the number dropped.
    fn delete(&self, keys: &[CacheKey]) -> usize;
    /// Drop keys whose elements start with any of `prefixes`.
    fn delete_prefix(&self, prefixes: &[CacheKey]) -> usize;
    /// Drop keys containing any of `segments` as an element.
    fn delete_segment(&self, segments: &[String]) -> usize;
    /// Drop every entry.
    fn clear(&self);
}

/// Bounded in-process LRU + TTL store.
pub struct InMemoryDocumentStore {
    /// Bound on the number of cached entries.
    pub max_entries: usize,
    // (key, LRU tick order) — we keep an OrderedDict equivalent via a
    // HashMap + access counter.
    entries: Mutex<InnerStore>,
    unswept_inserts: Mutex<usize>,
}

struct InnerStore {
    map: HashMap<CacheKey, (CacheEntry, u64)>,
    tick: u64,
}

impl InMemoryDocumentStore {
    /// How many over-capacity inserts pass with a plain LRU pop before the
    /// store re-scans for expired entries.
    const EVICT_SWEEP_INTERVAL: usize = 32;

    /// Create a store bounded to `max_entries`.
    pub fn new(max_entries: usize) -> Self {
        InMemoryDocumentStore {
            max_entries: max_entries.max(1),
            entries: Mutex::new(InnerStore {
                map: HashMap::new(),
                tick: 0,
            }),
            unswept_inserts: Mutex::new(0),
        }
    }

    /// Drop every entry past its `drop_after` deadline.
    pub fn sweep(&self) -> usize {
        let now = now_unix();
        let mut inner = self.entries.lock().unwrap_or_else(|e| e.into_inner());
        let doomed: Vec<CacheKey> = inner
            .map
            .iter()
            .filter(|(_, (e, _))| e.drop_after <= now)
            .map(|(k, _)| k.clone())
            .collect();
        for k in &doomed {
            inner.map.remove(k);
        }
        doomed.len()
    }

    /// Number of stored entries.
    pub fn len(&self) -> usize {
        self.entries
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .map
            .len()
    }

    /// Whether the store holds no entries.
    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

impl Default for InMemoryDocumentStore {
    fn default() -> Self {
        Self::new(DEFAULT_MAX_ENTRIES)
    }
}

impl DocumentStore for InMemoryDocumentStore {
    fn get(&self, key: &CacheKey) -> Option<CacheEntry> {
        let mut inner = self.entries.lock().unwrap_or_else(|e| e.into_inner());
        let entry = inner.map.get(key).cloned();
        match entry {
            Some((e, _)) if e.drop_after <= now_unix() => {
                inner.map.remove(key);
                None
            }
            Some((e, _)) => {
                inner.tick += 1;
                let tick = inner.tick;
                if let Some(slot) = inner.map.get_mut(key) {
                    slot.1 = tick;
                }
                Some(e)
            }
            None => None,
        }
    }

    fn set(&self, key: &CacheKey, value: CacheValue, ttl: f64, stale_ttl: f64) {
        let now = now_unix();
        let entry = CacheEntry {
            value,
            fresh_until: now + ttl,
            drop_after: now + ttl + stale_ttl,
        };
        let mut inner = self.entries.lock().unwrap_or_else(|e| e.into_inner());
        inner.tick += 1;
        let tick = inner.tick;
        inner.map.insert(key.clone(), (entry, tick));
        if inner.map.len() > self.max_entries {
            let mut unswept = self
                .unswept_inserts
                .lock()
                .unwrap_or_else(|e| e.into_inner());
            *unswept += 1;
            if *unswept >= Self::EVICT_SWEEP_INTERVAL {
                *unswept = 0;
                // Drop expired entries first, then the least-recently-used.
                let doomed: Vec<CacheKey> = inner
                    .map
                    .iter()
                    .filter(|(_, (e, _))| e.drop_after <= now)
                    .map(|(k, _)| k.clone())
                    .collect();
                for k in doomed {
                    inner.map.remove(&k);
                }
                while inner.map.len() > self.max_entries {
                    if let Some(oldest) = inner
                        .map
                        .iter()
                        .min_by_key(|(_, (_, tick))| *tick)
                        .map(|(k, _)| k.clone())
                    {
                        inner.map.remove(&oldest);
                    } else {
                        break;
                    }
                }
            } else if let Some(oldest) = inner
                .map
                .iter()
                .min_by_key(|(_, (_, tick))| *tick)
                .map(|(k, _)| k.clone())
            {
                inner.map.remove(&oldest);
            }
        }
    }

    fn delete(&self, keys: &[CacheKey]) -> usize {
        let mut inner = self.entries.lock().unwrap_or_else(|e| e.into_inner());
        let mut dropped = 0;
        for key in keys {
            if inner.map.remove(key).is_some() {
                dropped += 1;
            }
        }
        dropped
    }

    fn delete_prefix(&self, prefixes: &[CacheKey]) -> usize {
        let mut inner = self.entries.lock().unwrap_or_else(|e| e.into_inner());
        let doomed: Vec<CacheKey> = inner
            .map
            .keys()
            .filter(|k| {
                prefixes
                    .iter()
                    .any(|p| p.len() <= k.len() && k[..p.len()] == p[..])
            })
            .cloned()
            .collect();
        for k in &doomed {
            inner.map.remove(k);
        }
        doomed.len()
    }

    fn delete_segment(&self, segments: &[String]) -> usize {
        let wanted: HashSet<&str> = segments.iter().map(String::as_str).collect();
        let mut inner = self.entries.lock().unwrap_or_else(|e| e.into_inner());
        let doomed: Vec<CacheKey> = inner
            .map
            .keys()
            .filter(|k| k.iter().any(|e| wanted.contains(e.as_str())))
            .cloned()
            .collect();
        for k in &doomed {
            inner.map.remove(k);
        }
        doomed.len()
    }

    fn clear(&self) {
        self.entries
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .map
            .clear();
    }
}

/// Shared slot for an in-flight render (single-flight).
struct Slot {
    state: Mutex<Option<std::result::Result<CacheValue, Arc<ActivityPubError>>>>,
    cv: Condvar,
}

impl Slot {
    fn new() -> Self {
        Slot {
            state: Mutex::new(None),
            cv: Condvar::new(),
        }
    }
}

/// TTL document cache with single-flight request coalescing.
///
/// Concurrent fetches of the same key share one in-flight render: the
/// first caller runs it, waiters attach to its result.
pub struct DocumentCache {
    /// The underlying store backend.
    pub store: Arc<dyn DocumentStore>,
    /// TTL used when a call passes `ttl=None`.
    pub default_ttl: f64,
    /// Cap on the TTL of cached `None` results.
    pub miss_ttl: f64,
    /// Multiplier on the TTL defining how long an expired entry is
    /// retained for stale-if-error serving (`0` disables).
    pub stale_factor: f64,
    /// Seconds a *waiter* thread blocks on a shared in-flight render
    /// before falling back to stale data or erroring. `None` waits forever.
    pub wait_timeout: Option<f64>,
    state: Mutex<CacheState>,
}

struct CacheState {
    generation: u64,
    inflight: HashMap<CacheKey, Arc<Slot>>,
    #[cfg(feature = "async")]
    inflight_async: HashMap<CacheKey, AsyncShared>,
}

/// A completed async render shared by all waiters (cloneable future).
#[cfg(feature = "async")]
type AsyncShared = futures::future::Shared<
    futures::channel::oneshot::Receiver<std::result::Result<CacheValue, Arc<ActivityPubError>>>,
>;

impl Default for DocumentCache {
    fn default() -> Self {
        Self::new(
            None,
            DEFAULT_MAX_ENTRIES,
            60.0,
            DEFAULT_MISS_TTL,
            DEFAULT_STALE_FACTOR,
            Some(30.0),
        )
    }
}

impl DocumentCache {
    /// Create a document cache.
    ///
    /// :param store: Backend; `None` uses a bounded
    ///     [`InMemoryDocumentStore`].
    /// :param max_entries: Bound for the default in-memory store.
    /// :param default_ttl: TTL used when a call passes `ttl=None`.
    /// :param miss_ttl: Cap on the TTL of cached `None` results.
    /// :param stale_factor: Multiplier on the TTL for stale-if-error
    ///     serving (`0` disables).
    /// :param wait_timeout: Waiter timeout in seconds.
    pub fn new(
        store: Option<Arc<dyn DocumentStore>>,
        max_entries: usize,
        default_ttl: f64,
        miss_ttl: f64,
        stale_factor: f64,
        wait_timeout: Option<f64>,
    ) -> Self {
        DocumentCache {
            store: store.unwrap_or_else(|| Arc::new(InMemoryDocumentStore::new(max_entries))),
            default_ttl,
            miss_ttl,
            stale_factor,
            wait_timeout,
            state: Mutex::new(CacheState {
                generation: 0,
                inflight: HashMap::new(),
                #[cfg(feature = "async")]
                inflight_async: HashMap::new(),
            }),
        }
    }

    fn store_get(&self, key: &CacheKey) -> Option<CacheEntry> {
        match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| self.store.get(key))) {
            Ok(e) => e,
            Err(_) => {
                log::warn!("Document store get failed for {key:?}");
                None
            }
        }
    }

    fn store_set(&self, key: &CacheKey, value: CacheValue, ttl: f64, stale_ttl: f64) {
        if std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            self.store.set(key, value, ttl, stale_ttl)
        }))
        .is_err()
        {
            log::warn!("Document store set failed for {key:?}");
        }
    }

    /// Convert an entry to a [`Lookup`].
    fn classify(entry: CacheEntry) -> Lookup {
        if entry.value.is::<CachedMiss>() {
            Lookup::Miss
        } else {
            Lookup::Hit(entry.value)
        }
    }

    fn fresh(&self, key: &CacheKey) -> Lookup {
        match self.store_get(key) {
            Some(e) if e.fresh() => Self::classify(e),
            _ => Lookup::Absent,
        }
    }

    fn stale(&self, key: &CacheKey) -> Lookup {
        if self.stale_factor <= 0.0 {
            return Lookup::Absent;
        }
        match self.store_get(key) {
            Some(e) if e.usable_stale() => Self::classify(e),
            _ => Lookup::Absent,
        }
    }

    /// Seconds of freshness left on the stored entry, `0` when absent or
    /// expired.
    pub fn ttl_remaining(&self, key: &CacheKey) -> f64 {
        match self.store_get(key) {
            Some(e) => (e.fresh_until - now_unix()).max(0.0),
            None => 0.0,
        }
    }

    fn store_result(&self, key: &CacheKey, value: CacheValue, ttl: f64, miss_ttl: f64) {
        let effective_ttl = if value.is::<CachedMiss>() {
            ttl.min(miss_ttl)
        } else {
            ttl
        };
        if effective_ttl > 0.0 {
            self.store_set(key, value, effective_ttl, effective_ttl * self.stale_factor);
        }
    }

    /// Downcast a [`Lookup`] into the typed result.
    fn unwrap_lookup<V: Clone + Send + Sync + 'static>(lookup: Lookup) -> Option<Option<V>> {
        match lookup {
            Lookup::Absent => None,
            Lookup::Miss => Some(None),
            Lookup::Hit(v) => v
                .clone()
                .downcast::<V>()
                .ok()
                .map(|arc| Some((*arc).clone())),
        }
    }

    /// Return the cached value for `key` or call `render()` once.
    ///
    /// Concurrent callers share a single in-flight render; waiters block
    /// on its result. `render` returns `Ok(Some(v))` for a value,
    /// `Ok(None)` for a cached miss, `Err` to propagate the error.
    pub fn get_or_render<V, F>(
        &self,
        key: CacheKey,
        render: F,
        ttl: Option<f64>,
        miss_ttl: Option<f64>,
    ) -> crate::error::Result<Option<V>>
    where
        V: Clone + Send + Sync + 'static,
        F: FnOnce() -> crate::error::Result<Option<V>>,
    {
        let ttl = ttl.unwrap_or(self.default_ttl);
        let miss_ttl = miss_ttl.unwrap_or(self.miss_ttl);

        if ttl > 0.0 {
            if let Some(v) = Self::unwrap_lookup::<V>(self.fresh(&key)) {
                return Ok(v);
            }
        }

        let (slot, owner, generation) = {
            let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
            if let Some(slot) = state.inflight.get(&key) {
                (slot.clone(), false, 0)
            } else {
                // Re-check under the lock: an owner may have stored its
                // result between the unlocked check and now.
                if ttl > 0.0 {
                    if let Some(v) = Self::unwrap_lookup::<V>(self.fresh(&key)) {
                        return Ok(v);
                    }
                }
                let slot = Arc::new(Slot::new());
                state.inflight.insert(key.clone(), slot.clone());
                (slot, true, state.generation)
            }
        };

        if !owner {
            // Waiter path. Predicate loop: the result is re-checked
            // *before* every wait so a `notify_all` that fired between
            // the slot lookup and the lock acquisition is not lost.
            let deadline = self.wait_timeout.map(|t| {
                Instant::now() + Duration::try_from_secs_f64(t.max(0.0)).unwrap_or(Duration::ZERO)
            });
            let mut guard = slot.state.lock().unwrap_or_else(|e| e.into_inner());
            loop {
                if let Some(result) = guard.as_ref() {
                    return match result {
                        Ok(v) => Ok(Self::downcast_payload::<V>(v)),
                        Err(e) => {
                            if let Some(v) = Self::unwrap_lookup::<V>(self.stale(&key)) {
                                return Ok(v);
                            }
                            Err(clone_error(e))
                        }
                    };
                }
                guard = match deadline {
                    Some(d) => {
                        let remaining = d.saturating_duration_since(Instant::now());
                        if remaining.is_zero() {
                            if let Some(v) = Self::unwrap_lookup::<V>(self.stale(&key)) {
                                return Ok(v);
                            }
                            return Err(ActivityPubError::General(format!(
                                "wait timeout on shared render for {key:?}"
                            )));
                        }
                        slot.cv
                            .wait_timeout(guard, remaining)
                            .unwrap_or_else(|e| e.into_inner())
                            .0
                    }
                    None => slot.cv.wait(guard).unwrap_or_else(|e| e.into_inner()),
                };
            }
        }

        // Owner path
        let result = std::panic::catch_unwind(std::panic::AssertUnwindSafe(render));
        let outcome: std::result::Result<CacheValue, Arc<ActivityPubError>> = match result {
            Ok(Ok(Some(v))) => Ok(Arc::new(v)),
            Ok(Ok(None)) => Ok(Arc::new(CachedMiss)),
            Ok(Err(e)) => Err(Arc::new(e)),
            Err(_) => Err(Arc::new(ActivityPubError::General(
                "render panicked".to_string(),
            ))),
        };

        // Serve stale-if-error where configured. Mirrors the waiter
        // path: a stale hit *or* a stale cached miss is served.
        if outcome.is_err() {
            if let Some(stale) = Self::unwrap_lookup::<V>(self.stale(&key)) {
                let payload: CacheValue = match &stale {
                    Some(v) => Arc::new(v.clone()),
                    None => Arc::new(CachedMiss),
                };
                complete(&slot, Ok(payload));
                self.pop_inflight(&key);
                return Ok(stale);
            }
        }

        complete(&slot, outcome.clone());

        // A generation bump means an invalidation landed mid-render: the
        // result may be stale and must not be cached. The state lock is
        // held across the store so an invalidation cannot interleave
        // between the generation check and the write.
        if let Ok(v) = &outcome {
            let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
            state.inflight.remove(&key);
            if ttl > 0.0 && state.generation == generation {
                self.store_result(&key, v.clone(), ttl, miss_ttl);
            }
        } else {
            self.pop_inflight(&key);
        }

        match outcome {
            Ok(v) => Ok(Self::downcast_payload::<V>(&v)),
            Err(e) => Err(clone_error(&e)),
        }
    }

    fn downcast_payload<V: Clone + Send + Sync + 'static>(v: &CacheValue) -> Option<V> {
        v.clone().downcast::<V>().ok().map(|arc| (*arc).clone())
    }

    fn pop_inflight(&self, key: &CacheKey) {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .inflight
            .remove(key);
    }

    /// Discard the results of all in-flight renders.
    pub fn bump_generation(&self) {
        self.state
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .generation += 1;
    }

    /// Drop the given keys exactly.
    pub fn invalidate(&self, keys: &[CacheKey]) -> usize {
        self.bump_generation();
        match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| self.store.delete(keys))) {
            Ok(n) => n,
            Err(_) => {
                log::warn!("Document store delete failed");
                0
            }
        }
    }

    /// Drop keys whose leading elements equal any of `prefixes`.
    pub fn invalidate_prefix(&self, prefixes: &[CacheKey]) -> usize {
        self.bump_generation();
        match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            self.store.delete_prefix(prefixes)
        })) {
            Ok(n) => n,
            Err(_) => {
                log::warn!("Document store delete_prefix failed");
                0
            }
        }
    }

    /// Drop keys containing any of `segments` as an element.
    pub fn invalidate_segment(&self, segments: &[String]) -> usize {
        if segments.is_empty() {
            return 0;
        }
        self.bump_generation();
        match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            self.store.delete_segment(segments)
        })) {
            Ok(n) => n,
            Err(_) => {
                log::warn!("Document store delete_segment failed");
                0
            }
        }
    }

    /// Drop every cached entry and in-flight bookkeeping.
    pub fn clear(&self) {
        self.bump_generation();
        let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
        state.inflight.clear();
        #[cfg(feature = "async")]
        state.inflight_async.clear();
        drop(state);
        let _ = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| self.store.clear()));
    }

    /// Async variant of [`get_or_render`](Self::get_or_render) (feature
    /// `async`).
    ///
    /// Concurrent callers share one in-flight render: the first caller
    /// runs `render()` and waiters attach to its result through a shared
    /// future instead of a condition variable. `wait_timeout` bounds the
    /// wait for async callers too (via `futures-timer`).
    #[cfg(feature = "async")]
    pub async fn get_or_render_async<V, F, Fut>(
        &self,
        key: CacheKey,
        render: F,
        ttl: Option<f64>,
        miss_ttl: Option<f64>,
    ) -> crate::error::Result<Option<V>>
    where
        V: Clone + Send + Sync + 'static,
        F: FnOnce() -> Fut,
        Fut: std::future::Future<Output = crate::error::Result<Option<V>>>,
    {
        use futures::channel::oneshot;
        use futures::future::FutureExt;
        use futures::select_biased;

        let ttl = ttl.unwrap_or(self.default_ttl);
        let miss_ttl = miss_ttl.unwrap_or(self.miss_ttl);

        if ttl > 0.0 {
            if let Some(v) = Self::unwrap_lookup::<V>(self.fresh(&key)) {
                return Ok(v);
            }
        }

        let (shared, owner_tx, generation) = {
            let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
            if let Some(shared) = state.inflight_async.get(&key) {
                (shared.clone(), None, 0u64)
            } else {
                if ttl > 0.0 {
                    if let Some(v) = Self::unwrap_lookup::<V>(self.fresh(&key)) {
                        return Ok(v);
                    }
                }
                let (tx, rx) = oneshot::channel();
                let shared = rx.shared();
                state.inflight_async.insert(key.clone(), shared.clone());
                (shared, Some(tx), state.generation)
            }
        };

        let Some(tx) = owner_tx else {
            // Waiter path
            let outcome = match self.wait_timeout {
                Some(t) => {
                    let delay = Duration::try_from_secs_f64(t.max(0.0)).unwrap_or(Duration::ZERO);
                    select_biased! {
                        res = shared.fuse() => Some(res),
                        _ = futures_timer::Delay::new(delay).fuse() => None,
                    }
                }
                None => Some(shared.await),
            };
            return match outcome {
                Some(Ok(result)) => match result {
                    Ok(v) => Ok(Self::downcast_payload::<V>(&v)),
                    Err(e) => {
                        if let Some(v) = Self::unwrap_lookup::<V>(self.stale(&key)) {
                            return Ok(v);
                        }
                        Err(clone_error(&e))
                    }
                },
                Some(Err(_)) => {
                    if let Some(v) = Self::unwrap_lookup::<V>(self.stale(&key)) {
                        return Ok(v);
                    }
                    Err(ActivityPubError::General(format!(
                        "shared render for {key:?} dropped"
                    )))
                }
                None => {
                    if let Some(v) = Self::unwrap_lookup::<V>(self.stale(&key)) {
                        return Ok(v);
                    }
                    Err(ActivityPubError::General(format!(
                        "wait timeout on shared render for {key:?}"
                    )))
                }
            };
        };

        // Owner path
        let outcome: std::result::Result<CacheValue, Arc<ActivityPubError>> = match render().await {
            Ok(Some(v)) => Ok(Arc::new(v)),
            Ok(None) => Ok(Arc::new(CachedMiss)),
            Err(e) => Err(Arc::new(e)),
        };

        // Serve stale-if-error where configured (same as the sync path).
        if outcome.is_err() {
            if let Some(stale) = Self::unwrap_lookup::<V>(self.stale(&key)) {
                let payload: CacheValue = match &stale {
                    Some(v) => Arc::new(v.clone()),
                    None => Arc::new(CachedMiss),
                };
                let _ = tx.send(Ok(payload));
                self.state
                    .lock()
                    .unwrap_or_else(|e| e.into_inner())
                    .inflight_async
                    .remove(&key);
                return Ok(stale);
            }
        }

        let _ = tx.send(outcome.clone());

        // Same generation-check-then-store-under-lock as the sync path.
        if let Ok(v) = &outcome {
            let mut state = self.state.lock().unwrap_or_else(|e| e.into_inner());
            state.inflight_async.remove(&key);
            if ttl > 0.0 && state.generation == generation {
                self.store_result(&key, v.clone(), ttl, miss_ttl);
            }
        } else {
            self.state
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .inflight_async
                .remove(&key);
        }

        match outcome {
            Ok(v) => Ok(Self::downcast_payload::<V>(&v)),
            Err(e) => Err(clone_error(&e)),
        }
    }
}

fn complete(slot: &Slot, outcome: std::result::Result<CacheValue, Arc<ActivityPubError>>) {
    *slot.state.lock().unwrap_or_else(|e| e.into_inner()) = Some(outcome);
    slot.cv.notify_all();
}

fn clone_error(e: &ActivityPubError) -> ActivityPubError {
    match e {
        ActivityPubError::General(m) => ActivityPubError::General(m.clone()),
        ActivityPubError::SignatureVerification(m) => {
            ActivityPubError::SignatureVerification(m.clone())
        }
        ActivityPubError::Delivery { inbox_url, message } => ActivityPubError::Delivery {
            inbox_url: inbox_url.clone(),
            message: message.clone(),
        },
        ActivityPubError::AttributionMismatch(m) => {
            ActivityPubError::AttributionMismatch(m.clone())
        }
        ActivityPubError::RateLimit(m) => ActivityPubError::RateLimit(m.clone()),
        ActivityPubError::Unsupported(m) => ActivityPubError::Unsupported(m.clone()),
        ActivityPubError::InvalidValue(m) => ActivityPubError::InvalidValue(m.clone()),
        ActivityPubError::Io(e) => ActivityPubError::General(format!("I/O error: {e}")),
        ActivityPubError::Json(e) => {
            ActivityPubError::Json(serde_json::Error::io(std::io::Error::other(e.to_string())))
        }
    }
}

// ---------------------------------------------------------------------------
// Key builders for the built-in adapter routes
// ---------------------------------------------------------------------------

/// First element of every key the built-in adapter routes and the
/// handler's invalidations use.
pub const ROUTE_NAMESPACE: &str = "pubby";

/// Build a key (or key prefix) under pubby's adapter-route namespace.
pub fn route_key(elements: &[&str]) -> CacheKey {
    let mut key = vec![ROUTE_NAMESPACE.to_string()];
    key.extend(elements.iter().map(|s| s.to_string()));
    key
}

/// Cache key for the actor document.
pub fn actor_document_key() -> CacheKey {
    route_key(&["actor"])
}

/// Normalize a *valid* WebFinger `resource` parameter to `user@domain`.
pub fn normalize_webfinger_resource(resource: &str) -> String {
    let mut normalized = resource;
    if normalized.to_lowercase().starts_with("acct:") {
        normalized = &normalized[5..];
    }
    normalized.trim_start_matches('@').to_lowercase()
}

/// Cache key for a WebFinger `resource` parameter.
pub fn webfinger_key(resource: &str) -> CacheKey {
    route_key(&["webfinger", &normalize_webfinger_resource(resource)])
}

/// Cache key for the NodeInfo document.
pub fn nodeinfo_key() -> CacheKey {
    route_key(&["nodeinfo"])
}

/// Cache key for the NodeInfo discovery document.
pub fn nodeinfo_discovery_key() -> CacheKey {
    route_key(&["nodeinfo-discovery"])
}

/// Cache key for an outbox page.
pub fn outbox_key(limit: i64, offset: i64) -> CacheKey {
    route_key(&["outbox", &limit.to_string(), &offset.to_string()])
}

/// Cache key for the followers collection.
pub fn followers_key() -> CacheKey {
    route_key(&["followers"])
}

/// Cache key for the following collection.
pub fn following_key() -> CacheKey {
    route_key(&["following"])
}

/// Cache key for a quote authorization document.
pub fn quote_authorization_key(auth_id: &str) -> CacheKey {
    route_key(&["quote-authorization", auth_id])
}

// ---------------------------------------------------------------------------
// Redis-backed store (feature `redis`)
// ---------------------------------------------------------------------------

/// Redis-backed store — shares cached documents, and therefore
/// invalidations, across processes.
///
/// `client` is a `redis::Client`; single-flight coalescing stays
/// per-process. Values are JSON-serialized together with their deadlines;
/// `CachedResponse` values are supported natively, other payload types are
/// stored via their `serde_json` representation when serializable.
///
/// See the Python `RedisDocumentStore` docstring for the limits of the
/// cross-process invalidation guarantee.
#[cfg(feature = "redis")]
pub struct RedisDocumentStore {
    client: redis::Client,
    /// Key prefix in Redis.
    pub prefix: String,
}

#[cfg(feature = "redis")]
impl RedisDocumentStore {
    /// Default Redis key prefix.
    pub const DEFAULT_PREFIX: &'static str = "pubby:doc:";

    /// Create a store over `client` with the default key prefix.
    pub fn new(client: redis::Client) -> Self {
        RedisDocumentStore {
            client,
            prefix: Self::DEFAULT_PREFIX.to_string(),
        }
    }

    fn encode_key(&self, key: &CacheKey) -> String {
        use percent_encoding::{utf8_percent_encode, NON_ALPHANUMERIC};
        let body: Vec<String> = key
            .iter()
            .map(|e| utf8_percent_encode(e, NON_ALPHANUMERIC).to_string())
            .collect();
        format!("{}{}", self.prefix, body.join("/"))
    }

    fn decode_key(&self, encoded: &str) -> CacheKey {
        use percent_encoding::percent_decode_str;
        let body = &encoded[self.prefix.len()..];
        body.split('/')
            .map(|p| {
                percent_decode_str(p)
                    .decode_utf8()
                    .map(|s| s.to_string())
                    .unwrap_or_else(|_| p.to_string())
            })
            .collect()
    }

    fn scan(&self, pattern: &str) -> Vec<String> {
        use redis::Commands;
        let mut conn = match self.client.get_connection() {
            Ok(c) => c,
            Err(_) => return Vec::new(),
        };
        conn.scan_match(pattern)
            .map(|it| it.collect())
            .unwrap_or_default()
    }
}

#[cfg(feature = "redis")]
impl DocumentStore for RedisDocumentStore {
    fn get(&self, key: &CacheKey) -> Option<CacheEntry> {
        use redis::Commands;
        let mut conn = self.client.get_connection().ok()?;
        let raw: Option<String> = conn.get(self.encode_key(key)).ok()?;
        let raw = raw?;
        let envelope: Value = match serde_json::from_str(&raw) {
            Ok(v) => v,
            Err(_) => {
                let _ = conn.del::<_, ()>(self.encode_key(key));
                return None;
            }
        };
        let value = from_jsonable(envelope.get("v").cloned().unwrap_or(Value::Null));
        Some(CacheEntry {
            value,
            fresh_until: envelope.get("f").and_then(|v| v.as_f64()).unwrap_or(0.0),
            drop_after: envelope.get("d").and_then(|v| v.as_f64()).unwrap_or(0.0),
        })
    }

    fn set(&self, key: &CacheKey, value: CacheValue, ttl: f64, stale_ttl: f64) {
        use redis::Commands;
        let Ok(mut conn) = self.client.get_connection() else {
            return;
        };
        let now = now_unix();
        let envelope = json!({
            "v": to_jsonable(&value),
            "f": now + ttl,
            "d": now + ttl + stale_ttl,
        });
        // `set_ex` takes a *relative* TTL in seconds — the redis expiry
        // should match `drop_after` (now + ttl + stale_ttl).
        let expires_secs = (ttl + stale_ttl).ceil().max(1.0) as u64;
        let _: redis::RedisResult<()> =
            conn.set_ex(self.encode_key(key), envelope.to_string(), expires_secs);
    }

    fn delete(&self, keys: &[CacheKey]) -> usize {
        use redis::Commands;
        if keys.is_empty() {
            return 0;
        }
        let Ok(mut conn) = self.client.get_connection() else {
            return 0;
        };
        let encoded: Vec<String> = keys.iter().map(|k| self.encode_key(k)).collect();
        conn.del(encoded).unwrap_or(0)
    }

    fn delete_prefix(&self, prefixes: &[CacheKey]) -> usize {
        use redis::Commands;
        let mut doomed: HashSet<String> = HashSet::new();
        for prefix in prefixes {
            let encoded = self.encode_key(prefix);
            doomed.extend(self.scan(&encoded));
            doomed.extend(self.scan(&format!("{encoded}/*")));
        }
        if doomed.is_empty() {
            return 0;
        }
        let Ok(mut conn) = self.client.get_connection() else {
            return 0;
        };
        conn.del(doomed.into_iter().collect::<Vec<_>>())
            .unwrap_or(0)
    }

    fn delete_segment(&self, segments: &[String]) -> usize {
        use redis::Commands;
        let wanted: HashSet<&str> = segments.iter().map(String::as_str).collect();
        let doomed: Vec<String> = self
            .scan(&format!("{}*", self.prefix))
            .into_iter()
            .filter(|k| {
                let decoded = self.decode_key(k);
                decoded.iter().any(|e| wanted.contains(e.as_str()))
            })
            .collect();
        if doomed.is_empty() {
            return 0;
        }
        let Ok(mut conn) = self.client.get_connection() else {
            return 0;
        };
        conn.del(doomed).unwrap_or(0)
    }

    fn clear(&self) {
        use redis::Commands;
        let doomed = self.scan(&format!("{}*", self.prefix));
        if doomed.is_empty() {
            return;
        }
        if let Ok(mut conn) = self.client.get_connection() {
            let _: redis::RedisResult<usize> = conn.del(doomed);
        }
    }
}

#[cfg(feature = "redis")]
fn to_jsonable(value: &CacheValue) -> Value {
    if let Some(resp) = value.downcast_ref::<CachedResponse>() {
        return resp.to_jsonable();
    }
    if let Some(s) = value.downcast_ref::<String>() {
        return json!({"__bytes__": base64::engine::general_purpose::STANDARD.encode(s.as_bytes())});
    }
    if let Some(b) = value.downcast_ref::<Vec<u8>>() {
        return json!({"__raw_bytes__": base64::engine::general_purpose::STANDARD.encode(b)});
    }
    // Fallback: serde-serializable values pass through when they are
    // already stored as `serde_json::Value`.
    if let Some(v) = value.downcast_ref::<Value>() {
        return v.clone();
    }
    Value::Null
}

#[cfg(feature = "redis")]
fn from_jsonable(value: Value) -> CacheValue {
    if let Some(m) = value.as_object() {
        if m.get("__cached_response__").is_some() {
            return Arc::new(CachedResponse::from_jsonable(&value));
        }
        if let Some(b64) = m.get("__bytes__").and_then(|v| v.as_str()) {
            let bytes = base64::engine::general_purpose::STANDARD
                .decode(b64)
                .unwrap_or_default();
            return Arc::new(String::from_utf8_lossy(&bytes).to_string());
        }
        if let Some(b64) = m.get("__raw_bytes__").and_then(|v| v.as_str()) {
            let bytes = base64::engine::general_purpose::STANDARD
                .decode(b64)
                .unwrap_or_default();
            return Arc::new(bytes);
        }
    }
    Arc::new(value)
}
