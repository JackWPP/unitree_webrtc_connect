"""Telemetry formatting: raw topic payloads → compact, model-friendly dicts.

Two robots, two shapes:

* **R1** splits what the Go2 packs into one message across four topics, and
  its per-motor ``temperature`` is a two-element array ``[casing, winding]``
  of *signed bytes* (250 means −6 °C, not 250 °C).
* **Go2** nests BMS and IMU inside ``rt/lf/lowstate`` and carries a scalar
  motor temperature.

Battery voltages arrive in millivolts everywhere, so they are converted to
volts here rather than in the model's head.
"""

from __future__ import annotations

import contextlib
from typing import Any, Dict, List, Optional

from unitree_webrtc_connect.constants import (
    MOTOR_NAMES_HUMANOID,
    R1_MOTOR_INDICES,
    RTC_TOPIC,
    sign_byte,
)

#: Motor array slots that carry real data on the 29-slot humanoid layout.
R1_SLOTS = list(R1_MOTOR_INDICES)

# ── read budgets ──────────────────────────────────────────────────────
#
# Named, because the console wraps this call in a timeout of its own and that
# ceiling has to clear the real worst case. It used to be derived by parsing
# this file's source for `timeout=` literals, which silently under-counts the
# moment a read sits in a loop — and the state read below does exactly that.

#: `lowstate` is the one that must arrive; it gets the longest.
LOW_TIMEOUT_S = 2.0
#: Secondary topics: worth waiting for, not worth stalling the call over.
AUX_TIMEOUT_S = 1.0
#: State topics are tried in order, so a silent first candidate costs a full
#: slot before the fallback is even attempted.
SPORT_TIMEOUT_S = 1.0

#: How many state topics a Go2 will try. Keep in step with
#: `Go2Profile.state_topics`.
GO2_STATE_TOPICS = 2

#: Worst case for one full read, per model.
GO2_WORST_CASE_S = LOW_TIMEOUT_S + SPORT_TIMEOUT_S * GO2_STATE_TOPICS + AUX_TIMEOUT_S
R1_WORST_CASE_S = LOW_TIMEOUT_S * 2 + AUX_TIMEOUT_S * 2

#: The largest of them. A caller's own ceiling must exceed this or every read
#: times out on a robot with one silent topic — which is a real Go2, and was a
#: real bug.
WORST_CASE_S = max(GO2_WORST_CASE_S, R1_WORST_CASE_S)


async def first_topic(session, topics, *, timeout: float):
    """The first of `topics` that answers, or `(None, None)`.

    For scalar reads — the FSM id is one number and any topic carrying it will
    do — so the first answer ends the search.
    """
    for topic in topics:
        payload = await session.read_topic(topic, timeout=timeout)
        if payload is not None:
            return topic, payload
    return None, None


async def merge_topics(session, topics, *, timeout: float):
    """Read every candidate and merge them, earlier topics winning conflicts.

    They are **not alternatives**. `rt/lf/sportmodestate` carries `fsm_id`,
    which is what a humanoid reads, and `rt/sportmodestate` carries
    `gait_type`, `foot_raise_height` and `position`, which is what a quadruped
    reads. Taking the first topic that answers therefore silently drops the
    other's fields — on the mock that cost the gait readout entirely, and the
    only reason it was noticed is that a test asserted the field existed.

    Costs one read slot per topic, which is why the budget below counts them.
    """
    merged: Dict[str, Any] = {}
    for topic in topics:
        payload = await session.read_topic(topic, timeout=timeout)
        if not isinstance(payload, dict):
            continue
        for key, value in payload.items():
            merged.setdefault(key, value)
    return merged or None


def _num(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return default


def _temperature(value: Any) -> Optional[int]:
    """Normalise a SignedByte temperature field."""
    if isinstance(value, (list, tuple)):
        value = value[1] if len(value) > 1 else (value[0] if value else None)
    if value is None:
        return None
    with contextlib.suppress(TypeError, ValueError):
        return int(sign_byte(int(value)))
    return None


def r1_motors(low: dict, *, only_driven: bool = True) -> List[dict]:
    motors = (low or {}).get("motor_state") or []
    rows: List[dict] = []
    indices = R1_SLOTS if only_driven else range(len(motors))
    for index in indices:
        if index >= len(motors):
            continue
        motor = motors[index] or {}
        raw_temp = motor.get("temperature")
        casing = winding = None
        if isinstance(raw_temp, (list, tuple)):
            casing = _temperature(raw_temp[0]) if len(raw_temp) > 0 else None
            winding = _temperature(raw_temp[1]) if len(raw_temp) > 1 else None
        else:
            casing = _temperature(raw_temp)
        rows.append(
            {
                "index": index,
                "joint": MOTOR_NAMES_HUMANOID[index] if index < len(MOTOR_NAMES_HUMANOID) else f"#{index}",
                "q_rad": _num(motor.get("q")),
                "dq": _num(motor.get("dq")),
                "tau_est": _num(motor.get("tau_est")),
                "temp_casing_c": casing,
                "temp_winding_c": winding,
                "lost": motor.get("lost"),
            }
        )
    return rows


def go2_motors(low: dict) -> List[dict]:
    motors = (low or {}).get("motor_state") or []
    rows = []
    for index, motor in enumerate(motors):
        motor = motor or {}
        rows.append(
            {
                "index": index,
                "q_rad": _num(motor.get("q")),
                "dq": _num(motor.get("dq")),
                "tau_est": _num(motor.get("tau_est")),
                "temp_c": _temperature(motor.get("temperature")),
                "lost": motor.get("lost"),
            }
        )
    return rows


def _volts(millivolts: Any) -> Optional[float]:
    value = _num(millivolts)
    return round(value / 1000.0, 3) if value else None


def r1_battery(bms: Optional[dict], low: Optional[dict] = None) -> dict:
    bms = bms or {}
    low = low or {}
    raw_volts = bms.get("bmsvoltage") or []
    pack_mv = bms.get("pack_voltage") or (raw_volts[0] if len(raw_volts) > 0 else None)
    bat_mv = bms.get("bat_voltage") or (raw_volts[1] if len(raw_volts) > 1 else None)
    cells = [c for c in (bms.get("cell_vol") or []) if isinstance(c, (int, float)) and c > 0]
    temps = [t for t in (bms.get("temperature") or bms.get("temps") or []) if isinstance(t, (int, float))]
    out = {
        "soc_percent": bms.get("soc"),
        "current_ma": bms.get("current"),
        "cycles": bms.get("cycle"),
        "pack_voltage_v": _volts(pack_mv),
        "battery_voltage_v": _volts(bat_mv),
    }
    if cells:
        out["cell_count"] = len(cells)
        out["cell_min_mv"] = min(cells)
        out["cell_max_mv"] = max(cells)
        out["cell_spread_mv"] = max(cells) - min(cells)
    if temps:
        out["temp_mos_c"] = temps[0]
        if len(temps) > 2:
            out["temp_battery_c"] = temps[2]
    if not bms and low:
        out["note"] = "No rt/lf/bmsstate message received yet."
    return out


def go2_battery(low: Optional[dict]) -> dict:
    bms = ((low or {}).get("bms_state") or {})
    out = {
        "soc_percent": bms.get("soc"),
        "current_ma": bms.get("current"),
        "cycles": bms.get("cycle"),
        "temp_bq_ntc_c": bms.get("bq_ntc"),
        "temp_mcu_ntc_c": bms.get("mcu_ntc"),
    }
    version_high, version_low = bms.get("version_high"), bms.get("version_low")
    if version_high is not None or version_low is not None:
        out["bms_version"] = f"{version_high}.{version_low}"
    power_v = _num((low or {}).get("power_v"))
    if power_v is not None:
        out["power_voltage_v"] = power_v
    return out


def _imu(payload: Optional[dict]) -> dict:
    payload = payload or {}
    rpy = payload.get("rpy") or []
    out: Dict[str, Any] = {}
    if len(rpy) >= 3:
        out["roll_rad"] = _num(rpy[0])
        out["pitch_rad"] = _num(rpy[1])
        out["yaw_rad"] = _num(rpy[2])
    temperature = payload.get("temperature")
    if temperature is not None:
        out["temperature_c"] = _temperature(temperature) if temperature > 127 else temperature
    return out


async def collect_lowstate(session, profile, *, include_motors: bool = True) -> dict:
    """Read every telemetry topic for the configured robot."""
    backend = session.backend
    robot = profile.name

    if robot == "R1":
        low = await session.read_topic(RTC_TOPIC["LOW_STATE"], timeout=LOW_TIMEOUT_S)
        bms = await session.read_topic(RTC_TOPIC["BMS_STATE"], timeout=LOW_TIMEOUT_S)
        pelvis = await session.read_topic(RTC_TOPIC["SECONDARY_IMU"], timeout=AUX_TIMEOUT_S)
        board = await session.read_topic(RTC_TOPIC["MAIN_BOARD_STATE"], timeout=AUX_TIMEOUT_S)

        result: Dict[str, Any] = {
            "robot": robot,
            "battery": r1_battery(bms, low),
            "imu_torso": _imu((low or {}).get("imu_state")),
            "imu_pelvis": _imu(pelvis),
        }
        if board:
            temps = board.get("temperature") or []
            if temps:
                result["mainboard_temp_c"] = _temperature(temps[0])
        if include_motors:
            result["motors"] = r1_motors(low or {})
        if not low:
            result["note"] = (
                "No rt/lf/lowstate message received. The robot may be off, "
                "busy, or the topic may not be published in the current state."
            )
        return result

    # Go2
    low = await session.read_topic(RTC_TOPIC["LOW_STATE"], timeout=LOW_TIMEOUT_S)
    multi = await session.read_topic(RTC_TOPIC["MULTIPLE_STATE"], timeout=AUX_TIMEOUT_S)
    sport = await merge_topics(
        session, profile.state_candidates, timeout=SPORT_TIMEOUT_S
    )
    result = {
        "robot": robot,
        "battery": go2_battery(low),
        "imu": _imu((low or {}).get("imu_state")),
        "foot_force": (low or {}).get("foot_force"),
        "temperature_ntc1_c": (low or {}).get("temperature_ntc1"),
    }
    if include_motors:
        result["motors"] = go2_motors(low or {})
    if multi:
        result["aggregate_state"] = multi if isinstance(multi, dict) else _maybe_json(multi)
    if sport:
        result["sport_state"] = {
            key: sport.get(key)
            for key in ("mode", "gait_type", "body_height", "foot_raise_height", "velocity", "position")
            if key in sport
        }
    if not low:
        result["note"] = "No rt/lf/lowstate message received yet."
    return result


def _maybe_json(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    import json

    try:
        return json.loads(value)
    except ValueError:
        return value
