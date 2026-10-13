# Robot-control survey of `unitree_harness` — what a dual-dog web console should steal

Read-only survey of `/Users/jackwang/unitree_harness` (nothing there was modified).

**What the repo is.** One real project (`go2ctl/`, ~3.8k lines of Python + ~1k lines of vanilla
JS/CSS/HTML) plus nine *vendored reference clones* under `vendor/` (`README.md:76-93`). Only
`go2ctl/` is deployed; `vendor/` is upstream code kept for protocol/SDK reference. Both matter
here, so claims below are tagged **[go2ctl]** (the author's own work) or **[vendor]** (upstream
reference the author deliberately collected).

**Critical scope fact up front:** `go2ctl` is **strictly single-robot**. One process = one Go2,
bound to one IP and one network interface (`config.py:19-21`), with an in-process `StateCache`
keyed by nothing (`state.py:89-108`). There is **no** multi-robot, formation, leader/follower or
mirroring code anywhere in it — I grepped for `multi.?robot|双机|两台|leader|follower|formation|编队|mirror|mirror|robot_id|robot2` across all `go2ctl` sources and docs and got **zero hits**. The only
dual-robot material in the repo is in `vendor/go2_ros2_sdk` (upstream ROS2 wrapper). Heading 3
below is therefore mostly about that vendored code plus one very sharp hazard that applies
directly to your console.

---

## 1. How it takes operator input

All operator input lives in **one browser file**, `go2ctl/static/app.js` (715 lines), section
`// ---------------- 输入：摇杆 / 键盘 / 手柄 ----------------` (`app.js:392`).

### Four input sources, merged into one vector

`computeInput()` (`app.js:462-505`) is the single merge point. Precedence is explicit and
resolves the "gamepad drift eats my WASD" problem that otherwise always shows up:

```js
// app.js:475-481
// 键盘/摇杆有输入时立即接管，手柄只在最近 800ms 内有效
// （否则手柄漂移会吞掉 WASD/摇杆输入）
const kbActive = keys.size > 0;
const stActive = Math.abs(stickState.move.x) + ... > 0.05;
const gamepadActive = gpActiveNow && !kbActive && !stActive
  && (now - gamepadLastActive < 800);
```

**a) Keyboard** (`app.js:439-444`, mapped at `app.js:486-493`)

| key | effect | sign |
|---|---|---|
| `W` / `↑` | `kvx += 1` | forward |
| `S` / `↓` | `kvx -= 1` | back |
| `A` / `←` | `kvy += 1` | strafe **left** |
| `D` / `→` | `kvy -= 1` | strafe right |
| `Q` | `kvyaw += 1` | turn left |
| `E` | `kvyaw -= 1` | turn right |
| `Space` | `cmd("stop_move")` — immediate, **not** a movement key | — |

Two details worth copying: arrows and space are `preventDefault()`ed so the page never scrolls
under a held key (`app.js:440`), and every action button calls `b.blur()` after click so a later
space/arrow press can't re-trigger the focused button (`app.js:583`, `app.js:637`). `blur` on the
window clears the whole key set, so tabbing away cannot leave a key stuck down (`app.js:445`).

**b) Gamepad** (`app.js:463-470`, buttons at `app.js:507-521`)

```js
const [ax0, ax1, ax2] = gp.axes;
gvx += -ax1; gvy += -ax0; gvyaw += -ax2;   // app.js:468-470
```
That is the Unitree SDK axis convention (left stick `ly` = forward, `lx` = strafe-left positive,
right stick `rx` = yaw-left positive) — the comment says so explicitly (`app.js:463`). Axes are
polled in the same 20 Hz loop; buttons are edge-detected against a `gpPrevButtons` set so a held
button fires once (`app.js:510-521`), mapped as:

```js
const GP_BUTTONS = { 0: "stand_up", 1: "sit", 2: "stop_move", 3: "damp", 9: "balance_stand" };
```
(`app.js:507`) — A/B/X/Y plus Start. Every pad connected is summed, so two pads both work.

**c) On-screen joystick** (`bindStick`, `app.js:409-437`) — two of them, not an arrow pad. This is
the part most worth comparing against your current arrow pad:

- `pointerdown` calls `setPointerCapture(e.pointerId)` so a fast drag that leaves the circle keeps
  steering (`app.js:428`) — this is the bug that makes naive `<div>`+`mousemove` joysticks drop
  input mid-gesture.
- Output is **clamped to the unit disc**, not a square: `const m = Math.hypot(x,y); if (m > 1) { x /= m; y /= m; }` (`app.js:423-424`).
- The knob is positioned in percent-plus-pixels via CSS calc: `knob.style.left = calc(50% + ${x*R}px)` (`app.js:415-416`), `R = el.clientWidth/2 - 26` (`app.js:411`).
- **Release zeroes the stick** on `pointerup` *and* `pointercancel` (`app.js:430-436`). This is the
  joystick's dead-man: let go and the axis is 0, which then falls out of `computeInput()` as
  "not active" and triggers a `stop_move` (see heading 2).

**d) Sliders** — three, all debounced before hitting the wire, all read back into the UI:

- **Yaw-rate limit** (the interesting one): `<input type="range" id="yaw-limit" min="0.1" max="1.2" step="0.1">` (`index.html:82-84`). Value is persisted in `localStorage` under `go2ctl_yaw_limit` (`app.js:453-454`), restored into the slider (`app.js:642-643`), pushed to the server on every WebSocket (re)connect (`app.js:52`), and re-pushed 250 ms after the user stops dragging (`app.js:645-651`). Crucially the **local input is scaled by the same limit**, so the stick and the server agree:
  ```js
  // app.js:503
  vyaw: Math.max(-yawLimit, Math.min(yawLimit, curve(rawVyaw) * yawLimit)),
  ```
- **Volume / brightness**: 0–100 sliders (`index.html:43-44`), 300 ms debounce (`app.js:652-660`).
- **Speed level**: a 5-option `<select>` → `speed_level` action (`index.html:74-79`, `app.js:639`).
  (Author notes it's a no-op on this firmware, returning `-1` — `README.md:298-300`.)

### Response shaping: deadzone + squared curve + EMA

```js
// app.js:398-407
const DEADZONE = 0.08;
const SMOOTH_K = 0.25;
function curve(v) {
  const a = Math.abs(v);
  if (a < DEADZONE) return 0;
  const n = (a - DEADZONE) / (1 - DEADZONE);
  return Math.sign(v) * n * n;          // squared: fine near centre, full at the edge
}
```
The comment calls the exponent the "key to not rolling over" (`app.js:401`). Then a second,
*time-domain* filter on top, in the 50 ms loop:

```js
// app.js:526-529
smoothState.vx += (t.vx - smoothState.vx) * SMOOTH_K;
```
Note this is **client-side EMA (τ ≈ 100 ms) *plus* server-side acceleration ramping** (heading 2).
Two stages of smoothing is deliberate here but easy to over-do; if you copy both, expect a
noticeably laggier stop than a single stage.

### Continuous vs discrete, and send discipline

The transport loop is a single 50 ms `setInterval` (`app.js:523-545`):

```js
// app.js:532-543
const active  = |sx|+|sy|+|syaw| > 0.001;
const changed = |sx-last.vx| + ... > 0.01;
if (active && (changed || now - lastMoveSent > 150)) {
  cmd("drive", { vx: +sx.toFixed(3), vy: +sy.toFixed(3), vyaw: +syaw.toFixed(3) });
  lastMoveSent = now;
} else if (!active && lastMoveSent) {
  cmd("stop_move");  lastMoveSent = 0;
}
```
Three behaviours fall out of those six lines, and all three are worth having:

1. **Send-on-change with a 150 ms keep-alive floor.** Holding a stick perfectly still still
   re-sends every 150 ms, which is what keeps the server-side deadman (1000 ms) from firing.
2. **Release → explicit `stop_move`** the moment the merged vector goes to zero. Not a "send
   zero velocity" — the real stop command.
3. **Velocity is quantised to 3 decimals** before sending, which keeps the change detector from
   firing on floating-point noise.

Discrete commands (poses, motions, toggles, estop) go through a separate request/response path and
are logged in a status bar rather than the control loop (`cmd()` at `app.js:91-98`,
`logCmdResult()` at `app.js:100-107`).

### [vendor] The same author kept a key→joystick table for reference

`vendor/go2_webrtc_connect/examples/go2/data_channel/obstacles_avoid/obstacles_avoid.py:29-41` maps
keys to **`rt/wirelesscontroller` joystick axes** (the "fake a USB dongle" path) rather than to
`Move`:

```python
KEY_MAP = {
  "w": ("forward", ( 0.0,  0.9,  0.0)),   # lx, ly, rx
  "a": ("strafe left", (-0.9, 0.0, 0.0)),
  "j": ("turn left",   ( 0.0, 0.0, 0.9)),
  " ": ("stop",        None),
}
MOVE_DURATION = 0.5    # seconds to send commands per keypress
MOVE_INTERVAL = 0.02   # 50 Hz publish rate
```
(that file's lines 25-26). Note it's *burst* semantics: 50 Hz for 0.5 s then unconditionally zero
(`:94-102`). Also note the sign convention is **`lx` negative = left**, the mirror of the
browser's `gvy = -ax0`.

### [vendor] Official axis smoothing/deadzone values, for calibration

`vendor/unitree_sdk2/example/wireless_controller/advanced_gamepad.hpp:72-77` is the official
reference and uses far smaller numbers than `go2ctl`:

```cpp
lx = lx * (1 - smooth) + (fabs(key_msg.lx()) < dead_zone ? 0.0 : key_msg.lx()) * smooth;
...
float smooth = 0.03;   // vs go2ctl's 0.25
float dead_zone = 0.01; // vs go2ctl's 0.08
```
The file also decodes the 16-bit button bitfield into per-button `pressed/on_press/on_release`
edges (`advanced_gamepad.hpp:44-88`) — a clean model if you ever read the real robot dongle.

---

## 2. How it sends movement

### The command path

`RobotCore` is built around a **single worker thread that owns every RPC**. Nothing else calls
into the SDK; other threads only set flags. This is stated as an architectural rule in
`README.md:177-179` and is the reason the design survived a flaky channel:

```python
# robot.py:375-376
def _cmd_loop(self) -> None:
    """唯一执行运动/命令 RPC 的线程：命令优先，空闲时服务 mover/健康/开关标志位。"""
```

- `submit(fn, ...)` (`robot.py:358-366`) — blocking command with a **15 s default timeout**, and a
  **queue-overload guard**: `if self._queue.qsize() > 50: return {"ok": False, "code": -12, ...}`.
- `submit_async(fn, ...)` (`robot.py:368-373`) — fire-and-forget, drops on a full queue.
- The loop drains the queue each pass, sleeps `0.01` (`robot.py:502`), and **auto-retries a `3104`
  (channel timeout) exactly once after 300 ms** (`robot.py:406-418`), with a comment explaining
  that a longer retry chain blocks the only worker and trips the heartbeat watchdog.

### The actual `Move` call

Every motion goes through **one** Unitree API: `SportClient.Move(vx, vy, vyaw)`
(`robot.py:448`), which the SDK turns into a no-reply RPC carrying `{"x","y","z"}`
(`vendor/unitree_sdk2_python/unitree_sdk2py/go2/sport/sport_client.py:132-139`, API id `1008`).
Service/topic naming: `rt/api/<service>/request` with `service = "sport"`
(`vendor/unitree_sdk2_python/unitree_sdk2py/core/channel_name.py:14-20`) — remember this for
heading 3.

### The mover state machine — the single most valuable thing in the repo

`_move_loop()` (`robot.py:1858-2018`) is a dedicated thread ticking at **20 ms** (`tick = 0.02`,
`robot.py:1871`). Its ordering is the safety design. Reproduced in order:

**(1) Standing gate — don't send velocity at something that isn't standing** (`robot.py:1895-1920`)

```python
if self.cfg.auto_stand and not self._posture_walkable():
    if self._stand_until == 0.0:
        self._stand_until = now + self.cfg.auto_stand_timeout_s   # 5s
    if now > self._stand_until:
        ... clear target, set drive_state="stand_timeout"          # give up, stop
    self._set_drive_state("standing_wait")
    with self._mv_lock: self._move_cmd_ts = now   # refresh watchdog: we're not abandoned
    if now - self._stand_last_req >= 1.0:         # StandUp at <=1 Hz
        self._stand_dirty = True
    continue                                      # NO Move this tick
```
The `_move_cmd_ts = now` line is subtle and important: while waiting for the robot to stand, the
deadman timer is refreshed, so a slow stand-up is not misread as a lost operator.

**(2) Tumble detection before anything else** (`robot.py:1923-1932`)

```python
pitch = self._filtered_pitch()            # EMA alpha=0.3, robot.py:2039-2052
if pitch is not None and abs(pitch) > 1.4:   # ~80°, radians
    ... clear target, stop
```

**(3) Dead-man watchdog** (`robot.py:1934-1946`)

```python
if not until:                              # only for open-ended (no duration_ms) targets
    cmd_age = now - self._move_cmd_ts
    if cmd_age > self.cfg.move_watchdog_ms / 1000.0:     # 1000 ms
        ... clear target, set drive_state="watchdog"
```
Note the design choice: a command *with* an explicit `duration_ms` is exempt because it
self-expires (checked on the next line, `robot.py:1948-1953`). The watchdog protects exactly the
open-ended "operator is holding a key" case.

**(4) Turn-speed coupling** (`robot.py:1955-1959`)

```python
if self.cfg.turn_coupling:
    speed_ratio = math.hypot(vx_t, vy_t) / max(0.01, self.cfg.max_linear_speed)
    vyaw_limit = self.cfg.max_yaw_speed * max(0.25, 1.0 - 0.75 * speed_ratio)
    vyaw_t = max(-vyaw_limit, min(vyaw_limit, vyaw_t))
```
At a standstill you get 100 % yaw authority; at full linear speed only 25 %. The
`max(0.25, ...)` floor means it never fully locks steering.

**(5) Optional radar guard, forward-only** (`robot.py:1961-1982`) — height-band-filtered sector
distance, `guard_frames=3` consecutive confirmations before blocking, `guard_release_s=1.5`
release cooldown, and it **only zeroes `vx_t`**; reverse and yaw stay live so the operator can
back out of a corner. (Off by default — see heading 6.)

**(6) Acceleration-limited ramp — the core of the "no lurch" feel** (`robot.py:1984-2003`)

```python
def ramp(cur, goal, accel, decel):
    if abs(goal - cur) < 1e-6: return goal
    if abs(goal) < abs(cur):   a = decel      # decelerating (also covers sign flips)
    else:                      a = accel
    step = a * dt
    if goal > cur: return min(goal, cur + step)
    return max(goal, cur - step)

cvx   = ramp(cvx,   vx_t,   self.cfg.drive_accel, self.cfg.drive_decel)
cvy   = ramp(cvy,   vy_t,   self.cfg.drive_accel, self.cfg.drive_decel)
cvyaw = ramp(cvyaw, vyaw_t, self.cfg.yaw_accel,   self.cfg.yaw_accel)
```
This is a **slew-rate limiter on the commanded velocity**, updated every 20 ms tick. The
asymmetry (accelerate at 1.0 m/s², decelerate at 2.0 m/s²) is exactly right for a legged robot:
gentle to start moving, decisive to stop. Because `abs(goal) < abs(cur)` is tested on magnitudes,
a full-reverse stick input is treated as a deceleration through zero — no special case needed.

**(7) High-rate no-reply re-send** (`robot.py:2005-2018`)

```python
interval = 1.0 / max(5.0, self.cfg.move_hz)     # 50 Hz default
if now - self._last_move_sent >= interval:
    self._last_move_sent = now
    self._move_cmd = (cvx, cvy, cvyaw)
    self._move_dirty = True
```
The worker thread performs the actual `_sport.Move(...)` (`robot.py:443-459`). The docstring
justifies 50 Hz as the official pattern (`robot.py:1862-1863`), matching
`vendor/unitree_sdk2/example/go2/go2_sport_client.cpp:138,155` (`dt = 0.005` + a recurrent thread)
and `vendor/unitree_sdk2_python/example/a2/sport/a2_sport_client.py:85` (`dt = 0.02  # 50Hz`).

### All the numbers, in one place

From `config.py:37-62` (every one is a `GO2CTL_*` env override, `config.py:12-13`):

| parameter | default | line |
|---|---|---|
| `MAX_LINEAR` (vx/vy clamp, m/s) | **0.5** | `config.py:41` |
| `MAX_YAW` (vyaw clamp, rad/s) | **0.5** | `config.py:42` |
| `DRIVE_ACCEL` (m/s²) | **1.0** | `config.py:43` |
| `DRIVE_DECEL` (m/s²) | **2.0** | `config.py:44` |
| `YAW_ACCEL` (rad/s²) | **2.0** | `config.py:45` |
| `TURN_COUPLING` | on | `config.py:46` |
| `MOVE_WATCHDOG` (ms) | **1000** | `config.py:47` |
| `MOVE_HZ` | **50** | `config.py:48` |
| `AUTO_STAND_TIMEOUT` (s) | **5** | `config.py:49` |
| `CMD_TIMEOUT` (s) | **10** | `config.py:50` |
| `RADAR_GUARD` / `_DIST` | off / 0.35 m | `config.py:56-57` |
| `GUARD_RADIUS_MIN/HEIGHT_MIN/HEIGHT_MAX` | 0.35 m / 0.30 m / 1.20 m | `config.py:58-60` |
| `GUARD_FRAMES` / `GUARD_RELEASE_S` | 3 / 1.5 s | `config.py:61-62` |

The clamp itself (`robot.py:585-587`) is applied on *input*, and `set_yaw_limit()`
(`robot.py:934-943`) mutates `self.cfg.max_yaw_speed` live, so the web slider takes effect on the
very next tick without a restart.

### Independent closed-loop skills (rather than open-loop timing)

Two primitives that make "drive this exact amount" safe, both guarded by a non-blocking
`_skill_lock` that returns `code=-11 skill busy` instead of queueing (`robot.py:824-825`,
`robot.py:864-865`):

```python
# turn_deg, robot.py:822-860
deg = max(-90.0, min(90.0, float(deg)))         # ±90°
if abs(deg) < 2.0: ... "already"
timeout = max(10.0, abs(deg) / 5.0 + 4.0)
self.drive(0.0, 0.0, sign * 0.45, int(timeout * 1000))   # 0.45 rad/s
loop: actual = degrees(wrap(rpy[2] - yaw0))
      if abs(deg - actual) <= 3.0 or (deg - actual) * deg < 0: "reached"
```
```python
# forward_m, robot.py:862-896
meters = max(-0.8, min(0.8, float(meters)))     # ±0.8 m
timeout = max(8.0, abs(meters) / 0.15 + 3.0)
self.drive(sign * 0.28, 0.0, 0.0, int(timeout * 1000))   # 0.28 m/s
loop: actual = copysign(hypot(dx, dy), meters)
      if abs(actual) >= abs(meters) * 0.85: "reached"    # 85% counts as arrived
```
Both poll state at 20 Hz (`time.sleep(0.05)`), always `stop_move()` in the exit path, and return a
structured verdict — `{"ok", "reason": "reached"|"timeout"|"already"|"not_standing", "actual_deg"/"actual_m"}`.
The `remain * deg < 0` overshoot check in `turn_deg` is a nice touch: if the robot blew past the
target, that's a success, not a timeout.

The `duration_ms` path handles expiry by polling rather than by timer thread (checked every tick at
`robot.py:1948-1953`), which means a `stop_move()` always wins the race. Only the
*obstacles-avoid* variant uses a `threading.Timer` (`robot.py:765-768`), and `euler()` uses one to
self-recentre (`robot.py:916-922`).

### Channel hygiene that a dual-dog console should be aware of

- **Claim the joystick, with acknowledgement.** `SwitchJoystick(true)` must be the *acknowledged*
  call; the no-reply variant is silently ignored by the robot (`README.md:233-234`,
  `HANDOFF.md:119-120`). `go2ctl` auto-claims on first drive and **re-claims every 30 s** because
  the robot resets it on reboot or app reconnect (`robot.py:735-739`).
- **A dedicated client stub per thread.** The periodic health probe uses its *own* `SportClient`
  (`robot.py:645-655`) because two threads sharing one stub is a suspected cause of silent channel
  death.
- **Don't let the channel go quiet.** The health probe is an acknowledged `SwitchJoystick` every
  15 s (`robot.py:657-660`) — a pulse that doubles as liveness detection.
- **Fail loud on send failure.** 5 consecutive failed `Move` sends clears the velocity target
  (`robot.py:460-466`) so a stale target can't revive at full speed when the channel comes back.
- **Prefer the low-rate state topic**: `rt/lf/sportmodestate` with automatic fallback to
  `rt/sportmodestate` if no data after 8 s (`config.py:114-116`, `robot.py:343-355`). Subscribing
  to the 500 Hz variant costs real CPU for zero operator benefit.
- **Do not subscribe to the video topic.** `go2ctl` deliberately removed the
  `rt/videohub/compress` subscription: with the phone app connected it unicasts ~2.2 MB/s at you
  and, because the *sport request* channel is best-effort (no retransmit), the request packets get
  dropped and every RPC returns `3104` (`README.md:246-248`, `robot.py:254-257`). Camera frames
  come from a reliable RPC instead.
- **GIL budget is a real motion-control constraint.** The most-repeated lesson in the repo: a
  high-frequency DDS handler doing pure-Python work saturates the GIL, starves the RPC response
  thread, and the robot stops listening (`README.md:241-245`, `HANDOFF.md:153-159`). Point-cloud
  parsing (51–113 ms/frame) was moved to a **separate sidecar process** writing to `/dev/shm`;
  main-process CPU went 123 % → ~35 %, parse 51–113 ms → 5–8 ms (`config.py:91-96`,
  `README.md:254-260`). Numpy is used for the batch math because the pure-Python version alone
  pushed CPU past 108 % (`state.py:261-262`). If your console ever parses point clouds or video in
  the same process that sends velocity commands, this is your bug too.

---

## 3. Multi-robot coordination

**In `go2ctl`: none.** Structurally single-robot, in ways that matter if you were tempted to reuse
it directly:

- One fixed robot IP, one fixed NIC, one fixed DDS domain: `config.py:19-21`.
- `StateCache` is unkeyed — plain `self._data: Dict[str, Any]` (`state.py:89-94`), no robot id.
- `WebRTCTransport` holds exactly one connection: `self._conn` (`webrtc_transport.py:52`).
- The service-list cache is a **class attribute**, `RobotCore._svc_cache` (`robot.py:1720`), shared
  by every `RobotCore` instance in the process — two robots in one process would overwrite each
  other's cache.
- The web UI is single-robot throughout: one `#chip-*` set, one camera panel, one e-stop
  (`index.html:20-28`).

**The one architectural hazard you must plan around.** Unitree's DDS RPC topics are
**not namespaced per robot**:

```python
# vendor/unitree_sdk2_python/unitree_sdk2py/core/channel_name.py:14-20
def GetClientChannelName(serviceName: str, channelType: ChannelType):
    name = "rt/api/" + serviceName
    if channelType == ChannelType.SEND:  name += "/request"
    else:                                name += "/response"
    return name
```
So a `sport` client always publishes to `rt/api/sport/request`, and the DDS domain defaults to `0`.
Every robot reachable on the same DDS domain therefore **receives every command** — a `Move` from
dog A's console reaches dog B as well. `go2ctl` is safe only because it is point-to-point: a
dedicated NIC, one cable, one robot (`README.md:12-18`, `config.py:19`). A dual-dog console that
puts both robots on one LAN/DDS domain without *also* isolating them at the network layer will
drive both dogs with one stick. Worth verifying on your side before anything else: either separate
L2 segments (two NICs / VLANs), separate DDS domains, or the WebRTC command path (whose topics are
multiplexed over a per-connection datachannel and therefore *are* per-robot —
`vendor/unitree_webrtc_connect/unitree_webrtc_connect/constants.py:21-24`, `webrtc_driver.py:59-70`).

**[vendor] The one multi-robot design in the repo** — `vendor/go2_ros2_sdk`, which is how the ROS2
community does exactly your problem. Its whole approach is *one process per robot, coordinated by
namespacing*:

- Configured as a comma-separated IP list:
  `export ROBOT_IP="robot_ip_1, robot_ip_2, robot_ip_N"` (`vendor/go2_ros2_sdk/README.md:264-269`),
  parsed at `go2_robot_sdk/launch/robot.launch.py:21-22`.
- Mode is inferred from the list length: `"single" if len(robot_ip_list) == 1 and conn_type != "cyclonedds" else "multi"` (`robot.launch.py:47`) — note the quirk that CycloneDDS is *always* treated as multi.
- Everything is namespaced `robot{i}`: nodes (`robot.launch.py:127-129`), TF frames
  (`robot.launch.py:153-158`), URDF robot name (`robot.launch.py:121`), and RViz config
  (`robot.launch.py:56`).
- Commands are subscribed **per robot**:
  ```python
  # go2_robot_sdk/go2_robot_sdk/presentation/go2_driver_node.py:213-219
  for i in range(num_robots):
      self.create_subscription(
          Twist, f'robot{i}/cmd_vel_out',
          lambda msg, robot_id=str(i): self._on_cmd_vel(msg, robot_id), qos_profile)
  ```
- `robot_id` is threaded through the whole control layer as an explicit first-class argument —
  `handle_cmd_vel(x, y, z, robot_id, obstacle_avoidance=False)` and
  `handle_joy_command(joy_buttons, robot_id)`
  (`go2_robot_sdk/go2_robot_sdk/application/services/robot_control_service.py:22,47`), resolving to
  `self.config.robot_ip_list[int(robot_id)]`
  (`go2_robot_sdk/go2_robot_sdk/infrastructure/webrtc/webrtc_adapter.py:40-41`).
- Address **every** robot: connections are opened in a loop over the IP list
  (`go2_driver_node.py:338-343`) and each gets its own async control loop
  (`run_robot_control_loop(robot_id)`, `go2_driver_node.py:346-361`).

**Two useful negative findings from that code** — do not copy these:

1. There is **no** leader/follower, formation, offset or mirroring logic. Multi-robot means
   "N independent robots, N namespaces, one operator". If you want mirrored motion you are writing
   it yourself; nothing here will help.
2. `handle_joy_command` sends the *same button press to every robot* — the control loop calls it
   once per `robot_id` with the same `joy_state.buttons` (`go2_driver_node.py:350-354`). That is
   fine for "stand up both", actively dangerous for anything else.

---

## 4. Actions and sequences

**There is no sequence/choreography/timeline/keyframe/playlist subsystem.** I grepped
`sequence|choreograph|routine|macro|timeline|keyframe|playlist|编排|序列|时间轴|关键帧|播放`
across all of `go2ctl` (Python, JS, HTML, Markdown) and the only hits were the unrelated word
"sequence" in a DDS type declaration (`cloud_point.py:10`), FastAPI request serialisation, and the
phrase "Agent 侧推荐序列" (a *prompt* recipe, `perception.md:139-147`). There is no UI for authoring
motion, no stored motion data, no execution engine. Any claim that this repo "has choreography" is
wrong.

What does exist, and what it's actually worth:

**(a) `scripts/test_sequence.sh` — a linear smoke test, not a sequence system.**
75 lines of bash. A `cmd()` helper POSTs `{"action": ..., "params": ...}` to
`http://127.0.0.1:9000/api/command` (`test_sequence.sh:8-18`) and the script walks through
numbered phases with hard-coded `sleep`s between them (`test_sequence.sh:30-75`):

```bash
echo "### 3. 小步移动（前进/侧移/旋转，各 1.5s）"
cmd move '{"vx":0.3,"vy":0,"vyaw":0,"duration_ms":1500}'; sleep 2; state
cmd move '{"vx":0,"vy":0.3,"vyaw":0,"duration_ms":1500}'; sleep 2; state
cmd move '{"vx":0,"vy":0,"vyaw":0.5,"duration_ms":1500}'; sleep 2; state
cmd stop_move; sleep 1; state
```
Useful as a **shape**: it's a flat list of (action, params, settle time, read-back state), it
prints the state after every step, and it ends by leaving the robot in a known safe pose
(`sit`, `test_sequence.sh:73`). If you build a sequence player, this is the data model to
generalise: `{action, params, wait_ms, assert?}`. But note it is *not* machine-editable, has no
failure handling (`set -uo pipefail` without `-e`, so a failed step just prints), and the
`sleep`-based sequencing is open-loop — which is exactly what the repo's own closed-loop skills
were invented to replace.

**(b) The closed-loop skills are the real composable primitive.** `turn_deg` and `forward_m`
(heading 2) are the only units safe to string together, precisely because each one *verifies* it
arrived and returns `actual_deg` / `actual_m` plus a `reason`. A sequence engine built on these
gets idempotent, checkable steps for free, and the `_skill_lock` already prevents overlapping
execution (`robot.py:824`, `robot.py:864`).

**(c) `look_around()` and `find()` — the closest thing to a scripted routine** (`robot.py:1527-1679`).
`find()` is a genuine multi-phase state machine with bounded loops:

```python
# robot.py:1639-1650 — scan phase
for i in range(8):
    obs = self.get_obs()
    hits = [... score >= 0.25 ...]
    search.append({"i": i, "hits": [...], "yaw": ...})
    if hits: break
    if i < 7: self.turn_deg(45.0)
```
```python
# robot.py:1655-1676 — approach phase
limit = 8 if mode == "follow" else 4
for k in range(limit):
    r = self.approach_last(hit_name)
    steps.append({...})
    if r.get("reason") == "not_seen" or r.get("code") == -4:
        lost += 1
        if mode == "follow" and lost == 1:
            self.turn_deg(40.0); continue      # one recovery attempt, then give up
        break
    if (r.get("walk") or {}).get("reason") == "close": ...
self.stop_move()                                # ALWAYS stop at the end
```
Two things to steal regardless of vision: **every bounded loop ends with an unconditional
`stop_move()`** (`robot.py:1677`), and **every loop returns its trace** (`"search"`, `"steps"`)
so the caller can see what happened rather than just `ok: false`. `look_around()` likewise returns
four `views` with heading, turn result, pose and detections (`robot.py:1527-1554`), which is a
keyframe record in all but name.

**(d) Motion actions are fire-and-forget with a cooldown lock.** `MOTIONS` maps name →
`(api_id, needs_flag, cooldown_seconds)` (`robot.py:962-987`):

```python
"hello": (SPORT_API_ID_HELLO, False, 6.0),
"dance1": (SPORT_API_ID_DANCE1, False, 20.0),
"pose": (SPORT_API_ID_POSE, True, 3.0),     # needs_flag: toggle semantics
"front_flip": (SPORT_API_ID_FRONTFLIP, False, 12.0),
```
Sent via `_CallNoReply(api_id, param)` (`robot.py:1030`) with the cooldown recorded on success
(`robot.py:1031-1033`). The UI treats any name in `TOGGLE_MOTIONS` as a latching button that
re-sends with `flag` flipped and shows a `✓` (`app.js:561-577`).

**The precedence rules here are the interesting design, and they're easy to get wrong:**

- **Driving preempts a playing motion.** `drive()` detects an active cooldown lock and clears it,
  sending `StopMove` before entering velocity mode (`robot.py:740-750`). Without this, clicking
  "dance" then pressing W does nothing for up to 20 s — a real reported bug (`HANDOFF.md:185-187`).
- **A motion preempts driving.** `motion()` clears `_move_target`, sets `_stop_pending`, and
  re-centres the guard/stand state *before* issuing the action (`robot.py:999-1009`), with the
  comment "曾致摔倒" (this used to make the robot fall over).
- **`stop_move()` clears the cooldown lock too** (`robot.py:567`), so the stop button is always a
  full escape hatch.

**(e) [vendor] A real keyframe path API exists in the SDK — but not for Go2 in Python.**
`vendor/unitree_sdk2/example/go2/go2_trajectory_follow.cpp` is a full trajectory player: 30
`PathPoint`s, each carrying time, pose and velocity, re-planned and re-sent every 2 ms
(`dt = 0.002`, lines 14, 26, 41):

```cpp
struct stPathPoint { float timeFromStart, x, y, yaw, vx, vy, vyaw; };  // include/unitree/robot/go2/sport/sport_client.hpp:15-24
...
for (int i=0; i<30; i++) {
    p.timeFromStart = i * delta;   // delta = 0.06
    p.x = vx * var;  p.y = 0.6 * sin(M_PI * vx * var);
    p.vyaw = -M_PI * vx*2*0.6 * vx * M_PI * sin(M_PI * vx * var);
    path.push_back(p);
}
ret = tc.TrajectoryFollow(path);
```
**This is the API you would want for a timeline editor** — exactly a keyframe list with explicit
per-point velocities, and the robot interpolates internally. Two blockers to check first:
`SPORT_PATH_POINT_SIZE = 30` is a *hard* length check that returns `4101` otherwise
(`vendor/unitree_sdk2_python/unitree_sdk2py/go2/sport/sport_client.py:10,127-129`), and the
**Python Go2 client never registers the API id** — `TrajectoryFollow` exists on the B2 client
(`b2/sport/sport_client.py:127`) but not in `go2/sport/sport_client.py`'s `Init()`
(compare `go2/sport/sport_client.py:41-73`), and `go2/sport/sport_api.py` has no
`SPORT_API_ID_TRAJECTORYFOLLOW` constant at all. So this is a "possible, unverified, needs a raw
`_RegistApi` + `_CallNoReply` with a guessed id" avenue, not a drop-in.

**(f) Actions are exposed identically over three transports.** `ActionDispatcher` is a thin
named-method layer (`actions.py:14-171`) that the REST endpoint
(`web_server.py:60-69`), the WebSocket (`web_server.py:131-137`) and all 42 MCP tools
(`mcp_server.py`, e.g. `:152-166`) all call. One implementation, three surfaces — worth copying
for a console that also wants automation.

---

## 5. UI/UX patterns worth stealing

Layout is a 7-panel responsive grid plus a sticky header and a sticky log bar
(`index.html:33-124`, `style.css:66-69`, `style.css:183-186`).

**Top bar as the status surface** (`index.html:10-29`, CSS `style.css:23-56`) carries, in one
glanceable row: a connection dot, a battery bar, and six chips —
体态 / 驾驶 / 模式 / 步态 / 数据 / 身高. The two that matter most for a dual-dog console:

```js
// app.js:169-170
const age = (snap.data_age && snap.data_age.sport) ?? null;
setHtml("chip-age", `数据 <b>${age === null ? "--" : age + "s"}</b>` + (age > 2 ? " ⚠" : ""));
```
Server side computes per-stream freshness generically (`state.py:410-415`), so **every** stream
gets an age, and the UI escalates at 2 s. For two dogs, per-dog data age is the single most
important field on screen.

**The drive-state chip.** The server publishes a state machine value and the client just labels
it (`app.js:128-129`):

```js
const DRIVE_STATES = { idle: "空闲", standing_wait: "自动站立中…", stand_timeout: "站立超时",
  driving: "行驶中", guard_blocked: "护航急停", watchdog: "已停车" };
```
rendered at `app.js:165-168`. This is *much* better than a generic error toast: the operator can
see "the robot is standing up, wait" versus "the watchdog stopped you" versus "an obstacle is
blocking forward". Supporting targeted hints reuse the same signal, e.g. the auto-stand hint only
shows when a drive was sent and posture isn't walkable (`app.js:456-460`).

**Emergency stop as a fixed, high-contrast, unmissable control** — red, glowing, pushed to the far
right of the sticky header with `margin-left:auto` (`index.html:28`, `style.css:58-64`). It has its
own endpoint rather than going through the generic command path (`web_server.py:71-73`), and it
both clears the queue and damps the robot (`robot.py:1685-1717`). For two dogs, you want one per
dog *and* one for both; note the current design has exactly one, no "all stop".

**Banner + status line, both de-duplicated.** `showBanner()` (`app.js:111-122`) suppresses the
same text within 2 s — added because the 20 Hz drive loop was spamming identical failures and
causing visible flicker. The footer log line (`app.js:6-7`, `index.html:122-124`) shows the last
command result.

**The anti-flicker discipline.** This is 100–150 lines of the frontend and it is the difference
between a usable telemetry console and an unusable one. Four distinct techniques:

1. **Write-dom-only-on-change**, with a memo map:
   ```js
   // app.js:12-18
   const _lastDom = new Map();
   function setHtml(id, html) {
     if (_lastDom.get(id) === html) return;
     _lastDom.set(id, html);
     const el = $(id); if (el) el.innerHTML = html;
   }
   ```
   plus `setText` / `setChecked` / `setStyle` (`app.js:19-38`).
2. **Build the skeleton once, then update values in place.** The telemetry table rebuilds only
   when the *row shape* changes, keyed on a signature of the row labels:
   ```js
   // app.js:203-213
   const sig = rows.map(r => r[0]).join("|");
   if (!teleBuilt || sig !== teleKeySig) { /* rebuild */ }
   else { rows.forEach((r, i) => setText(`tele-v-${i}`, r[1])); }
   ```
   Same pattern for foot-force bars (`app.js:219-234`) and the sector wheel (`app.js:296-319`).
   The reasoning is in the comment: at 8 Hz, rebuilding subtrees is a continuous repaint.
3. **Cache expensive static layers in an offscreen canvas.** The 128×128 height map is ~16k
   `fillRect` calls; it is drawn once per height-map `stamp` change and blitted thereafter
   (`app.js:321-366`, note `hmCache` at `:323-325` and the `hmCacheValid` gate). Point drawing
   then just overlays.
4. **Throttle at the panel level.** The lidar panel is clamped to 2 Hz client-side
   (`app.js:258-279`, `LIDAR_PAINT_MIN_MS = 500`) *and* the server sends real point data only every
   4th telemetry tick (`config.py:78`, `web_server.py:160-165`).

**The placeholder-packet rule — a protocol pattern, not just UI.** When the server throttles a
large payload it sends `{"skipped": true}`, and the client's *first* action is to ignore it
entirely:

```js
// app.js:78-86
if (msg.lidar) {
  // 占位包必须完全忽略，否则 sector-wheel 会被清空重建、整页布局周期性塌陷/弹起（屏闪根因）
  if (!msg.lidar.skipped) { onLidar(msg.lidar); onSectors(msg.lidar.sectors); }
}
```
The root cause is documented at length: a skipped packet without a `sectors` field called
`onSectors(undefined)`, which cleared and rebuilt the wheel, which made a whole panel oscillate
481↔610 px every ~400 ms (`HANDOFF.md:267-283`). If you throttle any dual-dog payload, make the
"nothing new" case explicit and make the client treat it as a no-op.

**Joystick visualisation**: circle with a 52 px knob, `touch-action: none` (essential — otherwise
the browser steals the drag as a scroll), `cursor: grab`, labelled
前进/侧移 and 转向 (`index.html:50-59`, `style.css:87-100`). Direction text hints sit right below
(`index.html:60-62`).

**Obstacle visualisation — a 3×3 direction pad, not a radar sweep** (`app.js:293-319`). Eight
sectors with distance in metres, coloured by threshold: `near` under 0.4 m (red border + red
text), `mid` under 0.8 m (yellow), `empty` at 40 % opacity for no data
(`app.js:312-314`, `style.css:149-158`). 3×3 grid arrangement via `SEC_ORDER = [7,0,1,6,2,5,3,4]`
so the layout is spatially meaningful (`app.js:295`). This is cheap to render, needs no canvas, and
is far more legible than a point cloud when all you need is "can I go forward". For two dogs, two
of these side by side is a very strong operator aid.

**Switch state: commanded vs actual, with a grace period.** All six toggles are read back from the
robot every 8 s and pushed over WebSocket (`robot.py:1842-1850`, `README.md:249-251`). The client
only overwrites a checkbox if the user hasn't touched it in 10 s:

```js
// app.js:620-632
const v = sw[key];
if (v === undefined || v === null) { setText(stId, "·未知"); continue; }
setText(stId, v ? "·开" : "·关");
if (Date.now() - (switchTouched[id] || 0) > 10000) setChecked(id, v);
```
Three states (on / off / **unknown**), a text label next to every switch, and a grace window that
prevents the state poll from fighting the user. Copy this wholesale — "the checkbox says on but
the robot never turned it on" is the failure mode it eliminates, and it's worse with two robots.
Toggles are iOS-style sliding switches implemented as a hidden checkbox plus an adjacent
`.switch` span, so the semantics stay native `change` events (`style.css:117-135`, `HANDOFF.md:229-231`).

**Charts/logs**: measured velocity, orientation, foot forces, battery voltage/current and
temperatures in a two-column key/value grid (`app.js:179-200`). Note what's missing — see below.

**Gaps in the existing UI you should fill rather than copy:**

- There is **no visualisation of the commanded velocity** — only measured. Given the client EMA,
  the server ramp and the watchdog, an operator cannot see *what was asked for* versus *what the
  robot is doing*. For two dogs this is the single highest-value addition.
- No per-robot identity anywhere (one header, one set of chips) — you need two columns, two
  e-stops, and a visible "which dog am I driving" indicator so a stick can never be ambiguous.
- No authentication of any kind (see heading 6).

---

## 6. What NOT to copy

Ordered by how much damage each could do to you.

**1. `/api/command` is an unauthenticated, unbounded reflection onto the dispatcher.** The handler
does `getattr(dispatcher, req.action, None)` and calls it with arbitrary kwargs
(`web_server.py:60-69`), there is **no auth, no token, no CORS policy, and no allow-list**
(grep for `token|auth|apikey|depends|CORS` in `web_server.py` returns nothing), and the server
binds `0.0.0.0` (`config.py:32`). Combined with the far end being a 12 kg legged robot, this is
the worst thing in the repo. If you take one action item from this section: **allow-list action
names, require a token, and don't bind a wildcard address** on a two-dog console.

**2. `os._exit(1)` as a recovery mechanism, from inside motion code.** Four sites
(`robot.py:676`, `:1781`, `:1792`, `:1807`). It works here because systemd restarts the service
(`go2ctl.service:19-20`: `Restart=on-failure`, `RestartSec=5`) and everything important lives in
`/dev/shm` and is re-read. It is defensible for **one** robot on a dedicated box; with two dogs it
is a restart storm waiting to happen, and an exit during a motion command means nobody sends the
stop. If you adopt self-healing, make it per-robot supervision that re-establishes the link
*without* killing the process that owns the other dog.

**3. The restart-on-suspicion thresholds — read the history before choosing yours.** The tuning in
this repo exists because earlier values caused *crash loops*, which the author says were worse than
the original fault (`robot.py:703-709`, `HANDOFF.md:125`):

- claim restart after `claim_restart_failures=3` consecutive rounds, but only if uptime >
  `claim_restart_grace_s=90` **and** the robot's state topic is still fresh **and** a second,
  read-only `AutoRecoveryGet` also fails (`robot.py:661-676`, `config.py:51-52`).
- `_health_loop` thresholds are deliberately loose: mover thread 3 s, **worker thread 30 s**
  (`robot.py:1787-1788`) because a legitimate RPC retry chain can block ~6 s × 3.
- `_startup_check` explicitly does **not** restart on claim failure (`robot.py:703-709`).

If you copy a watchdog without these gates you will build a robot that reboots itself.

**4. In-place patching of a vendored dependency.** `scripts/vendor_patch.py` string-replaces into
`vendor/unitree_sdk2_python` at deploy time (idempotent, with `--check`; `vendor_patch.py:1-27`,
`README.md:221-226`). The patches are *genuinely necessary* — an unhandled handler exception
silently kills a DDS channel forever, which is the best-supported root cause of the whole `3104`
saga (`HANDOFF.md:83,150-152`). But patching a dependency by text substitution means deployment
correctness depends on the upstream source matching exactly (`_apply` raises if the block isn't
found, `vendor_patch.py:33-38`). Prefer a fork, a proper patch file, or upstreaming.

**5. Two hard-coded speed ceilings that disagree.** The client scales to a literal `const MAX = 0.8`
(`app.js:498`) while the server clamps to `MAX_LINEAR=0.5` (`config.py:41`). The stick's full
deflection therefore maps to a value the server silently truncates, so the top ~40 % of stick
travel does nothing. (The yaw axis is handled correctly — see `app.js:503`.) Pick one source of
truth and publish it in `/api/state`; with two dogs you also want per-dog limits.

**6. A shared-mutable class attribute for the service cache.** `RobotCore._svc_cache` is a *class*
variable (`robot.py:1720`), assigned via `RobotCore._svc_cache = result` (`robot.py:1732`) and read
at `robot.py:1738`. Harmless with one instance; a cross-contamination bug the moment you run two
robots in one process. Same smell as the unkeyed `StateCache` — if you go multi-robot in one
process, make the state per-robot explicitly.

**7. Loose generic RPC surfaces with the wrong error semantics.** `_CallNoReply` returns a code
that `move()` ignores entirely — `code = self._sport.Move(vx, vy, vyaw)` with no check
(`robot.py:448`), so a *rejected* velocity command looks identical to an accepted one; only the
mover's `_move_fail_streak` backstop notices (`robot.py:453-466`). Also `submit()`'s default
timeout is only 15 s (`robot.py:358`) while skills are allowed to run far longer
(`turn_deg` up to `max(10, |deg|/5+4)` s, `forward_m` up to `max(8, |m|/0.15+3)` s), so a long
skill surfaces to the caller as `"command timeout"` even though it's still running. Both are the
kind of thing that turns into a mystery bug report later.

**8. Dead code and stale comments.**

- `app.js` defines `onLidar` **twice** — `app.js:240-254` and again `app.js:261-279`. The second
  wins (function declarations hoist; last definition applies), so the first ~15 lines are dead.
  The throttling only exists in the live copy, so behaviour is accidentally correct.
- `robot.py:1158-1167` `_on_video_stream()` is unreachable: it's the handler for a subscription
  that was deliberately removed (`robot.py:254-259`, `if self.cfg.camera_enabled: pass`).
- `robot.py:576` docstring says the mover sends at "20Hz" while the code and config are 50 Hz
  (`config.py:48`, `robot.py:2006`) — `HANDOFF.md:199` records the 20→50 change as a fix, so the
  docstring is the leftover.
- `README.md:298-300` still documents `speed_level` / VUI as if they work; `HANDOFF.md:29` says the
  firmware returns `-1` / `100`.
- `web_server.py:137-143` — the `driving` variable is only assigned inside the `try`, so a
  `TypeError` on the first command leaves the disconnect safety-net (`:176-182`) reading a
  possibly-unbound value. Fragile, not currently broken.

**9. `avoid_move_increment` / `avoid_move_absolute` — verified dead on this firmware.**
`MoveToIncrementPosition` / `MoveToAbsolutePosition` produce **no motion at all** on the tested
Go2 MAX (`HANDOFF.md:313-317`, and the `turn_deg` docstring at `robot.py:823`). They're kept in the
REST layer for probing only and deliberately excluded from MCP (`README.md:140-141`). Don't design
your motion model around the official "move to point" API without testing it on your firmware
first.

**10. Unbounded growth.** The observation log is append-only with no rotation
(`obs_log.py:10-16`, acknowledged at `HANDOFF.md:336`), and `last_match` re-reads the last 80 lines
and parses JSON on every call (`obs_log.py:19-43`). Fine for a demo, not for a console left running
for days. Bound your logs and your history buffers from the start.

**11. Also worth knowing, not a bug but a trap:** the lidar guard's *default value changed* for a
good reason. It shipped on-by-default, and because the obstacle test ignored the z axis, the robot's
own legs (z ≈ 0.06–0.25 m) and body (z ≈ 0.3–0.4 m) appeared as obstacles 0.2–0.25 m ahead — so
"it triggers protection and gets stuck the moment it moves" (`config.py:53-55`, `HANDOFF.md:179-197`).
The fix was height-band filtering (0.30–1.20 m) plus a 0.35 m minimum radius plus 3-frame
confirmation, and the default flipped to **off**. If you add any proximity safety, filter
self-points by height *and* confirm over multiple frames, and ship it off by default.

---

## Top 5 things worth borrowing for a dual-dog console

**1. Server-side acceleration ramping + a 50 Hz no-reply `Move` loop with a dead-man watchdog.**
`robot.py:1858-2018` (ramp at `:1984-2003`, re-send at `:2005-2018`, watchdog at `:1934-1946`);
defaults at `config.py:43-48`.
*Why:* your console currently jumps straight from stick position to full velocity, which is what
makes a Go2 lurch and feel like it's about to tip. A single `ramp(cur, goal, accel, decel)` with
`1.0 / 2.0 m/s²` (asymmetric: gentle start, firm stop) plus a 1000 ms dead-man is maybe 40 lines
and transforms the feel. Do it **on the server**, per dog, so it also protects against network
drops and a second operator — the browser must not be the only thing that can stop the robot.
This is also the prerequisite for every other safety feature (coupling, guard, watchdog) since
they all act on the ramped commanded velocity.

**2. The drive-state machine as a first-class, published value.** `robot.py:2020-2024` publishes
`drive_state ∈ {idle, standing_wait, stand_timeout, driving, guard_blocked, watchdog}`; labels at
`app.js:128-129`; also included in the agent-facing observation record (`perception.md:111`).
*Why:* it converts every silent failure ("I pressed W and nothing happened") into one word on
screen. For two dogs it's doubly valuable: a single glance tells you dog A is `standing_wait` while
dog B is `watchdog`. It also gives you a natural place to hang per-dog state colouring and to
refuse contradictory commands.

**3. The commanded-vs-actual switch reconciliation with a 10 s user grace window.**
Server: `robot.py:1810-1850` (8 s read-back of obstacle-avoid / auto-recovery / VUI / lidar /
guard / joystick-claim). Client: `syncSwitches()` at `app.js:620-632`, three-state rendering
(`开` / `关` / `·未知`), `switchTouched` timestamps.
*Why:* robot firmware silently ignores or resets switches — this repo documents `SpeedLevel`
returning `-1` and VUI returning `100` because the app owns those services (`HANDOFF.md:29`), and
`SwitchJoystick` being reset by any app reconnect (`README.md:233-234`). With two dogs, "the UI says
it's on but the robot says otherwise" is guaranteed to happen. This pattern makes the robot the
source of truth without making the UI feel laggy.

**4. The deadzone + squared response curve, and the input-arbitration rule.**
`curve()` at `app.js:398-407` (deadzone 0.08, quadratic); precedence logic at `app.js:475-481`
(gamepad only counts within 800 ms of real activity and yields to key/stick input); joystick
pointer capture + unit-disc clamp + release-to-zero at `app.js:409-437`.
*Why:* four cheap, independent wins: (a) the squared curve restores fine control at low speed,
which is where you actually manoeuvre two dogs; (b) the 800 ms gamepad gate kills the classic
"drifting stick blocks WASD" bug; (c) `setPointerCapture` makes an on-screen joystick usable with a
fast finger; (d) release-to-zero is what makes the dead-man meaningful when a browser tab loses
focus. All four are directly applicable to your arrow-pad-plus-WASD setup, and three of them are
things you can only discover by having your input silently eaten.

**5. Turn-speed coupling plus the closed-loop `turn_deg` / `forward_m` primitives.**
Coupling: `robot.py:1955-1959` (`vyaw_limit = max_yaw * max(0.25, 1 - 0.75*speed_ratio)`, live-tunable
via `set_yaw_limit()` at `robot.py:934-943` and the persisted slider at `app.js:452-454`).
Skills: `robot.py:822-896` (yaw ±90° / 3° tolerance / 0.45 rad/s; distance ±0.8 m / 85 % arrival /
0.28 m/s; both under a non-blocking lock that returns `-11 skill busy`).
*Why:* coupling is a one-line change that removes the main mechanism by which a fast, hard-turning
quadruped falls over, and it's the piece your arrow pad most needs since a fixed-max yaw command is
exactly the dangerous case. The skills then give you *verifiable* discrete motions — and they're the
right foundation for the sequence feature you actually want, because each step reports `reason` and
`actual_deg`/`actual_m` rather than just success. Build "turn 90 then walk 0.5 m" out of these and
it will be repeatable; build it out of `Move` + `sleep` (the `test_sequence.sh` approach,
`test_sequence.sh:44-48`) and it will drift every single run.
