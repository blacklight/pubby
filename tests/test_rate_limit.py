"""
Tests for the rate limiter.
"""

import time

import pytest

from mypub._exceptions import RateLimitError
from mypub._rate_limit import RateLimiter


class TestRateLimiter:
    def test_allows_within_limit(self):
        limiter = RateLimiter(max_requests=5, window_seconds=60)
        for _ in range(5):
            limiter.check("127.0.0.1")

    def test_blocks_over_limit(self):
        limiter = RateLimiter(max_requests=3, window_seconds=60)
        limiter.check("127.0.0.1")
        limiter.check("127.0.0.1")
        limiter.check("127.0.0.1")

        with pytest.raises(RateLimitError):
            limiter.check("127.0.0.1")

    def test_different_keys_independent(self):
        limiter = RateLimiter(max_requests=2, window_seconds=60)
        limiter.check("127.0.0.1")
        limiter.check("127.0.0.1")

        # Different key should still be allowed
        limiter.check("192.168.1.1")

        with pytest.raises(RateLimitError):
            limiter.check("127.0.0.1")

    def test_window_expiry(self):
        limiter = RateLimiter(max_requests=2, window_seconds=0.1)
        limiter.check("127.0.0.1")
        limiter.check("127.0.0.1")

        # Wait for window to expire
        time.sleep(0.15)

        # Should be allowed again
        limiter.check("127.0.0.1")

    def test_is_allowed_does_not_record(self):
        limiter = RateLimiter(max_requests=1, window_seconds=60)
        assert limiter.is_allowed("127.0.0.1") is True
        assert limiter.is_allowed("127.0.0.1") is True

        # Only check() records
        limiter.check("127.0.0.1")
        assert limiter.is_allowed("127.0.0.1") is False

    def test_reset_single_key(self):
        limiter = RateLimiter(max_requests=1, window_seconds=60)
        limiter.check("127.0.0.1")

        with pytest.raises(RateLimitError):
            limiter.check("127.0.0.1")

        limiter.reset("127.0.0.1")
        limiter.check("127.0.0.1")  # Should work now

    def test_reset_all(self):
        limiter = RateLimiter(max_requests=1, window_seconds=60)
        limiter.check("127.0.0.1")
        limiter.check("192.168.1.1")

        limiter.reset()
        limiter.check("127.0.0.1")
        limiter.check("192.168.1.1")

    def test_sliding_window(self):
        limiter = RateLimiter(max_requests=2, window_seconds=0.2)
        limiter.check("127.0.0.1")

        time.sleep(0.12)
        limiter.check("127.0.0.1")

        # Both within window — next should fail
        with pytest.raises(RateLimitError):
            limiter.check("127.0.0.1")

        # Wait for the first request to expire
        time.sleep(0.1)

        # Now one slot should be free
        limiter.check("127.0.0.1")
