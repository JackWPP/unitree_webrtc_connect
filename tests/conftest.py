"""Shared fixtures.

Every test runs against the **mock backend**, so the suite needs no robot,
no network and no AES key.
"""

from __future__ import annotations

import pytest

from unitree_robot_agent.config import AgentConfig
from unitree_robot_agent.runtime import Runtime, create_runtime


def make_config(**overrides) -> AgentConfig:
    base = dict(
        robot="R1",
        backend="mock",
        connection="sta-l",
        idle_disconnect_s=0.0,       # no background reaping unless a test asks
        min_command_interval_s=0.0,  # keep the suite fast
        audit_enabled=False,
        log_level="ERROR",
    )
    base.update(overrides)
    return AgentConfig(**base)


@pytest.fixture
def r1_config() -> AgentConfig:
    return make_config(robot="R1")


@pytest.fixture
def go2_config() -> AgentConfig:
    return make_config(robot="Go2")


@pytest.fixture
def r1_runtime(r1_config) -> Runtime:
    return create_runtime(r1_config)


@pytest.fixture
def go2_runtime(go2_config) -> Runtime:
    return create_runtime(go2_config)


@pytest.fixture
def invoke():
    """Call one registered tool through the bridge, the way the CLI does."""
    from unitree_robot_agent.bridge import ToolBridge

    async def _invoke(bundle, name, arguments=None):
        return await ToolBridge(bundle).call(name, arguments or {})

    return _invoke


# ── the console's HTTP fixture, shared ────────────────────────────────
#
# Moved here from `test_console_app` so that any test module can open the
# console without copying the roster. Two copies of a fixture is how two
# modules end up testing subtly different applications.


def _console_app(tmp_path, *, token: str = ""):
    from unitree_robot_agent.fleet import FleetManager, Roster
    from unitree_fleet_web.app import create_app
    from unitree_fleet_web.config import ConsoleConfig
    from unitree_fleet_web.service import ConsoleService

    roster = Roster.from_mapping(
        {
            "state_dir": str(tmp_path / "state"),
            "default": "dog1",
            "robots": [
                {"id": "dog1", "model": "Go2", "ip": "192.168.31.245", "sn": "B42D2000AAAA"},
                {"id": "dog2", "model": "Go2", "ip": "192.168.31.246", "sn": "B42D2000BBBB"},
            ],
        }
    )
    fleet = FleetManager(roster, backend="mock", audit_to_stderr=False)
    config = ConsoleConfig(host="127.0.0.1", token=token, deadman_s=0.2)
    service = ConsoleService(fleet, config=config)
    app = create_app(service, config)
    app._console_service = service
    return app


import pytest as _pytest


@_pytest.fixture
def open_client(tmp_path):
    """No token: the loopback-only case."""
    from starlette.testclient import TestClient

    with TestClient(_console_app(tmp_path)) as client:
        yield client
