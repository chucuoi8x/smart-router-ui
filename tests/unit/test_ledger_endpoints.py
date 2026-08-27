"""Smoke tests for durable ledger query endpoints.

Verifies that the new ``/ledger/*`` routes are mounted on the admin
router and return proper auth-401 / route-not-found responses when
no database is reachable (expected in most CI/test environments).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from fastapi.testclient import TestClient

from router import app


class LedgerEndpointsSmokeTests(unittest.TestCase):
    """Confirm new ledger routes exist and respond consistently."""

    def setUp(self):
        self.client = TestClient(app, raise_server_exceptions=False)

    # ── auth guard ────────────────────────────────────────────────────

    def test_ledger_stats_requires_admin_auth(self):
        resp = self.client.get("/api/admin/v1/ledger/stats")
        self.assertEqual(resp.status_code, 401)

    def test_ledger_requests_requires_admin_auth(self):
        resp = self.client.get("/api/admin/v1/ledger/requests")
        self.assertEqual(resp.status_code, 401)

    def test_ledger_request_detail_requires_admin_auth(self):
        resp = self.client.get("/api/admin/v1/ledger/requests/some-id")
        self.assertEqual(resp.status_code, 401)

    def test_ledger_stats_detail_requires_admin_auth(self):
        # GET /ledger/requests/{request_id} also goes through admin router
        resp = self.client.get("/api/admin/v1/ledger/stats", headers={"Authorization": "Bearer wrong"})
        self.assertEqual(resp.status_code, 401)

    # ── graceful degradation when no DB ───────────────────────────────

    def test_ledger_stats_returns_500_when_no_db(self):
        """Without DATABASE_URL pointing to a valid PostgreSQL, the
        session factory will create an engine that cannot connect.
        This should surface as an HTTP 500 from the endpoint, NOT a
        404 or unexpected crash."""
        resp = self.client.get(
            "/api/admin/v1/ledger/stats",
            headers={"Authorization": "Bearer test-admin-key"},
        )
        # Accept 500 (connection refused) as evidence the route exists
        # and attempted to use the DB backend.
        self.assertEqual(resp.status_code, 500)

    def test_ledger_requests_list_returns_500_when_no_db(self):
        resp = self.client.get(
            "/api/admin/v1/ledger/requests",
            headers={"Authorization": "Bearer test-admin-key"},
        )
        self.assertEqual(resp.status_code, 500)

    def test_ledger_request_detail_returns_404_when_not_found(self):
        """A real DB would return 404; without a DB it may return 500.
        Either non-2xx response confirms the route matched."""
        resp = self.client.get(
            "/api/admin/v1/ledger/requests/nonexistent-req",
            headers={"Authorization": "Bearer test-admin-key"},
        )
        self.assertIn(resp.status_code, [404, 500])


if __name__ == "__main__":
    unittest.main()
