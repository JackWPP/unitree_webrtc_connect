# Adversarial review of the fixes, round 35

The ten fixes in `92b704e`, `9c0c598` and `fa92084` (with `6334048`, the
docs-only commit that records the mirror-guard lesson, as their preface) were reviewed by
**trying to falsify each one** — for the guards, the question is always "can it
fire on a correct input?". Every finding below was reproduced by running the
code. Where I could not falsify a fix, it says so at the end; for a fix review
that is half the result.

## Revision reviewed

| file | md5 (`f182bfe`, where the fixes under review were committed) | md5 (tree at the end of this review: `eecd39d` + uncommitted) |
|---|---|---|
| `unitree_robot_agent/vision/detect.py` | `fa0e311b401ed74e7784fb3776ac064f` | `d80a684de663332c7bb5847c10b707fa` |
| `unitree_fleet_web/formation.py` | `415b61a05b4e31bcdccaaeb06e3a10c7` | `c8b3bbfe6e8573cdc1057fd74c9c5c2f` |
| `unitree_fleet_web/service.py` | `ffe025a4e51e0eacb485cf59ee8dc8e5` | `e323821763c045c507e7b7ad7c5cd537` |
| `unitree_robot_agent/motion/controller.py` | `860a95809e6e3ad4ac86148edd2caa6a` | (unmodified) |
| `unitree_robot_agent/vision/markers.py` | `992821a4ee9d60698a7a00c9e9a5b3e9` | (unmodified) |
| `unitree_fleet_web/static/console.js` | `b0bf41b195ebb62bd5107b6d93e62852` | (unmodified) |
| `tests/test_console_formation_loop.py` | `ded8debff86c3bb69540dd5878932ccd` | (unmodified) |
| `tests/test_console_formation.py` | `12656db209bf2b5678b6af5ebc3e96f9` | `17b1ec05a1a0a8ac26b3b9d5fa8712fc` |
| `tests/test_motion_walk.py` | `446f01b480b365a38854ae79ef683307` | (unmodified) |
| `tests/test_vision_detect.py` | `60582ad568325cb1386c1cd112388d14` | `a518f6df283abfc053e68e0ce5bde912` |

**The tree changed underneath this review — twice.** While it ran, a concurrent
agent (a) landed an `inf → None` rewrite of the parallax chain, now committed as
`af51f1d` + `eecd39d`, and (b) rewrote `converged()` to decide `ready` from the
positional error, still uncommitted. Findings 1 and 3 below are therefore
reported twice: the fix as it was asked about, and the follow-up fix, checked for
its own defects. **Citations are for the current tree (`eecd39d`); a number
written `f182bfe:NNN` refers to the state the review started from.**

Snapshots used: `/tmp/head35` = `f182bfe`, `/tmp/prefix35` = `git archive
6334048` (the pre-fix baseline), `/tmp/wt35` = the tree above. All 970 tests pass
at `f182bfe` before any of this, so nothing below is a consequence of a red
suite.

---

## F1 — the `inf` guard on `parallax_m` reaches the wire, and the console goes dark (HIGH, availability)

`unitree_robot_agent/vision/detect.py` `f182bfe:182-186` (`parallax_m` returns
`inf`), `f182bfe:207` (`residual_m` → `inf`), `f182bfe:232-233` (`scale_factor` →
`inf`); `unitree_fleet_web/service.py` `f182bfe:1726-1727` (`start_arena`),
`f182bfe:1878-1879` (`arena_state`'s calibration block), `f182bfe:2084`
(`calibrate_arena`). In the current tree the same sites are `detect.py:193`,
`:214`, `:243` and `service.py:1737`, `:1889`, `:2098` — see F2 for what the
committed fix did to them.

**Input.** A calibration whose camera sits at or below tag height — the case the
new guard exists for. Both numbers come from the page's own two fields
(`console.js:2318-2319`, `index.html:108-109` default 35 cm / 250 cm) and
**nothing validates that the camera is above the tag**, so an operator puts the
tag on a box and the camera on a low tripod: `tag_height_m=0.35,
camera_height_m=0.30`.

**Expected.** `adequate=False`, the bound reported as "not computable", the
console still usable.

**Actual, measured at `f182bfe`** (`/tmp/exp6_inf.py`, `/tmp/exp6b_status.py`,
`/tmp/exp6c_endpoints.py`):

```
parallax_m -> inf   residual_m -> inf   scale_factor -> inf
JSONResponse(Calibration.as_dict())                ValueError: Out of range float values … : inf
JSONResponse(status payload)                       ValueError: Out of range float values … : inf

GET  /api/arena/calibration  -> 500 "Internal Server Error" (handler unwrapped)
POST /api/arena/calibrate    -> 500 {"code":"internal_error","error":"…not JSON compliant: inf"}
GET  /api/fleet/status       -> 500 {"code":"internal_error","error":"…not JSON compliant: inf"}
ws   /ws  (the 4 Hz feed)    -> {…"calibration":{"residual_m":Infinity,"scale_factor":Infinity…}}
```

`start_arena()`'s return dict carries the same `inf`, but it has no HTTP route
(it is called from `service.start()`), so that one never reaches a response.

Starlette's `JSONResponse.render` is `json.dumps(..., allow_nan=False)`, so every
HTTP endpoint carrying the arena payload 500s. The 4 Hz **websocket status push**
uses `WebSocket.send_json`, which does *not* pass `allow_nan=False`, so it emits
the bare token `Infinity`; the page then runs `JSON.parse(event.data)` inside
`console.js:1676`'s `try { … } catch (_) { /* ignore */ }` — **the parse fails and
is silently swallowed, and the whole console stops updating with no error shown
anywhere.** That is the failure mode of this fix: not a crash, a console that
looks alive and is frozen.

**Test coverage.** None. Every calibration in the suite uses either
`camera_height_m=0` (which hits the earlier `<= 0` return) or a camera well above
the tag; `grep -n "camera_height_m=" tests/*.py` has no case with
`0 < camera <= tag`. The five new tests in `9c0c598` cover the formula and the
`<= 0` early return only.

**Severity: HIGH.** Triggered by a wrong-but-accepted operator input, and the
result takes out the arena panel *and*, via the websocket, the entire live
console.

---

## F2 — the committed fix for F1 is incomplete, and adds a `TypeError` (HIGH)

`unitree_fleet_web/service.py:1737` and `:1889` (`round(…scale_factor, 3)`),
`:2098` (`round(calibration.residual_m, 3)`);
`unitree_robot_agent/vision/detect.py:193` (`return None` instead of `inf`) —
all in `af51f1d`, the commit that closes the original finding.

That change (`_maybe_round` plus `Optional[float]` through the chain) is the
right shape, and it fixes the pose payload and `Calibration.as_dict()`. Two holes
remain, both measured against the committed code (`service.py` md5
`e323821763c045c507e7b7ad7c5cd537`):

1. **`scale_factor` is still `inf`** at `service.py:1737` and `:1889` — it is
   returned by `Calibration.scale_factor` (`detect.py:242-243`, unchanged) and
   still rounded with a bare `round`. Re-running the same reproduction:

   ```
   start_arena()          -> {'residual_m': None, 'scale_factor': inf, …}
   GET /api/fleet/status  -> 500 (internal_error: … inf)
   websocket payload      -> "calibration":{"residual_m":null,"scale_factor":Infinity,…}
   GET /api/arena/calibration -> 500
   ```

   So the console is still frozen, and `GET /api/arena/calibration` and
   `GET /api/fleet/status` still 500 (both confirmed through a `TestClient`
   against the working tree).

2. **`calibrate_arena` now raises `TypeError`** at `service.py:2098`, because
   `residual_m` became `None` and that last `round` was missed:

   ```
   TypeError: type NoneType doesn't define __round__ method
   ```

   The endpoint is reached *after* the calibration file has been written and
   `self._calibration` replaced, so the operator gets a 500 from a request that
   in fact took effect. (Before the in-flight change this line produced `inf`
   and the 500 came one layer later, from the encoder.)

**Test coverage.** None of the new tests exercise `camera <= tag` through
`calibrate_arena`/`start_arena`; `test_vision_detect.py`'s new tests only call
`parallax_m`/`residual_m` directly.

**Severity: HIGH** — the committed fix does not close the hole it was written
for, and introduces a new crash on the same input. Both are one-line changes (`_maybe_round` on all three sites, or better, validation that
`camera_height_m > tag_height_m` in `Calibration.from_dict`/the calibrate route
so the state cannot exist at all).

---

## F3 — `converged()` and `plan()` disagree about what "done" means, in both directions (MEDIUM; fixed in the working tree)

`unitree_fleet_web/formation.py:391-397` (`ready` = two scalars: radial distance
and bearing) vs `:301-303` (`plan` zeroes the move on the **positional** error)
and `unitree_fleet_web/service.py:2304` (`no_move`, `f182bfe:2290`), `:2294`
(the progress metric, `f182bfe:2280`)

Adding the bearing term made `converged()` stricter without teaching `plan()` the
same rule, and the two are no longer the same test. Swept over 52 920
combinations (`/tmp/exp9_invariant.py`), HEAD disagrees in 2 485 of them.

**Input A — the loop cannot satisfy its own exit condition.** Target distance
0.5 m, bearing 0°, dogs measured 0.497 m apart at a 9.7° bearing error (8.4 cm of
lateral error, i.e. *inside* `tolerance_m=0.10`):

```
converged() -> ready False   (bearing 9.7° > 8°)
plan()      -> converged True, moves [], notes ['已经到位。']
the loop    -> outcome "no_move", detail "规划不出可执行的修正", 0 steps, 0 commands
pre-fix     -> outcome "converged"   (the same input reported success)
```

Reproduced through `align_formation` with the simulated arena
(`tests/test_zz_review35.py::test_r35_wedge_right_distance_wrong_bearing_planner_refuses`).
For the symmetric case (dogs exactly at the target distance, bearing off) the
wedge runs up to D≈0.70 m. With the dogs' distance left free it also occurs at
the **page's default 1.0 m** — 160 disagreements in a fine scan of 373 996
combinations (12 of 18 050 in a coarser one) — and at D=5 m (312 of the same
fine scan).

**Input B — "已到位" while the planner would move.** D=1.0 m, dogs 0.90 m apart
at a bearing error of −3°: `converged().ready is True` (radial error exactly
−0.100), `plan()` orders **two moves** (positional error 0.107 > 0.100). The loop
stops with 已到位 about a shape the planner calls 10.7 cm out. This is the
direction 92b704e did not fix: it only made `ready` stricter, in one dimension.

**Test coverage.** The two tests added with the fix (`test_console_formation.py`,
both fail on the pre-fix tree) exercise `converged()` only. Nothing cross-checks
`converged()` against `plan()`, and the loop-level consequence (`no_move`) has no
test.

**The uncommitted rewrite.** `formation.py:419-424` now decides `ready` from
`position_error <= tolerance_m and |yaw_error| <= tolerance_deg`, computed the
same way `plan` computes its target. I swept it independently over the same
52 920 combinations **including dog distances that differ from the target
distance, negative and >180° bearings, heading spreads, and four tolerance
pairs**: 0 disagreements, and the 74 formation/vision tests pass in that tree. It
also removes input B. Two notes on it:

* the sweep test added with it varies the dogs' distance only by setting it
  *equal* to the target distance (`test_converged_and_plan_agree_about_what_done_means`,
  `tests/test_console_formation.py:363-388`), so it cannot see the axis where
  input B lives — the invariant does hold, but not because of that test;
* the literal invariant has one counterexample the sweep cannot reach:
  `converged()` with fewer than two poses returns `ready False` while `plan()`
  returns no moves. Unreachable from the loop (it returns `no_measurement`
  first), worth one clause in the test's docstring at most;
* the old comment at `formation.py:387-390` — "The same tolerance as the heading
  … at the default 1 m separation, 8° is 14 cm of lateral error, which is the
  order of `tolerance_m`" — **survives** the rewrite while the code below it no
  longer checks the bearing at all. 14 cm is also *larger* than `tolerance_m`,
  which is exactly the inconsistency that was just removed; the comment now
  documents a rule that does not exist.

**Severity: MEDIUM.** No unsafe motion (the refusal is a refusal), but the
operator's button can end in a hard failure on a shape the planner itself calls
"已经到位", and the reverse case reports 已到位 up to 10–17 cm out.

---

## F4 — the 64 px floor refuses the project's own default tag on a realistic camera, and the doc's px/m table assumes the arena fills the frame (MEDIUM)

`unitree_robot_agent/vision/detect.py:318-325` (`legible`), `:321`
(`floor = MIN_PX_PER_CELL × CELLS_ACROSS` = 64 px),
`unitree_robot_agent/vision/markers.py:77` (`DEFAULT_SIZE_M = 0.15`),
`unitree_fleet_web/service.py:2271-2285` (the refusal),
`docs/OPEN_QUESTIONS.md:64-78` (B2)

**Input.** The setup the project's own defaults point at: the printed sheet's
default 150 mm tag, a 4 m arena, a 1080p camera. The page's calibration corners
must be *clicked inside the frame*, so the arena rectangle never spans the full
frame width — the simulation the tests use has its corners 10% in from each edge
(`tests/test_console_formation_loop.py:50-56`).

**Measured** (`/tmp/exp8_ppm.py`, rendering the tags and running the real
detector):

```
simulation calibration: corners 160..1440 on a 1600 px frame = 320 px/m
                        (the docstring says 400 px/m, which is frame/4 m)
  0.15 m tag -> 47.8 px   legible False     (DEFAULT_SIZE_M)
  0.20 m tag -> 63.8 px   legible False     (the floor needs > 0.200 m)
  0.30 m tag -> 95.9 px   legible True      (what the tests were moved to)
64 px / 0.15 m = 427 px/m, i.e. 1707 px between the corners, i.e. a ~2133 px
frame with the same margins.
```

So the guard fires on the project's own default tag: `arena_state()["illegible"]`
lists both dogs and `align_formation` refuses with 标签太小. B2's table computes
px/m as `frame_width / 4 m` (1280 → 320, 1920 → 480) and marks 1080p "✅ 刚够";
with the inset convention the page itself forces, 1080p is **384 px/m → 58 px →
below the floor**. The table's third row (print 300 mm) is the only one that
holds, and nothing in the marker sheet's own path defaults to it.

**Test coverage.** The new sub-floor test reaches the regime by shrinking the tag
to 0.06 m; no test covers the default 150 mm tag, and three comments in the new
test file quote the wrong resolution ("400 px/m", "0.06 m … is 24 px" — measured
18.6 px; "a 150 mm tag is 60 px" — measured 47.8 px).

**Severity: MEDIUM.** The refusal is safe and the commit says so in prose, but
the effect is that the formation feature is dead on the documented default rig,
and the number an operator would use to buy a camera (`1080p 刚够`) is optimistic
by the margin that decides it.

---

## F5 — the no-odometry fallback now under-travels by about 2× and the fix's arithmetic misses the profile (MEDIUM-LOW)

`unitree_robot_agent/motion/controller.py:429-432` (`reach`, `seconds`, `ly`),
`:178-182` (`drive` builds a `TrapezoidProfile`)

The fix correctly stopped publishing a full-scale stick, and both branches now
command the same *magnitude*. But the fallback turns a distance into a duration
as if the stick were constant for the whole move, while `drive` publishes
`ly × scale(t)` with a symmetric profile whose mean is `0.5` for any move short
enough to stay in the ramp (`ramp = min(duration/2, peak/accel)`, and
`peak/accel = 1/1.5 = 0.667 s` with the real `motion_accel` default).

**Measured** (`/tmp/exp2_forward.py`, `/tmp/exp2b_forward.py`; real config,
`motion_accel=1.5`, TICK = 1/20 s):

| call | `timed_s` | peak `ly` published | stick-seconds delivered | distance at the nominal 3.0 m/s |
|---|---|---|---|---|
| `forward_m(0.35, speed=0.28)` pre-fix | 1.25 | 0.96 | 0.624 | 1.87 m |
| `forward_m(0.35, speed=0.28)` post-fix | 0.42 | 0.269 | 0.058 | **0.175 m** |
| `forward_m(1.0, speed=0.28)` post-fix | 1.19 | 0.278 | 0.167 | **0.50 m** |
| `forward_m(4.0, speed=1.0)` post-fix | 1.33 | 0.975 | 0.666 | **2.00 m** |

Half the requested distance, every time, for everything up to ~4 m. The direction
is safe and the loop re-measures, but `robot_walk` is what the formation prefers
for forward corrections, the step budget is six, and the tool's caller has no way
to see the shortfall: the response carries `timed_s` and a note, not a delivered
distance.

**Test coverage.** `test_the_timed_fallback_is_not_a_sprint` asserts only an
upper bound (`integral < 0.35`), which a no-op would also pass, and converts
publish counts with `1.0 / 50.0` while `TICK_HZ = 20.0` — the printed
"stick-seconds" are 2.5× off. `test_without_odometry_it_walks_on_a_timer_and_says_so`
asserts `0 < max(commanded) <= speed`; neither test can see an undershoot.

**Severity: MEDIUM-LOW.** Over-driving was the safety bug and it is fixed; this
is the same arithmetic half-done, in the conservative direction, with a test that
cannot report it. A `seconds = |m| / (|speed| × full_stick × mean_scale)` or a
`drive(..., constant=True)` for the timed path would close it.

---

## F6 — the "swapped reason labels" fix is a tautology, and the deadline path is still mislabelled (LOW-MEDIUM)

`unitree_robot_agent/motion/controller.py:519-523` (the label),
`:503-504` (`while … else: aborted = True`), `:462` (the e-stop flag)

```python
pre-fix:  "timeout" if not aborted else "stopped"
post-fix: "stopped" if aborted else "timeout"
```

These are the same expression. Evaluated over both values of `aborted` they agree
on both; run over four exit paths on the pre-fix and post-fix trees the outputs
are **byte-identical**:

```
odometry reached          -> "reached"  (both)
odometry lost mid-walk    -> "timeout"  (both)
deadline expires          -> "stopped"  (both)   <- the word the commit set out to fix
e-stop during the walk    -> "stopped"  (both)
```

The real defect is untouched: the `while … else` clause sets `aborted = True`
when the **deadline** expires, and `aborted` is simultaneously the e-stop flag —
so a timeout still reports "stopped" (the word a person reads as "the operator
stopped me"), and a mid-walk odometry loss reports "timeout". The commit's own
description of the bug ("a deadline reported `stopped`") is still true of the
code it committed. The fix needed a second flag (`timed_out`), not a rewrite of
the conditional.

**Test coverage.** None: no test asserts `"stopped"` or `"timeout"` from
`forward_m`. `test_motion_walk.py:349` accepts `("reached", "timeout")` for a
creeping walk, and whether it passes at all depends on which of the two the code
returns — measured: it returns `"reached"` (0.26 m of 0.3 m), so the label the
test tolerates is not the one under review. If a deadline expiry were exercised
there it would return `"stopped"` and fail.

**Severity: LOW-MEDIUM.** The reported word is the diagnosis an operator gets
from the tool result.

---

## F7 — the new `illegible` outcome and flag are not wired into the page (LOW)

`unitree_fleet_web/service.py:2271-2285` (the refusal, `f182bfe:2257-2271`),
`:1937` (the `arena_state()` flag, `f182bfe:1927`); `unitree_fleet_web/static/console.js:2357-2368`
(`FORMATION_STATES`), `:1967-2010` (`renderArena`)

The server-side guard works and the refusal leaves a sane state (`state:
"illegible"`, `converged: false`, `steps: []`, the task completes normally so a
new run can start). The page, however:

* `FORMATION_STATES` has **no `illegible` entry** (nor `no_move`, which F3 now
  reaches) — `renderFormation` falls back to `[formation.state, '']`, so the
  operator sees the raw English token `illegible · 标签太小…` with no warning
  tone, where every other state has a Chinese label;
* `renderArena` never reads `arena.illegible` (nor the per-pose `legible`), and
  `ARENA_STATES` still says `ok`. So the arena panel draws the uncertain pose as
  a normal dog until the operator presses 对齐 and gets a refusal — the exact
  "the refusal is a mystery" the new test's own docstring warns about.

**Test coverage.** `test_arena_state_surfaces_which_poses_are_uncertain` asserts
the server-side dict only; nothing asserts the page reads it. This is the same
"computed, documented, shipped — and compared to nothing" shape as the `tag_px`
finding the same commit was fixing.

**Severity: LOW** (display), but it costs one line in each table.

---

## F8 — the marker manifest: the mismatch is reported where nobody reads it, the wrong sheet is obeyed, duplicates are undetected (LOW)

`unitree_fleet_web/service.py:1788-1865` (`_marker_manifest`), `:1861` (the note),
`unitree_fleet_web/app.py:406` (the only consumer),
`unitree_fleet_web/static/console.js:2277-2283` (the only page reader)

* **The note is never shown.** `_marker_manifest()` has two call sites: the arena
  loop (`service.py:1779`, which ignores `source`/`note`) and
  `GET /api/arena/calibration` (`app.py:406`). The page fetches that endpoint in
  `startCalibration()` and uses `spec.frame_size` only —
  `grep -rn "markers" static/` finds no reader. So the fallback's "this mapping
  was **guessed from the roster order**" and the wrong-pair warning are invisible
  to the operator; the commit message's "it says which mapping it used, and the
  page shows it" is not true of the code.
* **A sheet for another pair is reported *and obeyed*.** Measured: a manifest
  naming `dog3`/`dog4` against a `dog1`/`dog2` roster returns
  `source="manifest"` with the note, the arena map's poses are keyed
  `dog3`/`dog4`, and `align_formation` then plans against robots that do not
  exist — it records steps and ends in `no_progress`. No motion is possible
  (`call_tool` validates the id at `service.py:371`, and the loop suppresses the
  error), so this is a display/claim mismatch, not a safety one.
* **Duplicates are not detected.** A manifest with two entries for `tag_id 0`
  produces no note at all; `locate`'s `by_tag` is last-wins, so the first robot
  silently gets no pose and the formation stops with `no_measurement`. A manifest
  with two tags for one robot produces a note that says the *other* robot is
  missing from the sheet — never that one dog has two tags. Both are one-line
  checks on `len(set(...))`.

**Test coverage.** The five new tests call `_marker_manifest()` directly and
assert `source`/`note` (all five fail on the pre-fix tree). None covers
duplicates, and none asserts the note reaches the page.

**Severity: LOW.** The mapping fix itself is correct and the fallback is
reachable; the warning it exists to deliver is not.

---

## F9 — `stop_all` with the formation: the behaviour is right, two display nits (LOW/INFO)

`unitree_fleet_web/service.py:2873-2879` (WT) / `:2859-2865` (HEAD)

Verified, not falsified (details in the next section): cancelling a running
formation works, no command follows a STOP ALL, and the state leaves `running`.
Two cosmetic issues found while testing it:

* the stop writes `_formation["reason"] = reason` but leaves
  `_formation["detail"] = "已取消"` (set by `_run_formation`'s `CancelledError`
  handler). `renderFormation` displays `detail`, and `stop_formation` sets
  `detail = reason` — so a STOP ALL reads 已停下 · 已取消 while the formation's
  own stop reads 已停下 · operator. Measured: state `'stopped'`, detail
  `'已取消'`, reason `'r35_b'`.
* `estop()` still does not cancel `_formation_task` (only `stop_all` and
  `aclose` do), so after a latched e-stop the formation reports `running` for up
  to one iteration (`_wait_for_fresh_frame` allows 2.5 s) before returning
  `estopped`. No motion is possible in that window — `call_tool` refuses the
  motion-gated tools while the latch is engaged (`service.py:385`) — so this is a
  status lag, not a hazard, and it is pre-existing.

**Severity: LOW / INFO.**

---

## Tests added with each fix, against the pre-fix code

Run by copying the HEAD test files onto the `6334048` tree
(`/tmp/prefix35`), so "the test would have caught it" is checked rather than
asserted:

| test | pre-fix result |
|---|---|
| `test_the_right_distance_in_the_wrong_direction_is_not_aligned` | FAIL |
| `test_the_bearing_check_wraps_the_short_way_round` | FAIL (no `bearing_error_deg` key) |
| `test_stop_all_stops_a_running_formation` | FAIL |
| `test_without_odometry_it_walks_on_a_timer_and_says_so` | FAIL |
| `test_the_timed_fallback_is_not_a_sprint` | FAIL |
| the 5 `_marker_manifest` tests | FAIL (no `source` key) |
| `test_the_parallax_matches_the_geometry_scale_factor_derives` | FAIL |
| `test_a_big_tag_is_legible_and_a_small_one_is_not` | FAIL |
| `test_legibility_travels_with_the_pose` | FAIL |
| `test_the_residual_is_a_bound_and_not_an_optimistic_estimate` | **passes on both** — it asserts `residual_m > 0.3`, true of the old formula (0.396) and the new (0.460). The commit says as much; the discriminating test is the one above. |

No test added with the fixes is a no-op except that last one, which is a
renamed/kept test rather than a new one.

## Looked wrong, and I could not falsify it

* **`stop_all` cancelling `_formation_task`.** Null-safe: never-started
  (`None`) and already-finished tasks are skipped by
  `is not None and not done()`. Two concurrent STOP ALLs (`asyncio.gather`) both
  return, in 0.31 s, with no further commands and the state out of `running`; a
  new formation can start immediately afterwards. Cancelling a task that has not
  started yet is safe on the event loop. There is **no self-cancel path**: the
  console's `call_tool` routes to the agent server's own fleet tools
  (`robot_stop_all` → `FleetManager.stop_all`/`safe_stop`), not to
  `ConsoleService.stop_all`, whose only callers are the HTTP route and
  `stop_formation` (which nulls the task first). No `asyncio.shield` or
  cancellation-swallowing layer sits between the cancel and the awaits in the
  motion path (the tree's two `asyncio.shield` calls are a video-frame waiter and
  a connect task). The order — cancel, then zero the sticks — is the right one.
* **The e-stop path.** The latch is checked before every step and `call_tool`
  refuses motion-gated tools while engaged; an in-flight step's commands are
  suppressed. Unchanged by the fixes and still correct.
* **`forward_m`'s arithmetic.** Negative distances use `copysign` and keep a
  positive duration; `speed > 1` is clamped (pre-fix, `speed=3.0` published
  `ly=3.0`; post-fix, `ly=1.0` with a matching duration); `nan`/`inf` now raise
  `AgentError` instead of publishing `ly=nan`; the default (`speed=None` →
  `max_walk_speed=0.28`) reaches both branches as the same number. The only
  residual asymmetry — a negative `speed` is honoured in the closed-loop branch
  (`sign * speed`) and ignored in the fallback (`copysign(|speed|, …)`) — is
  unreachable from the tool layer, where `robot_walk` takes `abs(speed)`.
* **The `converged()` bearing wrap.** `wrap_degrees(bearing − target)` handles
  179° vs −179° correctly, and both new tests fail on the pre-fix tree. The
  stricter check does not make the loop oscillate: it either converges in one
  step (measured at 25°, 30° and 45° errors) or returns before commanding
  anything.
* **`_marker_manifest`'s fallback and shape.** No path configured, an unreadable
  path, and an empty `markers` list all fall back to the roster mapping with
  `source="roster"` and a note, and `locate` reads only `markers[].tag_id` /
  `robot_id` (extra keys are ignored), so the returned shape is what the detector
  reads (the page reads none of it — F8). A manifest that reorders the roster is
  obeyed over the roster — the bug the fix was written for.
* **`DogPose.legible` firing on a normal case.** No production caller constructs
  a `DogPose` without `tag_px`: `detect.locate` is the only construction site and
  always fills `span`. The formation's test is `pose.get("legible") is False`, so
  a pose dict lacking the key cannot trigger a refusal (fail-open, which is the
  right direction for this guard). `span` is the *larger* of two adjacent sides,
  so a foreshortened tag is not punished twice. The floor's real problem is F4:
  it fires on a *legitimate but small* tag, not on a normal one.
* **The simulation's doubled calibration points.** The two new image points
  correspond to the same world corners, the renderer and the detector use the
  same homography, and the detector measures exactly what the renderer drew
  (0.30 m → 95.9 px, both before and after the change). No test now passes for a
  reason it did not before, **except** that the resolution is 320 px/m rather
  than the 400 px/m the new comments claim — see F4. (`SimulatedArena.status()`
  still reports `size: [800, 800]` for a 1600 px frame; test-double cosmetics.)
* **The parallax formula itself.** `L·h/(H−h)` equals the old value times
  `scale_factor`, `residual_m == max(parallax_m over the corners)`, and
  `adequate` is false when the bound cannot be computed. The new pinning test
  fails on the old form.
* **`legible` reaching the wire.** It is in `DogPose.as_dict()`, in
  `arena_state()`'s poses, and as the aggregate `illegible` list; the payload
  budget test still passes with it.

## Limits of this review

* Everything here is desktop reproduction: the simulated arena, a mock fleet and
  fake sessions; no robot or camera was involved.
* The inf finding (F1/F2) is an argument about *serialisation*, so it holds
  whichever camera is attached; the px/m finding (F4) depends on the arena
  occupying less than the full frame, which the page's click-the-corners
  workflow forces but a hardware session should confirm with a tape measure.
* `converged()` and `service.py` were being edited while this review ran; the
  line numbers above are for `HEAD` with the working-tree equivalents given
  inline, and the md5s at the top pin what was actually verified. The uncommitted
  rewrites were re-checked against their then-current hashes, not re-reviewed
  end-to-end.
