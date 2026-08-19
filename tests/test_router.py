import asyncio
import sys
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

    def test_rate_limit_cooldowns_differ_for_free_and_paid_upstreams(self):
        router = SmartRouter({"routes": {}, "logging": {"level": "CRITICAL"}})
        free = Candidate("proxypal", "free-model")
        paid = Candidate("aibox", "paid-model")
        asyncio.run(router._record_failure(free, 429, "rate limited"))
        asyncio.run(router._record_failure(paid, 429, "rate limited"))
        self.assertGreater(router.circuits[free.key].cooldown_until, 0)
        self.assertGreater(
            router.circuits[paid.key].cooldown_until,
            router.circuits[free.key].cooldown_until + 170,
        )

    def test_authentication_failures_do_not_open_circuit_breakers(self):
        router = SmartRouter({"routes": {}, "logging": {"level": "CRITICAL"}})
        candidate = Candidate("proxypal", "model")
        asyncio.run(router._record_failure(candidate, 401, "unauthorized"))
        self.assertNotIn(candidate.key, router.circuits)

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

        async def order():
            return await router._candidate_order("r")

        self.assertEqual(asyncio.run(order()), [Candidate("proxypal", "a")])


if __name__ == "__main__":
    unittest.main()
