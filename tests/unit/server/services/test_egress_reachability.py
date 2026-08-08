"""Provider-aware relay reachability: the OSS Docker default must work with
zero configuration, an explicit value must always be honored verbatim, and a
remote (Daytona) provider pointed at a local address must produce a warning —
never a silent sandbox-side connection failure."""

from __future__ import annotations

import pytest

from src.server.services.egress.reachability import (
    effective_relay_base_url,
    relay_reachability_warning,
)


@pytest.fixture
def env(monkeypatch):
    def _set(base_url: str, *, is_default: bool) -> None:
        monkeypatch.setattr("src.config.env.EGRESS_RELAY_BASE_URL", base_url)
        monkeypatch.setattr(
            "src.config.env.EGRESS_RELAY_BASE_URL_IS_DEFAULT", is_default
        )

    return _set


class TestEffectiveBaseUrl:
    @pytest.mark.parametrize(
        "base,expected",
        [
            ("http://localhost:8000", "http://host.docker.internal:8000"),
            ("http://127.0.0.1:8060", "http://host.docker.internal:8060"),
            ("http://0.0.0.0:8000", "http://host.docker.internal:8000"),
            ("http://localhost", "http://host.docker.internal"),
        ],
    )
    def test_docker_rewrites_a_defaulted_loopback_to_the_host_gateway(
        self, env, base, expected
    ):
        env(base, is_default=True)
        assert effective_relay_base_url("docker") == expected

    def test_docker_leaves_a_defaulted_public_base_alone(self, env):
        env("https://app.example.com", is_default=True)
        assert effective_relay_base_url("docker") == "https://app.example.com"

    def test_an_explicit_value_is_honored_verbatim_even_when_loopback(self, env):
        env("http://localhost:8000", is_default=False)
        assert effective_relay_base_url("docker") == "http://localhost:8000"

    def test_daytona_never_gets_the_docker_rewrite(self, env):
        env("http://localhost:8000", is_default=True)
        assert effective_relay_base_url("daytona") == "http://localhost:8000"


class TestReachabilityWarning:
    @pytest.mark.parametrize(
        "base",
        [
            "http://localhost:8000",
            "http://127.0.0.1:8060",
            "http://wt3.localhost",
            "http://host.docker.internal:8000",
            "http://10.0.0.5:8000",
            "http://192.168.1.20:8000",
            "http://172.17.0.1:8000",
        ],
    )
    def test_daytona_plus_a_local_address_warns(self, base):
        warning = relay_reachability_warning("daytona", base)
        assert warning is not None
        assert base in warning
        assert "EGRESS_RELAY_BASE_URL" in warning

    @pytest.mark.parametrize(
        "base",
        [
            "https://api.example.com",
            "https://something.trycloudflare.com",
            "http://93.184.216.34:8000",
        ],
    )
    def test_daytona_plus_a_routable_address_is_silent(self, base):
        assert relay_reachability_warning("daytona", base) is None

    def test_local_providers_never_warn(self):
        assert relay_reachability_warning("docker", "http://localhost:8000") is None
