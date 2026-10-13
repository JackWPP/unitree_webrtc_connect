# Wiring audit — `unitree_fleet_web`

**Scope.** Read-only audit of the seams between parts of the console that are each
individually tested but not necessarily connected to each other: service method →
route, payload → UI, UI control → route, config field → consumer, tool name →
caller, and declared-but-unused code.

**Revision audited.** `service.py` md5 `9d6c8225185af01580ffb41db85d2e67`,
`static/console.js` `af5c9c74e8fe0e617024156aa06b7133`,
`static/index.html` `8efb862f5a51c6c1a666d5b72972398c`,
`config.py` `61a5dadbc71702d3a7174ea795f4ce83`. All line numbers below refer to
that revision. A concurrent editor was touching `service.py` and `console.js`
while this audit started; the working tree was frozen to a snapshot at
04:37:59 and re-hashed afterwards (unchanged), so the citations are valid for
the tree as it stands. If `service.py` is edited further, line numbers in the
2400s will drift.

**How candidates were verified.** Reference counting was done with exact
attribute-access regexes (`\.name\b`) plus, for the service layer, a call-graph
reachability pass: parse `ConsoleService`, build `self.X` edges between methods,
seed the walk with the `service.X` references in `app.py` only, and mark
everything the walk misses. That is what makes the difference between "never
mentioned" and "mentioned only from a method nothing calls".

---

## Confirmed disconnected

### 1. The fault list is rendered into a container that is never revealed

- `unitree_fleet_web/static/index.html:311` — `<div class="faults hidden" data-role="faults-wrap">`
- `unitree_fleet_web/static/console.css:48` — `.hidden { display: none !important; }`
- `unitree_fleet_web/static/console.js:231-284` — `renderFaults()` writes the list into
  `[data-role="faults"]` (`console.js:136`), i.e. *inside* that hidden wrapper.

**Verification.** `grep -rn "faults-wrap"` over the whole repo returns exactly one
hit: the `index.html:311` declaration. Zero readers. The only thing `renderFaults`
toggles is the head badge (`console.js:279-283`), and `renderControls` moves only
`entry.sources` into the 感知 panel (`console.js:1045-1049`) — the fault list is
never moved and never un-hidden. `console.css` has no `.pen.has-faults .faults`
rule to override `.hidden` (`has-faults` appears once, at `console.js:274`, and is
not styled).

**Consequence.** The badge click switches to the `sense` tab (`console.js:283`),
which is where the list is *documented* to live (`index.html:308-310`: "the list
lives under 感知") — but the list is not in that panel and is not visible
anywhere. The operator sees a count and can never see which source, which motor
or which code.

**To connect.** Either drop `hidden` from `index.html:311` so the list always
renders in the pen as the comment at `index.html:308-310` implies, or move the
wrapper into the sense panel the way `entry.sources` is moved at
`console.js:1045-1049` and unhide it there.

### 2. `#watching` can never show anything: the UI reads it from the wrong payload

- `unitree_fleet_web/static/console.js:537-538` — reads `payload.watching` inside
  `renderStatus()`.
- `unitree_fleet_web/static/index.html:197` — `<span id="watching" class="muted">`.

**Verification.** `renderStatus` is only ever called with the **status** payload:
`/api/fleet/status` at `console.js:1650` and `2496`, and the WebSocket message at
`console.js:1670` (built at `app.py:559-562` as `{"type": "status", **await service.status(live=False)}`).
`ConsoleService.status()` (`service.py:2403-2433`) does **not** emit `watching`.
It is emitted only by `snapshot()` (`service.py:2398`), served by `/api/fleet`,
whose only consumer reads `snapshot.fleet` and `snapshot.state` and nothing else
(`console.js:2809-2814`).

So `payload.watching` is `undefined` on every path that reaches it, and the span
stays empty for the life of the page.

**The test that should have caught this says the opposite.** `tests/test_console_app.py:452-457`:

```python
def test_the_snapshot_tells_the_ui_what_it_may_offer(open_client):
    """`tools` and `watching` drive the UI, so they are part of the contract."""
    payload = open_client.get("/api/fleet").json()
    assert payload["watching"] == 0
```

The field exists on the endpoint the test checks; the UI reads it from the other
endpoint; the test passes. This is the clearest case in the tree of a test
asserting a contract it does not actually verify.

**To connect.** Add `"watching": self._clients` to the `status()` dict
(`service.py:2403-2433`) — it is one line and the field is already tracked — or
have `boot()` stash `snapshot.watching` into `state`.

### 3. `snapshot().limits` — five config values exported to the UI, zero readers

- `unitree_fleet_web/service.py:2391-2397` — `limits: {deadman_s, stream_fps, axis_limit, lease_s, unwatched_release_s}`
- Consumed by `/api/fleet` (`app.py:169`).

**Verification.** `grep -n "limits" unitree_fleet_web/static/console.js` → no hits
at all. The boot consumer (`console.js:2809-2814`) reads only `fleet` and `state`.
Independently, the front end hardcodes the same constant it is being sent:
`unitree_fleet_web/static/gamepad.js:59` `const AXIS_LIMIT = 1.0;` (exported at
`gamepad.js:140`), duplicating `AXIS_LIMIT` at `service.py:55` which is what
`service.py:2394` ships.

Two sources of truth for the axis clamp, deliberately unlinked. The server is
authoritative (`_clamp`, `service.py:214`), so this is latent drift rather than a
present safety hole — but if the exported limit and the hardcoded one ever
disagree, the pad will draw a range the server will not honour.

Same pattern, lower stakes: `snapshot()["tools"]` (`service.py:2400`) is unread —
the page fetches `/api/console/tools` separately at `console.js:2819`.

**To connect.** Either consume `snapshot.limits` in `boot()` and delete the
hardcoded `AXIS_LIMIT` in `gamepad.js`, or stop shipping the block.

### 4. `per_source` mode tells the operator to use a `bind` control that does not exist

- `unitree_fleet_web/static/console.js:341` — `per_source: [[], '按输入设备绑定（下方 bind）']`
- `unitree_fleet_web/app.py:599` — `Route("/api/fleet/bind", post_bind, methods=["POST"])`
- `unitree_fleet_web/service.py:2820` — `bind_source()`

**Verification.** `grep -rn "bind" unitree_fleet_web/static/` returns exactly three
kinds of hit: the `<option value="per_source">` label at `index.html:188`, the
`bindJoystick` identifier, and the legend string above. There is no bind control
in `index.html`, and `console.js` never posts to `/api/fleet/bind`. `bind_source`
therefore has exactly one caller in the tree: the route at `app.py:221`.

**Why this is worse than a cosmetic gap.** In `PER_SOURCE` mode an unbound source
resolves to *nothing*: `state.py:263-265` returns `[bound] if bound in ids else []`.
So selecting Per-source produces a console where every key press is silently
accepted by the UI and drives no robot, while the on-screen hint points at a
control that is not on the page. (Fail-safe direction, so not a runaway risk —
but a "the console is broken" dead end.)

**To connect.** Either add a bind control (source → robot `<select>` per pen)
that posts to `/api/fleet/bind`, or change the legend to say that Per-source
needs an API call, or hide the mode.

### 5. `service.py:89 DEFAULT_MOTION_HZ` is dead, and `config.py` points readers at the dead copy

- `unitree_fleet_web/service.py:89` — `DEFAULT_MOTION_HZ = 50.0`
- `unitree_fleet_web/config.py:51` — `#: Velocity re-send rate while driving. See DEFAULT_MOTION_HZ in service.py.`
- `unitree_fleet_web/config.py:57` — `DEFAULT_MOTION_HZ = 50.0` (the live one, used at `config.py:107`)

**Verification.** `grep -rn "DEFAULT_MOTION_HZ"` over `unitree_fleet_web/ tests/ scripts/ docs/`
returns four hits: the two definitions, the comment, and `config.py:107`
(`motion_hz: float = DEFAULT_MOTION_HZ`). The service copy has **zero readers**.
The real consumers are `ConsoleConfig.motion_hz` at `service.py:415` and `1116`.

Trap: the comment tells a reader to go look at `service.py`, where editing the
value would change nothing. Relatedly, that comment block was left mangled — the
motion comment at `config.py:51` sits directly above the *arena* comment
(`config.py:52-54`) and `DEFAULT_ARENA_HZ` (`config.py:55`), leaving
`DEFAULT_MOTION_HZ` (`config.py:57`) with no doc comment of its own.

**To connect.** Delete `service.py:89` and fix the comment at `config.py:51`.

### 6. `formation.py:69 DEFAULT_MAX_STEPS` is dead; the budget is a duplicate literal

- `unitree_fleet_web/formation.py:69` — `DEFAULT_MAX_STEPS = 6` (with a four-line comment explaining what the budget is worth)
- `unitree_fleet_web/service.py:2054` — `max_steps: int = 6` (the value actually used, at `service.py:2089`)

**Verification.** `grep -rn "DEFAULT_MAX_STEPS"` over the whole repo returns one
hit: the definition. `align_formation` hardcodes `6` as its own default. The two
agree today, which is exactly why this is invisible; changing the documented
constant would change nothing.

**To connect.** `from .formation import DEFAULT_MAX_STEPS` and use it as the
default at `service.py:2054`.

### 7. `controls.py:665 _CATALOG` is dead — and is the wrong catalogue

- `unitree_fleet_web/controls.py:665` — `_CATALOG: Tuple[Control, ...] = _COMMON_CONTROLS + _GO2_CONTROLS + _R1_CONTROLS`
- `unitree_fleet_web/controls.py:711` — `catalog = _COMMON_CONTROLS + (_GO2_CONTROLS if family == "Go2" else _R1_CONTROLS)`

**Verification.** `grep -rn "_CATALOG"` over the whole repo returns one hit: the
definition. `controls_for` builds its own family-specific catalogue inline.

This is the most hazardous of the dead declarations, because it is not merely
unused — it is a union of *both* families. Any future reader who reaches for
`_CATALOG` ("the catalogue") instead of line 711 would offer R1 arm gestures on a
Go2. The `available_tools` filter would mask most of it, which is what makes it
survive review.

**To connect.** Delete it. There is nothing to wire it to; the family split at
line 711 is the intended behaviour.

### 8. `/api/fleet/routine/insert` has no UI, but the UI displays the queue it fills

- `unitree_fleet_web/app.py:613` — the only route to `queue_insert`
- `unitree_fleet_web/service.py:829` — `queue_insert()`
- `unitree_fleet_web/static/console.js:2612-2615` — renders `routine.pending` as
  `已排队 N 个动作，将在当前步骤结束后执行`

**Verification.** `queue_insert` has exactly one non-test caller (`app.py:462`);
`console.js` never posts to `/api/fleet/routine/insert`. `queue_insert` is the
only writer of `pending`. So the "queued actions" line the page is built to
display can only ever light up for an API client, never from the page.

**To connect.** If the queue is meant to be an operator feature, add an
"insert after this step" affordance in the routine timeline
(`console.js:2623`) that posts to the route. Otherwise the display at
`console.js:2612-2615` is advertising a capability the UI does not have.

### 9. `#mode-hint` is never written

- `unitree_fleet_web/static/index.html:190` — `<span id="mode-hint" class="muted"></span>`

**Verification.** Zero references in `console.js`, `gamepad.js` or `console.css`.
`setMode()` (`console.js:643-650`) updates `state.mode` and calls
`updateKeysHint()`, which writes to each pen's `[data-role="keys"]` — never to
`#mode-hint`. The span is permanently empty.

**To connect.** Delete it, or have `setMode`/`updateKeysHint` put the mode's
one-line explanation there.

### 10. Routes that can move or stop a robot with no UI caller and no behavioural test

Not "service methods with no caller" — these have a route — but they are UI-dead,
and two of them are also body-untested:

| Route | Route line | UI caller | Test coverage |
|---|---|---|---|
| `/api/robot/{id}/move` | `app.py:625` | none | only a 401 auth assertion, `tests/test_console_app.py:85` |
| `/api/robot/{id}/status` | `app.py:603` | none | only a 401 auth assertion, `tests/test_console_app.py:86` |
| `/api/robot/{id}/estop` | `app.py:627` | none | **none** |
| `/api/robot/{id}/estop/release` | `app.py:628-632` | none | **none** |
| `/api/robot/{id}/teleop` | `app.py:624` | none | yes (`test_console_app.py:186,196,226`) |

**Verification.** `ConsoleService.move` (`service.py:2594-2623`) has exactly one
caller in the tree, `app.py:329`. `console.js` never names `/move`; the page's
bounded moves go through the generic tool route with `control.tool == "robot_move"`
(from `controls.py`). Since the only test touching the route asserts 401, the
handler body — including `require_motion_allowed` and `runtime.motion.drive` — has
never executed in the suite.

The per-robot e-stop routes are worse in coverage terms: `grep -rn "dog1/estop" tests/`
returns nothing, and the UI's per-dog 急停 button posts to the *fleet* route
(`console.js:622-633`).

**Assessment.** `/teleop` is deliberate (documented at `console.js:10`: telemetry
goes to `/api/fleet/teleop` with a source name, not per-robot). The e-stop and
status duplicates look like deliberate API surface. `/move` looks like the odd
one out — a second, parallel motion path that the UI replaced with `robot_move`
through the controls panel, and that nothing exercises.

**To connect.** Decide per route: keep as documented API-only (and give the
motion ones a behavioural test, since an untested motion endpoint is the same
shape as the bug that prompted this audit), or delete `/move`.

### 11. Two smaller wiring defects

- **Dead defensive branch.** `unitree_fleet_web/static/console.js:2433-2436` re-parents
  `#form-steps` into `#formation` only `if (host && !host.parentElement)`. But
  `#form-steps` is already a child of `#formation` in the template
  (`index.html:82`), so the condition is false at every call and the block never
  runs. Harmless, but it is a guard that has never been exercised.
- **Listener accumulation.** `runFleetAction` registers a *fresh* `click`
  listener on `#routine-stop` every time it is called
  (`console.js:2729`). N fleet actions → N handlers → N `stopRoutine()` calls
  (each an API POST plus a toast) from one click. Not a disconnection, but the
  same family of "the wire is attached more than once" defect.

---

## Suspicious but deliberate

- **`CONSOLE_TOOLS` entries nothing sends.** `robot_status`, `robot_errors`,
  `robot_fleet_status`, `robot_roster`, `robot_stop_all` have **zero** references
  anywhere in `unitree_fleet_web/` outside the `CONSOLE_TOOLS` literal
  (`service.py:148-193`). They are still reachable through the generic
  `/api/robot/{id}/tool` route, so they read as deliberate API-only allowances:
  - `robot_fleet_status`, `robot_roster` — fleet-level reads the console replaces
    with its own `/api/fleet`; no UI need. Deliberate.
  - `robot_status` — replaced by `fleet.status_all()` behind `/api/fleet/status`.
    Deliberate, though it is the one tool in the set that a test actually exercises
    through the tool route (`tests/test_console_app.py:553`).
  - `robot_errors` — the outlier. Faults are the one diagnostic the console *does*
    surface, and this read is never invoked. The UI does not depend on it (faults
    arrive passively via `runtime.faults.active()`, `unitree_robot_agent/fleet/manager.py:257`),
    so this is an orphaned allowance rather than a missing wire — but it is worth a
    decision, especially given finding 1.
  - `robot_stop_all` — orphan; `service.stop_all()` implements stop-all itself via
    `self.fleet.stop_all(...)` (`service.py:2666`), not via the tool.
- **`/api/fleet/routines` returns `policies` that nothing reads.**
  `app.py:376` ships `"policies": list(POLICIES)`; `console.js` never reads
  `spec.policies` and instead hardcodes `EDITOR_POLICIES` (`console.js:1695`)
  and `POLICY_LABELS` (`console.js:2593`) — the same three values, duplicated.
  Deliberate-looking (the editor needs labels, not just ids), but it is a
  client-side copy of a server constant, so a change to `POLICIES`
  (`routines.py:47`) would silently not reach the editor's dropdown.
- **Status-payload fields shipped and not rendered.** These are informational
  rather than broken, but each is computed per robot on the 4 Hz path:
  `status.posture` (the full `PostureBelief` — `service.py:2422`) is **entirely
  unread** by the UI: the only `posture.*` reads in `console.js`
  (`console.js:496-504`) are on `state.body[id].fsm`, a different structure
  (`body()` at `service.py:1488-1492`). Worth noting because `body()`'s own
  comment goes out of its way to explain the naming split so that the two are not
  confused — and then nothing consumes the `posture` half.
  Partially read producers: `video_status.last_error`/`failures`/`enabled`
  (`service.py:2916-2920` — the operator sees 无画面 but never the reason),
  `telemetry_health.has_data`/`refresh_hz`, `where.frame`, `arena.camera`,
  `motion.snapshot.{accel,decel,limits,turn_coupling,watchdog_s,yaw_accel}`,
  `state.{driving_by_source,driving_for_s,engaged_at,last_teleop_age_s,source_bindings,video_enabled}`.
- **`gamepad.js` axis constant duplication** — covered in finding 3; listed here
  as well because `AXIS_LIMIT` living in both the payload and the JS looks like a
  deliberate "the client should not trust the server for a constant this small"
  decision, and it is defensible if nobody edits one of them.

---

## Test-only

No `ConsoleService` method is reachable *only* from tests: the reachability walk
seeded from `app.py` reaches all 92 methods (every public one either directly or
through a route-reachable chain such as `status()` → `flags`/`where`/`body`/`sources`/
`telemetry_health`/`video_status`/`posture`/`motion`/`routine_state`/`formation_state`,
or `start()` → the background loops). I could not find a method that satisfies the
"written, unit-tested, never wired" description. What does exist is narrower:

- **`queue_insert` (`service.py:829`)** — route-reachable (`app.py:462`) but
  UI-unreachable, and its three tests (`tests/test_console_routines.py:214,227,238`)
  are the only thing that ever calls it. The route is exercised once by
  `tests/test_console_app.py:1046`. A feature whose entire reality is a test file
  and an unused route. (See finding 8.)
- **`bind_source` (`service.py:2820`)** — one route, one test
  (`tests/test_console_app.py:213`), no UI. (See finding 4.)
- **`move` (`service.py:2594`)** — one route, and the only test that touches the
  route stops at the auth middleware (`tests/test_console_app.py:85`). So the
  method body is covered by nothing at all: not a unit test, not a route test,
  not the UI.
- **White-box tests reaching internals directly** (recorded so their green ticks
  are not read as wiring evidence): `_correct_turn` (`tests/test_console_sync.py:390,412,425,439,449`),
  `_publish_commanded` (`tests/test_console_service.py:997`), `_note_frame`
  (`tests/test_console_service.py:816,828`), `_release_unwatched`
  (`tests/test_console_soak.py:231,252`), `_arena_loop`
  (`tests/test_vision_arena.py:231,340`). All of these *do* have real system
  callers, so they are not broken — but each is tested at a seam that bypasses the
  route, which is how a route-level regression would slip through.
- **`telemetry_health`, `frame`, `video_status`, `align_formation`, `start_arena`**
  are called both by system code and directly by tests
  (`tests/test_console_robustness.py:436+`, `tests/test_console_service.py:409+`,
  `tests/test_console_formation_loop.py:252+`, `tests/test_vision_arena.py:267+`).
  Wired, and additionally covered. Not test-only.

---

## Verified connected

Checked and found sound, so that the absence of a finding above is not read as
absence of looking:

- **Service method → route, the exact question asked.** The reachability walk
  seeded from `app.py` alone reaches every one of the 92 methods. The public
  methods with no direct `app.py` reference (`flags`, `where`, `body`, `sources`,
  `source_status`, `source_catalogue`, `telemetry_health`, `video_status`,
  `posture`, `motion`, `routine_state`, `formation_state`, `custom_routines`,
  `frame`, `note_watch`, `start_arena`, `align_formation`) are all reached through
  `status()`, `controls_payload()`, `snapshot()`, `all_routines()` or `start()`,
  and each internal callee was traced back to a route. No dead public method.
- **`robot_walk` preference (the cited bug class).** `_apply_formation_step`
  (`service.py:2159-2195`) prefers `robot_walk` for mostly-forward corrections and
  falls back to `robot_move`/`robot_turn`. The test arena now routes all three:
  `tests/test_console_formation_loop.py:211-218` handles `robot_move`, `robot_walk`
  and `robot_turn`, with a regression test at line 202 and a dedicated
  `robot_walk` model at line 143. This one is wired at both ends.
- **`ConsoleConfig` fields → consumers.** All 17 fields have at least one real
  reader outside `config.py`: `host`/`port`/`token` (`cli.py`, `app.py`),
  `deadman_s` (`service.py:308,2368,2373`), `status_hz` (`app.py:555`),
  `stream_fps` (`service.py:2926`), `lease_s`, `telemetry_hz`, `motion_hz`
  (`service.py:415,1116`), `stand_timeout_s` (`service.py:1184`), `arena_camera`,
  `arena_calibration`, `arena_hz` (`service.py:1711`), `unwatched_release_s`,
  `serve_ui` (`app.py:574`), `video_autoconnect` (`service.py:2849`),
  `static_dir` (`app.py:576,638`). The one dead item in this area is the *duplicate
  constant* in finding 5, not a config field.
- **`data-role` / `id` cross-check.** Every `getElementById` and
  `data-role="..."` query in `console.js` resolves to an id or role that exists in
  `index.html` (or is created by the script, e.g. the `toast` element). No
  listener is bound to a nonexistent element. The reverse direction surfaced only
  `#mode-hint`, `#calib-note`, `#fleet-sync`, `#calib`, `data-role="battery-wrap"`
  and `data-role="faults-wrap"` as never-queried markup; of these only
  `faults-wrap` (finding 1) is functionally broken — `#calib-note` carries static
  help text that renders fine, and `#fleet-sync`, `#calib`, `battery-wrap` are
  layout wrappers whose children *are* used.
- **Static controls → listeners.** Every button and input in `index.html` outside
  the pen template has a handler or is read at submit time: `#btn-stop-all`,
  `#btn-estop`, `#btn-estop-release`, `#input-mode` (`console.js:1623-1630`),
  `#trim` (`console.js:2786`), `#fleet-repeat` (`console.js:2789`),
  `#form-start`/`#form-stop` (`console.js:2427-2432`), the four formation inputs
  (read in `startFormation`, `console.js:2398-2408`), `#calib-start`/`-cancel`/`-save`/
  `-overlay`/`-width`/`-depth` (`console.js:2327-2340`), the two calib height
  inputs (read in `saveCalibration`, `console.js:2312-2313`), `#sync-mark`/`#sync-clear`
  (`console.js:2520-2536`), the editor's clear/save/run/name/loop
  (`console.js:1907+`), and `#routine-stop` (`console.js:2581`, modulo finding 11).
- **`unitree_robot_agent/`** — no module-level function, class or constant is left
  without a reference outside its own file. No dead public API found there.
- **Dataclass fields.** Every field on `Reading`, `Pair`, `ConsoleConfig`,
  `Formation`, `DogMove`, `Plan`, `EStop` and `ConsoleState` is referenced somewhere
  in system code. No unused field.
- **Function-level sweep.** Across `unitree_fleet_web/` and `unitree_robot_agent/`,
  every module-level and class-level function has at least one non-test reference.
  The only declarations with zero system references in the whole package are the
  four reported above: `DEFAULT_MAX_STEPS`, `_CATALOG`, and (counting readers
  rather than textual mentions) `service.DEFAULT_MOTION_HZ`.
- **`gamepad.js` is genuinely loaded and used** — `index.html:324` loads it before
  `console.js`, and `console.js:1521,1535` call `window.UnitreeGamepad`, with
  `tests/test_console_app.py:534-547` asserting both halves stay attached.
- **`status()` internals** — `note_watch()` (`service.py:2406`) does fire from the
  status path, so the lease/telemetry keying off "a browser is watching" works
  even though the *display* of that count does not (finding 2). These are separate
  wires and only the second is broken.
- **`fleet_action` / `fleet_actions` / `queue_insert` / `routine_*` payloads** —
  the fleet action bar (`console.js:2688-2775`), routine buttons
  (`console.js:2541-2585`) and routine timeline (`console.js:2595-2623`) all read
  fields the corresponding producers emit; `routine.pending`, `routine.steps`,
  `step.state`, `needs_dangerous`, `seconds` and `label` all line up.

---

## Limitations of this audit

- Static analysis only, per the brief's "read-only" constraint: no browser was
  driven and no flow was executed end to end. Everything above is a claim about
  references and payload keys, verified by grep and by the call-graph walk.
- The reachability walk follows `self.X` and `service.X` edges only. A method
  reached exclusively through `getattr(service, name)`, a dispatch table, or a
  string-keyed registry would be misreported as unreachable; I found no such
  indirection in `ConsoleService` (the tool path goes through `CONSOLE_TOOLS` by
  name, which is an allow-list, not a dispatcher).
- "Never read by `console.js`" is a statement about the current `console.js`. Tests
  that fetch the HTML and JS and assert element existence
  (`tests/test_console_app.py:494-514`) cannot see any of findings 1–4, because
  every element involved *does* exist — it is the data that never arrives.
