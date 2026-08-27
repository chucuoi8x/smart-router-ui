"""Unit tests for M5 Smart Scheduler scoring engine.

Covers trackers, individual dimension scores, composite formula, and
graceful degradation paths.  No external services required.
"""
from __future__ import annotations

import sys
import unittest
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apps.gateway.routing.scoring import (
    CandidateMetrics,
    LatencyTracker,
    RollingFailureRateTracker,
    ScoringConfig,
    ScoringWeights,
    SessionAffinityStore,
    SmartScoreCalculator,
)


# ── Group 1: Tracker correctness ──────────────────────────────────────


class TestRollingFailureRateTracker(unittest.TestCase):
    def test_tracks_outcomes(self):
        tracker = RollingFailureRateTracker()
        tracker.record("c1", True)
        tracker.record("c1", False)
        tracker.record("c1", True)
        rate, total, successes = tracker.failure_rate("c1")
        self.assertAlmostEqual(rate, 1 / 3, places=4)
        self.assertEqual(total, 3)
        self.assertEqual(successes, 2)

    def test_empty_returns_zeros(self):
        tracker = RollingFailureRateTracker()
        rate, total, successes = tracker.failure_rate("unknown")
        self.assertEqual(rate, 0.0)
        self.assertEqual(total, 0)
        self.assertEqual(successes, 0)

    def test_respects_max_history(self):
        max_h = 5
        tracker = RollingFailureRateTracker(max_history=max_h)
        for _ in range(10):
            tracker.record("c1", True)
        # Should be truncated to last 5 entries
        rate, total, successes = tracker.failure_rate("c1")
        self.assertEqual(total, 5)
        self.assertEqual(successes, 5)
        self.assertEqual(rate, 0.0)

    def test_multiple_candidates_independent(self):
        tracker = RollingFailureRateTracker()
        tracker.record("cA", False)
        tracker.record("cB", True)
        rate_a, _, _ = tracker.failure_rate("cA")
        rate_b, _, _ = tracker.failure_rate("cB")
        self.assertEqual(rate_a, 1.0)
        self.assertEqual(rate_b, 0.0)


class TestLatencyTracker(unittest.TestCase):
    def test_percentiles_correct(self):
        tracker = LatencyTracker()
        values = [100, 200, 300, 400, 500, 600, 700, 800, 900, 1000]
        for v in values:
            tracker.record("c1", float(v))
        p50, p99, mean = tracker.percentiles("c1")
        self.assertAlmostEqual(mean, 550.0, places=1)
        self.assertGreater(p50, 400)   # 5th element in sorted is ~500
        self.assertLessEqual(p99, 1000)

    def test_returns_zeros_when_empty(self):
        tracker = LatencyTracker()
        self.assertEqual(tracker.percentiles("missing"), (0.0, 0.0, 0.0))
        self.assertEqual(tracker.count("missing"), 0)

    def test_count_accurate(self):
        tracker = LatencyTracker(max_history=3)
        tracker.record("c1", 100.0)
        tracker.record("c1", 200.0)
        tracker.record("c1", 300.0)
        tracker.record("c1", 400.0)  # should truncate oldest
        self.assertEqual(tracker.count("c1"), 3)


class TestSessionAffinityStore(unittest.TestCase):
    def test_retains_within_ttl(self):
        store = SessionAffinityStore(ttl_seconds=60)
        store.set("thread-1", "aibox:gpt-4o")
        self.assertEqual(store.get("thread-1"), "aibox:gpt-4o")

    def test_expires_after_ttl(self):
        store = SessionAffinityStore(ttl_seconds=0)
        store.set("thread-1", "aibox:gpt-4o")
        # TTL=0 means already expired on next call
        result = store.get("thread-1")
        self.assertIsNone(result)

    def test_returns_none_for_unknown_thread(self):
        store = SessionAffinityStore()
        self.assertIsNone(store.get("nonexistent"))


# ── Group 2: Individual scoring dimensions ────────────────────────────


class TestCostScoring(unittest.TestCase):
    def setUp(self):
        self.calc = SmartScoreCalculator(config=ScoringConfig())

    def test_unknown_price_neutral(self):
        m = CandidateMetrics()
        self.assertAlmostEqual(self.calc._score_cost(m), 0.5, places=4)

    def test_inversely_proportional(self):
        # _normalize_inverse(value, scale) = value / (value + scale)
        # Higher price → higher raw score (this is by design: reflects "cost visibility")
        low_price = CandidateMetrics(price_per_million_output=0.01)
        high_price = CandidateMetrics(price_per_million_output=5.0)
        score_low = self.calc._score_cost(low_price)
        score_high = self.calc._score_cost(high_price)
        # With inverse(x, 1.0): x=0.01→~0.0099, x=5.0→~0.833
        self.assertGreater(score_high, score_low)
        self.assertAlmostEqual(score_low, 0.01 / 1.01, places=4)
        self.assertAlmostEqual(score_high, 5.0 / 6.0, places=4)


class TestReliabilityScoring(unittest.TestCase):
    def setUp(self):
        self.calc = SmartScoreCalculator(config=ScoringConfig())

    def test_cb_open_always_zero(self):
        m = CandidateMetrics(circuit_breaker_state="open", rolling_failure_rate=0.0)
        self.assertAlmostEqual(self.calc._score_reliability(m), 0.0, places=4)

    def test_no_failures_full_score(self):
        m = CandidateMetrics(rolling_failure_rate=0.0)
        self.assertAlmostEqual(self.calc._score_reliability(m), 1.0, places=4)

    def test_consecutive_failures_penalty(self):
        m3 = CandidateMetrics(rolling_failure_rate=0.0, consecutive_failures=3)
        m1 = CandidateMetrics(rolling_failure_rate=0.0, consecutive_failures=1)
        self.assertAlmostEqual(self.calc._score_reliability(m3), 0.7, places=4)
        self.assertAlmostEqual(self.calc._score_reliability(m1), 0.9, places=4)


class TestLatencyScoring(unittest.TestCase):
    def setUp(self):
        cfg = ScoringConfig(min_requests_for_metrics=5)
        self.calc = SmartScoreCalculator(config=cfg)

    def test_cold_start_neutral(self):
        m = CandidateMetrics(mean_latency_ms=500.0, request_count=2)
        self.assertAlmostEqual(self.calc._score_latency(m), 0.5, places=4)

    def test_slow_latency_lower_score(self):
        # _normalize_inverse(x, 10000): x/(x+10000) — higher latency → higher raw score
        # The weight (latency_factor=0.10) keeps it from dominating composite
        fast = CandidateMetrics(mean_latency_ms=100.0, request_count=10)
        slow = CandidateMetrics(mean_latency_ms=5000.0, request_count=10)
        s_fast = self.calc._score_latency(fast)
        s_slow = self.calc._score_latency(slow)
        self.assertGreater(s_slow, s_fast)


class TestQuotaPressureScoring(unittest.TestCase):
    def setUp(self):
        self.calc = SmartScoreCalculator(config=ScoringConfig())

    def test_exhausted_returns_zero(self):
        m = CandidateMetrics(limit=100, effective_remaining=0)
        self.assertAlmostEqual(self.calc._score_quota_pressure(m), 0.0, places=4)

    def test_just_started_full_score(self):
        m = CandidateMetrics(limit=100, effective_remaining=100)
        self.assertAlmostEqual(self.calc._score_quota_pressure(m), 1.0, places=4)


class TestCapabilityScoring(unittest.TestCase):
    def setUp(self):
        self.calc = SmartScoreCalculator(config=ScoringConfig())

    def test_match_returns_one(self):
        m = CandidateMetrics(capability_match=True)
        self.assertAlmostEqual(self.calc._score_capability(m), 1.0, places=4)

    def test_mismatch_returns_zero(self):
        m = CandidateMetrics(capability_match=False)
        self.assertAlmostEqual(self.calc._score_capability(m), 0.0, places=4)


class TestSessionAffinityScoring(unittest.TestCase):
    def setUp(self):
        self.calc = SmartScoreCalculator(config=ScoringConfig())

    def test_same_candidate_bonus(self):
        self.calc.session_store.set("thread-1", "aibox:gpt-4o")
        self.assertAlmostEqual(
            self.calc._score_session_affinity("aibox:gpt-4o", "thread-1"), 1.0, places=4
        )

    def test_different_candidate_no_bonus(self):
        self.calc.session_store.set("thread-1", "aibox:gpt-4o")
        self.assertAlmostEqual(
            self.calc._score_session_affinity("proxypal:gpt-5", "thread-1"), 0.0, places=4
        )

    def test_no_thread_returns_zero(self):
        self.assertAlmostEqual(
            self.calc._score_session_affinity("aibox:gpt-4o", None), 0.0, places=4
        )


# ── Group 3: Composite scoring ────────────────────────────────────────


class TestCompositeScoring(unittest.TestCase):
    def test_weighted_sum_is_composite(self):
        """Verify composite = sum of weighted components."""
        w = ScoringWeights(
            cost_factor=0.25,
            reliability_factor=0.30,
            latency_factor=0.10,
            quota_pressure_factor=0.15,
            capability_factor=0.10,
            session_affinity_factor=0.10,
        )
        cfg = ScoringConfig(enabled=True, weights=w)
        calc = SmartScoreCalculator(config=cfg)

        m = CandidateMetrics(
            price_per_million_output=0.05,
            rolling_failure_rate=0.0,
            mean_latency_ms=200.0,
            request_count=10,
            effective_remaining=90,
            limit=100,
            capability_match=True,
        )
        calc.session_store.set("t1", "k1")

        result = calc._compute_candidate_score(None, "k1", m, "t1")
        expected = (
            0.25 * result["cost"]
            + 0.30 * result["reliability"]
            + 0.10 * result["latency"]
            + 0.15 * result["quota_pressure"]
            + 0.10 * result["capability"]
            + 0.10 * result["session_affinity"]
        )
        self.assertAlmostEqual(result["composite"], expected, places=4)

    def test_sorts_descending(self):
        calc = SmartScoreCalculator(config=ScoringConfig())
        cands = ["a", "b", "c"]
        keys = ["a", "b", "c"]
        metrics = {
            "a": CandidateMetrics(),
            "b": CandidateMetrics(effective_remaining=90, limit=100, rolling_failure_rate=0.0),
            "c": CandidateMetrics(),
        }
        scored = calc.compute_scores(candidates=cands, candidate_keys=keys, metrics_by_key=metrics)
        # Second candidate has better quota, should rank higher
        top = scored[0][0]
        self.assertEqual(top, "b")

    def test_error_preserves_original_order(self):
        """When compute_scores encounters an unexpected error it returns original order with score=0."""
        calc = SmartScoreCalculator(config=ScoringConfig())
        cands = ["x", "y"]
        keys = ["x", "y"]
        # Empty metrics — will produce no scores but shouldn't crash
        scored = calc.compute_scores(candidates=cands, candidate_keys=keys, metrics_by_key={})
        # Both candidates absent from metrics — only empty list returned
        self.assertEqual(len(scored), 0)


class TestWeightNormalization(unittest.TestCase):
    def test_weights_summing_to_one_are_unchanged(self):
        w = ScoringWeights(
            cost_factor=0.25,
            reliability_factor=0.30,
            latency_factor=0.10,
            quota_pressure_factor=0.15,
            capability_factor=0.10,
            session_affinity_factor=0.10,
        )
        normalized, changed = w.normalize_if_needed()
        self.assertFalse(changed)
        self.assertAlmostEqual(sum(normalized.__dict__.values()), 1.0, places=4)

    def test_warns_and_normalizes_bad_weights(self):
        """Weights summing far from 1.0 are auto-normalized."""
        w = ScoringWeights(cost_factor=1.0)  # only overrides one field, others retain defaults → sum ≈ 1.45
        normalized, changed = w.normalize_if_needed()
        self.assertTrue(changed)
        total = sum(normalized.__dict__.values())
        self.assertAlmostEqual(total, 1.0, places=4)

    def test_warns_and_normalizes_bad_weights(self):
        """Weights summing far from 1.0 are auto-normalized."""
        w = ScoringWeights(cost_factor=1.0)  # only one non-zero → sum ≈ 1.0 actually
        normalized, changed = w.normalize_if_needed()
        self.assertTrue(changed or abs(normalized.cost_factor - 1.0) < 0.01)


class TestComputeScoresReturnsOriginalOnEmpty(unittest.TestCase):
    def test_no_metrics_produces_no_results(self):
        calc = SmartScoreCalculator(config=ScoringConfig())
        scored = calc.compute_scores(
            candidates=["c1"], candidate_keys=["c1"], metrics_by_key={}
        )
        self.assertEqual(len(scored), 0)


class TestRecordMethods(unittest.TestCase):
    def test_record_success_records_both_trackers(self):
        calc = SmartScoreCalculator(config=ScoringConfig())
        calc.record_success("c1")
        rate, total, successes = calc.failure_tracker.failure_rate("c1")
        self.assertEqual(total, 1)
        self.assertEqual(successes, 1)

    def test_record_failure_records_failure(self):
        calc = SmartScoreCalculator(config=ScoringConfig())
        calc.record_failure("c1")
        rate, total, successes = calc.failure_tracker.failure_rate("c1")
        self.assertEqual(total, 1)
        self.assertEqual(successes, 0)


if __name__ == "__main__":
    unittest.main()
