"""Application wiring / factory behaviour (`api`)."""
import pytest

from app.dlq import DeadLetterQueue
from app.main import create_app
from app.resilience import ResilientPipeline

pytestmark = pytest.mark.api


async def test_default_lifespan_builds_client_and_pipeline(temp_db_path):
    """With no injection, startup creates the HTTP client and pipeline."""
    app = create_app(
        dlq=DeadLetterQueue(temp_db_path),
        mock_url="http://127.0.0.1:9",  # unreachable; no connection is made here
    )

    assert app.state.resilient is None

    async with app.router.lifespan_context(app):
        assert isinstance(app.state.resilient, ResilientPipeline)
        assert app.state.mock_client is not None
        created_client = app.state.mock_client

    # Shutdown closes the client it created.
    assert created_client.is_closed


async def test_mock_client_without_resilient_is_wired(
    app_factory, client_for, mock_client, sample_event
):
    """Passing only a mock client still yields a working resilient pipeline."""
    app = app_factory(mock_client=mock_client, test_controls=True)

    assert isinstance(app.state.resilient, ResilientPipeline)

    async with client_for(app) as client:
        response = await client.post(
            "/calls",
            json=sample_event("wired"),
            headers={"X-Test-Scenario": "always_succeed"},
        )

    assert response.status_code == 200
    assert response.json()["pipeline"]["status"] == "ok"
