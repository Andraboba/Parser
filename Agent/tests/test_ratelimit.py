# -*- coding: utf-8 -*-
"""Ограничитель частоты: скорость, изменение на лету, реакция на 429."""

from __future__ import annotations

import time
import unittest

from agent.ratelimit import AdaptiveLimiter, TokenBucket


class TokenBucketTests(unittest.TestCase):
    def test_burst_is_limited(self) -> None:
        bucket = TokenBucket(rate=50, burst=5)
        started = time.monotonic()
        for _ in range(5):
            self.assertTrue(bucket.acquire())
        # первые пять токенов уходят из запаса мгновенно
        self.assertLess(time.monotonic() - started, 0.05)

        started = time.monotonic()
        self.assertTrue(bucket.acquire())
        self.assertGreaterEqual(time.monotonic() - started, 0.01)

    def test_set_rate(self) -> None:
        bucket = TokenBucket(rate=1, burst=1)
        bucket.acquire()
        bucket.set_rate(1000)
        started = time.monotonic()
        self.assertTrue(bucket.acquire())
        self.assertLess(time.monotonic() - started, 0.05)
        self.assertEqual(bucket.rate, 1000)

    def test_rejects_bad_rate(self) -> None:
        with self.assertRaises(ValueError):
            TokenBucket(rate=0)


class AdaptiveLimiterTests(unittest.TestCase):
    def test_throttle_halves_rate(self) -> None:
        limiter = AdaptiveLimiter(rate=10)
        limiter.on_throttled()
        self.assertEqual(limiter.rate, 5)
        limiter.on_throttled()
        self.assertEqual(limiter.rate, 2.5)

    def test_rate_does_not_drop_below_minimum(self) -> None:
        limiter = AdaptiveLimiter(rate=2, min_rate=1)
        for _ in range(10):
            limiter.on_throttled()
        self.assertEqual(limiter.rate, 1)

    def test_recovery_after_successes(self) -> None:
        limiter = AdaptiveLimiter(rate=10, recover_after=5, recover_factor=2.0)
        limiter.on_throttled()
        self.assertEqual(limiter.rate, 5)
        for _ in range(5):
            limiter.on_success()
        self.assertEqual(limiter.rate, 10)

    def test_recovery_is_capped_by_base_rate(self) -> None:
        limiter = AdaptiveLimiter(rate=10, recover_after=1, recover_factor=10.0)
        limiter.on_throttled()
        limiter.on_success()
        self.assertLessEqual(limiter.rate, 10)

    def test_coordinator_share_is_recovery_ceiling(self) -> None:
        """После 429 нельзя восстанавливаться выше выданной координатором доли."""
        limiter = AdaptiveLimiter(rate=200, recover_after=1, recover_factor=2.0)
        limiter.set_share(5)
        limiter.on_throttled()
        for _ in range(40):
            limiter.on_success()
        self.assertLessEqual(limiter.rate, 5)

    def test_local_limit_caps_large_share(self) -> None:
        limiter = AdaptiveLimiter(rate=10)
        limiter.set_share(1000)
        self.assertEqual(limiter.rate, 10)


if __name__ == "__main__":
    unittest.main()
