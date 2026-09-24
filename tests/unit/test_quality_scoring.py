from apps.gateway.routing.scoring import (
    CandidateMetrics,
    ScoringConfig,
    ScoringWeights,
    SmartScoreCalculator,
)


def test_quality_is_independent_scoring_dimension():
    metrics = CandidateMetrics(quality_score=0.9, capability_match=False)
    weights = ScoringWeights(
        cost_factor=0.0, reliability_factor=0.0, latency_factor=0.0,
        quota_pressure_factor=0.0, capability_factor=0.0,
        session_affinity_factor=0.0, quality_factor=1.0,
    )
    scorer = SmartScoreCalculator(ScoringConfig(weights=weights))
    score = scorer._compute_candidate_score(object(), "k", metrics, None)
    assert score["quality"] == 0.9
    assert score["capability"] == 0.0
    assert score["composite"] == 0.9
