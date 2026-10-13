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
