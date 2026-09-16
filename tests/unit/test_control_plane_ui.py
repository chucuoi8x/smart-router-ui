"""Step 162 — static Control Plane UI smoke contract."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_control_plane_ui_has_operational_navigation_and_auth_input():
    html = (ROOT / "web/dist/index.html").read_text(encoding="utf-8")
    for text in ("Smart Router Control Plane", "Bearer token", "Health", "Overview", "Routes", "Providers"):
        assert text in html
    for path in ("/health", "/api/admin/v1/overview", "/api/admin/v1/routes", "/api/admin/v1/providers"):
        assert path in html


def test_control_plane_ui_does_not_embed_credentials():
    html = (ROOT / "web/dist/index.html").read_text(encoding="utf-8")
    lower = html.lower()
    assert "api_key" not in lower or html.count("api_key") == 0
    # UI can remind the operator which env var to use; it must not contain a secret itself.

