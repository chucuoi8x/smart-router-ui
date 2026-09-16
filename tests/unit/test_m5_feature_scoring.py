"""RED tests cho M5 feature scoring mở rộng (Step 90).

4 chiều mới theo README §18.3:
  expiry_urgency: risk lãng phí dung lượng sắp reset
  scarcity:       opportunity cost dùng model hiếm
  retry_cost:     chi phí retry lặp lại input
  uncertainty:    penalty khi metadata thiếu tin cậy
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import unittest
from apps.gateway.routing.scoring import CandidateMetrics, ScoringConfig, SmartScoreCalculator


def _calc(**overrides):
    cfg = ScoringConfig(enabled=True, mode="active")
    # weights mới mặc định 0, không normalize ngay để test composite gán trait riêng
    if overrides:
        from dataclasses import replace
        cfg.weights = replace(cfg.weights, **overrides)
        cfg.weights, _ = cfg.weights.normalize_if_needed()
    return SmartScoreCalculator(config=cfg)


class TestExpiryUrgencyScoring(unittest.TestCase):
    def test_high_expiry_urgency_scores_higher(self):
        calc = _calc()
        low = CandidateMetrics(expiry_urgency=0.1)
        high = CandidateMetrics(expiry_urgency=0.9)
        self.assertGreater(calc._score_expiry_urgency(high), calc._score_expiry_urgency(low))

    def test_unknown_expiry_neutral(self):
        calc = _calc()
        m = CandidateMetrics(expiry_urgency=0.0)
        # Chưa có reset semantics -> 0, nhưng không crash, trả về 0..0.5 neutral?
        self.assertGreaterEqual(calc._score_expiry_urgency(m), 0.0)


class TestScarcityScoring(unittest.TestCase):
    def test_scarce_model_scores_lower(self):
        calc = _calc()
        abundant = CandidateMetrics(scarcity=0.0)
        scarce = CandidateMetrics(scarcity=0.9)
        self.assertGreater(calc._score_scarcity(abundant), calc._score_scarcity(scarce))


class TestRetryCostScoring(unittest.TestCase):
    def test_high_retry_cost_scores_lower(self):
        calc = _calc()
        cheap_retry = CandidateMetrics(retry_expected_cost=0.0)
        expensive_retry = CandidateMetrics(retry_expected_cost=20.0)
        self.assertGreater(calc._score_retry_cost(cheap_retry), calc._score_retry_cost(expensive_retry))


class TestUncertaintyScoring(unittest.TestCase):
    def test_high_uncertainty_scores_lower(self):
        calc = _calc()
        certain = CandidateMetrics(uncertainty=0.0)
        uncertain = CandidateMetrics(uncertainty=0.9)
        self.assertGreater(calc._score_uncertainty(certain), calc._score_uncertainty(uncertain))


class TestCompositeIncludesNewDimensions(unittest.TestCase):
    def test_composite_prefers_low_scarcity_high_expiry_when_weights_present(self):
        from dataclasses import replace
        # Gán weights mới >0 để composite phản ánh
        calc = _calc()
        calc._config.weights = replace(
            calc._config.weights,
            expiry_urgency_factor=0.20,
            scarcity_factor=0.20,
            retry_cost_factor=0.20,
            uncertainty_factor=0.20,
        )
        calc._config.weights, _ = calc._config.weights.normalize_if_needed()

        good = CandidateMetrics(expiry_urgency=0.9, scarcity=0.1, retry_expected_cost=0.0, uncertainty=0.0)
        bad = CandidateMetrics(expiry_urgency=0.1, scarcity=0.9, retry_expected_cost=20.0, uncertainty=0.9)
        scored = calc.compute_scores(
            candidates=["good", "bad"],
            candidate_keys=["good", "bad"],
            metrics_by_key={"good": good, "bad": bad},
        )
        # good phải xếp trên bad khi weights mới tham gia composite
        self.assertEqual("good", scored[0][0])
        self.assertGreater(scored[0][1], scored[1][1])
