"""RED test cho endpoint POST /api/admin/v1/routes/simulate theo README §22.3."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from router import app

client = TestClient(app)
AUTH_HEADER = {"Authorization": "Bearer test-admin-key"}


def test_admin_simulate_route_endpoint():
    response = client.post(
        "/api/admin/v1/routes/simulate",
        json={
            "route": "claude-router-main",
            "estimated_input_tokens": 45000,
            "max_output_tokens": 6000,
            "tools": True,
            "vision": False,
            "session": "example-session",
        },
        headers=AUTH_HEADER,
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["route_name"] == "claude-router-main"
    assert "candidates" in data
    assert "selected_resource" in data
    assert "reason" in data
