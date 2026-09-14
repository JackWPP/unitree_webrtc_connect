"""Session lifecycle: lazy connect, single-client handling, idle release."""

from __future__ import annotations

import asyncio

import pytest

from unitree_robot_agent.errors import CommandTimeout, RobotBusy, RobotRefused
from unitree_robot_agent.runtime import create_runtime

from .conftest import make_config


async def test_connects_lazily_and_only_once(r1_runtime):
    session = r1_runtime.session
    assert session.is_connected is False
    await session.ensure_connected()
    await session.ensure_connected()
    assert session.is_connected is True
    assert session.stats.connects == 1
    await r1_runtime.aclose()


async def test_robot_busy_is_typed_and_actionable(r1_runtime):
    r1_runtime.backend.set_busy(True)
    with pytest.raises(RobotBusy) as excinfo:
        await r1_runtime.session.ensure_connected()
    assert "unitree_ui" in excinfo.value.hint
    await r1_runtime.aclose()


async def test_idle_watchdog_releases_the_robot():
    runtime = create_runtime(make_config(idle_disconnect_s=0.4))
    session = runtime.session
    await session.ensure_connected()
    assert session.is_connected is True
    await asyncio.sleep(1.6)
    assert session.is_connected is False, "the link must be freed for the official app"
    assert session.stats.disconnects >= 1
    await runtime.aclose()


async def test_hold_prevents_the_idle_reaper():
    runtime = create_runtime(make_config(idle_disconnect_s=0.4))
    session = runtime.session
    await session.ensure_connected()
    async with session.hold("test"):
        await asyncio.sleep(1.2)
        assert session.is_connected is True
    await asyncio.sleep(1.2)
    assert session.is_connected is False
    await runtime.aclose()


async def test_unhandled_api_id_reads_as_silence_not_refusal(r1_runtime):
    from unitree_webrtc_connect.constants import LOCO_API, RTC_TOPIC

    response = await r1_runtime.session.call(
        RTC_TOPIC["SPORT_MOD"], LOCO_API["GET_FSM_MODE"]
    )
    assert response.timed_out is True
    assert response.code == -1
    assert response.answered is False
    await r1_runtime.aclose()


async def test_refusal_carries_the_fsm_hint(r1_runtime):
    from unitree_webrtc_connect.constants import LOCO_API, RTC_TOPIC

    with pytest.raises(RobotRefused) as excinfo:
        await r1_runtime.session.call_checked(
            RTC_TOPIC["SPORT_MOD"], LOCO_API["SET_FSM_ID"], {"data": 9999}
        )
    assert excinfo.value.robot_code == 1003
    assert "firmware" in excinfo.value.hint
    await r1_runtime.aclose()


async def test_timeout_is_distinct_from_refusal():
    runtime = create_runtime(make_config(request_timeout_s=0.05))
    from unitree_webrtc_connect.constants import LOCO_API, RTC_TOPIC

    await runtime.session.ensure_connected()
    runtime.backend.latency = 0.5
    response = await runtime.session.call(
        RTC_TOPIC["SPORT_MOD"], LOCO_API["GET_FSM_ID"], timeout=0.05
    )
    assert response.timed_out is True
    assert response.answered is False
    await runtime.aclose()


async def test_safe_stop_zeroes_motion_without_damping(r1_runtime):
    session = r1_runtime.session
    await session.ensure_connected()
    backend = r1_runtime.backend

    from unitree_webrtc_connect.constants import LOCO_API, RTC_TOPIC

    await session.call(
        RTC_TOPIC["SPORT_MOD"], LOCO_API["SET_VELOCITY"], {"velocity": [0.5, 0, 0], "duration": 5}
    )
    assert backend.velocity != [0.0, 0.0, 0.0]

    assert await session.safe_stop(reason="test") is True
    assert backend.velocity == [0.0, 0.0, 0.0]
    # A stop must preserve the posture: Damping is 1, the mock started there,
    # and `_r1_sport` would have moved to 4 only for a Lock request.
    assert backend.fsm_id == 1
    await r1_runtime.aclose()


async def test_emergency_stop_damps(r1_runtime):
    session = r1_runtime.session
    await session.ensure_connected()
    from unitree_webrtc_connect.constants import LOCO_API, R1_FSM, RTC_TOPIC

    await session.call(RTC_TOPIC["SPORT_MOD"], LOCO_API["SET_FSM_ID"], {"data": R1_FSM["Lock"]})
    assert r1_runtime.backend.fsm_id == 4
    assert await session.emergency_stop(reason="test") is True
    assert r1_runtime.backend.fsm_id == 1
    await r1_runtime.aclose()


async def test_state_read_is_a_cache_hit_after_the_first_call(r1_runtime):
    session = r1_runtime.session
    await session.ensure_connected()
    first = await session.read_topic(r1_runtime.profile.state_topic, timeout=2.0)
    assert first is not None
    cache_before = len(getattr(r1_runtime.backend, "_subscribed", ()))
    second = await session.read_topic(r1_runtime.profile.state_topic, timeout=2.0)
    assert second is not None
    assert len(getattr(r1_runtime.backend, "_subscribed", ())) == cache_before
    await r1_runtime.aclose()


async def test_command_timeout_error_is_typed():
    runtime = create_runtime(make_config(request_timeout_s=0.05))
    await runtime.session.ensure_connected()
    runtime.backend.latency = 0.5
    from unitree_webrtc_connect.constants import LOCO_API, RTC_TOPIC

    with pytest.raises(CommandTimeout):
        await runtime.session.call_checked(
            RTC_TOPIC["SPORT_MOD"], LOCO_API["GET_FSM_ID"], timeout=0.05
        )
    await runtime.aclose()
