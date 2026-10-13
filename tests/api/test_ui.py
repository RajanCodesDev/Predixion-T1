"""The visual dashboard route (`api`)."""
import pytest

from app.resilience import CircuitBreaker, ResilientPipeline
from tests.helpers import ScriptedAdapter

pytestmark = pytest.mark.api


def _app(app_factory):
    return app_factory(
        resilient=ResilientPipeline(ScriptedAdapter(), CircuitBreaker(), max_attempts=3)
    )


async def test_ui_is_served(app_factory, client_for):
    app = _app(app_factory)

    async with client_for(app) as client:
        response = await client.get("/ui")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "E2E Console" in response.text
    assert "/calls" in response.text


async def test_root_redirects_to_health(app_factory, client_for):
    app = _app(app_factory)

    async with client_for(app) as client:
        response = await client.get("/")

    assert response.status_code in (307, 308)
    assert response.headers["location"] == "/health"


async def test_openapi_and_docs_available(app_factory, client_for):
    app = _app(app_factory)

    async with client_for(app) as client:
        openapi = await client.get("/openapi.json")
        docs = await client.get("/docs")

    assert openapi.status_code == 200
    assert "/calls" in openapi.json()["paths"]
    assert docs.status_code == 200
