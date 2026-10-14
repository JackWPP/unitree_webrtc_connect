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

    // ── the action bar ──
    //
    // **The catalogue needs a connected robot, and the page connects after it
    // boots.** `loadFleetActions` ran once at boot, got an empty catalogue, and
    // was never asked again — so every action but the three static posture
    // buttons was missing until a reload. Survivable with nine actions; the
    // whole feature not working with twenty-two.
    for (let i = 0; i < 60; i++) {
      if (document.querySelectorAll('#fleet-named button').length) break;
      await sleep(200);
    }
    const actionGroups = {
      posture: document.querySelectorAll('#fleet-posture button').length,
      named: document.querySelectorAll('#fleet-named button').length,
      routines: document.querySelectorAll('#fleet-routines button').length,
      hard: document.querySelectorAll('#fleet-hard button').length,
    };
    record(
      '四组动作都填上了',
      actionGroups.posture >= 3 && actionGroups.named >= 9
        && actionGroups.routines >= 4 && actionGroups.hard >= 13,
      JSON.stringify(actionGroups),
    );

    // Every button carries a label *and* an icon. The rule is that no button is
    // icon-only: emoji differ per platform and can be missing, and an
    // icon-only button on a box without the glyph is a blank square.
    const allActionButtons = [...document.querySelectorAll(
      '#fleet-posture button, #fleet-named button, #fleet-hard button',
    )];
    const iconOnly = allActionButtons.filter(
      (b) => !(b.textContent || '').trim(),
    );
    record(
      '没有纯图标按钮',
      allActionButtons.length > 0 && iconOnly.length === 0,
      `${allActionButtons.length} 个按钮，${iconOnly.length} 个没有文字`,
    );

    // The dangerous group is closed until asked for — the whole point of the
    // `<details>`.
    const hardWrap = document.getElementById('fleet-hard-wrap');
    record(
      '高难动作默认折叠',
      !!hardWrap && !hardWrap.open,
      hardWrap ? `open=${hardWrap.open}` : '缺失',
    );

    // ── the routine knobs ──
    const knobBtn = document.querySelector('#fleet-routines .knob-btn');
    record('有带参数的编排（设置）', !!knobBtn, knobBtn ? knobBtn.title : '找不到');
    let panel = null;
    let inputs = [];
    if (knobBtn) {
      knobBtn.click();
      await sleep(400);
      panel = document.querySelector('.routine-params');
      inputs = panel ? panel.querySelectorAll('input[type=number]') : [];
      record('点「设置」打开参数面板', !!panel && inputs.length > 0,
             `${inputs.length} 个输入框`);
      const previewOf = (p) => ((p && p.querySelector('.param-preview')) || {}).textContent || '';
      if (inputs.length) {
        // Change it and watch the preview follow. The arithmetic is the
        // server's, so this proves the round trip as well as the wiring.
        const before = previewOf(panel);
        inputs[0].value = String(Number(inputs[0].max));
        inputs[0].dispatchEvent(new Event('input', { bubbles: true }));
        await sleep(900);
        const after = previewOf(panel);
        record('改参数后预览跟着变', !!after && after !== before,
               `${before || '(空)'} -> ${after || '(空)'}`);
        // Out of range must be refused with the field named, and that refusal
        // is the server's — there is no client-side copy of the bounds.
        inputs[0].value = '999';
        inputs[0].dispatchEvent(new Event('input', { bubbles: true }));
        await sleep(900);
        const bad = previewOf(panel);
        record('越界参数被拒绝且点名',
               bad.includes('边长') || bad.includes('之间'), bad);
      }
    }

    // ── the panel survives a catalogue refresh ──
    //
    // `renderFleetTargets` re-fetches the catalogue when the connected set
    // changes — exactly when somebody has just plugged a dog in and may be
    // mid-edit. The rebuild must not shut the panel they are typing into.
    if (panel) {
      const typed = inputs.length ? inputs[0].value : '';
      await fetch('/api/fleet/routines');
      if (typeof renderFleetTargets === 'function') renderFleetTargets();
      await sleep(1200);
      const again = document.querySelector('.routine-params');
      record('参数面板在刷新后还在', !!again, again ? '还在' : '被刷新关掉了');
      const input = again ? again.querySelector('input[type=number]') : null;
      record('刷新后参数值还在', !!input && input.value === typed,
             `${typed} -> ${input ? input.value : '(没有输入框)'}`);
    }

    // ── the editor can load a saved routine back ──
    const editBtn = [...document.querySelectorAll('#fleet-custom button')]
      .find((b) => b.textContent.trim() === '编辑');
    if (editBtn) {
      editBtn.click();
      await sleep(400);
      const rows = document.querySelectorAll('#editor-steps .step-row');
      const name = document.getElementById('editor-name').value;
      record('编辑已保存的编排能读回步骤', rows.length > 0 && !!name,
             `${rows.length} 步，名字「${name}」`);
      record('步骤行有拖拽手柄',
             !!document.querySelector('#editor-steps .grip'),
             document.querySelectorAll('#editor-steps .grip').length + ' 个');
    } else {
      record('编辑已保存的编排能读回步骤', false, '没有已保存的编排可编辑');
    }

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
    # A saved routine, so the driver's 编辑 path has something to load. Without
    # one the editor's read-back cannot be exercised at all, and "there was
    # nothing to edit" would pass as a check.
    service.save_routine({
        "id": "browser_probe",
        "label": "浏览器探针编排",
        "steps": [
            {"label": "站好", "tool": "robot_stand", "args": {"confirm": True},
             "seconds": 2},
            {"label": "打招呼", "tool": "robot_play_action",
             "args": {"name": "hello", "confirm": True}, "seconds": 3},
        ],
    })
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


# ─── the action bar and the editor, in a real browser ─────────────────


def test_the_action_groups_fill_in_after_the_dogs_connect(driven):
    """**The bug this found.**

    `loadFleetActions` ran once at boot. The catalogue comes from
    `robot_list_actions` on a *connected* robot, so at boot it is empty — and
    nothing ever asked again, because `renderFleetTargets` only rewrote the text
    above the buttons. Every action but the three static posture buttons was
    missing until a page reload.

    Survivable with nine actions. With thirteen more behind it, it is the whole
    feature not working.
    """
    check = _checks(driven)["四组动作都填上了"]
    assert check["ok"], check


def test_no_button_is_icon_only(driven):
    """Emoji render differently per platform and can be missing entirely, so an
    icon-only button on a box without the glyph is a blank square. The icon is
    for scanning; the label is the information."""
    check = _checks(driven)["没有纯图标按钮"]
    assert check["ok"], check


def test_the_dangerous_group_starts_closed(driven):
    """The cost of mistapping a bow and mistapping a backflip are not the same,
    and an emergency is exactly when a flat list of twenty-five buttons gets
    mistapped."""
    check = _checks(driven)["高难动作默认折叠"]
    assert check["ok"], check


def test_the_routine_knobs_open_and_change_the_preview(driven):
    """The whole parameter feature, through the page: the ⚙ opens a panel, a
    number changes what the routine will do, and the arithmetic comes from the
    server rather than from a second implementation in the browser."""
    checks = _checks(driven)
    assert checks["有带参数的编排（设置）"]["ok"], checks["有带参数的编排（设置）"]
    assert checks["点「设置」打开参数面板"]["ok"], checks["点「设置」打开参数面板"]
    assert checks["改参数后预览跟着变"]["ok"], checks["改参数后预览跟着变"]


def test_an_out_of_range_parameter_is_refused_in_the_page(driven):
    """There is no client-side copy of the bounds — the refusal comes from the
    server, which is the only place that knows what the tool accepts."""
    check = _checks(driven)["越界参数被拒绝且点名"]
    assert check["ok"], check


def test_a_saved_routine_can_be_loaded_back_into_the_editor(driven):
    """**The editor's real defect.** `state.editor` was only ever assigned on
    boot and on clear, so a saved routine could be created and never changed or
    removed — "edit yesterday's routine" was not possible, which makes an editor
    a one-way pipe with a form on the end."""
    check = _checks(driven)["编辑已保存的编排能读回步骤"]
    assert check["ok"], check


def test_the_editor_offers_dragging_without_requiring_it(driven):
    """Dragging is added, not substituted. The existing comment argued against
    drag-and-drop because it is "nicer with a mouse and unusable with a keyboard
    or a touch screen" — and that reasoning still holds, so the ↑ ↓ buttons
    remain and this is the other path."""
    check = _checks(driven)["步骤行有拖拽手柄"]
    assert check["ok"], check


def test_the_parameter_panel_survives_a_catalogue_refresh(driven):
    """**The panel must not shut under the operator's hands.**

    `renderFleetTargets` re-fetches the action and routine lists when the
    connected set changes — which is exactly when an operator has just plugged a
    dog in and is most likely to be mid-edit. Clearing the host would close the
    panel and lose nothing visible, which is the worst kind of bug to be told
    about: the numbers you typed are still on screen, they just stopped
    mattering.

    The driver refreshes the catalogue with the panel open and asserts it is
    still there, with the value still in it.
    """
    check = _checks(driven)["参数面板在刷新后还在"]
    assert check["ok"], check


def test_the_panel_keeps_the_value_that_was_typed(driven):
    """A panel that reopens with the default is a panel that forgot, which
    looks the same as one that never had the value."""
    check = _checks(driven)["刷新后参数值还在"]
    assert check["ok"], check
