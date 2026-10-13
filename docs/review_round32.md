# Adversarial review, round 32

Scope, in the order asked for: `unitree_robot_agent/vision/{markers,detect,arena}.py`,
`unitree_fleet_web/{formation,sync,posture,motion}.py`,
`unitree_robot_agent/motion/controller.py`, `unitree_robot_agent/fleet/discovery.py`,
and the arena / formation / vision parts of `unitree_fleet_web/service.py`.

Every finding below was reproduced by running the code (`.venv/bin/python`), except
where it says otherwise. Test names are given so a claim that "no test covers this"
can be checked.

## Revision reviewed

| file | md5 |
|---|---|
| `unitree_robot_agent/vision/detect.py` | `75cb46d20e42de5271c62a388be8cbf3` |
| `unitree_robot_agent/vision/markers.py` | `992821a4ee9d60698a7a00c9e9a5b3e9` |
| `unitree_robot_agent/vision/arena.py` | `3b1bcc7efc7bf210a4d1bcc542f6426e` |
| `unitree_fleet_web/formation.py` | `49503f1af6187f54ae163cd61abbba3f` |
| `unitree_fleet_web/sync.py` | `17937c1e84d56887ceade499b554df58` |
| `unitree_fleet_web/posture.py` | `81b99607e04682b5c502a2ec2a77150b` |
| `unitree_fleet_web/motion.py` | `d90b00060bb12ddba53d12ef8e936bcb` |
| `unitree_fleet_web/service.py` | `f803a138887a5a9d58825dd804a066c4` |
| `unitree_robot_agent/motion/controller.py` | `3750330d513dc3e51a9218d27e9a9424` |
| `unitree_robot_agent/fleet/discovery.py` | `e53d84ba4abf121a1a4fab91988f694a` |

**Files changed underneath this review.** While it was running, a concurrent edit
added and then partially removed a mirrored-calibration guard
(`detect.py` `Calibration.mirrored`, `service.calibrate_arena`). The line numbers
below are for the revision above; findings F7 and F9 were re-checked against it
after the change. The `service.stop_all` region and `controller.py` were not
touched by that edit.

---

## F1 — `stop_all()` does not stop a running formation, and the loop drives the dogs again (HIGH, safety)

`unitree_fleet_web/service.py:2724` (`stop_all`), `:2757` (`estop`), `:2020`
(`start_formation`), `:2110`/`:2166`/`:2221` (the loop)

**Trigger.** Start a formation on two connected robots that need more than one
step (`POST /api/fleet/formation {"distance_m": 1.0, "bearing_deg": 0,
"mover": "b"}` with the dogs ~3 m apart), then press **STOP ALL**
(`POST /api/fleet/stop`) while it runs.

**What happens.** `stop_all` cancels `self._routine_task` — its own comment says
why: *"Leaving it running would mean the fleet stops and then the sequence
immediately issues its next step — the operator's stop would look like it did
nothing."* It never touches `self._formation_task`. The formation loop checks
only `self.state.estop.engaged` (an e-stop latch, not a STOP ALL), so on its next
iteration it plans again and calls `robot_walk`/`robot_move`/`robot_turn`.

Reproduced with the simulated arena from
`tests/test_console_formation_loop.py` (dogs at `(0.5, 2.0)` and `(3.5, 2.0)`):

```
started: running
commands before STOP ALL: 1
stop_all stopped: []
commands at stop: 1  3s later: 2
formation state: running steps: 1
commands issued after STOP ALL: [('dog2', {'walk_m': -0.35})]
```

**What should happen.** STOP ALL should cancel `_formation_task` and mark the run
stopped, the way it does for the routine — or the formation loop should treat a
STOP ALL as a stop reason and return `estopped`/`stopped` before its next command.

**Test coverage.** None. `tests/test_console_formation_loop.py::
test_stopping_a_formation_stops_the_dogs` uses the formation's own stop endpoint
(`/api/fleet/formation/stop`), which *does* cancel the task. `::
test_the_estop_stops_it` covers the e-stop, which also works (the latch makes
`call_tool` refuse the motion-gated tools at the next step). STOP ALL is the one
stop that leaves the loop running, and it is the button an operator reaches for.

**Class.** The six-bug list's #3 (two writers) in its stop-shaped form, plus
"state that is not reset": `_formation["state"]` stays `running` after the stop.

## F2 — the no-odometry walk fallback commands full stick while timing the move at 0.28 m/s (HIGH, safety)

`unitree_robot_agent/motion/controller.py:398-414`, specifically `:403-404`;
contrast the closed-loop publish at `:469`

**Trigger.** Any `robot_walk`/`forward_m` on a robot that publishes no
`position` — which this project documents as the normal case on its own
hardware (*"the Go2 used for testing never publishes it over WebRTC"*).

**What happens.**

```python
speed = float(speed if speed is not None else getattr(self.config, "max_walk_speed", 0.28))
...
seconds = abs(target_m) / max(0.05, speed)
result = await self.drive(ly=math.copysign(1.0, target_m), duration=seconds)
```

`drive`'s `ly` is a *raw joystick axis* (`robot_joystick` documents it as "the raw
stick values ... each in [-1, 1]"), and `TrapezoidProfile(peak=1.0, ...)` scales
it to a full-scale 1.0 at cruise. The duration is computed as if the robot moved
at `speed` *metres per second*. One of the two must be wrong, and the closed-loop
branch of the same function publishes the same walk at `ly = 0.28`.

Measured for `forward_m(0.35)`:

```
{ok: True, reason: 'no_odometry', timed_s: 1.25}
peak |ly| commanded = 0.96 (full-scale 1.0 at cruise)
integral = 0.624 stick-seconds
   -> at a 1.4 m/s robot that is 0.87 m for a requested 0.35 m  (x2.5)
   -> at a 3.0 m/s robot that is 1.87 m for a requested 0.35 m  (x5.3)
closed-loop branch of the same call: peak |ly| = 0.28
```

The over-drive factor is the robot's maximum speed in m/s (relative to 1.0): the
commanded stick carries no information about `speed` at all.

**What should happen.** The fallback must command the same magnitude the
closed-loop path commands (`ly = ±speed`), or scale the duration by an honest
nominal maximum — and say which it did. `reason: "no_odometry"` currently labels
a guess about *distance*; the number that is actually wrong is *speed*.

**Why it matters here.** `unitree_fleet_web/service.py:2236-2245`
(`_apply_formation_step`) prefers `robot_walk` for forward corrections, so on the
documented real-hardware case every formation step's forward correction becomes a
full-stick 1.25 s sprint — two dogs in a room, aimed at each other by a loop that
believes it is shuffling 35 cm.

**Test coverage.** None for the magnitude.
`tests/test_motion_walk.py::test_without_odometry_it_walks_on_a_timer_and_says_so`
asserts `reason`, `requested_m`, `timed_s` and the note. `::
test_a_forward_walk_commands_forward` — the test written for the *previous*
sign bug on this exact line — drives a position sequence, so it never enters the
fallback. This is the same shape as that bug: the test checks what was reported,
not what was commanded.

**Class.** The units class (#1's family): a normalised stick value and a
metres-per-second figure share one constant.

## F3 — `converged()` ignores the target bearing, so the formation reports "已到位" for a formation that is 90° wrong (MEDIUM)

`unitree_fleet_web/formation.py:357-389` (the verdict is `:381`), used as the only
stop condition at `unitree_fleet_web/service.py:2166`

**Trigger.** Two dogs 1 m apart due north, parallel headings; operator asks for
`distance_m=1.0, bearing_deg=0.0` (B to the east of A).

```python
poses = {'dog1': {'x':0,'y':0,'heading_deg':0}, 'dog2': {'x':0,'y':1,'heading_deg':0}}
converged(poses, Formation(distance_m=1.0, bearing_deg=0.0))
# -> {'ready': True, 'distance_m': 1.0, 'bearing_deg': 90.0, ...}
converged(poses, Formation(distance_m=1.0, bearing_deg=270.0))['ready']  # -> True
```

`ready` is `abs(distance_error) <= tolerance_m and abs(yaw_error) <= tolerance_deg`;
`target.bearing_deg` is never read. `align_formation` returns `"converged"` with
an empty step list, and the page shows 已到位 — for a commanded formation the pair
is not in.

The module is internally inconsistent about this: `plan()` (`:281-293`) *does*
use the bearing, and `tests/test_console_formation.py::
test_the_bearing_moves_b_around_a_not_along_the_gap` exists precisely to prove
that "the bearing, not just the distance" is what makes a formation rather than a
queue. The measurement function the loop stops on disagrees with the planner it
stops.

**What should happen.** `ready` should also require the actual bearing to be
within tolerance of `target.bearing_deg` (or the loop should stop on `plan()`
returning no moves, so there is one notion of "done").

**Test coverage.** None. Every `converged()` call in
`tests/test_console_formation.py` and `tests/test_console_formation_loop.py`
either uses `bearing_deg=0` with the dogs laid out along +x, or omits the bearing.
`test_the_bearing_moves_b_around_a_not_along_the_gap` calls `plan()`, not
`converged()`.

**Class.** "An assumption that silently produces a plausible wrong number" — the
number that is wrong is the *verdict*. Not a safety issue on its own: the failure
is that the dogs do not move when the operator asked them to.

## F4 — the marker→robot mapping is positional over the roster, and the printed manifest is never read (MEDIUM, wrong-robot)

`unitree_fleet_web/service.py:1778-1787` (`_marker_manifest`),
`unitree_robot_agent/vision/markers.py:144-160` (`tag_id=index`), `:321`
(`load_manifest`)

**Trigger.** Print the sheet with the documented snippet
(`write_markers(Path(...), default_markers(['dog1','dog2']))` →
tag 0 = dog1, tag 1 = dog2, `docs/REAL_DOG_SESSION.md:450`), then run a console
whose roster lists the robots the other way round:

```json
{"default": "dog2", "robots": [{"id": "dog2", ...}, {"id": "dog1", ...}]}
```

`Roster.from_mapping` preserves the JSON order (`fleet/roster.py:253-263`),
`FleetManager._runtimes` preserves it again (`fleet/manager.py:93`), and
`_marker_manifest` calls `default_markers(list(self.fleet.ids))`, so tag 0 is now
"dog2". Every pose the camera reports is attributed to the other dog — positions,
headings and the separation all look completely plausible — and
`align_formation` then steers each physical dog by the other one's error.

**What should happen.** The mapping should come from the artifact that exists for
it: `markers.json`, written by `write_markers` (`markers.py:302-317`), whose own
docstring says it "is what the detector reads to know which tag is which robot
and how big the tags physically are ... Without it the mapping lives in
somebody's head, and it will be wrong on the second session."
`load_manifest` has **no call site anywhere in the repository** — it is exported
in `vision/__init__.py` and then never used, so the console re-derives a mapping
that the printed sheet may disagree with, and a mismatch is silent.

**Test coverage.** None. `tests/test_vision_arena.py::
test_the_console_maps_the_arena_from_a_camera` renders its scene with
`default_markers(["dog1","dog2"])` and the console roster is in the same order,
so the two positional mappings agree by construction.
`tests/test_vision_markers.py::test_writing_markers_produces_a_manifest_the_detector_can_read`
writes and reads the manifest itself and never goes through the console.

**Class.** #2 (wrong-robot routing), one layer down: not a tool reaching for the
wrong runtime, but a detector attributing a tag to the wrong robot.

## F5 — `parallax_m` under-reports the parallax by exactly `scale_factor`, and is documented as an upper bound (MEDIUM-LOW)

`unitree_robot_agent/vision/detect.py:155-173` (`parallax_m`), `:176-192`
(`residual_m`), `:196-218` (`scale_factor`)

**The arithmetic.** With the camera at height `H` above the floor point `c` and a
tag at height `h` above the dog's floor point `p`, the homography (calibrated on
the floor) reports `q = c + (p - c)·H/(H-h)`, so the error is

    |q - p| = |p - c| · h/(H - h) = |p - c| · (h/H) · H/(H-h)

The code computes `self.tag_height_m * lateral / self.camera_height_m`, i.e.
`|p - c|·h/H` — the first-order form, **short by exactly the class's own
`scale_factor`** (`H/(H-h)`, 1.163 in its worked example). The two properties
contradict each other: `scale_factor`'s docstring derives the expansion
correctly ("a metre between two dogs reads as 1.16 m" — which is the same
statement as "the position error is L·h/(H-h)"), while `parallax_m` treats the
same geometry as L·h/H.

**Concrete case where the verdict flips.** `tag_height_m=0.35`,
`camera_height_m=2.5`, a 1.5 × 1.5 m arena calibrated from the centre, camera at
the centre: the worst corner is 1.06 m out → reported residual 0.148 m (≤ 0.15 →
`adequate: True`, "the parallax is small enough to call the positions positions"),
true residual 0.172 m (> 0.15 → not adequate). In the module's own 4 m arena
example the reported 0.40 m is really 0.46 m.

**What should happen.** Multiply by `scale_factor` (or state the number as a
first-order estimate and stop calling it an upper bound). `residual_m`'s
docstring says "An upper bound rather than a typical value, because the number is
used to decide whether a floor calibration is good enough, and a typical value
would answer that question optimistically" — as written it *is* the optimistic
answer, by 16%.

**Test coverage.** Not discriminating:
`tests/test_vision_detect.py::test_a_floor_calibration_reports_its_own_error`
asserts `residual_m > 0.15` (0.46 also passes) and pins `scale_factor`
separately; `::test_a_higher_camera_is_better_but_not_enough` only asserts
monotonicity. No test evaluates the true projection error, so no test can see the
factor.

**Class.** Units/geometry, "a plausible wrong number": it is a diagnostic number
whose whole purpose is honesty about precision.

## F6 — `forward_m`'s reason labels are swapped: a timeout says "stopped", a lost odometry says "timeout" (LOW-MEDIUM)

`unitree_robot_agent/motion/controller.py:472-487` (`else: aborted = True` at
`:473`; the reason expression at `:483-487`)

**Trigger and result.**

```
A: creeping forward, never arrives, timeout=1.0  -> reason "stopped", ok False
B: odometry returns None after the first read    -> reason "timeout", ok False
```

`while ... else: aborted = True` fires on the *deadline*, so the deadline path
takes the `"stopped"` branch, while `"timeout"` is reachable only from the
`break` on `current is None` — the opposite of both words. `aborted` is also what
`ok` uses (`reached and not self._abort.is_set()`), so `aborted` means two
different things in one function.

**What should happen.** A deadline is `"timeout"`; `aborted` should mean the
abort flag only. The tool surfaces `reason` verbatim to the caller
(`robot_walk` → `ctx.ok(**payload)`), so an agent or an operator reading
`"stopped"` concludes the walk was interrupted — by a stop, an e-stop or an
operator — when in fact nothing stopped it.

**Test coverage.** None.
`tests/test_motion_walk.py::test_a_slow_walk_is_not_mistaken_for_a_stalled_one`
asserts `reason in ("reached", "timeout")` and currently gets `"reached"`, so the
deadline branch is never taken; `::
test_a_robot_pushed_backwards_is_not_detected_as_such` uses a 1.0 s timeout and
asserts only `actual_m`. The author's own set of allowed reasons shows what was
expected.

**Class.** "A field set on one path and read on another" / the reported value
being a plausible wrong one.

## F7 — `Calibration.mirrored` is inverted with respect to the page's own convention and still ships in `as_dict()` (LOW-MEDIUM)

`unitree_robot_agent/vision/detect.py:92-133` (the predicate at `:130-133`),
`:239` (`"mirrored": self.mirrored` in `as_dict`)

Measured against the winding the console actually generates
(`defaultWorldPoints`, `console.js:2186-2190` → `[[0,d],[w,d],[w,0],[0,0]]` with
clicks 左上→右上→右下→左下), and against the reversed click order it was written to
catch:

```
console / loop-test convention (defaultWorldPoints): mirrored = True
the winding the new test calls "normal":            mirrored = False
reversed click order (the bug it was written for):  mirrored = True
console winding with clicks reversed:               mirrored = False
```

So `mirrored` is `True` for a *correct* console calibration and `False` for the
reversed one, and the two tests that pin its semantics
(`tests/test_vision_detect.py::test_the_four_corners_wound_one_way_are_not_mirrored`,
`::test_reversing_the_click_order_is_detected_as_mirrored`) use the opposite
world winding from the page, so they lock the inverted meaning in rather than
catching it.

**What happens now.** The concurrent edit removed the only consumer (the refusal
in `service.calibrate_arena`) and documented the trap in the docstring — but the
field is still emitted by `as_dict()` (`:239`), which is what
`service.calibrate_arena` writes to the calibration file and returns to the page
(`service.py:1984`, `:2002`). Any future consumer of the saved JSON reads
`"mirrored": true` for a good calibration.

**What should happen.** Either delete the field, or make it mean what it says
(compare the winding of the two point sets, not the sign of a determinant whose
sign is fixed by the choice of affine chart), or rename it to
`orientation_reversing` so nobody re-derives the guard that was just removed.

**Test coverage.** Covered — for the wrong semantics (see above). Treat this as a
warning about the tests, not as an untested path.

**Class.** Sign/convention (#1), in a predicate that is currently inert but
persisted.

## F8 — `ArenaCamera.close()` abandons a thread that did not join, and `start()` revives it (LOW, resource)

`unitree_robot_agent/vision/arena.py:103-111` (`start`: `self._stop.clear()`),
`:116-121` (`close`: `join(timeout=3.0)` then `self._thread = None`)

**Trigger.** A read that does not return within 3 s — the unplugged/busy device
this class exists to survive (`_open`/`_read_loop` can block inside
`cv2.VideoCapture.read`) — then the camera is closed and opened again.

Demonstrated with a stub whose `read()` blocks:

```
after start: thread alive: True running: True
close() returned after 3.0s; .running says False
BUT the old thread is still alive: True
live 'arena-camera' threads now: 2 | old thread alive: True
```

`close()` reports False from `running` (so `status()` says `"stopped"` while the
device is still held), and `start()` clears `_stop`, which un-cancels the
abandoned thread: two readers on one device, both writing `_frame` and
double-counting `decoded`/`dropped`.

**What should happen.** A join that times out should not be forgotten: keep the
old thread handle (or a generation counter) so `start()` cannot clear a stop flag
that a live thread is still polling, and so the device is released before a
second open.

**Test coverage.** None. `tests/test_vision_arena.py::test_closing_releases_and_says_stopped`
and `::test_closing_twice_is_fine` use a camera that stops promptly, so the join
always succeeds.

**Class.** State that is not reset / thread-lifetime correctness.

## F9 — the vision pipeline computes its own confidence measure and then ignores it (LOW-MEDIUM)

`unitree_robot_agent/vision/detect.py:408-420` (`tag_px=span`),
`unitree_robot_agent/vision/markers.py:85,93` (`CELLS_ACROSS`, `MIN_PX_PER_CELL`),
`:404-412` (the note that says what happens below the floor)

`DogPose.tag_px` is documented as "the honest measure of how much to trust the
numbers beside it" and is shipped in every status push, but it is compared to
nothing: `MIN_PX_PER_CELL * CELLS_ACROSS` = 64 px is used only by the offline
`pixel_budget` helper, `arena_state` reports the pose as `"ok"`, and `tag_px` is
not read anywhere in `console.js` either. A tag detected at 39.6 px (measured:
`locate` reports a pose for a 40 × 40 px tag) is treated exactly like a 200 px
one, while `markers.py` says of that regime: *"低于这个数检测器不是失败，是开始瞎猜
id —— 而猜错 id 会把一只狗的位置算到另一只身上，比检测不到更糟"*.

**What should happen.** Either flag a pose whose `span` is below
`MIN_PX_PER_CELL * CELLS_ACROSS` (and surface it, since it is already on the
wire), or stop calling it the honesty measure. The `heading` half of the same
concern is handled by the detector itself — see the disproof list.

**Test coverage.** None (nothing consumes the value).

**Class.** "A plausible wrong number": the pose is right, the id may not be, and
nothing says so.

## F10 — `SyncMonitor.mark()`'s `missing` is always empty (LOW)

`unitree_fleet_web/sync.py:133-144`

```python
self._reference = {robot_id: (...) for robot_id, (...) in poses.items()}
...
return {"marked": self._members, "missing": [r for r in poses if r not in self._reference]}
```

Every key of `poses` is in `_reference` by construction, so `missing` is `[]` for
every input, while the docstring promises "Robots missing a pose are not recorded,
and are named in the return so the operator knows the reference does not cover
them." The operator-facing path is unaffected only because
`service.mark_sync` computes its own, correct `skipped`
(`unitree_fleet_web/service.py:1664`). Dead field, documented as live.

**Test coverage.** None; no test reads `missing` (they read `marked`).

**Class.** A field that reads as information and never carries any.

## F11 — `discover()`'s `address_hint` counts the wrong set (LOW)

`unitree_robot_agent/fleet/discovery.py:396-400`

```python
"账号里有 {n} 台设备只有序列号、没有 IP".format(n=len(by_sn))
if any(not item.ip for item in by_sn.values()) else ""
```

The guard tests for *any* device without an IP; the count is *all* found devices.
With one dog found on the LAN (IP + SN) and one account-only device, the message
says "账号里有 2 台设备只有序列号、没有 IP" when only one is — in the one place an
operator goes to decide whether a roster with an SN-only entry is normal.

**Test coverage.** None (`address_hint` is not read by `tests/test_discovery.py`).

**Class.** A plausible wrong number in a user-facing note.

## F12 — `robot_walk`'s `speed` reaches the wire unclamped and unchecked (LOW)

`unitree_robot_agent/tools/locomotion.py:402-403`,
`unitree_robot_agent/motion/controller.py:388`, `:469`

`robot_move` and `robot_joystick` clamp every axis with
`guard.clamp_speed(..., limit=SPEED_LIMIT)`; `robot_walk` passes `speed` straight
to `forward_m`, which publishes it as-is:

```
forward_m(0.05, speed=nan) -> published ly: nan
forward_m(0.05, speed=8.0) -> published ly: 8.0
forward_m(0.05, speed=-3.0) -> published ly: -3.0
```

The console's own path defends against both (`motion._clip` maps NaN to 0.0 and
clips to ±1.0; `service._frame` calls `_clamp`). Through the tool the negative
case is neutralised by `abs(speed)`, so the reachable ones are NaN (an axis
outside any valid range, and `json.dumps` would emit the non-standard `NaN`) and
out-of-range magnitudes. `robot_turn` at least checks `math.isfinite(speed)`;
`robot_walk` does not.

**Test coverage.** None.

**Class.** Units/limits — the same axis, validated on three of four paths.

## F13 — `max_walk_speed` is not a config field, so the walk speed is always the literal 0.28 (LOW)

`unitree_robot_agent/motion/controller.py:388`

`getattr(self.config, "max_walk_speed", 0.28)` — `AgentConfig`
(`unitree_robot_agent/config.py:179-184`) defines `motion_accel`,
`max_turn_speed`, `turn_tolerance_deg`, `max_turn_seconds` and no walk speed. The
`getattr` default therefore hides that the knob does not exist: a roster that
sets `"max_walk_speed"` is rejected by `FleetManager.config_for`'s unknown-option
check, and an operator who believes they tuned the walk speed has not. The same
literal is the one F2 mis-uses as metres per second.

**Test coverage.** None. (`tests/test_motion_walk.py`'s `FakeConfig` defines the
attribute, which is exactly why the missing field is invisible in tests.)

**Class.** A plausibly-configurable constant that is not.

## F14 — the page has two heading-arrow tables that disagree by the sign of the rotation (LOW, display)

`unitree_fleet_web/static/console.js:1205` (`renderWhere`) vs `:2135`
(`drawArena`), both fed a yaw that is counter-clockwise-positive

```js
// renderWhere, from sport_state.position[2] (radians):
const arrow = ['→','↘','↓','↙','←','↖','↑','↗'][round(yaw/(π/4)) % 8];
// drawArena, from DogPose.heading_deg (degrees, 0 = +x, CCW):
const arrow = ['→','↗','↑','↖','←','↙','↓','↘'][round(bearing/45) % 8];
```

Same quantity, same page: a robot whose heading is 90° shows **↑** on the arena
map (justified there by "arena +y is up" and the `toY` flip) and **↘** on its own
odometry chip — the two disagree by the sign of the rotation, so a dog turning
left appears to turn right on one of the two panels. `where()` passes
`position[2]` through unchanged (`service.py:1418`, `:1425`), and the chip has no
axes to make a screen-down convention explicit.

**What should happen.** One table, or an explicit statement of which frame each
chip draws in. (Harmless to motion; it is a picture, not a command.)

**Test coverage.** None — `tests/test_console_app.py:1070` only asserts that
`renderWhere` exists in the source.

**Class.** Sign/axis convention (#1), in a display path.

---

## Looked wrong, and I disproved it

* **`MotionController.hard_stop` does not zero `published`.** The loop compares
  against `controller.published` before publishing (`service.py:1168-1177`), so a
  hard stop is re-sent — that *is* the fix, not a hole. `hard_stop`'s duplicated
  docstring comment (`motion.py:174-183`) is a copy-paste artefact, not a bug.
* **`sync._pair` compares `yaw_spread_deg` (degrees) with `distance_spread_m`
  (metres) to choose the leader.** Wrong in principle, and the `leader` it picks
  feeds the sign of `correction()`. I could not construct a realistic input where
  the distance branch wins with a yaw spread above `ALIGNED_YAW_DEG`: it needs
  `distance_spread_m > yaw_spread_deg` in raw numbers, i.e. tens of metres of
  odometry divergence. Downgraded to a note.
* **`turn()`'s `elapsed_s`.** `round(max_seconds - max(0.0, deadline - now), 2)`
  reads like "always the budget"; it is correct on both exits (early break gives
  the real elapsed time, the deadline gives the budget).
* **`_body_frame`'s lateral sign vs `robot_move`'s `vy`.** `left = (-sin h, cos h)`
  with heading CCW from +x, `robot_move`'s `vy` documented left-positive
  (`locomotion.py:191`), and the console's `teleop`/`_publish_commanded` round-trip
  (`vy = -lx`, `lx = -vy`) agree. Covered by
  `tests/test_console_formation.py::test_the_same_error_reached_from_a_different_heading`
  and its mirror.
* **`_heading_deg`'s `return 0.0` for a degenerate corner pair.**
  `detect.py:361-362` reports an unresolvable heading as a confident 0°. I tried
  to reach it with slivers (200×4, 80×6 px): the detector rejects those outright,
  so I could not construct an input that reaches the fallback. Downgraded.
* **The calibration panel's centimetres.** `index.html:108-109` collects cm and
  `console.js:2318-2319` divides by 100 before posting; the click→frame-pixel
  conversion uses `frame_size` from the native frame, not the displayed one.
  Handled, and pinned by
  `tests/test_vision_arena.py::test_the_frame_size_is_reported_beside_the_frame`
  and `::test_the_console_js_converts_clicks_back_to_frame_pixels`.
* **`detect.separation`'s wrap and the service's duplicate of it.** Both use
  `abs((a - b + 180) % 360 - 180)`; the frontend's bearing glyph table
  (`console.js:2135-2140`) maps 0/45/90/... to →/↗/↑ with arena +y up and screen
  y down, which matches `toY`.
* **`plan()`'s `if robot_id == a_id: ... else: ...` with identical branches**
  (formation.py:320-324). The duplicated arms are pointless but the values are
  right: A's share is `-0.5`, so `error * share` moves A toward B. Covered by
  `test_splitting_the_correction_moves_both_dogs`.
* **`MotionController._window` and `abort_window`.** The check-and-claim has no
  `await` between them, `_idle`/`_skill_owner` are restored in `finally`, and
  `service._motion_loop` stands aside on `fleet.get(rid).motion.window_active`
  against the *agent's* controller (the console's ramp has the same class name but
  is a different object) — the arbitration is real.
* **`align_formation`'s progress check uses the previous step's plan error, not
  the previous measurement.** `steps[-1]` holds the plan computed from the same
  poses as its own `measured`, so `before` and `current` are one move apart, which
  is what the check wants. (It does compare a tolerance-zeroed plan error against
  an un-zeroed measurement, so a heading-only step can be reported as
  "间距差没有变小" — confusing wording, not a wrong decision.)
* **`ArenaCamera.status()`'s state machine** and `ReplayCamera`'s dropped-frame
  accounting: consistent with their tests, including the `_at == 0.0` trap the
  loop tests document.

## Limits of this review

* I did not run the hardware paths; everything above is code plus the mock/loop
  harnesses in `tests/`.
* F2's real-world distance depends on a Go2's actual maximum speed, which this
  project says nobody has measured (`formation.NOMINAL_MAX_SPEED_MPS = 3.0` is a
  named guess). The structural defect — a full-scale stick commanded by a
  duration computed from a metres-per-second figure, while the closed-loop branch
  of the same function commands 0.28 — does not depend on that number.
* F4's trigger requires a roster whose order differs from the sheet's print
  order; with the two-dog roster in the documented order the mapping agrees. The
  defect is that nothing checks, so the failure is silent when it happens.
* F5's flipped verdict needs a small arena (≈1.5 m) or a high camera; in the
  module's own 4 m example both figures say "not adequate".
