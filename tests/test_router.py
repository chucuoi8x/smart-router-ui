import asyncio
import sys
import time
import unittest
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from router import Candidate, CircuitState, SmartRouter


class WeightedRouterTests(unittest.TestCase):
    def test_smooth_weighted_round_robin_uses_expected_eight_request_cycle(self):
        router = SmartRouter(
            {
                "routes": {
                    "test-route": {
                        "strategy": "smooth_weighted_rr",
                        "candidates": [
                            {"upstream": "proxypal", "model": "openai", "weight": 4},
                            {"upstream": "proxypal", "model": "gemini-lite", "weight": 3},
                            {"upstream": "proxypal", "model": "gemini-extra", "weight": 1},
                        ],
                    }
                },
                "logging": {"level": "CRITICAL"},
            }
        )

        async def select_models():
            return [(await router._candidate_order("test-route"))[0].model for _ in range(8)]

        selected = asyncio.run(select_models())
        self.assertEqual(
            Counter(selected),
            Counter({"openai": 4, "gemini-lite": 3, "gemini-extra": 1}),
        )

    def test_rate_limit_cooldowns_are_provider_agnostic_and_honor_retry_after(self):
        router = SmartRouter({"routes": {}, "logging": {"level": "CRITICAL"}})
        proxypal = Candidate("proxypal", "free-model")
        aibox = Candidate("aibox", "paid-model")
        classification = {
            "kind": "RATE_LIMIT",
            "scope": "credential/model/connection",
            "retry_after": "42",
            "reset_at": None,
            "consumption_uncertainty": "unknown",
        }

        asyncio.run(router._record_failure(proxypal, 429, "rate limited", classification))
        asyncio.run(router._record_failure(aibox, 429, "rate limited", classification))

        free_remaining = router.circuits[proxypal.key].cooldown_until - time.monotonic()
        paid_remaining = router.circuits[aibox.key].cooldown_until - time.monotonic()
        self.assertGreater(free_remaining, 35)
        self.assertGreater(paid_remaining, 35)
        self.assertLess(abs(free_remaining - paid_remaining), 2)
        self.assertEqual(router.circuits[proxypal.key].last_kind, "RATE_LIMIT")
        self.assertEqual(router.circuits[proxypal.key].retry_after, "42")

    def test_request_scoped_failures_do_not_open_circuit_breakers(self):
        router = SmartRouter({"routes": {}, "logging": {"level": "CRITICAL"}})
        candidate = Candidate("proxypal", "model")
        for kind, status in (("INVALID_REQUEST", 400), ("CONTEXT_TOO_LARGE", 400), ("CONTENT_POLICY", 403)):
            classification = {
                "kind": kind,
                "scope": "request",
                "retry_after": None,
                "reset_at": None,
                "consumption_uncertainty": "none",
            }
            asyncio.run(router._record_failure(candidate, status, kind, classification))
            self.assertNotIn(candidate.key, router.circuits)

    def test_authentication_failures_use_candidate_suppression_not_health_penalty(self):
        router = SmartRouter({"routes": {}, "logging": {"level": "CRITICAL"}})
        candidate = Candidate("proxypal", "model")
        classification = {
            "kind": "AUTH_EXPIRED",
            "scope": "credential",
            "retry_after": None,
            "reset_at": None,
            "consumption_uncertainty": "unknown",
        }
        asyncio.run(router._record_failure(candidate, 401, "unauthorized", classification))
        self.assertIn(candidate.key, router.circuits)
        self.assertEqual(router.circuits[candidate.key].last_kind, "AUTH_EXPIRED")
        self.assertEqual(router.circuits[candidate.key].consecutive_failures, 1)

    def test_overloaded_backoff_is_provider_agnostic(self):
        router = SmartRouter({"routes": {}, "logging": {"level": "CRITICAL"}})
        proxypal = Candidate("proxypal", "model")
        aibox = Candidate("aibox", "model")
        classification = {
            "kind": "OVERLOADED",
            "scope": "model/provider",
            "retry_after": None,
            "reset_at": None,
            "consumption_uncertainty": "unknown",
        }

        asyncio.run(router._record_failure(proxypal, 529, "overloaded", classification))
        asyncio.run(router._record_failure(aibox, 529, "overloaded", classification))

        proxypal_remaining = router.circuits[proxypal.key].cooldown_until - time.monotonic()
        aibox_remaining = router.circuits[aibox.key].cooldown_until - time.monotonic()
        self.assertLess(abs(proxypal_remaining - aibox_remaining), 2)
        self.assertEqual(router.circuits[proxypal.key].last_kind, "OVERLOADED")

    def test_fallback_candidates_always_after_primary(self):
        # Fallback (aibox) must never be part of the weighted rotation; it is
        # only appended after all primary candidates.
        router = SmartRouter(
            {
                "routes": {
                    "r": {
                        "strategy": "smooth_weighted_rr",
                        "candidates": [
                            {"upstream": "proxypal", "model": "a", "weight": 4},
                            {"upstream": "proxypal", "model": "b", "weight": 3},
                        ],
                        "fallback": [{"upstream": "aibox", "model": "z"}],
                    }
                },
                "logging": {"level": "CRITICAL"},
            }
        )

        async def order():
            return [c.key for c in await router._candidate_order("r")]

        for _ in range(5):
            keys = asyncio.run(order())
            self.assertEqual(keys[-1], "aibox:z")
            self.assertEqual(set(keys[:2]), {"proxypal:a", "proxypal:b"})
            self.assertEqual(len(keys), 3)

    def test_fallback_excluded_when_unavailable(self):
        router = SmartRouter(
            {
                "routes": {
                    "r": {
                        "strategy": "priority",
                        "candidates": [{"upstream": "proxypal", "model": "a"}],
                        "fallback": [{"upstream": "aibox", "model": "z"}],
                    }
                },
                "logging": {"level": "CRITICAL"},
            }
        )
        router.circuits.setdefault("aibox:z", CircuitState()).cooldown_until = 10 ** 9
        # Bridge: cũng trip engine circuit repository cho test
        from apps.gateway.routing.models import ResourceRef
        router.router_engine.circuit_repository.trip(ResourceRef("aibox", "aibox", "z"), cooldown_seconds=10**9)

        async def order():
            return await router._candidate_order("r")

        self.assertEqual(asyncio.run(order()), [Candidate("proxypal", "a")])


if __name__ == "__main__":
    unittest.main()
