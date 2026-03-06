import threading
import time
from collections import defaultdict

from ._exceptions import RateLimitError


class RateLimiter:
    """
    Simple in-memory per-IP sliding window rate limiter.

    :param max_requests: Maximum number of requests allowed within the window.
    :param window_seconds: Length of the sliding window in seconds.
    """

    def __init__(self, max_requests: int = 60, window_seconds: float = 60.0):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._requests: dict[str, list[float]] = defaultdict(list)
        self._lock = threading.Lock()

    def _cleanup(self, key: str, now: float) -> None:
        """Remove expired timestamps for the given key."""
        cutoff = now - self.window_seconds
        timestamps = self._requests[key]
        # Find the first index that is within the window
        i = 0
        while i < len(timestamps) and timestamps[i] < cutoff:
            i += 1
        if i > 0:
            self._requests[key] = timestamps[i:]
        # Clean up empty entries to prevent memory leaks
        if not self._requests[key]:
            del self._requests[key]

    def check(self, key: str) -> None:
        """
        Check if a request from the given key (typically an IP address)
        is allowed. Raises :class:`RateLimitError` if the limit is exceeded.

        :param key: The identifier for rate limiting (e.g. IP address).
        :raises RateLimitError: If the rate limit is exceeded.
        """
        now = time.monotonic()
        with self._lock:
            self._cleanup(key, now)
            timestamps = self._requests[key]
            if len(timestamps) >= self.max_requests:
                raise RateLimitError(
                    f"Rate limit exceeded for {key}: "
                    f"{self.max_requests} requests per {self.window_seconds}s"
                )
            self._requests[key].append(now)

    def is_allowed(self, key: str) -> bool:
        """
        Check if a request from the given key is allowed without recording it.

        :param key: The identifier for rate limiting.
        :return: True if the request would be allowed.
        """
        now = time.monotonic()
        with self._lock:
            self._cleanup(key, now)
            return len(self._requests.get(key, [])) < self.max_requests

    def reset(self, key: str | None = None) -> None:
        """
        Reset rate limit state.

        :param key: If provided, reset only this key. Otherwise reset all.
        """
        with self._lock:
            if key is None:
                self._requests.clear()
            elif key in self._requests:
                del self._requests[key]
