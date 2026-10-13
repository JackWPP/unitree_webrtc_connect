/* Gamepad → Unitree wireless-controller mapping.
 *
 * Pure logic, no DOM, and dual-target on purpose: the browser loads it as a
 * plain script (`window.UnitreeGamepad`), and the test suite loads the same
 * file with `require()` in node. A controller mapping is exactly the kind of
 * thing that is wrong in a way nobody notices until a dog walks the wrong
 * way, and it is arithmetic — so it gets tested rather than eyeballed.
 *
 * The contracts here come from two independent sources that agree: the
 * decompiled Unitree app and `unitree_ui`'s `gamepad-manager.ts` (whose bit
 * order matches the table already recorded in this repo's
 * `docs/r1_action_list.md`).
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) {
    module.exports = api;
  } else {
    root.UnitreeGamepad = api;
  }
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  /* The robot's `rt/wirelesscontroller` message carries a 16-bit button mask.
   * Bit i is BUTTON_ORDER[i]. Getting this order wrong does not fail loudly —
   * it makes a different button do the thing. */
  const BUTTON_ORDER = [
    'R1', 'L1', 'Start', 'Select', 'R2', 'L2', 'F1', 'F2',
    'A', 'B', 'X', 'Y', 'Up', 'Right', 'Down', 'Left',
  ];

  /* Standard-mapping indices, in the same order as BUTTON_ORDER.
   *
   * `null` means the robot has that button but a standard gamepad cannot
   * produce it: F1/F2 exist on the Unitree BLE remote, not on a pad. The
   * tempting shortcut — point them at the pad's A/B — is wrong, because it
   * would set *both* bits and the robot would act on a button nobody pressed.
   */
  const BUTTON_INDICES = [
    5,    // R1  -> RB
    4,    // L1  -> LB
    9,    // Start
    8,    // Select
    7,    // R2  -> RT
    6,    // L2  -> LT
    null, // F1  -> no standard equivalent
    null, // F2  -> no standard equivalent
    0,    // A
    1,    // B
    2,    // X
    3,    // Y
    12,   // Up
    15,   // Right
    13,   // Down
    14,   // Left
  ];

  const DEFAULT_DEADZONE = 0.08;
  const AXIS_LIMIT = 1.0;

  /* Chrome does not fire `gamepadconnected` for a pad that was already
   * attached when the page loaded, so discovery is by polling. */
  const RESCAN_MS = 500;

  function deadzone(value, zone) {
    const z = zone === undefined ? DEFAULT_DEADZONE : zone;
    const v = Number(value) || 0;
    if (Math.abs(v) < z) return 0;
    /* Rescale so the stick still reaches full deflection: without this the
     * usable range starts at the deadzone edge and the robot never quite
     * reaches full speed. */
    const sign = v < 0 ? -1 : 1;
    return sign * Math.min(1, (Math.abs(v) - z) / (1 - z));
  }

  /* `rt/wirelesscontroller` axes. The left stick drives translation and the
   * right stick yaw; the pad's Y axes point the other way from the robot's,
   * which is what the negations are for. */
  function axes(gamepad, options) {
    const opts = options || {};
    const zone = opts.deadzone === undefined ? DEFAULT_DEADZONE : opts.deadzone;
    const a = (gamepad && gamepad.axes) || [];
    return {
      lx: deadzone(a[0], zone),
      ly: -deadzone(a[1], zone),
      rx: deadzone(a[2], zone),
      ry: -deadzone(a[3], zone),
    };
  }

  function keys(gamepad) {
    if (!gamepad || !gamepad.buttons) return 0;
    let mask = 0;
    for (let bit = 0; bit < BUTTON_ORDER.length; bit += 1) {
      const index = BUTTON_INDICES[bit];
      if (index === null) continue;  // the pad has no such button
      const button = gamepad.buttons[index];
      /* `pressed` misses analogue triggers on some pads, which report only a
       * `value`; treat either as pressed. */
      if (button && (button.pressed || button.value > 0.5)) {
        mask |= 1 << bit;
      }
    }
    return mask;
  }

  function isIdle(frame) {
    return frame.lx === 0 && frame.ly === 0 && frame.rx === 0 && frame.ry === 0
      && frame.keys === 0;
  }

  function frameFor(gamepad, options) {
    const a = axes(gamepad, options);
    return { lx: a.lx, ly: a.ly, rx: a.rx, ry: a.ry, keys: keys(gamepad) };
  }

  /* A pad that is not in `standard` mapping reports its buttons in an
   * arbitrary order, so the mask above is meaningless for it. Better to say
   * so than to drive the robot with a guess. */
  function mappingWarning(gamepad) {
    if (gamepad && gamepad.mapping !== 'standard') {
      return '手柄不是 standard 映射，按键位可能不对（仅摇杆可用）';
    }
    return '';
  }

  function connectedPads(pads) {
    const out = [];
    const list = pads || [];
    for (let i = 0; i < list.length; i += 1) {
      if (list[i]) out.push(i);
    }
    return out;
  }

  return {
    BUTTON_ORDER: BUTTON_ORDER,
    BUTTON_INDICES: BUTTON_INDICES,
    DEFAULT_DEADZONE: DEFAULT_DEADZONE,
    AXIS_LIMIT: AXIS_LIMIT,
    RESCAN_MS: RESCAN_MS,
    axes: axes,
    keys: keys,
    frameFor: frameFor,
    isIdle: isIdle,
    deadzone: deadzone,
    mappingWarning: mappingWarning,
    connectedPads: connectedPads,
  };
});
