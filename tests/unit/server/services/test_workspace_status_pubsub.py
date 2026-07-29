"""Unit tests for the workspace status pub/sub primitive.

Focus on the contract callers depend on:
- publish_status_change writes a JSON payload to the per-workspace channel
- subscribe_to_status yields a wait() that returns decoded payloads
- Redis-disabled paths are no-ops / return None so callers fall back cleanly
"""

import json

import pytest

from src.server.services import workspace_status_pubsub
from src.server.services.workspace_status_pubsub import (
    publish_status_change,
    status_channel,
    subscribe_to_status,
    wait_for_status_change,
)


class _FakePubsub:
    def __init__(self, messages=None):
        self._messages = list(messages or [])
        self.subscribed: list[str] = []
        self.unsubscribed: list[str] = []
        self.closed = False

    async def subscribe(self, channel):
        self.subscribed.append(channel)

    async def unsubscribe(self, channel):
        self.unsubscribed.append(channel)

    async def aclose(self):
        self.closed = True

    async def get_message(self, ignore_subscribe_messages=True, timeout=None):
        if self._messages:
            return self._messages.pop(0)
        return None


class _FakeRedisClient:
    def __init__(self, pubsub_obj=None):
        self.published: list[tuple[str, str]] = []
        self._pubsub_obj = pubsub_obj

    async def publish(self, channel, payload):
        self.published.append((channel, payload))

    def pubsub(self):
        return self._pubsub_obj if self._pubsub_obj is not None else _FakePubsub()


class _FakeCache:
    def __init__(self, *, enabled, client, url="redis://localhost:6379/0"):
        self.enabled = enabled
        self.client = client
        self.url = url


def _install_cache(monkeypatch, cache):
    monkeypatch.setattr(
        workspace_status_pubsub, "get_cache_client", lambda: cache
    )

    # Stand in for the dedicated pubsub pool. Patched explicitly because the
    # subscriber must never reach the shared cache client on its own — these
    # tests used to pass through the (now deleted) fallback that did exactly
    # that, so the isolation they appear to exercise was fictional.
    async def _fake_pubsub_client(_cache):
        return cache.client

    monkeypatch.setattr(
        workspace_status_pubsub, "_get_pubsub_client", _fake_pubsub_client
    )


@pytest.mark.asyncio
async def test_publish_is_noop_when_redis_disabled(monkeypatch):
    _install_cache(monkeypatch, _FakeCache(enabled=False, client=None))
    # Must not raise even though there's no client.
    await publish_status_change("ws-1", "running")


@pytest.mark.asyncio
async def test_publish_writes_payload_to_channel(monkeypatch):
    client = _FakeRedisClient()
    _install_cache(monkeypatch, _FakeCache(enabled=True, client=client))

    await publish_status_change("ws-abc", "starting")

    assert len(client.published) == 1
    channel, payload = client.published[0]
    assert channel == status_channel("ws-abc")
    assert json.loads(payload) == {"workspace_id": "ws-abc", "status": "starting"}


@pytest.mark.asyncio
async def test_publish_merges_extra_fields(monkeypatch):
    client = _FakeRedisClient()
    _install_cache(monkeypatch, _FakeCache(enabled=True, client=client))

    await publish_status_change("ws-1", "running", extra={"src": "winner"})

    _, payload = client.published[0]
    decoded = json.loads(payload)
    assert decoded == {"workspace_id": "ws-1", "status": "running", "src": "winner"}


@pytest.mark.asyncio
async def test_publish_swallows_client_error(monkeypatch):
    class _ExplodingClient(_FakeRedisClient):
        async def publish(self, channel, payload):
            raise RuntimeError("redis down")

    _install_cache(
        monkeypatch, _FakeCache(enabled=True, client=_ExplodingClient())
    )
    # Must not raise — pub/sub is best-effort.
    await publish_status_change("ws-1", "running")


@pytest.mark.asyncio
async def test_subscribe_yields_none_when_redis_disabled(monkeypatch):
    _install_cache(monkeypatch, _FakeCache(enabled=False, client=None))

    async with subscribe_to_status("ws-1") as wait:
        assert wait is None


@pytest.mark.asyncio
async def test_subscribe_yields_none_when_the_dedicated_pool_is_unavailable(
    monkeypatch,
):
    """It must NOT quietly borrow the shared cache pool. A 600s subscription
    held there is the exact shape that exhausted it in production; callers keep
    a DB-poll path precisely so this can degrade instead."""
    client = _FakeRedisClient(pubsub_obj=_FakePubsub())
    monkeypatch.setattr(
        workspace_status_pubsub,
        "get_cache_client",
        lambda: _FakeCache(enabled=True, client=client),
    )

    async def _no_pool(_cache):
        return None

    monkeypatch.setattr(workspace_status_pubsub, "_get_pubsub_client", _no_pool)

    async with subscribe_to_status("ws-1") as wait:
        assert wait is None


@pytest.mark.asyncio
async def test_pool_build_failure_returns_none_and_backs_off(monkeypatch):
    monkeypatch.setattr(workspace_status_pubsub, "_pubsub_client", None)
    monkeypatch.setattr(workspace_status_pubsub, "_pubsub_retry_after", 0.0)
    calls = {"n": 0}

    def _boom(*_args, **_kwargs):
        calls["n"] += 1
        raise ValueError("unparseable url")

    monkeypatch.setattr(
        workspace_status_pubsub.ConnectionPool, "from_url", staticmethod(_boom)
    )

    cache = _FakeCache(enabled=True, client=_FakeRedisClient())
    assert await workspace_status_pubsub._get_pubsub_client(cache) is None
    # Second call inside the cooldown must not retry a build that can't work.
    assert await workspace_status_pubsub._get_pubsub_client(cache) is None
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_subscribe_yields_wait_and_decodes_payload(monkeypatch):
    payload = json.dumps({"workspace_id": "ws-1", "status": "running"})
    pubsub = _FakePubsub([{"type": "message", "data": payload.encode()}])
    client = _FakeRedisClient(pubsub_obj=pubsub)
    _install_cache(monkeypatch, _FakeCache(enabled=True, client=client))

    async with subscribe_to_status("ws-1") as wait:
        assert wait is not None
        msg = await wait(0.1)
        assert msg == {"workspace_id": "ws-1", "status": "running"}

    # Cleanup happens in the contextmanager __aexit__.
    assert pubsub.subscribed == [status_channel("ws-1")]
    assert pubsub.unsubscribed == [status_channel("ws-1")]
    assert pubsub.closed is True


@pytest.mark.asyncio
async def test_subscribe_decodes_string_payload(monkeypatch):
    payload = json.dumps({"workspace_id": "ws-1", "status": "error"})
    pubsub = _FakePubsub([{"type": "message", "data": payload}])
    _install_cache(
        monkeypatch,
        _FakeCache(enabled=True, client=_FakeRedisClient(pubsub_obj=pubsub)),
    )

    async with subscribe_to_status("ws-1") as wait:
        assert await wait(0.1) == {"workspace_id": "ws-1", "status": "error"}


@pytest.mark.asyncio
async def test_subscribe_returns_none_for_non_message(monkeypatch):
    pubsub = _FakePubsub([{"type": "subscribe", "data": 1}])
    _install_cache(
        monkeypatch,
        _FakeCache(enabled=True, client=_FakeRedisClient(pubsub_obj=pubsub)),
    )

    async with subscribe_to_status("ws-1") as wait:
        assert await wait(0.1) is None


@pytest.mark.asyncio
async def test_subscribe_returns_none_on_invalid_json(monkeypatch):
    pubsub = _FakePubsub([{"type": "message", "data": "not-json"}])
    _install_cache(
        monkeypatch,
        _FakeCache(enabled=True, client=_FakeRedisClient(pubsub_obj=pubsub)),
    )

    async with subscribe_to_status("ws-1") as wait:
        assert await wait(0.1) is None


@pytest.mark.asyncio
async def test_subscribe_yields_none_when_subscribe_raises(monkeypatch):
    class _FailingPubsub(_FakePubsub):
        async def subscribe(self, channel):
            raise RuntimeError("subscribe failed")

    pubsub = _FailingPubsub()
    _install_cache(
        monkeypatch,
        _FakeCache(enabled=True, client=_FakeRedisClient(pubsub_obj=pubsub)),
    )

    async with subscribe_to_status("ws-1") as wait:
        # Subscribe failure must downgrade to "no pub/sub" so callers fall
        # back to DB polling instead of raising mid-request.
        assert wait is None


@pytest.mark.asyncio
async def test_wait_paces_on_get_message_error(monkeypatch):
    """A broken pubsub connection (get_message raises) must return None AND
    sleep, so looping callers don't busy-spin DB reads until their deadline."""

    class _ErroringPubsub(_FakePubsub):
        async def get_message(self, ignore_subscribe_messages=True, timeout=None):
            raise RuntimeError("connection reset")

    _install_cache(
        monkeypatch,
        _FakeCache(enabled=True, client=_FakeRedisClient(pubsub_obj=_ErroringPubsub())),
    )

    slept: list[float] = []

    async def _fake_sleep(delay):
        slept.append(delay)

    monkeypatch.setattr(workspace_status_pubsub.asyncio, "sleep", _fake_sleep)

    async with subscribe_to_status("ws-1") as wait:
        assert await wait(0.1) is None

    # Floored at the caller's timeout (capped at 1.0s).
    assert slept == [0.1]


@pytest.mark.asyncio
async def test_wait_for_status_change_returns_payload(monkeypatch):
    payload = json.dumps({"workspace_id": "ws-1", "status": "running"})
    pubsub = _FakePubsub([{"type": "message", "data": payload.encode()}])
    _install_cache(
        monkeypatch,
        _FakeCache(enabled=True, client=_FakeRedisClient(pubsub_obj=pubsub)),
    )

    result = await wait_for_status_change("ws-1", timeout=0.1)
    assert result == {"workspace_id": "ws-1", "status": "running"}


@pytest.mark.asyncio
async def test_wait_for_status_change_returns_none_when_disabled(monkeypatch):
    _install_cache(monkeypatch, _FakeCache(enabled=False, client=None))
    assert await wait_for_status_change("ws-1", timeout=0.05) is None
