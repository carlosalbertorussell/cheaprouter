"""
Upstash history must never slow routing down (ST2).

These tests drive the REAL UpstashBackend._cmd / _pipeline and background writer
over an in-memory Upstash REST stand-in (httpx.MockTransport), so request shape,
request counts, timeouts and failure handling are all exercised for real.
"""

import asyncio
import json
import threading
import time

import httpx
import pytest

import storage
from storage import UpstashBackend

URL = "https://fake-db.upstash.io"
TOKEN = "tok-test-FAKE"


class FakeUpstash:
    """In-memory Upstash REST endpoint: single commands at / and /pipeline."""

    def __init__(self, delay: float = 0.0, fail: Exception | None = None, status: int = 200):
        self.lists: dict[str, list] = {}
        self.sets: dict[str, set] = {}
        self.requests: list[httpx.Request] = []
        self.delay = delay
        self.fail = fail
        self.status = status
        self._lock = threading.Lock()

    def _run(self, args):
        cmd = str(args[0]).upper()
        if cmd == "LPUSH":
            self.lists.setdefault(args[1], []).insert(0, args[2]); return len(self.lists[args[1]])
        if cmd == "LTRIM":
            k, s, e = args[1], int(args[2]), int(args[3]); self.lists[k] = self.lists.get(k, [])[s:e + 1]; return "OK"
        if cmd == "SADD":
            self.sets.setdefault(args[1], set()).add(args[2]); return 1
        if cmd == "SMEMBERS":
            return sorted(self.sets.get(args[1], set()))
        if cmd == "LRANGE":
            k, s, e = args[1], int(args[2]), int(args[3]); return self.lists.get(k, [])[s:e + 1]
        raise ValueError(cmd)

    def handler(self, request: httpx.Request) -> httpx.Response:
        with self._lock:
            self.requests.append(request)
        if self.delay:
            time.sleep(self.delay)
        if self.fail is not None:
            raise self.fail
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "boom"})
        body = json.loads(request.content)
        with self._lock:
            if request.url.path.endswith("/pipeline"):
                return httpx.Response(200, json=[{"result": self._run(c)} for c in body])
            return httpx.Response(200, json={"result": self._run(body)})

    def paths(self):
        return [r.url.path for r in self.requests]


@pytest.fixture
def fake(monkeypatch):
    """Route every httpx.Client created by storage.py to a FakeUpstash."""
    f = FakeUpstash()
    real_client = httpx.Client
    timeouts = []

    def client_factory(*args, timeout=None, **kw):
        timeouts.append(timeout)
        return real_client(transport=httpx.MockTransport(f.handler), timeout=timeout)

    monkeypatch.setattr(storage.httpx, "Client", client_factory)
    f.timeouts = timeouts
    return f


@pytest.fixture
def upstash_env(monkeypatch, tmp_path):
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", URL)
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", TOKEN)
    monkeypatch.delenv("UPSTASH_TIMEOUT_S", raising=False)
    # Should Upstash be bypassed by mistake, writes land here, not in /tmp.
    monkeypatch.setenv("ARBITRAGE_HISTORY_FILE", str(tmp_path / "h.jsonl"))


def _rec(session, ts, cost=0.001, pid="groq"):
    return {"timestamp": ts, "session": session, "provider_id": pid,
            "cost_usd": cost, "success": True}


# ─── request count ───────────────────────────────────────────────────────────

def test_unscoped_read_is_one_request_regardless_of_sessions(fake):
    be = UpstashBackend(URL, TOKEN)
    for i in range(20):
        be.write(_rec(f"s_{i}", f"2026-11-01T00:00:{i:02d}"))
    fake.requests.clear()
    out = be.read(None, 10)
    assert len(fake.requests) == 1
    assert len(out) == 10
    assert out[0]["session"] == "s_19"          # newest first


# ─── timeouts ────────────────────────────────────────────────────────────────

def test_default_timeout_is_short(fake):
    UpstashBackend(URL, TOKEN).read("s_a", 5)
    UpstashBackend(URL, TOKEN).write(_rec("s_a", "t"))
    assert fake.timeouts == [2.0, 2.0]


def test_timeout_env_override_and_bad_value(fake, monkeypatch):
    monkeypatch.setenv("UPSTASH_TIMEOUT_S", "0.5")
    UpstashBackend(URL, TOKEN).read("s_a", 5)
    monkeypatch.setenv("UPSTASH_TIMEOUT_S", "not-a-number")
    UpstashBackend(URL, TOKEN).read("s_a", 5)
    assert fake.timeouts == [0.5, 2.0]


# ─── background writes ───────────────────────────────────────────────────────

def test_write_record_returns_before_slow_upstash(fake, upstash_env):
    fake.delay = 0.5
    t0 = time.monotonic()
    storage.write_record(_rec("s_a", "t"))
    assert time.monotonic() - t0 < 0.2              # did not wait for the request
    assert storage.flush_writes(timeout=5.0) is True
    assert fake.paths() == ["/pipeline"]


def test_read_after_write_sees_pending_write(fake, upstash_env):
    fake.delay = 0.2
    storage.write_record(_rec("s_a", "2026-11-01T00:00:00"))
    out = storage.read_records("s_a", 10)            # waits for the queued write
    assert len(out) == 1


def test_writes_stay_in_order(fake, upstash_env):
    for i in range(10):
        storage.write_record(_rec("s_a", f"2026-11-01T00:00:{i:02d}"))
    out = storage.read_records("s_a", 20)
    assert [r["timestamp"][-2:] for r in out] == [f"{i:02d}" for i in range(9, -1, -1)]


def test_full_queue_drops_instead_of_blocking(fake):
    w = storage._BackgroundWriter(maxsize=1)
    gate = threading.Event()

    class Slow:
        def write(self, record):
            gate.wait(5)

    w.submit(Slow(), {})      # taken by the worker, which then blocks on the gate
    time.sleep(0.05)
    w.submit(Slow(), {})      # fills the queue
    t0 = time.monotonic()
    w.submit(Slow(), {})      # queue full → dropped, immediately
    assert time.monotonic() - t0 < 0.1
    assert w.dropped == 1
    gate.set()
    assert w.flush(timeout=5.0)


def test_jsonl_writes_stay_inline(tmp_path, monkeypatch):
    monkeypatch.delenv("UPSTASH_REDIS_REST_URL", raising=False)
    monkeypatch.delenv("UPSTASH_REDIS_REST_TOKEN", raising=False)
    monkeypatch.setenv("ARBITRAGE_HISTORY_FILE", str(tmp_path / "h.jsonl"))
    storage.write_record(_rec("s_a", "t"))
    assert storage._writer.pending() == 0
    assert (tmp_path / "h.jsonl").read_text().strip()


def test_budget_status_reads_history_once(monkeypatch):
    import history
    calls = []
    def fake_read(scope, limit):
        calls.append(scope)
        return [{"provider_id": history._BUDGET_MARKER, "cost_usd": 10.0,
                 "timestamp": "2000-01-01T00:00:00"}]
    monkeypatch.setattr(history, "read_records", fake_read)
    assert history.budget_status("me") is not None
    assert len(calls) == 1


# ─── end to end: routing with a slow Upstash ─────────────────────────────────

@pytest.fixture
def fresh_prices(tmp_path, monkeypatch):
    import importlib
    from datetime import date, timedelta
    real = json.loads(open("prices.json").read())
    real["verified_at"] = (date.today() - timedelta(days=1)).isoformat()
    pf = tmp_path / "prices.json"
    pf.write_text(json.dumps(real))
    monkeypatch.setenv("ARBITRAGE_PRICES_FILE", str(pf))
    monkeypatch.delenv("ARBITRAGE_ALLOW_STALE_PRICES", raising=False)
    import pricing_table, providers, server
    importlib.reload(pricing_table); importlib.reload(providers); importlib.reload(server)
    yield server
    monkeypatch.delenv("ARBITRAGE_PRICES_FILE", raising=False)
    importlib.reload(pricing_table); importlib.reload(providers); importlib.reload(server)


def _route_params(server):
    from providers import PROVIDERS
    return server.RouteCompletionInput(
        messages=[{"role": "user", "content": "hi"}], tier="tier_fast",
        api_keys={p: "sk-test-FAKE" for p in PROVIDERS},
        session="user-1", health_aware=True)


def _ok():
    from client import CompletionResult
    return CompletionResult(text="hi", input_tokens_used=10, output_tokens_used=5,
                            latency_ms=5, model_id="m", raw_response={})


def test_slow_upstash_does_not_block_event_loop(fake, upstash_env, fresh_prices, monkeypatch):
    server = fresh_prices
    fake.delay = 0.3                       # every Upstash request takes 300 ms
    async def provider(**kw):
        return _ok()
    monkeypatch.setattr(server, "call_provider", provider)

    async def main():
        gaps, done = [], asyncio.Event()

        async def ticker():
            last = time.monotonic()
            while not done.is_set():
                await asyncio.sleep(0.02)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        t = asyncio.create_task(ticker())
        out = await server.arbitrage_route_completion(_route_params(server))
        done.set()
        await t
        return json.loads(out), gaps

    out, gaps = asyncio.run(main())
    assert out["response"] == "hi"
    assert len(gaps) >= 10                 # the routing call spanned several Upstash requests
    assert max(gaps) < 0.2                 # ...yet the loop was never stalled for one
