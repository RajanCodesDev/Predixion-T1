"""Failure-reason formatting (`unit`).

Failure reasons surface through the API and the DLQ, so they must be short,
useful and free of internal URLs / multi-line trace noise.
"""
import httpx
import pytest

from app.main import describe_exception

pytestmark = pytest.mark.unit


def test_empty_timeout_uses_type_name():
    assert describe_exception(httpx.ReadTimeout("")) == "ReadTimeout"


def test_typed_message_is_prefixed():
    assert describe_exception(RuntimeError("downstream 503")) == (
        "RuntimeError: downstream 503"
    )


def test_only_first_line_is_kept():
    exc = RuntimeError("first line\nsecond line\nmore")
    assert describe_exception(exc) == "RuntimeError: first line"


def test_http_status_error_hides_internal_url():
    request = httpx.Request("POST", "http://mock:8001/pipeline/process?secret=abc")
    response = httpx.Response(503, request=request)
    exc = httpx.HTTPStatusError("Server error", request=request, response=response)

    reason = describe_exception(exc)

    assert reason == "HTTPStatusError: 503 Service Unavailable"
    assert "mock:8001" not in reason
    assert "secret" not in reason
