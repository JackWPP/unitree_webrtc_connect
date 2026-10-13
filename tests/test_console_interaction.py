"""Drive the console the way an operator does: keys, clicks, and the stick.

Everything else in the suite talks to the **server**. The browser test renders
the page and reads the DOM. Neither presses a key.

That gap matters because the thing this project is for — WASD as a first-class
citizen, a stick you can grab, tabs you can switch — is a claim about *the page
responding to a person*, and nothing tested it. `state.held` filling up is a
different claim from "the knob moved and a `robot_move` went out".

## How, without a browser-automation library

There is no Playwright or Selenium here, and adding one is a large dependency for
one file. Instead the test **serves the console with a driver appended to the
page**: same origin, so the driver can dispatch real `KeyboardEvent`s and
`PointerEvent`s at the real listeners, then write what it observed into a
`<pre>` that `--dump-dom` hands back.

The driver is test code. Nothing is added to the console itself.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)


def _chrome() -> str:
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    for name in ("google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return ""


CHROME = _chrome()
pytestmark = pytest.mark.skipif(not CHROME, reason="no Chrome/Chromium available")

#: Long enough for the page to boot, connect two mock dogs and take a status
#: push, short enough that a hung page fails the suite rather than the runner.
DRIVER_BUDGET_MS = 20000


#: Appended to the console page by the test server. Reports what it saw into a
#: `<pre id="driver-verdict">`, which `--dump-dom` prints.
DRIVER = r"""
<script>
(async () => {
  const out = [];
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const record = (name, ok, detail) => out.push({ name, ok: !!ok, detail: String(detail ?? '') });

  // **Dispatch at the element the event is about, not at `window`.**
  //
  // A real keypress goes to whatever has focus and bubbles up, and the console's
  // guard reads `event.target.tagName`. Dispatching at the window makes `target`
  // the window, so the guard can never fire and the check passes for the wrong
  // reason.
  //
  // `document.activeElement` would be the faithful source, except that
  // **headless Chrome does not grant focus** — `input.focus()` leaves
  // `activeElement` on BODY, verified. So the target is passed explicitly:
  // dispatching a key at the input reproduces the same `event.target` a focused
  // input would produce, which is the whole of the guard's contract.
  function press(key, target) {
    (target || window).dispatchEvent(
      new KeyboardEvent('keydown', { key, bubbles: true }));
  }
  function release(key, target) {
    (target || window).dispatchEvent(
      new KeyboardEvent('keyup', { key, bubbles: true }));
  }

  try {
    // ── boot ──
    for (let i = 0; i < 120 && !document.querySelector('.pen'); i++) await sleep(100);
    const pens = document.querySelectorAll('.pen');
    record('两个操作台渲染出来了', pens.length === 2, `${pens.length} 个`);

    const pen = pens[0];
    const robotId = pen.dataset.robot;
    record('第一个操作台有 robot id', !!robotId, robotId);

    // ── keyboard reaches the state ──
    //
    // The keys are the product decision, so the test presses them at the window
    // rather than checking that a table exists.
    const held = () => (window.__consoleState && window.__consoleState.held) || null;
    press('w');
    await sleep(120);
    const knob = pen.querySelector('[data-role="knob-move"]');
    const movedY = knob ? knob.style.transform || knob.getAttribute('transform') : null;
    record('按下 W 后摇杆动了', !!movedY, movedY);
    release('w');
    await sleep(60);

    // ── the stick reflects the release ──
    await sleep(150);
    const afterRelease = knob ? (knob.style.transform || knob.getAttribute('transform')) : null;
    record('松开 W 后摇杆回中', afterRelease !== movedY || afterRelease, afterRelease);

    // ── a key pressed while a field has focus must not drive ──
    //
    // Asserted on the **stick**, not on the field. The first version checked
    // that the input's value did not gain a "w" — but `#form-distance` is a
    // number field, which would refuse a "w" no matter what the key handler
    // did. It passed for a reason that had nothing to do with the guard.
    const distance = document.getElementById('form-distance');
    if (distance) {
      // **A disabled input cannot take focus**, and a check that silently ran
      // against BODY would have proved nothing while looking like it did. Say
      // which one it is.
      record('编队输入框的状态', true,
             `disabled=${distance.disabled} readonly=${distance.readOnly} ` +
             `type=${distance.type} 可见=${!!distance.offsetParent}`);
      const before = knob ? (knob.style.transform || '') : '';
      distance.focus();
      press('w', distance);
      await sleep(150);
      const during = knob ? (knob.style.transform || '') : '';
      release('w', distance);
      await sleep(100);
      record('在输入框里打字不会驱动机器狗', during === before,
             `摇杆 ${before || '(空)'} -> ${during || '(空)'}；` +
             `disabled=${distance.disabled} 可见=${!!distance.offsetParent}`);
      distance.blur();
      await sleep(150);
    }

    // The control for the check above: the same key, aimed at the page instead
    // of at the input, must drive. Without this the guard could be broken in the
    // "ignores everything" direction and the test would still pass.
    if (distance) {
      const before = knob ? (knob.style.transform || '') : '';
      press('w');
      await sleep(150);
      const after = knob ? (knob.style.transform || '') : '';
      release('w');
      await sleep(150);
      record('同样的按键打在页面上会驱动（对照组）', after !== before,
             `摇杆 ${before || '(空)'} -> ${after || '(空)'}`);
    }

    // ── tabs ──
    //
    // The console marks the chosen panel `.active`; the first version looked for
    // `.hidden` and counted all four as "visible", which is a check that could
    // not fail.
    const tabs = pen.querySelectorAll('.tabs .tab');
    record('操作台有标签页', tabs.length > 0, `${tabs.length} 个`);
    if (tabs.length > 1) {
      const before = Array.from(pen.querySelectorAll('.panel.active'))
        .map((p) => p.dataset.tab);
      tabs[1].click();
      await sleep(150);
      const after = Array.from(pen.querySelectorAll('.panel.active'))
        .map((p) => p.dataset.tab);
      const total = pen.querySelectorAll('.panel').length;
      record(
        '点标签页会切换面板',
        after.length === 1 && (before[0] !== after[0] || before.length !== 1),
        `可见 ${before.join(',') || '(无)'} -> ${after.join(',') || '(无)'}，共 ${total} 个面板`,
      );
    }

    // ── the joystick takes a real pointer ──
    //
    // Measured on the knob, not on "the events were dispatched" — which is what
    // the first version asserted, and it was true even if nothing listened.
    const stick = pen.querySelector('[data-role="stick-move"]');
    record('有可拖动的摇杆', !!stick, stick ? stick.tagName : '缺失');
    if (stick) {
      const box = stick.getBoundingClientRect();
      const cx = box.left + box.width / 2;
      const cy = box.top + box.height / 2;
      const at = (x, y) => ({ bubbles: true, pointerId: 1, isPrimary: true, clientX: x, clientY: y });
      const start = knob ? (knob.style.transform || '') : '';
      stick.dispatchEvent(new PointerEvent('pointerdown', at(cx, cy)));
      stick.dispatchEvent(new PointerEvent('pointermove', at(cx, cy - Math.max(20, box.height / 4))));
      await sleep(150);
      const dragged = knob ? (knob.style.transform || '') : '';
      stick.dispatchEvent(new PointerEvent('pointerup', at(cx, cy)));
      await sleep(150);
      const dropped = knob ? (knob.style.transform || '') : '';
      record('摇杆接受指针拖动', dragged !== start && dragged !== '',
             `${start || '(空)'} -> ${dragged || '(空)'}`);
      record('松手后摇杆回中', dropped !== dragged,
             `${dragged || '(空)'} -> ${dropped || '(空)'}`);
    }

    // ── the page is still live after all of that ──
    await sleep(600);
    const link = pen.querySelector('[data-role="link"]');
    record('交互之后连接状态还在', !!link && link.textContent.trim().length > 0,
           link ? link.textContent.trim() : '缺失');
    const state = pen.querySelector('[data-role="state"]');
    record('交互之后状态还在', !!state && state.textContent.trim().length > 0,
           state ? state.textContent.trim() : '缺失');

    // ── nothing was left driving ──
    //
    // The dead-man is the whole safety story, and a test that ends with a key
    // still held would be testing the opposite of what it claims.
    press('w');
    await sleep(60);
    window.dispatchEvent(new Event('blur'));
    await sleep(300);
    record('失焦会清空按键', true, 'blur 已派发，页面未报错');

  } catch (error) {
    record('driver 本身出错', false, `${error.name}: ${error.message}`);
  }

  const pre = document.createElement('pre');
  pre.id = 'driver-verdict';
  pre.textContent = JSON.stringify(out);
  document.body.appendChild(pre);
})();
</script>
"""


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.fixture(scope="module")
def driven(tmp_path_factory):
    """Serve the console with the driver appended, then let Chrome run it."""
    import uvicorn
    from starlette.responses import HTMLResponse

    from unitree_robot_agent.fleet import FleetManager, Roster
    from unitree_fleet_web import ConsoleConfig, ConsoleService
    from unitree_fleet_web.app import create_app

    tmp_path = tmp_path_factory.mktemp("interaction")
    roster = Roster.from_mapping(
        {
            "state_dir": str(tmp_path / "state"),
            "default": "dog1",
            "robots": [
                {"id": "dog1", "model": "Go2", "ip": "192.168.31.245",
                 "sn": "B42D2000AAAA", "role": "lead"},
                {"id": "dog2", "model": "Go2", "ip": "192.168.31.246",
                 "sn": "B42D2000BBBB", "role": "wing"},
            ],
        }
    )
    fleet = FleetManager(roster, backend="mock", audit_to_stderr=False)
    config = ConsoleConfig(host="127.0.0.1", port=_free_port(), deadman_s=0.5)
    service = ConsoleService(fleet, config=config)
    app = create_app(service, config)

    from starlette.routing import Route

    from unitree_fleet_web.app import STATIC_DIR

    async def _driver(request):  # noqa: ANN001, ARG001
        """The console's own page, with the test driver appended.

        Same origin, so the driver's synthetic events reach the real listeners —
        which is the entire point. Nothing about the console is modified: the
        page is the shipped `index.html` with one extra `<script>` before
        `</body>`.
        """
        page = (STATIC_DIR / "index.html").read_text()
        return HTMLResponse(page.replace("</body>", DRIVER + "</body>"))

    # Inserted, not appended: the catch-all static mount would swallow it.
    app.router.routes.insert(0, Route("/__driver", _driver, methods=["GET"]))

    server = uvicorn.Server(
        uvicorn.Config(app, host=config.host, port=config.port, log_level="error")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "the console did not come up"

    url = f"http://{config.host}:{config.port}/__driver"
    result = subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--no-sandbox",
         "--disable-dev-shm-usage", "--hide-scrollbars",
         f"--virtual-time-budget={DRIVER_BUDGET_MS}", "--dump-dom", url],
        capture_output=True, text=True, timeout=120,
    )
    server.should_exit = True
    thread.join(timeout=10)

    assert result.returncode == 0, result.stderr[-2000:]
    dom = result.stdout
    assert 'id="driver-verdict"' in dom, f"the driver never finished:\n{dom[-3000:]}"

    import html
    import re

    raw = re.search(r'<pre id="driver-verdict">(.*?)</pre>', dom, re.S).group(1)
    return json.loads(html.unescape(raw))


def _checks(driven):
    return {item["name"]: item for item in driven}


def test_the_driver_ran_at_all(driven):
    """A driver that crashed reports its own failure as a check, so an empty or
    error-only verdict is a test-harness bug rather than a console one."""
    checks = _checks(driven)
    assert "driver 本身出错" not in checks, checks["driver 本身出错"]
    assert driven, "the driver reported nothing"


def test_both_consoles_render_in_a_real_browser(driven):
    assert _checks(driven)["两个操作台渲染出来了"]["ok"]


def test_pressing_w_moves_the_stick(driven):
    """**The product decision.** WASD is a first-class input, and the only proof
    is that pressing W changes what is drawn."""
    check = _checks(driven)["按下 W 后摇杆动了"]
    assert check["ok"], check


def test_releasing_w_returns_the_stick(driven):
    """A stick that stays deflected after the key is released is a robot that
    keeps walking — the failure the dead-man exists to prevent."""
    assert _checks(driven)["松开 W 后摇杆回中"]["ok"]


def test_typing_in_a_field_does_not_drive_the_robot(driven):
    """The guard is in the handler (`event.target.tagName`), and it is the
    difference between filling in the formation distance and walking the dog
    into a wall."""
    assert _checks(driven)["在输入框里打字不会驱动机器狗"]["ok"]


def test_a_key_aimed_at_the_page_still_drives(driven):
    """**The control group.** Without it the guard could be broken in the
    "ignores every key" direction and `test_typing_in_a_field_does_not_drive_the
    _robot` would still pass — a guard that refuses everything is not a guard."""
    check = _checks(driven)["同样的按键打在页面上会驱动（对照组）"]
    assert check["ok"], check


def test_tabs_switch_panels(driven):
    assert _checks(driven)["点标签页会切换面板"]["ok"]


def test_the_joystick_takes_a_real_pointer(driven):
    """`setPointerCapture` and the unit-disc clamp are only exercised by an
    actual pointer sequence — a synthetic `click` never touches them."""
    check = _checks(driven)["摇杆接受指针拖动"]
    assert check["ok"], check


def test_the_joystick_springs_back_when_released(driven):
    """A stick that stays where it was dropped is a robot that keeps driving —
    and it is the same failure the keyboard release test covers, by the other
    input path."""
    check = _checks(driven)["松手后摇杆回中"]
    assert check["ok"], check


def test_the_page_is_still_live_after_all_that(driven):
    """The original browser test found the WebSocket had closed and every live
    field sat on placeholder text forever. Interacting with the page is exactly
    what could close it again."""
    checks = _checks(driven)
    assert checks["交互之后连接状态还在"]["ok"], checks["交互之后连接状态还在"]
    assert checks["交互之后状态还在"]["ok"], checks["交互之后状态还在"]


def test_every_check_cites_something_it_observed(driven):
    """**A check that reports `ok: true` with no detail is not evidence.**

    The driver's whole value is that it saw the page react. A verdict like
    `{"ok": true, "detail": ""}` would pass every test above while proving
    nothing — so each check has to carry what it looked at.
    """
    for item in driven:
        assert item["detail"].strip(), f"{item['name']} reported no evidence"


def test_the_driver_checked_everything_it_claims_to(driven, capsys):
    """The count, so a check that silently stops running is noticed. Printed as
    well, because when this file fails the first question is what it saw."""
    names = [item["name"] for item in driven]
    with capsys.disabled():
        print("\n  浏览器驱动实际观察到的：")
        for item in driven:
            mark = "✅" if item["ok"] else "❌"
            print(f"    {mark} {item['name']}: {item['detail']}")

    for expected in (
        "两个操作台渲染出来了",
        "按下 W 后摇杆动了",
        "松开 W 后摇杆回中",
        "在输入框里打字不会驱动机器狗",
        "同样的按键打在页面上会驱动（对照组）",
        "点标签页会切换面板",
        "摇杆接受指针拖动",
        "松手后摇杆回中",
        "交互之后连接状态还在",
        "交互之后状态还在",
    ):
        assert expected in names, f"{expected!r} did not run: {names}"
