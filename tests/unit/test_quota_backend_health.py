"""RED tests Step 156 — quota backend readiness visibility."""
import sys
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from router import app


def test_health_ready_reports_quota_backend_without_connection_secret():
    with TestClient(app) as client:
        response = client.get("/health/ready")

    assert response.status_code == 200
    data = response.json()
    quota = data["checks"]["quota"]
    assert quota["backend"] in {"memory", "redis"}
    assert "redis://" not in response.text
    assert "password" not in response.text.lower()


def test_metrics_reports_quota_backend_without_redis_url():
    with TestClient(app) as client:
        response = client.get("/metrics")

    assert response.status_code == 200
    data = response.json()
    assert data["quota_backend"] in {"memory", "redis"}
    assert "REDIS_URL" not in response.text
    assert "redis://" not in response.text
