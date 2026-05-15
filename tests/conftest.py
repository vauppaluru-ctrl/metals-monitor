"""
Pytest fixtures for the Metals Monitor dashboard tests.

Assumes the web server is already running on BASE_URL (default: http://localhost:8080).
Start it before running: .venv/bin/uvicorn metals_web_server:app --host 0.0.0.0 --port 8080
"""
import time
import pytest
import requests

BASE_URL = "http://localhost:8080"


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: marks tests that trigger a live monitor run (~30-60s)")


@pytest.fixture(scope="session", autouse=True)
def require_server():
    """Fail fast if the server is not reachable before any test runs."""
    for attempt in range(5):
        try:
            r = requests.get(f"{BASE_URL}/api/status", timeout=3)
            if r.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(1)
    pytest.exit(
        f"Server not reachable at {BASE_URL}. "
        "Start it with: .venv/bin/uvicorn metals_web_server:app --host 0.0.0.0 --port 8080",
        returncode=3,
    )


@pytest.fixture(scope="session")
def base_url():
    return BASE_URL


@pytest.fixture
def fresh_page(page):
    """Navigate to the dashboard and wait for initial JS to settle."""
    page.goto(BASE_URL)
    page.wait_for_timeout(2000)
    return page
