"""Shared test fixtures.

Network guard: no test may open a real network connection. Real httpx transports
are replaced for every test; any attempt is recorded and fails the test at
teardown (product code often swallows exceptions as best-effort, so raising
alone would not surface it). Tests that need HTTP use httpx.MockTransport,
which bypasses the real transports.
"""

import httpx
import pytest


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    attempts: list[str] = []

    def _blocked_sync(self, request):
        attempts.append(str(request.url))
        raise httpx.ConnectError("network disabled in tests", request=request)

    async def _blocked_async(self, request):
        attempts.append(str(request.url))
        raise httpx.ConnectError("network disabled in tests", request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", _blocked_sync)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _blocked_async)
    yield
    assert not attempts, f"test attempted real network access: {attempts}"
