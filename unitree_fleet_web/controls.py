"""Declarative control descriptors for the fleet console.

Why a table instead of one widget per tool
------------------------------------------
The console used to hardcode a button per tool in ``static/console.js``:
adding a tool meant touching HTML, CSS and JavaScript, and every new model
re-argued which widgets it deserved. This module inverts that. It publishes
*data* — one dict per control — and the frontend renders whatever it is given
from ``kind`` alone. Consequences that matter:

* a capability the robot did not register simply is not in the list, so the
  panel can never offer something that will only fail
  (``service.tools_available`` is the filter),
* Go2-only and R1-only controls live in the same table with the model as the
  only difference, so "the R1 must never show a LiDAR button" is a data fact
  rather than a branch in the browser,
* the safety-relevant metadata (``confirm``, ``read``) travels with the
  control, so the UI never has to know which tool is dangerous.

The module is deliberately dependency-free: it imports nothing from
``unitree_robot_agent`` or ``unitree_webrtc_connect`` at import time. The
console is designed to run without the ``agent`` extra (the tool bridge is
optional, see ``service.ToolBridge``), so a control panel must not be the
thing that drags MCP in. The values below are therefore *copies* of the
source tables, pinned against them by ``tests/test_console_controls.py``,
which imports the real profiles, tool modules and registry and fails if any
option, range or tool name drifts.

Provenance — every tool name, argument name, range and accepted value below
was read from the source, not guessed:

* ``unitree_robot_agent/tools/locomotion.py``
  - ``robot_stand(confirm=False)`` → R1 Lock (4) / Go2 BalanceStand (1002)
  - ``robot_lie_down(confirm=False)`` → R1 LieDown (702) / Go2 StandDown (1005)
  - ``robot_move(vx, vy, omega, duration_s, confirm)``
  - ``robot_turn(degrees, speed, confirm)`` — the angle argument is
    ``degrees``, **not** ``angle_deg``; the hard limit is ±1080° and
    ``speed=0`` means "let the runtime pick the rate".
  - ``robot_switch_state(state, confirm)``
* ``unitree_robot_agent/tools/actions.py``
  - ``robot_play_action(name, confirm)``
  - ``robot_arm_gesture(gesture)`` — no ``confirm`` parameter exists, so the
    control must not send one.
* ``unitree_robot_agent/tools/common.py``
  - ``robot_lowstate(include_motors=True)``, ``robot_get_posture()``.
* ``unitree_robot_agent/tools/go2.py``
  - ``go2_motion_mode(mode, confirm)``; ``mode`` must be one of
    ``MOTION_MODES`` (``robots/go2.py``) and setting one requires confirm.
  - ``go2_obstacle_avoid(enable: Optional[bool])`` — ``None`` reads.
  - ``go2_set_led(color, seconds=5, flash_cycle_ms=0)``; ``color`` must be one
    of ``LED_COLORS`` (7 values, mirrored by ``constants.VUI_COLOR``);
    ``flash_cycle_ms`` must be 499..seconds*1000 (left at 0 = steady here).
  - ``go2_set_brightness(level)`` / ``go2_set_volume(volume)`` — both clamped
    to 0..10 inside the tool, so the slider range is 0..10 step 1.
  - ``go2_get_vui()`` — reads volume and brightness **only**; there is no LED
    colour read-back anywhere.
  - ``go2_get_state()``, ``go2_lidar(enable: Optional[bool])``.
  - ``go2_recover(mode, confirm)`` — ``mode`` must be one of
    ``Go2Profile.RECOVER_MODES`` (``robots/go2.py``: ``stand_up`` =
    StandUp(1004), ``recovery_stand`` = RecoveryStand(1006)) and, unlike
    ``robot_stand`` (BalanceStand), it is the one that works on a Go2 that is
    actually lying down.
* ``unitree_webrtc_connect/constants.py`` — ``R1_FSM`` (state names accepted by
  ``robot_switch_state``) and ``ARM_ACTION`` (the constants behind the gesture
  names exposed by ``robots/r1.py``).

What could NOT be determined from the code (each one is modelled
conservatively, i.e. not offered as if it worked):

1. **Gait selection has no tool.** ``SPORT_CMD["SwitchGait"] = 1011`` exists as
   a constant (``unitree_webrtc_connect/constants.py``), but nothing under
   ``unitree_robot_agent/`` ever sends it, the Go2 action catalog
   (``robots/go2.py``, ``_ACTIONS``) contains no gait entry, and
   ``robot_play_action`` resolves names only through that catalog
   (``ctx.profile.resolve_action``). Its parameter shape is not visible
   anywhere in this repo (not even in ``examples/``), so it was not modelled.
   The ``gait`` group therefore holds the only gait-adjacent things an
   operator can actually reach: the catalogued step/gallop sport moves, named
   as one-shot gait-like moves rather than a persistent gait switch. Adding a
   real gait tool later means adding one ``_select`` here — the group is
   already in ``CONTROL_GROUPS``.
2. **The Go2 does not report the gait/mode either.** ``robots/go2.py`` says the
   profile has no FSM and ``classify()`` returns "n/a", and the bring-up notes
   (``docs/DUAL_DOG_CONSOLE_PLAN.md`` P0.6) record that a real Go2 never
   publishes ``rt/sportmodestate`` at all. So ``go2_get_state`` is offered as a
   read, but its ``sport_state`` may legitimately come back null.
3. **No "LED off" value.** The tool validates against the seven colours and a
   time window; there is no documented way to switch the LED off, so no such
   option is offered.
4. **LED colour cannot be read back** (see ``go2_get_vui`` above), so there is
   no ``led`` read control and the UI has nothing to seed that select with.
5. **The ``media`` group is empty.** The only media tools — ``robot_snapshot``,
   ``robot_snapshot_history``, ``robot_vision_probe`` — are absent from
   ``CONSOLE_TOOLS`` (``service.py``), so a control for them could never be
   reached through ``/api/console/controls``. The group stays in
   ``CONTROL_GROUPS`` so the display order does not shift once that changes.
6. **Whether the firmware honours ``advanced``/``mcf``** is robot-dependent.
   The switcher accepts all four ``MOTION_MODES``; ``robots/go2.py`` notes that
   MCF api_ids need the robot to already be in MCF mode, which cannot be
   checked from here.

One unit caveat that is easy to get wrong: ``robot_move``'s ``vx``/``vy``/
``omega`` are **normalised to the robot's own limit** (``locomotion.SPEED_LIMIT
= 1.0``, "1.0 means as fast as the robot allows"), not metres per second. A
"0.15 m/s" nudge cannot be expressed in this tool; 0.15 here means 15 % of
whatever the robot's own maximum is.

Option lists are static on purpose. ``static/console.js`` also understands
``options_from`` (call a tool such as ``robot_list_actions`` and fill the picker
from its rows); this module does not use it, for three reasons: the Go2 catalog
this panel mirrors is a fixed curated table rather than something the firmware
varies, the drift it would guard against is already caught by
``tests/test_console_controls.py``, and a failed ``options_from`` call leaves
the picker disabled with the static options replaced — a control that can only
fail. Static options also keep the Chinese labels, which the dynamic path would
overwrite with raw action names.
"""

from __future__ import annotations

import copy
from typing import Any, Dict, Iterable, List, Mapping, Sequence, Tuple

__all__ = ["CONTROL_GROUPS", "controls_for"]

#: Stable display order of the panel sections. The UI keeps its own Chinese
#: headings (presentation), so these ids are only chrome. Order is a contract:
#: ``controls_for`` returns rows already sorted by it, and
#: ``service.controls_payload`` derives the payload's ``groups`` from the
#: first-seen order of the returned rows.
CONTROL_GROUPS: Tuple[str, ...] = (
    "posture",
    "gait",
    "motion",
    "safety_net",
    "lighting",
    "media",
    "motion_nudge",
)

#: A control descriptor. Keys depend on ``kind`` — see ``_button`` and friends,
#: which are the only places descriptors are built.
Control = Dict[str, Any]

#: ``(value to send, Chinese label)`` for a select's options.
Option = Tuple[Any, str]

#: Widget kind used for read-only controls.
#:
#: Read-only-ness travels in the ``read`` flag, not in ``kind``: the published
#: API allows button/toggle/select/range here, and ``static/console.js``
#: dispatches "click to fetch and show a summary" from ``read`` *before* it
#: looks at ``kind``. A reader is therefore a button that reads.
_READ_KIND = "button"

# ─── source tables, copied and pinned by tests ─────────────────────────

#: ``unitree_robot_agent/tools/go2.py`` LED_COLORS (== constants.VUI_COLOR).
_LED_COLOURS: Tuple[Option, ...] = (
    ("white", "白"),
    ("red", "红"),
    ("yellow", "黄"),
    ("blue", "蓝"),
    ("green", "绿"),
    ("cyan", "青"),
    ("purple", "紫"),
)

#: ``unitree_robot_agent/robots/go2.py`` MOTION_MODES — the exact set
#: ``go2_motion_mode`` validates against.
_MOTION_MODES: Tuple[Option, ...] = (
    ("normal", "普通 normal"),
    ("ai", "AI 模式 ai"),
    ("advanced", "高级 advanced"),
    ("mcf", "MCF 多控制框架 mcf"),
)

#: Ways back onto the feet (``robots/go2.py`` RECOVER_MODES). Not the same
#: command as ``robot_stand``: BalanceStand holds an already-upright robot,
#: these two get a fallen one up.
_GO2_RECOVER_MODES: Tuple[Option, ...] = (
    ("recovery_stand", "侧躺/仰躺翻滚起身 RecoveryStand"),
    ("stand_up", "蹲姿起身 StandUp"),
)

#: Go2 sport moves that are safe by the profile's own classification
#: (``dangerous=False`` in ``robots/go2.py``). The flips, dances, handstand and
#: friends are dangerous and stay out of the panel's default action list.
_GO2_SAFE_ACTIONS: Tuple[Option, ...] = (
    ("hello", "打招呼"),
    ("stretch", "伸懒腰"),
    ("content", "满足姿态"),
    ("pose", "摆造型"),
    ("scrape", "鞠躬"),
    ("wiggle_hips", "扭屁股"),
    ("finger_heart", "比心"),
    ("sit", "坐下"),
    ("rise_sit", "从坐姿站起"),
)

#: The dangerous half of the same catalogue, in the order they are offered.
#:
#: **These were always reachable and never shown.** ``allow_dangerous`` defaults
#: to true, so every one of them would run — the console simply did not offer
#: them, which meant the capability existed for anyone willing to read
#: ``robots/go2.py`` and not for the operator.
#:
#: The console shows them in a group that is **collapsed by default**, because
#: the cost of mistapping a bow and mistapping a backflip are not the same, and
#: an emergency is when a flat list of twenty-two buttons gets mistapped. The
#: order runs from "on the spot" to "needs a runway", so the list reads as a
#: risk gradient rather than alphabetically.
_GO2_HARD_ACTIONS: Tuple[Option, ...] = (
    ("dance_1", "跳舞 1"),
    ("dance_2", "跳舞 2"),
    ("handstand", "单手倒立"),
    ("front_jump", "前跳"),
    ("front_pounce", "前扑"),
    ("bound", "奔跑跳步 Bound"),
    ("cross_step", "交叉步 CrossStep"),
    ("onesided_step", "单侧步 OnesidedStep"),
    ("moon_walk", "月球步 MoonWalk"),
    ("front_flip", "前空翻"),
    ("back_flip", "后空翻"),
    ("left_flip", "左空翻"),
    ("right_flip", "右空翻"),
)

#: Actions that need space and a soft floor, and are marked ``⚠`` wherever they
#: appear. A **character** rather than a colour: colour is spoken for by state
#: (running / done / failed), and a warning carried only by colour is invisible
#: to a colour-blind operator.
_GO2_NEEDS_SPACE: Tuple[str, ...] = (
    "front_jump", "front_pounce",
    "front_flip", "back_flip", "left_flip", "right_flip",
)

#: Step/gallop moves from the same catalog. All four are ``dangerous=True``
#: (they need ``UNITREE_AGENT_ALLOW_DANGEROUS``, default true) and all four are
#: one-shot moves: this is the closest thing to gait selection that any tool
#: can reach. See "What could NOT be determined" #1 in the module docstring.
_GO2_GAIT_MOVES: Tuple[Option, ...] = (
    ("bound", "奔跑跳步 Bound"),
    ("cross_step", "交叉步 CrossStep"),
    ("onesided_step", "单侧步 OnesidedStep"),
    ("moon_walk", "月球步 MoonWalk"),
)

#: R1 states an operator actually reaches for, as exact ``constants.R1_FSM``
#: keys (``robot_switch_state`` resolves names against that table). The dances
#: and the full 22-state table stay behind ``robot_list_actions`` on purpose:
#: this is a console, not a state-machine debugger.
_R1_STATES: Tuple[Option, ...] = (
    ("Damping", "阻尼 Damping(1)"),
    ("Lock", "锁定/站立 Lock(4)"),
    ("SitDown", "坐下 SitDown(7)"),
    ("StandUp", "起身 StandUp(701)"),
    ("LieDown", "躺下 LieDown(702)"),
    ("Run", "跑步 Run(811，Air 会重定向到 830)"),
    ("Walk", "行走 Walk(814)"),
    ("Loco20Dof", "20 自由度行走 Loco20Dof(830)"),
)

#: R1 upper-limb gestures (``robots/r1.py`` ``_GESTURES``; the corresponding
#: ``ARM_ACTION`` constants exist for every one of them).
_R1_GESTURES: Tuple[Option, ...] = (
    ("release", "复位/取消手势"),
    ("handshake", "握手"),
    ("high_five", "击掌"),
    ("hug", "拥抱"),
    ("high_wave", "高举挥手"),
    ("clap", "鼓掌"),
    ("face_wave", "面前挥手"),
    ("left_kiss", "左手飞吻"),
    ("x_ray", "奥特曼光线"),
    ("hands_up", "双手举起"),
    ("right_hand_up", "右手举起"),
    ("reject", "拒绝手势"),
    ("forward_push", "前推"),
)

# ─── descriptor builders ───────────────────────────────────────────────


def _base(
    *,
    group: str,
    id: str,
    label: str,
    title: str,
    kind: str,
    tool: str,
    args: Mapping[str, Any],
    confirm: bool,
    read: bool,
) -> Control:
    """The keys every control carries, validated once at import time."""
    if group not in CONTROL_GROUPS:
        raise ValueError(f"unknown control group {group!r} for {id!r}")
    return {
        "group": group,
        "id": id,
        "label": label,
        "title": title,
        "kind": kind,
        "tool": tool,
        "args": dict(args),
        "confirm": bool(confirm),
        "read": bool(read),
    }


def _button(
    *,
    group: str,
    id: str,
    label: str,
    title: str,
    tool: str,
    args: Mapping[str, Any] | None = None,
    confirm: bool = False,
    read: bool = False,
) -> Control:
    return _base(
        group=group,
        id=id,
        label=label,
        title=title,
        kind=_READ_KIND if read else "button",
        tool=tool,
        args=args or {},
        confirm=confirm,
        read=read,
    )


def _read(
    *,
    group: str,
    id: str,
    label: str,
    title: str,
    tool: str,
    args: Mapping[str, Any] | None = None,
) -> Control:
    """A control that only reads state: never moves the robot, never confirms.

    ``confirm`` is structurally absent rather than merely false, so a future
    edit cannot quietly turn a read into a state change that skips the gate.
    """
    if "confirm" in (args or {}):
        raise ValueError(f"read control {id!r} must not send confirm")
    return _button(
        group=group, id=id, label=label, title=title, tool=tool, args=args, read=True
    )


def _toggle(
    *,
    group: str,
    id: str,
    label: str,
    title: str,
    tool: str,
    arg_name: str,
    on_value: Any,
    off_value: Any,
    args: Mapping[str, Any] | None = None,
    confirm: bool = False,
) -> Control:
    control = _base(
        group=group,
        id=id,
        label=label,
        title=title,
        kind="toggle",
        tool=tool,
        args=args or {},
        confirm=confirm,
        read=False,
    )
    control.update(arg_name=arg_name, on_value=on_value, off_value=off_value)
    return control


def _select(
    *,
    group: str,
    id: str,
    label: str,
    title: str,
    tool: str,
    arg_name: str,
    options: Sequence[Option],
    args: Mapping[str, Any] | None = None,
    confirm: bool = False,
) -> Control:
    """A control whose value goes into ``arg_name``.

    ``arg_name`` is what makes the select renderable: the option *values* are
    plain JSON scalars (the exact strings the tool validates), so the renderer
    needs to be told which argument they land in — the same key range and
    toggle controls use.
    """
    if not options:
        raise ValueError(f"select control {id!r} has no options")
    control = _base(
        group=group,
        id=id,
        label=label,
        title=title,
        kind="select",
        tool=tool,
        args=args or {},
        confirm=confirm,
        read=False,
    )
    control.update(
        arg_name=arg_name,
        options=[{"value": value, "label": text} for value, text in options],
    )
    return control


def _range(
    *,
    group: str,
    id: str,
    label: str,
    title: str,
    tool: str,
    arg_name: str,
    min_value: float,
    max_value: float,
    step: float,
    args: Mapping[str, Any] | None = None,
    confirm: bool = False,
) -> Control:
    control = _base(
        group=group,
        id=id,
        label=label,
        title=title,
        kind="range",
        tool=tool,
        args=args or {},
        confirm=confirm,
        read=False,
    )
    control.update(arg_name=arg_name, min=min_value, max=max_value, step=step)
    return control


# ─── the controls ──────────────────────────────────────────────────────
#
# Declared in CONTROL_GROUPS order. ``controls_for`` sorts by group anyway, so
# this ordering is for the reader (and for stable order *within* a group: the
# sort is stable).

#: Controls both models offer.
_COMMON_CONTROLS: Tuple[Control, ...] = (
    _button(
        group="posture",
        id="stand",
        label="站立",
        title="站起来并保持平衡：R1 → Lock(4)，Go2 → BalanceStand(1002)。",
        tool="robot_stand",
        args={"confirm": True},
        confirm=True,
    ),
    _button(
        group="posture",
        id="lie_down",
        label="趴下",
        title="趴到地面：R1 → LieDown(702)，Go2 → StandDown(1005)；结束后它就是躺着的，这是预期结果。",
        tool="robot_lie_down",
        args={"confirm": True},
        confirm=True,
    ),
    _read(
        group="safety_net",
        id="lowstate",
        label="电量/IMU",
        title="读取电池、IMU 与板温；不拉取整张电机表，面板刷新保持轻量。",
        tool="robot_lowstate",
        args={"include_motors": False},
    ),
    # ── motion_nudge: small, bounded, always confirmed ──────────────────
    # vx/vy/omega are normalised (1.0 = the robot's own maximum), so "0.15"
    # is 15 % of that maximum, not 0.15 m/s. 0.5 s keeps a mis-click to a
    # nudge; robot_move itself clamps the duration to the server ceiling.
    _button(
        group="motion_nudge",
        id="nudge_forward",
        label="前进",
        title="以 15% 归一化速度前进 0.5 秒（vx 是归一化速度：1.0 = 机器人自身最大速度，不是 m/s）。",
        tool="robot_move",
        args={"vx": 0.15, "vy": 0.0, "omega": 0.0, "duration_s": 0.5, "confirm": True},
        confirm=True,
    ),
    _button(
        group="motion_nudge",
        id="nudge_back",
        label="后退",
        title="以 15% 归一化速度后退 0.5 秒。",
        tool="robot_move",
        args={"vx": -0.15, "vy": 0.0, "omega": 0.0, "duration_s": 0.5, "confirm": True},
        confirm=True,
    ),
    _button(
        group="motion_nudge",
        id="nudge_left",
        label="左移",
        title="以 15% 归一化速度左移 0.5 秒（vy 正值 = 左）。",
        tool="robot_move",
        args={"vx": 0.0, "vy": 0.15, "omega": 0.0, "duration_s": 0.5, "confirm": True},
        confirm=True,
    ),
    _button(
        group="motion_nudge",
        id="nudge_right",
        label="右移",
        title="以 15% 归一化速度右移 0.5 秒（vy 负值 = 右）。",
        tool="robot_move",
        args={"vx": 0.0, "vy": -0.15, "omega": 0.0, "duration_s": 0.5, "confirm": True},
        confirm=True,
    ),
    # The turn tool's angle argument is `degrees` (not `angle_deg`), it is
    # closed-loop on the IMU, and speed=0 means "let the runtime choose".
    _button(
        group="motion_nudge",
        id="turn_left",
        label="左转 30°",
        title="原地左转 30°（degrees 正值 = 逆时针，IMU 闭环；返回残余误差 residual_deg）。",
        tool="robot_turn",
        args={"degrees": 30, "confirm": True},
        confirm=True,
    ),
    _button(
        group="motion_nudge",
        id="turn_right",
        label="右转 30°",
        title="原地右转 30°（degrees 负值 = 顺时针，IMU 闭环）。",
        tool="robot_turn",
        args={"degrees": -30, "confirm": True},
        confirm=True,
    ),
)

#: Go2-only controls. None of these tools exists on an R1
#: (``tools/__init__.register_all`` registers ``go2.py`` only for a Go2).
_GO2_CONTROLS: Tuple[Control, ...] = (
    _select(
        group="posture",
        id="recover",
        label="起身",
        title="把 Go2 从倒地/阻尼状态弄回站姿：recovery_stand 用于侧躺或仰躺（会翻滚起身），stand_up 用于蹲姿起身；robot_stand（BalanceStand）只保持已经站立的姿态。",
        tool="go2_recover",
        arg_name="mode",
        options=_GO2_RECOVER_MODES,
        args={"confirm": True},
        confirm=True,
    ),
    _select(
        group="posture",
        id="play_action",
        label="动作",
        title="播放 Go2 动作目录里的安全动作（打招呼/伸懒腰/坐下…）；需要危险动作许可的翻滚、跳舞类不在这个列表里。",
        tool="robot_play_action",
        arg_name="name",
        options=_GO2_SAFE_ACTIONS,
        args={"confirm": True},
        confirm=True,
    ),
    # "gait" group: there is no gait tool (see the module docstring #1), so the
    # group carries the catalogued step/gallop moves — one-shot moves, not a
    # persistent gait switch. They are dangerous=True, hence the title.
    _select(
        group="gait",
        id="gait_move",
        label="步法动作",
        title="播放动作目录里的步法类动作（奔跑跳步/交叉步/单侧步/月球步）；这是一次性动作，不是持续步态切换，且需要 UNITREE_AGENT_ALLOW_DANGEROUS。",
        tool="robot_play_action",
        arg_name="name",
        options=_GO2_GAIT_MOVES,
        args={"confirm": True},
        confirm=True,
    ),
    _select(
        group="motion",
        id="motion_mode",
        label="运动模式",
        title="切换运动控制模式：ai 把控制权交给内置 AI 模式（机器人会站起来），normal 恢复运动指令；MCF 需要固件支持。",
        tool="go2_motion_mode",
        arg_name="mode",
        options=_MOTION_MODES,
        args={"confirm": True},
        confirm=True,
    ),
    _read(
        group="motion",
        id="get_state",
        label="状态回读",
        title="读取 sport 状态（模式/步态/速度/身高）与多状态帧（音量/亮度/避障开关）；真机上 sportmodestate 可能从不发布，此时 sport_state 为 null。",
        tool="go2_get_state",
    ),
    _toggle(
        group="safety_net",
        id="obstacle_avoid",
        label="避障",
        title="开关避障：打开后避障服务会拦截并过滤摇杆/移动指令。",
        tool="go2_obstacle_avoid",
        arg_name="enable",
        on_value=True,
        off_value=False,
    ),
    _toggle(
        group="safety_net",
        id="lidar",
        label="雷达",
        title="开关激光雷达并返回最新点云摘要（点数/分辨率/坐标系）；mock 后端没有点云，会直接拒绝。",
        tool="go2_lidar",
        arg_name="enable",
        on_value=True,
        off_value=False,
    ),
    _select(
        group="lighting",
        id="led",
        label="灯光颜色",
        title="设置头部 LED 颜色，亮 5 秒后熄灭（VUI 1007）；只有这 7 种颜色，且颜色不能回读。",
        tool="go2_set_led",
        arg_name="color",
        options=_LED_COLOURS,
        args={"seconds": 5, "flash_cycle_ms": 0},
    ),
    _range(
        group="lighting",
        id="brightness",
        label="亮度",
        title="头部灯光亮度 0-10（VUI 1005）。",
        tool="go2_set_brightness",
        arg_name="level",
        min_value=0,
        max_value=10,
        step=1,
    ),
    _range(
        group="lighting",
        id="volume",
        label="音量",
        title="扬声器音量 0-10（VUI 1003）。",
        tool="go2_set_volume",
        arg_name="volume",
        min_value=0,
        max_value=10,
        step=1,
    ),
    _read(
        group="lighting",
        id="get_vui",
        label="灯光回读",
        title="读取当前音量与亮度（VUI 1004/1006）；LED 颜色没有回读接口。",
        tool="go2_get_vui",
    ),
)

#: R1-only controls. A Go2 registers neither ``robot_switch_state`` (no FSM) nor
#: ``robot_arm_gesture`` (no arms), so these can never leak into a Go2 panel.
_R1_CONTROLS: Tuple[Control, ...] = (
    _select(
        group="posture",
        id="switch_state",
        label="状态切换",
        title="按语义名切换人形 FSM 状态（SET_FSM_ID 7101）：Damping(1) 任何状态都接受，其余多数需要先在 Lock(4)。",
        tool="robot_switch_state",
        arg_name="state",
        options=_R1_STATES,
        args={"confirm": True},
        confirm=True,
    ),
    # `robot_arm_gesture(gesture)` has no confirm parameter at all — sending
    # one would be an unexpected argument, so args stays empty.
    _select(
        group="posture",
        id="arm_gesture",
        label="手臂手势",
        title="播放一个上肢手势（ARM_REQUEST 7106）；手势只动手臂，工具本身不要求确认，但要先清空手臂活动范围。",
        tool="robot_arm_gesture",
        arg_name="gesture",
        options=_R1_GESTURES,
    ),
    _read(
        group="posture",
        id="get_posture",
        label="姿态回读",
        title="读取当前语义姿态（standing/damping/locomotion…）；R1 的 5/6/7/800/813/814 等状态会冻结上报，回读可能是旧值而不是失败。",
        tool="robot_get_posture",
    ),
)

_CATALOG: Tuple[Control, ...] = _COMMON_CONTROLS + _GO2_CONTROLS + _R1_CONTROLS


# ─── public API ────────────────────────────────────────────────────────


def _family(model: str) -> str:
    """Normalise a model name to a catalog key.

    Case-insensitive, and anything that is not recognisably an R1 falls back to
    the Go2 set (a wrong panel that offers too little is the safe failure; the
    ``available_tools`` filter then removes whatever does not exist).
    """
    key = str(model or "").strip().lower().replace(" ", "").replace("-", "").replace("_", "")
    return "R1" if "r1" in key else "Go2"


def action_label(name: str) -> str:
    """A Chinese label for a catalogued action name, or the name itself.

    Shared with the fleet action bar so "打招呼" means the same action in both
    places. The catalogues in `robots/go2.py` are English descriptions —
    "Wave hello (1016)." — which read as debug output on a control button.
    """
    key = str(name or "").strip().lower().replace(" ", "_")
    for table in (_GO2_SAFE_ACTIONS, _GO2_GAIT_MOVES):
        for value, label in table:
            if value == key:
                return label
    return str(name or "")


def controls_for(model: str, available_tools: Iterable[str]) -> List[Control]:
    """The controls ``model`` can actually honour, in display order.

    ``model`` is ``"Go2"`` or ``"R1"`` (case-insensitive); an unknown model
    gets the Go2 set. ``available_tools`` is the iterable of MCP tool names the
    robot really registered — a control whose tool is not in it is dropped, so
    the panel never renders a button that can only fail.

    Pure: the returned dicts are fresh copies, so a caller (or a browser
    payload) can mutate them without touching the catalog, and the same inputs
    always produce the same output.
    """
    available = {str(name) for name in (available_tools or ())}
    family = _family(model)
    catalog = _COMMON_CONTROLS + (_GO2_CONTROLS if family == "Go2" else _R1_CONTROLS)

    order = {group: index for index, group in enumerate(CONTROL_GROUPS)}
    selected = [control for control in catalog if control["tool"] in available]
    # Stable sort: the declaration order inside a group is preserved.
    selected.sort(key=lambda control: order[control["group"]])
    return [copy.deepcopy(control) for control in selected]
