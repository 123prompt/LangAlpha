"""Per-grant rate + concurrency limits for the egress relay.

Limits are protective plumbing rather than the security boundary, so the two
contracts worth pinning are: they bind per grant (one saturated connector never
starves another), and every Redis failure path yields instead of taking the
relay down.
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from types import SimpleNamespace

import pytest

from src.server.services.egress import limits as limits_mod
from src.server.services.egress.limits import (
    CONCURRENCY_LIMIT,
    RATE_LIMIT_RPM,
    RelayLimited,
    acquire_slot,
)
from tests.unit.redis_mock_pipeline import attach_pipeline

GRANT_A = "grant-egress-a"
GRANT_B = "grant-egress-b"

# Two deliberately tiny budgets so boundary arms stay cheap, each applied by a
# fixture that isolates a single dimension (a low rpm would otherwise trip
# first in the concurrency arms). The shipped budgets get their own test.
TIGHT_RPM = 2
NARROW_CONCURRENCY = 2


class _FakeRedis:
    """Only the commands acquire_slot issues: incr/expire/decr."""

    def __init__(self) -> None:
        self.values: dict[str, int] = {}
        self.ttls: dict[str, int] = {}
        self.fail: set[str] = set()
        # Fail incr calls from this 0-based index on — lets a test kill Redis
        # BETWEEN the rate round trip and the concurrency one.
        self.fail_incr_from: int | None = None
        self.incr_seen = 0
        self.calls: list[tuple[str, str]] = []

    def _guard(self, command: str) -> None:
        if command in self.fail:
            raise ConnectionError(f"redis {command} unavailable")

    async def incr(self, key: str) -> int:
        self.calls.append(("incr", key))
        index = self.incr_seen
        self.incr_seen += 1
        if self.fail_incr_from is not None and index >= self.fail_incr_from:
            raise ConnectionError("redis incr unavailable")
        self._guard("incr")
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    async def decr(self, key: str) -> int:
        self.calls.append(("decr", key))
        self._guard("decr")
        self.values[key] = self.values.get(key, 0) - 1
        return self.values[key]

    async def expire(self, key: str, ttl: int) -> bool:
        self.calls.append(("expire", key))
        self._guard("expire")
        self.ttls[key] = ttl
        return True


@pytest.fixture
def redis(monkeypatch):
    client = _FakeRedis()
    attach_pipeline(client)
    cache = SimpleNamespace(enabled=True, client=client)
    monkeypatch.setattr(
        "src.utils.cache.redis_cache.get_cache_client", lambda: cache
    )
    return client


@pytest.fixture
def tight_rate(monkeypatch):
    """A 2/minute budget, with concurrency left generous so rate trips first."""
    monkeypatch.setattr(limits_mod, "RATE_LIMIT_RPM", TIGHT_RPM)
    monkeypatch.setattr(limits_mod, "CONCURRENCY_LIMIT", 8)


@pytest.fixture
def narrow_concurrency(monkeypatch):
    """A 2-slot cap, with rpm left high so concurrency trips first."""
    monkeypatch.setattr(limits_mod, "RATE_LIMIT_RPM", 1000)
    monkeypatch.setattr(limits_mod, "CONCURRENCY_LIMIT", NARROW_CONCURRENCY)


@pytest.fixture
def frozen_clock(monkeypatch):
    """Pin the module's minute bucket so rate keys are stable within a test."""
    clock = SimpleNamespace(now=1_700_000_000.0)
    monkeypatch.setattr(
        limits_mod, "time", SimpleNamespace(time=lambda: clock.now)
    )
    return clock


def _conc_key(grant_id: str) -> str:
    return f"egress:conc:{grant_id}"


# ---------------------------------------------------------------------------
# Rate
# ---------------------------------------------------------------------------


class TestRateLimit:
    @pytest.mark.asyncio
    async def test_allows_up_to_the_budget_then_denies(self, redis, frozen_clock, tight_rate):
        for _ in range(TIGHT_RPM):
            async with acquire_slot(GRANT_A):
                pass

        with pytest.raises(RelayLimited) as excinfo:
            async with acquire_slot(GRANT_A):
                pass
        assert excinfo.value.kind == "rate"

    @pytest.mark.asyncio
    async def test_the_shipped_budget_is_the_one_enforced(self, redis, frozen_clock):
        """No fixture override: the constants the relay actually runs with."""
        for _ in range(RATE_LIMIT_RPM):
            async with acquire_slot(GRANT_A):
                pass

        with pytest.raises(RelayLimited) as excinfo:
            async with acquire_slot(GRANT_A):
                pass
        assert excinfo.value.kind == "rate"

    @pytest.mark.asyncio
    async def test_a_denied_request_takes_no_concurrency_slot(
        self, redis, frozen_clock, tight_rate
    ):
        for _ in range(TIGHT_RPM):
            async with acquire_slot(GRANT_A):
                pass
        assert redis.values[_conc_key(GRANT_A)] == 0

        with pytest.raises(RelayLimited):
            async with acquire_slot(GRANT_A):
                pass
        assert redis.values[_conc_key(GRANT_A)] == 0

    @pytest.mark.asyncio
    async def test_budget_resets_on_the_next_minute_bucket(self, redis, frozen_clock, tight_rate):
        for _ in range(TIGHT_RPM):
            async with acquire_slot(GRANT_A):
                pass
        with pytest.raises(RelayLimited):
            async with acquire_slot(GRANT_A):
                pass

        frozen_clock.now += 60
        async with acquire_slot(GRANT_A):
            pass

        rate_keys = {k for k in redis.values if k.startswith("egress:rate:")}
        assert len(rate_keys) == 2

    @pytest.mark.asyncio
    async def test_counters_carry_a_ttl(self, redis, frozen_clock):
        async with acquire_slot(GRANT_A):
            pass

        assert set(redis.ttls) == {
            f"egress:rate:{GRANT_A}:{int(frozen_clock.now // 60)}",
            _conc_key(GRANT_A),
        }
        assert all(ttl > 0 for ttl in redis.ttls.values())


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


class TestConcurrencyLimit:
    @pytest.mark.asyncio
    async def test_holds_a_slot_for_the_body_and_releases_on_exit(
        self, redis, frozen_clock, narrow_concurrency
    ):
        async with acquire_slot(GRANT_A):
            assert redis.values[_conc_key(GRANT_A)] == 1
        assert redis.values[_conc_key(GRANT_A)] == 0

    @pytest.mark.asyncio
    async def test_the_shipped_cap_is_the_one_enforced(self, redis, frozen_clock):
        """No fixture override: the constants the relay actually runs with."""
        async with AsyncExitStack() as held:
            for _ in range(CONCURRENCY_LIMIT):
                await held.enter_async_context(acquire_slot(GRANT_A))

            with pytest.raises(RelayLimited) as excinfo:
                async with acquire_slot(GRANT_A):
                    pass
            assert excinfo.value.kind == "concurrency"

    @pytest.mark.asyncio
    async def test_over_limit_acquisition_is_denied(self, redis, frozen_clock, narrow_concurrency):
        cap = NARROW_CONCURRENCY
        async with AsyncExitStack() as held:
            for _ in range(cap):
                await held.enter_async_context(acquire_slot(GRANT_A))
            assert redis.values[_conc_key(GRANT_A)] == cap

            with pytest.raises(RelayLimited) as excinfo:
                async with acquire_slot(GRANT_A):
                    pass
            assert excinfo.value.kind == "concurrency"
            # The refused attempt gives its own increment back, so a burst of
            # denials cannot wedge the counter above the cap forever.
            assert redis.values[_conc_key(GRANT_A)] == cap

    @pytest.mark.asyncio
    async def test_a_released_slot_frees_capacity(self, redis, frozen_clock, narrow_concurrency):
        cap = NARROW_CONCURRENCY
        async with AsyncExitStack() as held:
            for _ in range(cap - 1):
                await held.enter_async_context(acquire_slot(GRANT_A))

            async with acquire_slot(GRANT_A):
                with pytest.raises(RelayLimited):
                    async with acquire_slot(GRANT_A):
                        pass

            # The cap-th holder exited; the next caller fits again.
            async with acquire_slot(GRANT_A):
                assert redis.values[_conc_key(GRANT_A)] == cap

    @pytest.mark.asyncio
    async def test_slot_is_released_when_the_body_raises(self, redis, frozen_clock, narrow_concurrency):
        with pytest.raises(RuntimeError):
            async with acquire_slot(GRANT_A):
                raise RuntimeError("relayed request blew up")

        assert redis.values[_conc_key(GRANT_A)] == 0


# ---------------------------------------------------------------------------
# Per-grant keying
# ---------------------------------------------------------------------------


class TestPerGrantIsolation:
    @pytest.mark.asyncio
    async def test_a_saturated_grant_does_not_block_another(self, redis, frozen_clock, narrow_concurrency):
        cap = NARROW_CONCURRENCY
        async with AsyncExitStack() as held:
            for _ in range(cap):
                await held.enter_async_context(acquire_slot(GRANT_A))
            with pytest.raises(RelayLimited):
                async with acquire_slot(GRANT_A):
                    pass

            async with acquire_slot(GRANT_B):
                assert redis.values[_conc_key(GRANT_B)] == 1

    @pytest.mark.asyncio
    async def test_rate_budgets_are_counted_per_grant(self, redis, frozen_clock, tight_rate):
        for _ in range(TIGHT_RPM):
            async with acquire_slot(GRANT_A):
                pass
        with pytest.raises(RelayLimited):
            async with acquire_slot(GRANT_A):
                pass

        async with acquire_slot(GRANT_B):
            pass

        minute = int(frozen_clock.now // 60)
        assert redis.values[f"egress:rate:{GRANT_B}:{minute}"] == 1


# ---------------------------------------------------------------------------
# Fail-open
# ---------------------------------------------------------------------------


class TestFailsOpen:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "cache",
        [
            SimpleNamespace(enabled=False, client=object()),
            SimpleNamespace(enabled=True, client=None),
        ],
        ids=["cache-disabled", "no-client"],
    )
    async def test_unavailable_redis_yields(self, monkeypatch, cache):
        monkeypatch.setattr(
            "src.utils.cache.redis_cache.get_cache_client", lambda: cache
        )
        entered = False
        async with acquire_slot(GRANT_A):
            entered = True
        assert entered is True

    @pytest.mark.asyncio
    async def test_failed_rate_check_yields(self, redis, frozen_clock):
        redis.fail.add("incr")

        entered = False
        async with acquire_slot(GRANT_A):
            entered = True

        assert entered is True
        assert ("decr", _conc_key(GRANT_A)) not in redis.calls

    @pytest.mark.asyncio
    async def test_failed_concurrency_check_yields(self, redis, frozen_clock):
        # Regression: Redis dying BETWEEN the rate round trip and the
        # concurrency one used to escape acquire_slot as a raw exception
        # (a relay 500), while the docstring promises fail-open for both.
        redis.fail_incr_from = 1  # rate incr succeeds, concurrency incr dies

        entered = False
        async with acquire_slot(GRANT_A):
            entered = True

        assert entered is True
        # Nothing was acquired, so nothing must be released.
        assert ("decr", _conc_key(GRANT_A)) not in redis.calls

    @pytest.mark.asyncio
    async def test_failed_release_does_not_surface_to_the_caller(
        self, redis, frozen_clock
    ):
        async with acquire_slot(GRANT_A):
            redis.fail.add("decr")

        assert ("decr", _conc_key(GRANT_A)) in redis.calls
