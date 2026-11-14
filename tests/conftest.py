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


class MockHTTP:
    """Routes every httpx.Client / httpx.AsyncClient built while installed through
    an httpx.MockTransport. Records each request and the client's timeout."""

    def __init__(self, handler):
        self.handler = handler
        self.requests: list[httpx.Request] = []
        self.timeouts: list = []

    def _handle(self, request):
        self.requests.append(request)
        return self.handler(request)


@pytest.fixture
def mock_http(monkeypatch):
    real_sync, real_async = httpx.Client, httpx.AsyncClient

    def install(handler) -> MockHTTP:
        m = MockHTTP(handler)
        transport = httpx.MockTransport(m._handle)

        class _Sync(real_sync):
            def __init__(self, *a, **kw):
                m.timeouts.append(kw.get("timeout"))
                kw["transport"] = transport
                super().__init__(*a, **kw)

        class _Async(real_async):
            def __init__(self, *a, **kw):
                m.timeouts.append(kw.get("timeout"))
                kw["transport"] = transport
                super().__init__(*a, **kw)

        monkeypatch.setattr(httpx, "Client", _Sync)
        monkeypatch.setattr(httpx, "AsyncClient", _Async)
        return m

    return install


@pytest.fixture
def route_env(tmp_path, monkeypatch):
    """Deterministic environment for calling the server tools end to end.

    - a copy of the shipped price table dated yesterday (never stale, whatever
      today's date is), optionally edited via the returned `write_table`
    - routing history isolated to a temp JSONL file (Upstash disabled)
    - provider API-key env vars cleared, so only per-request BYOK keys count
    Yields a namespace with `history` (Path), `server`, and `write_table(fn)`.
    """
    import importlib
    import json as _json
    import types
    from datetime import date, timedelta
    from pathlib import Path

    repo = Path(__file__).resolve().parent.parent
    real = _json.loads((repo / "prices.json").read_text())
    for p in real["providers"].values():
        monkeypatch.delenv(p["api_key_env"], raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    monkeypatch.delenv("ARBITRAGE_ALLOW_STALE_PRICES", raising=False)
    monkeypatch.delenv("ARBITRAGE_EMIT_OBSERVED", raising=False)
    hist = tmp_path / "history.jsonl"
    monkeypatch.setenv("ARBITRAGE_HISTORY_FILE", str(hist))
    pf = tmp_path / "prices.json"
    monkeypatch.setenv("ARBITRAGE_PRICES_FILE", str(pf))

    def write_table(edit=None, verified_at=None):
        t = _json.loads(_json.dumps(real))
        t["verified_at"] = verified_at or (date.today() - timedelta(days=1)).isoformat()
        if edit:
            edit(t)
        pf.write_text(_json.dumps(t))
        import pricing_table, providers, server
        importlib.reload(pricing_table)
        importlib.reload(providers)
        importlib.reload(server)

    write_table()
    import pricing_table, providers, server
    yield types.SimpleNamespace(history=hist, server=server, write_table=write_table)
    monkeypatch.delenv("ARBITRAGE_PRICES_FILE", raising=False)
    importlib.reload(pricing_table)
    importlib.reload(providers)
    importlib.reload(server)


@pytest.fixture(autouse=True)
def _http_transport_defaults(monkeypatch):
    """Every test starts as the hosted (HTTP) server with no HEALTH_MIN_SESSIONS
    override, whatever the developer's shell or an earlier test set (ST6)."""
    import providers
    monkeypatch.delenv("HEALTH_MIN_SESSIONS", raising=False)
    monkeypatch.setattr(providers, "_local_transport", False)
    yield


@pytest.fixture(autouse=True)
def _drain_background_history_writes(_no_network):
    """
    Upstash history writes run on a background thread (storage._BackgroundWriter).
    Drain them after every test so a write queued in one test can never land
    during another. Depends on _no_network so the drain happens while the
    network guard is still installed.
    """
    yield
    import storage
    assert storage.flush_writes(timeout=5.0), "background history writes did not drain"
