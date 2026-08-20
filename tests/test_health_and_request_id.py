import sys
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from router import app


class HealthAndRequestIdTests(unittest.TestCase):
    def test_live_health_endpoint_returns_request_id(self):
        client = TestClient(app)

        response = client.get("/health/live")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertTrue(response.headers.get("x-request-id"))

    def test_request_id_header_is_preserved_when_client_sends_one(self):
        client = TestClient(app)

        response = client.get("/health/live", headers={"X-Request-ID": "client-request-123"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("x-request-id"), "client-request-123")

    def test_ready_health_endpoint_returns_request_id(self):
        client = TestClient(app)

        response = client.get("/health/ready")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertTrue(response.headers.get("x-request-id"))


if __name__ == "__main__":
    unittest.main()
