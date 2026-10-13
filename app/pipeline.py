"""Model-agnostic dependency boundary.

Resilience code depends only on the ``Pipeline`` protocol below. The bundled
``MockPipeline`` is an HTTP adapter for the challenge's mock STT/LLM/TTS
service; a real open-weight implementation can implement the same protocol
(or expose the same HTTP contract) without any change to ``app.resilience``.
See ``MODEL_SWAP.md``.
"""
from typing import Protocol

from app.models import PipelineRequest


class Pipeline(Protocol):
    async def process(self, request: PipelineRequest) -> dict:
        ...


class MockPipeline:
    """HTTP adapter for the challenge's mock STT/LLM/TTS service."""

    def __init__(self, client):
        self.client = client

    async def process(self, request: PipelineRequest) -> dict:
        response = await self.client.post(
            "/pipeline/process",
            # exclude_none keeps the ordinary payload unchanged when no test
            # scenario is set; when set, it is forwarded to the mock.
            json=request.model_dump(exclude_none=True),
        )
        # raise_for_status() surfaces 5xx as retryable httpx.HTTPStatusError and
        # 4xx as non-retryable (see app.resilience.is_retryable).
        response.raise_for_status()
        return response.json()
