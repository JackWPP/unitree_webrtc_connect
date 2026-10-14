/* Unitree dual-dog console.
 *
 * Three rules shape this file:
 *
 * 1. Every value that came from the server or an operator is written with
 *    `textContent`, never `innerHTML`. Robot names are operator-supplied, and
 *    the previous web console built an inline `onclick` out of one — a stored
 *    XSS with a feed path straight through "add robot".
 *
 * 2. Teleop is sent to `/api/fleet/teleop` with a *source* name, not to a
 *    specific robot. The server resolves which dogs that source drives, so an
 *    out-of-date tab cannot command a robot the operator did not select.
 *
 * 3. Letting go must be explicit. On key-up, blur, or mode change we send a
 *    zero frame — the robot holds its last non-zero velocity otherwise.
 */
'use strict';

const POLL_HZ = 20;
const TICK_MS = 1000 / POLL_HZ;

/* Key groups. `split` mode gives each dog its own cluster so one operator can
 * drive both at once — the capability neither the old console nor unitree_ui
 * has. */
const KEY_GROUPS = {
  wasd: {
    w: ['ly', +1], s: ['ly', -1], a: ['lx', -1], d: ['lx', +1],
    q: ['rx', -1], e: ['rx', +1],
  },
  ijkl: {
    i: ['ly', +1], k: ['ly', -1], j: ['lx', -1], l: ['lx', +1],
    u: ['rx', -1], o: ['rx', +1],
  },
};

const TOKEN = new URLSearchParams(location.search).get('token') || '';

const state = {
  fleet: null,
  live: null,
  mode: 'focus',
  focused: '',
  focusMap: {},           // robot_id -> { pen, video, ... }
  held: new Set(),        // physical keys currently down
  // robot_id -> {mx, my, turn}, each -1..1, from dragging the on-screen stick.
  // Continuous, not boolean: a stick you can only push fully is six buttons
  // wearing a circle.
  pointer: {},
  lastZeroSent: 0,
  // Grouped control descriptors per robot, from /api/console/controls.
  controls: {},
  // Live values the robot reports, so a widget starts from reality.
  flags: {},
};

/* ── transport ─────────────────────────────────────────────────────── */

async function api(path, options = {}) {
  const headers = Object.assign({ 'Content-Type': 'application/json' }, options.headers || {});
  if (TOKEN) headers['Authorization'] = 'Bearer ' + TOKEN;
  const response = await fetch(path, Object.assign({}, options, { headers }));
  let payload = null;
  try { payload = await response.json(); } catch (_) { /* empty body */ }
  if (!response.ok) {
    const err = new Error((payload && payload.error) || response.statusText);
    err.payload = payload;
    throw err;
  }
  return payload;
}

function toast(message, kind) {
  let el = document.getElementById('toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast';
    el.className = 'toast';
    document.body.appendChild(el);
  }
  el.textContent = message;
  el.className = 'toast' + (kind ? ' ' + kind : '');
  el.classList.remove('hidden');
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.classList.add('hidden'), 4000);
}

/* ── rendering ─────────────────────────────────────────────────────── */

function buildPens(fleet) {
  const host = document.getElementById('pens');
  host.textContent = '';
  state.focusMap = {};

  for (const robot of fleet.robots) {
    const node = document.getElementById('pen-template').content.cloneNode(true);
    const pen = node.querySelector('.pen');
    pen.dataset.robot = robot.robot_id;
    if (robot.role) pen.classList.add('role-' + robot.role);

    pen.querySelector('[data-role="name"]').textContent = robot.robot_id;
    pen.querySelector('[data-role="role"]').textContent = robot.role || '';
    pen.querySelector('[data-role="target"]').textContent = robot.target || '—';

    const video = pen.querySelector('[data-role="video"]');
    const videoOff = pen.querySelector('[data-role="video-off"]');

    const toggleVideo = () => {
      const on = videoOff.classList.contains('hidden') === false;
      setVideo(robot.robot_id, on);
    };

    pen.querySelector('[data-role="focus"]').addEventListener('click', () => setFocus(robot.robot_id));
    pen.querySelector('[data-role="stop"]').addEventListener('click', () => robotStop(robot.robot_id));
    pen.querySelector('[data-role="estop"]').addEventListener('click', () => estop(robot.robot_id));
    pen.querySelector('[data-role="video-toggle"]').addEventListener('click', toggleVideo);
    pen.querySelector('[data-role="link-toggle"]').addEventListener('click', () => toggleLink(robot.robot_id));

    // On-screen sticks. Dragging either one drives the same two axes WASD/QE
    // drive; they are not a separate input path, they are a second way to
    // express the same intent — which is why the knobs also follow the keys.
    const moveEl = pen.querySelector('[data-role="stick-move"]');
    const turnEl = pen.querySelector('[data-role="stick-turn"]');
    if (moveEl) bindJoystick(robot.robot_id, moveEl, 'move');
    if (turnEl) bindJoystick(robot.robot_id, turnEl, 'turn');

    host.appendChild(node);

    state.focusMap[robot.robot_id] = {
      pen: host.querySelector(`.pen[data-robot="${CSS.escape(robot.robot_id)}"]`),
      video: host.querySelector(`.pen[data-robot="${CSS.escape(robot.robot_id)}"] [data-role="video"]`),
      videoOff: host.querySelector(`.pen[data-robot="${CSS.escape(robot.robot_id)}"] [data-role="video-off"]`),
      link: host.querySelector(`.pen[data-robot="${CSS.escape(robot.robot_id)}"] [data-role="link"]`),
      state: host.querySelector(`.pen[data-robot="${CSS.escape(robot.robot_id)}"] [data-role="state"]`),
      battery: host.querySelector(`.pen[data-robot="${CSS.escape(robot.robot_id)}"] [data-role="battery"]`),
      keys: host.querySelector(`.pen[data-robot="${CSS.escape(robot.robot_id)}"] [data-role="keys"]`),
      faults: host.querySelector(`.pen[data-robot="${CSS.escape(robot.robot_id)}"] [data-role="faults"]`),
      faultCount: host.querySelector(`.pen[data-robot="${CSS.escape(robot.robot_id)}"] [data-role="fault-count"]`),
      linkToggle: host.querySelector(`.pen[data-robot="${CSS.escape(robot.robot_id)}"] [data-role="link-toggle"]`),
      controls: host.querySelector(`.pen[data-robot="${CSS.escape(robot.robot_id)}"] [data-role="controls"]`),
      flags: host.querySelector(`.pen[data-robot="${CSS.escape(robot.robot_id)}"] [data-role="flags"]`),
      tabs: host.querySelector(`.pen[data-robot="${CSS.escape(robot.robot_id)}"] [data-role="tabs"]`),
      panels: host.querySelector(`.pen[data-robot="${CSS.escape(robot.robot_id)}"] [data-role="panels"]`),
      batteryBar: host.querySelector(`.pen[data-robot="${CSS.escape(robot.robot_id)}"] [data-role="battery-bar"]`),
      faultBadge: host.querySelector(`.pen[data-robot="${CSS.escape(robot.robot_id)}"] [data-role="fault-badge"]`),
      joyMove: host.querySelector(`.pen[data-robot="${CSS.escape(robot.robot_id)}"] [data-role="stick-move"]`),
      joyTurn: host.querySelector(`.pen[data-robot="${CSS.escape(robot.robot_id)}"] [data-role="stick-turn"]`),
      knobMove: host.querySelector(`.pen[data-robot="${CSS.escape(robot.robot_id)}"] [data-role="knob-move"]`),
      knobTurn: host.querySelector(`.pen[data-robot="${CSS.escape(robot.robot_id)}"] [data-role="knob-turn"]`),
      drive: host.querySelector(`.pen[data-robot="${CSS.escape(robot.robot_id)}"] [data-role="drive"]`),
      where: host.querySelector(`.pen[data-robot="${CSS.escape(robot.robot_id)}"] [data-role="where"]`),
      bodyStrip: host.querySelector(`.pen[data-robot="${CSS.escape(robot.robot_id)}"] [data-role="body-strip"]`),
      bubble: host.querySelector(`.pen[data-robot="${CSS.escape(robot.robot_id)}"] [data-role="bubble"]`),
      bubbleDot: host.querySelector(`.pen[data-robot="${CSS.escape(robot.robot_id)}"] [data-role="bubble-dot"]`),
      feet: host.querySelector(`.pen[data-robot="${CSS.escape(robot.robot_id)}"] [data-role="feet"]`),
      speed: host.querySelector(`.pen[data-robot="${CSS.escape(robot.robot_id)}"] [data-role="speed"]`),
      gait: host.querySelector(`.pen[data-robot="${CSS.escape(robot.robot_id)}"] [data-role="gait"]`),
      sources: host.querySelector(`.pen[data-robot="${CSS.escape(robot.robot_id)}"] [data-role="sources"]`),
      faultsWrap: host.querySelector(`.pen[data-robot="${CSS.escape(robot.robot_id)}"] [data-role="faults-wrap"]`),
      auditWrap: host.querySelector(`.pen[data-robot="${CSS.escape(robot.robot_id)}"] [data-role="audit-wrap"]`),
      audit: host.querySelector(`.pen[data-robot="${CSS.escape(robot.robot_id)}"] [data-role="audit"]`),
      auditCount: host.querySelector(`.pen[data-robot="${CSS.escape(robot.robot_id)}"] [data-role="audit-count"]`),
    };
    updateVideo(robot.robot_id, true);
    loadAuditOnOpen(robot.robot_id);
  }
  updateKeysHint();
}

/* The trail is fetched when the operator opens the panel, not on every status
 * tick: it is history, it changes only when something is done, and polling a
 * file read per dog per second would be work nobody asked for. */
function loadAuditOnOpen(robotId) {
  const entry = state.focusMap[robotId];
  if (!entry || !entry.auditWrap || !entry.audit) return;
  entry.auditWrap.addEventListener('toggle', () => {
    if (entry.auditWrap.open) refreshAudit(robotId);
  });
}

async function refreshAudit(robotId) {
  const entry = state.focusMap[robotId];
  if (!entry || !entry.audit) return;
  let payload;
  try {
    payload = await api(`/api/robot/${encodeURIComponent(robotId)}/audit?limit=40`);
  } catch (err) {
    entry.audit.textContent = '';
    const item = document.createElement('li');
    item.className = 'what bad';
    item.textContent = '读取失败: ' + err.message;
    entry.audit.appendChild(item);
    return;
  }

  entry.audit.textContent = '';
  const records = (payload.records || []).slice().reverse();  // newest first
  entry.auditCount.textContent = records.length ? `(${records.length})` : '';

  if (!records.length) {
    const empty = document.createElement('li');
    empty.className = 'what muted';
    empty.textContent = '暂无记录';
    entry.audit.appendChild(empty);
    return;
  }

  for (const record of records) {
    const item = document.createElement('li');

    const what = document.createElement('span');
    what.className = 'what' + (record.ok === false ? ' bad' : '');
    // Every value here is operator- or robot-supplied and goes in as text.
    const label = record.tool || '?';
    const why = record.error ? ` — ${record.error}` : '';
    what.textContent = label + why;

    const when = document.createElement('span');
    when.className = 'when';
    when.textContent = (record.iso || '').slice(11) || '';

    item.appendChild(when);
    item.appendChild(what);
    entry.audit.appendChild(item);
  }
}

/* Faults are rendered from the server's reconciled active set, never from the
 * raw notification history: that history is append-only, so a fault cleared in
 * a snapshot you missed stays in it forever. An empty list is only reassuring
 * when `faults_authoritative` is true — that means the robot has sent a full
 * snapshot this session, and until then we simply do not know. */
function renderFaults(robotId, robot) {
  const entry = state.focusMap[robotId];
  if (!entry || !entry.faults) return;

  const faults = robot.faults || [];
  const known = robot.faults_authoritative === true;

  const list = entry.faults;
  list.textContent = '';

  if (!faults.length) {
    const empty = document.createElement('li');
    empty.className = 'fault-empty';
    empty.textContent = known ? '无活动故障' : '尚未收到完整故障快照';
    list.appendChild(empty);
  } else {
    for (const fault of faults) {
      const item = document.createElement('li');
      item.className = 'fault';

      const what = document.createElement('span');
      what.className = 'fault-what';
      what.textContent = fault.label || fault.key;

      const where = document.createElement('span');
      where.className = 'fault-where';
      // Per-motor sources are the same catalog as source 300, reported for one
      // joint — so name the joint rather than printing "301".
      const parts = [fault.source_label];
      if (fault.motor) parts.push(`#${fault.motor}`);
      parts.push(fault.code_hex);
      where.textContent = parts.join(' ');

      item.appendChild(what);
      item.appendChild(where);
      list.appendChild(item);
    }
  }

  entry.faultCount.textContent = known
    ? String(faults.length)
    : (faults.length ? String(faults.length) + '?' : '?');
  entry.faultCount.className = 'pill' + (faults.length ? ' bad' : (known ? ' up' : ''));
  entry.pen.classList.toggle('has-faults', faults.length > 0);

  // The badge lives in the head, so a fault is visible from whichever tab is
  // open. Hiding it behind a panel would mean the one thing that must not be
  // missed is the one thing that can be.
  if (entry.faultBadge) {
    entry.faultBadge.classList.toggle('hidden', faults.length === 0);
    entry.faultBadge.title = faults.length
      ? `${faults.length} 条故障 —— 点击查看` : '';
    entry.faultBadge.onclick = () => showTab(robot.robot_id, 'sense');
  }
}

/* The pad, the keys and a gamepad all end up as the same four numbers. Drawing
 * those numbers is the only way an operator can tell "my key is stuck" from
 * "the robot is not listening". */
function renderStick(robotId, axes) {
  const entry = state.focusMap[robotId];
  if (!entry) return;
  const lx = Math.max(-1, Math.min(1, axes.lx || 0));
  const ly = Math.max(-1, Math.min(1, axes.ly || 0));
  const rx = Math.max(-1, Math.min(1, axes.rx || 0));

  // The knobs show the axes, whichever input produced them. A key held down
  // moves the same knob a drag would — that is what makes the on-screen stick
  // a readout of the keyboard rather than a competing control.
  if (entry.knobMove) {
    const radius = (entry.joyMove ? entry.joyMove.clientWidth : 104) / 2 - 17;
    entry.knobMove.style.transform =
      `translate(${(lx * radius).toFixed(1)}px, ${(-ly * radius).toFixed(1)}px)`;
  }
  if (entry.knobTurn) {
    const radius = (entry.joyTurn ? entry.joyTurn.clientWidth : 104) / 2 - 14;
    entry.knobTurn.style.transform = `translateX(${(rx * radius).toFixed(1)}px)`;
  }
}

/* The key legend, written as keycaps rather than prose.
 *
 * "WASD 前进/平移 · QE 转向" wrapped to three lines in a 118 px column and
 * buried the two things that matter — which keys, and which axis. The keycaps
 * are the first thing the eye lands on, and the mode is one short line under
 * them.
 *
 * Built as elements rather than `innerHTML`: the text is ours, but a console
 * that assembles markup from strings is one edit away from doing it with
 * something that is not. */
function keycapLegend(keys, description) {
  const wrap = document.createDocumentFragment();
  for (const key of keys) {
    const cap = document.createElement('kbd');
    cap.textContent = key;
    wrap.appendChild(cap);
    wrap.appendChild(document.createTextNode(' '));
  }
  const note = document.createElement('span');
  note.className = 'hint-note';
  note.textContent = description;
  wrap.appendChild(note);
  return wrap;
}

const KEY_LEGENDS = {
  focus: [['W', 'A', 'S', 'D'], '平移'],
  both: [['W', 'A', 'S', 'D'], '同时驱动所有已选狗'],
  split_a: [['W', 'A', 'S', 'D'], '平移'],
  split_b: [['I', 'J', 'K', 'L'], '平移'],
  // **Not a lie.** This used to read "按输入设备绑定（下方 bind）" and there is
  // no bind control: the mode resolves to no robot, accepts keys, and moves
  // nothing. Saying so is the difference between a mode that does not work
  // and a console that looks broken.
  per_source: [[], '此模式没有绑定界面 —— 按键会被接受，但不会驱动任何狗'],
};

function updateKeysHint() {
  const mode = state.mode || 'focus';
  for (const id of Object.keys(state.focusMap)) {
    const el = state.focusMap[id].keys;
    if (!el) continue;
    el.textContent = '';

    let legend;
    if (mode === 'split') {
      legend = id === firstId() ? KEY_LEGENDS.split_a : KEY_LEGENDS.split_b;
      const turn = document.createElement('div');
      turn.className = 'hint-row';
      turn.appendChild(keycapLegend(
        id === firstId() ? ['Q', 'E'] : ['U', 'O'], '转向'
      ));
      el.appendChild(keycapLegend(legend[0], legend[1]));
      el.appendChild(turn);
    } else {
      legend = KEY_LEGENDS[mode] || KEY_LEGENDS.focus;
      el.appendChild(keycapLegend(legend[0], legend[1]));
      if (mode === 'focus') {
        const turn = document.createElement('div');
        turn.className = 'hint-row';
        turn.appendChild(keycapLegend(['Q', 'E'], '转向'));
        el.appendChild(turn);
      }
    }
  }
}

function firstId() {
  return state.fleet && state.fleet.robots.length ? state.fleet.robots[0].robot_id : '';
}

/* A connected robot with no picture looks exactly like a broken console.
 * These are the cases the panel cannot otherwise tell apart: no camera on the
 * chassis, the video channel never opened, or the track ended. */
const VIDEO_MESSAGES = {
  offline: '未连接',
  stalled: '无画面 —— 相机不可用或视频通道未开启',
  starting: '画面启动中…',
  disabled: '画面已关闭',
};

function renderVideoHealth(robotId) {
  const entry = state.focusMap[robotId];
  if (!entry) return;
  const health = (state.videoHealth && state.videoHealth[robotId]) || null;
  if (!health) return;

  const show = health.state !== 'ok' && health.state !== 'disabled';
  entry.videoOff.classList.toggle('hidden', !show);
  if (show) {
    entry.videoOff.textContent = VIDEO_MESSAGES[health.state] || '';
  }
  // Leave the <img> alone while the stream is merely starting: tearing the
  // connection down would restart the handshake it is waiting on.
  if (health.state === 'ok' && entry.video.getAttribute('src')) return;
  if (health.state === 'stalled' && entry.video.getAttribute('src')) {
    entry.video.removeAttribute('src');
  }
}

function updateVideo(robotId, enabled) {
  const entry = state.focusMap[robotId];
  if (!entry) return;
  if (enabled) {
    entry.videoOff.classList.add('hidden');
    const suffix = TOKEN ? '?token=' + encodeURIComponent(TOKEN) : '';
    entry.video.src = `/api/robot/${encodeURIComponent(robotId)}/stream.mjpeg${suffix}`;
  } else {
    entry.videoOff.classList.remove('hidden');
    entry.video.removeAttribute('src');
  }
}

function renderStatus(payload) {
  if (!payload || !payload.robots) return;
  state.live = payload;

  const estopState = (payload.state && payload.state.estop) || {};
  const engaged = !!estopState.engaged;
  document.getElementById('estop-banner').classList.toggle('hidden', !engaged);
  if (engaged) {
    const scope = estopState.scope === 'all' ? '全部' : estopState.robot_id;
    document.getElementById('estop-text').textContent =
      `急停已锁存（${scope}）—— 所有运动指令被拒绝`;
  }
  document.getElementById('btn-estop-release').disabled = !engaged;
  document.getElementById('btn-estop').disabled = engaged;

  if (payload.state) {
    if (payload.state.input_mode) {
      state.mode = payload.state.input_mode;
      document.getElementById('input-mode').value = state.mode;
    }
    state.focused = payload.state.focused || state.focused;
    const driving = new Set(payload.state.driving || []);
    state.driving = driving;
  }

  let online = 0;
  for (const robot of payload.robots) {
    const entry = state.focusMap[robot.robot_id];
    if (!entry) continue;
    const connected = !!robot.connected;
    if (connected) online += 1;

    const connecting = (state.connecting || []).includes(robot.robot_id);
    entry.link.textContent = connected ? '在线' : (connecting ? '连接中…' : '离线');
    entry.link.className = 'pill ' + (connected ? 'up' : (connecting ? 'warn' : 'down'));
    if (entry.linkToggle) {
      // Connecting is how you take the link back after the official app held
      // it; disconnecting hands it back deliberately.
      entry.linkToggle.textContent = connected ? '断开' : '连接';
      entry.linkToggle.classList.toggle('btn-estop-sm', !connected);
    }

    // Battery and posture come from the telemetry sampler, which runs far
    // slower than this push. Reading them off the session (as this used to)
    // meant both fields sat on "—" forever: `session` carries connection
    // counters, not robot state.
    // `body` rather than a raw `telemetry` blob: the payload no longer ships
    // the whole snapshot, and everything the page shows from it is derived.
    const telemetry = (state.body && state.body[robot.robot_id]) || {};
    const battery = telemetry.battery || {};
    const percent = battery.soc_percent;
    entry.battery.textContent = (percent === undefined || percent === null)
      ? '—' : `${percent}%`;
    entry.battery.title = battery.power_voltage_v
      ? `${battery.power_voltage_v} V` : '';

    // A number alone makes the operator read and compare; a bar is seen at a
    // glance. Thresholds are the ones an operator acts on: under 30% stop
    // planning long runs, under 15% bring it home.
    if (entry.batteryBar) {
      const known = typeof percent === 'number';
      entry.batteryBar.style.width = known ? `${Math.max(0, Math.min(100, percent))}%` : '0';
      entry.batteryBar.classList.toggle('warn', known && percent < 30 && percent >= 15);
      entry.batteryBar.classList.toggle('bad', known && percent < 15);
    }

    // When the panels have nothing, say why. An empty panel and a robot that
    // reports nothing look identical, and they want different reactions.
    const health = (state.telemetryHealth && state.telemetryHealth[robot.robot_id]) || {};
    if (!battery.soc_percent && health.error) {
      entry.battery.title = health.error;
      entry.state.title = health.error;
    }

    const posture = telemetry.fsm || {};
    const parts = [];
    if (posture.state && posture.state !== 'n/a' && posture.state !== 'unknown') {
      parts.push(posture.state);
    }
    if (telemetry.gait) parts.push(telemetry.gait);
    if (posture.reporting === 'frozen') parts.push('状态停更');
    entry.state.textContent = parts.length
      ? parts.join(' · ')
      : (connected ? '已连接' : '—');
    entry.state.title = posture.note || '';

    entry.pen.classList.toggle('focused', robot.robot_id === state.focused);
    entry.pen.classList.toggle('driving', state.driving ? state.driving.has(robot.robot_id) : false);
    renderVideoHealth(robot.robot_id);
    renderFlags(robot.robot_id);
    renderDrive(robot.robot_id);
    renderWhere(robot.robot_id);
    renderBody(robot.robot_id);
    renderSources(robot.robot_id);
    renderFaults(robot.robot_id, robot);
  }

  state.connecting = payload.connecting || [];

  // Named `videoHealth` rather than `video`: `focusMap[id].video` is the
  // <img> element, and two things called video would invite a mix-up.
  state.videoHealth = payload.video || {};
  state.lastStatus = payload;
  state.motion = payload.motion || {};
  state.where = payload.where || {};
  state.body = payload.body || {};
  state.sources = payload.sources || {};
  state.sync = payload.sync || null;
  renderSync(state.sync);
  renderArena(payload.arena);
  renderFormation(payload.formation);
  renderFleetTargets();
  renderRoutine(payload.routine);
  state.flags = payload.flags || state.flags || {};
  state.telemetryHealth = payload.telemetry_health || {};
  document.getElementById('fleet-summary').textContent =
    `${online}/${payload.robots.length} 在线`;
  document.getElementById('watching').textContent =
    payload.watching ? `${payload.watching} 个浏览器在观看` : '';
  const gp = document.getElementById('gamepad');
  if (gp) {
    const pads = state.pads || [];
    gp.textContent = pads.length
      ? `手柄 ${pads.join(',')}${state.padWarning ? ' ⚠ ' + state.padWarning : ''}`
      : '';
    gp.className = state.padWarning ? 'muted warn' : 'muted';
  }
  document.getElementById('link-state').textContent =
    payload.state && payload.state.driving && payload.state.driving.length
      ? '正在驱动: ' + payload.state.driving.join(', ')
      : '待命';
}

/* ── commands ──────────────────────────────────────────────────────── */

async function setFocus(robotId, quiet) {
  try {
    await api('/api/fleet/focus', {
      method: 'POST',
      body: JSON.stringify({ robot_id: robotId }),
    });
    state.focused = robotId;
    for (const id of Object.keys(state.focusMap)) {
      state.focusMap[id].pen.classList.toggle('focused', id === robotId);
    }
  } catch (err) {
    if (!quiet) toast('切换焦点失败: ' + err.message, 'bad');
  }
}

async function setVideo(robotId, enabled) {
  try {
    await api(`/api/robot/${encodeURIComponent(robotId)}/video`, {
      method: 'POST',
      body: JSON.stringify({ enabled }),
    });
    updateVideo(robotId, enabled);
    const entry = state.focusMap[robotId];
    const btn = entry.pen.querySelector('[data-role="video-toggle"]');
    btn.textContent = enabled ? '关闭画面' : '开启画面';
  } catch (err) {
    toast('切换画面失败: ' + err.message, 'bad');
  }
}

async function toggleLink(robotId) {
  const entry = state.focusMap[robotId];
  const connected = entry && entry.link.textContent === '在线';
  const endpoint = connected ? 'disconnect' : 'connect';
  try {
    const result = await api(
      `/api/robot/${encodeURIComponent(robotId)}/${endpoint}`, { method: 'POST' });
    if (endpoint === 'connect' && result.connected === false) {
      // A failed connect is a normal answer, not a server error: show why.
      toast(`${robotId} 连接失败: ${result.message || result.error || 'unknown'}` +
            (result.hint ? ' — ' + result.hint : ''), 'bad');
    }
  } catch (err) {
    toast(`${robotId} ${endpoint} 失败: ${err.message}`, 'bad');
  }
}

async function robotStop(robotId) {
  try {
    const res = await api(`/api/robot/${encodeURIComponent(robotId)}/stop`, { method: 'POST' });
    if (!res.delivered) toast(`${robotId} 未确认停止（${res.link}）`, 'bad');
    const entry = state.focusMap[robotId];
    if (entry && entry.auditWrap && entry.auditWrap.open) refreshAudit(robotId);
  } catch (err) { toast('停止失败: ' + err.message, 'bad'); }
}

async function stopAll() {
  try {
    const res = await api('/api/fleet/stop', { method: 'POST' });
    if (res.not_stopped && res.not_stopped.length) {
      toast('未确认停止: ' + res.not_stopped.join(', '), 'bad');
    } else {
      toast('全部已停止', 'ok');
    }
  } catch (err) { toast('全部停止失败: ' + err.message, 'bad'); }
}

async function estop(robotId) {
  try {
    const res = await api('/api/fleet/estop', {
      method: 'POST',
      body: JSON.stringify({ robot_id: robotId || '', reason: 'operator' }),
    });
    if (res.not_damped && res.not_damped.length) {
      toast('急停未能触达: ' + res.not_damped.join(', '), 'bad');
    } else {
      toast('急停已锁存', 'ok');
    }
  } catch (err) { toast('急停失败: ' + err.message, 'bad'); }
}

async function estopRelease() {
  try {
    const res = await api('/api/fleet/estop/release', { method: 'POST' });
    toast(res.released ? '急停已解除（机器人仍是阻尼姿态）' : '没有可解除的锁存', res.released ? 'ok' : 'bad');
  } catch (err) { toast('解除失败: ' + err.message, 'bad'); }
}

async function setMode(mode) {
  try {
    await api('/api/fleet/mode', { method: 'POST', body: JSON.stringify({ mode }) });
    state.mode = mode;
    updateKeysHint();
  } catch (err) { toast('切换模式失败: ' + err.message, 'bad'); }
}

/* Every button below calls the SAME guarded tool function the MCP server
 * exposes, through /api/robot/<id>/tool. That means the `confirm` gate, the
 * dangerous-action gate, safe mode, the rate limiter and the audit trail all
 * apply unchanged.
 *
 * These buttons send `confirm: true`. That is a deliberate decision, not a
 * bypass: for an LLM `confirm` is a separate deliberation step, but a human
 * clicking a labelled button on a console *is* the confirmation. The gates
 * that are not about human intent — safe mode, the dangerous-action
 * allow-list, rate limiting — still refuse. */
async function callTool(robotId, tool, args) {
  try {
    const payload = Object.assign({ tool, args: args || {} });
    const result = await api(`/api/robot/${encodeURIComponent(robotId)}/tool`, {
      method: 'POST',
      body: JSON.stringify(payload),
    });
    if (result && result.ok === false) {
      toast(`${robotId} ${tool}: ${result.error || 'failed'}` +
            (result.hint ? ' — ' + result.hint : ''), 'bad');
    } else {
      toast(`${robotId} ${tool} ok`, 'ok');
    }
    const entry = state.focusMap[robotId];
    if (entry && entry.auditWrap && entry.auditWrap.open) refreshAudit(robotId);
    return result;
  } catch (err) {
    toast(`${robotId} ${tool}: ${err.message}`, 'bad');
    return null;
  }
}

/* ── the control panel ─────────────────────────────────────────────── */

/* Group headings. The descriptor's group ids are stable; the labels live here
   because they are presentation. An unknown group falls back to its id rather
   than disappearing, so adding a group server-side shows up immediately. */
const GROUP_LABELS = {
  posture: '姿态与动作',
  // Not "步态": there is no gait tool, and these are one-shot moves. Calling
  // the group 步态 would promise a persistent gait switch that does not exist.
  gait: '步法动作',
  motion: '运动模式',
  safety_net: '避障与感知',
  lighting: '灯光与声音',
  media: '画面',
  motion_nudge: '微动',
};

/* A control is rendered from its `kind`, so the frontend never hardcodes a
   widget per tool — adding a control server-side is enough. */
function controlWidget(robotId, control) {
  // `read` is independent of `kind`: a reader can be a button (fetch and show)
  // or a toggle that only reports. Checked first so a reader never renders as
  // a plain button that appears to do nothing.
  let widget;
  if (control.read) {
    widget = readWidget(robotId, control);
  } else {
    switch (control.kind) {
      case 'toggle': widget = toggleWidget(robotId, control); break;
      case 'select': widget = selectWidget(robotId, control); break;
      case 'range': widget = rangeWidget(robotId, control); break;
      default: widget = buttonWidget(robotId, control);
    }
  }
  return applyBlocked(widget, control);
}

function baseArgs(control, extra) {
  return Object.assign({}, control.args || {}, extra || {});
}

/* A control the server marked blocked is disabled, not hidden: safe mode can
 * be turned off, and a capability that vanishes looks like one that does not
 * exist. */
function applyBlocked(widget, control) {
  if (!control.blocked) return widget;
  widget.disabled = true;
  widget.classList.add('blocked');
  widget.title = `${control.title || control.label} —— ${control.blocked}`;
  return widget;
}

function buttonWidget(robotId, control) {
  const button = document.createElement('button');
  button.className = 'btn';
  button.textContent = control.label;
  if (control.title) button.title = control.title;
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      await callTool(robotId, control.tool, baseArgs(control));
    } finally {
      button.disabled = false;
    }
  });
  return button;
}

function toggleWidget(robotId, control) {
  const button = document.createElement('button');
  button.className = 'btn';
  button.textContent = control.label;
  if (control.title) button.title = control.title;
  // Start from what the robot reports, not from an assumption: a toggle that
  // flips locally but is refused would otherwise look like it worked.
  const current = (state.flags[robotId] || {})[control.id];
  button.dataset.on = current ? '1' : '0';
  paintToggle(button, control, button.dataset.on === '1');

  button.addEventListener('click', async () => {
    const next = button.dataset.on !== '1';
    button.disabled = true;
    try {
      const result = await callTool(robotId, control.tool,
        baseArgs(control, { [control.arg_name]: next ? control.on_value : control.off_value }));
      // Only believe it if the call succeeded; a refusal must not leave the
      // button showing a state the robot is not in.
      if (result !== null && result !== undefined) {
        button.dataset.on = next ? '1' : '0';
        paintToggle(button, control, next);
      }
    } finally {
      button.disabled = false;
    }
  });
  return button;
}

function paintToggle(button, control, on) {
  button.classList.toggle('toggle-on', on);
  button.textContent = on ? `${control.label}：开` : `${control.label}：关`;
}

function selectWidget(robotId, control) {
  const wrap = document.createElement('label');
  wrap.className = 'ctl-field';
  // The caption is a label, not a paragraph: `title` is a full explanation and
  // belongs in the tooltip. Rendering it as the caption turned one dropdown
  // into a sentence wide enough to push everything else off the row.
  const caption = document.createElement('span');
  caption.textContent = control.label;
  if (control.title) caption.title = control.title;
  wrap.appendChild(caption);

  const picker = document.createElement('select');
  if (control.title) picker.title = control.title;
  // Not `control.label`: the caption beside it already says that, and the
  // result read "运动模式 [运动模式 ▾]" — the same words twice, which is what
  // a pile of widgets looks like rather than a designed row.
  const placeholder = document.createElement('option');
  placeholder.value = '';
  placeholder.textContent = '选择…';
  picker.appendChild(placeholder);

  const fill = (options) => {
    for (const option of options) {
      const element = document.createElement('option');
      element.value = typeof option.value === 'string' ? option.value : JSON.stringify(option.value);
      element.textContent = option.label;
      picker.appendChild(element);
    }
  };
  fill(control.options || []);

  // Some lists only the robot knows — the actions a particular Go2 firmware
  // supports, for instance. Asking beats hardcoding a catalogue that would go
  // stale on the next firmware.
  if (control.options_from) {
    const source = control.options_from;
    const arg = control.options_arg || 'name';
    api(`/api/robot/${encodeURIComponent(robotId)}/tool`, {
      method: 'POST',
      body: JSON.stringify({ tool: source, args: { include_dangerous: false } }),
    }).then((listed) => {
      const items = (listed && listed.actions) || [];
      if (!items.length) return;
      fill(items.map((item) => ({
        value: item[arg] !== undefined ? item[arg] : item.name,
        label: item.label || item.name,
      })));
      picker.disabled = false;
    }).catch(() => { /* leave the picker empty rather than wrong */ });
    picker.disabled = true;
  }
  picker.addEventListener('change', () => {
    const raw = picker.value;
    picker.value = '';
    if (!raw) return;
    let value = raw;
    try { value = JSON.parse(raw); } catch (_) { /* a plain string */ }
    callTool(robotId, control.tool, baseArgs(control, { [control.arg_name || 'value']: value }));
  });
  wrap.appendChild(picker);
  return wrap;
}

function rangeWidget(robotId, control) {
  const wrap = document.createElement('label');
  wrap.className = 'ctl-field';
  const caption = document.createElement('span');
  caption.textContent = control.label;
  if (control.title) caption.title = control.title;

  const slider = document.createElement('input');
  slider.type = 'range';
  slider.min = control.min;
  slider.max = control.max;
  slider.step = control.step || 1;
  const current = (state.flags[robotId] || {})[control.id];
  slider.value = typeof current === 'number' ? current : control.min;

  const readout = document.createElement('output');
  readout.textContent = slider.value;
  slider.addEventListener('input', () => { readout.textContent = slider.value; });
  // `change`, not `input`: a slider drag would otherwise send a command per
  // pixel, and each one is a real robot command.
  slider.addEventListener('change', () => {
    callTool(robotId, control.tool,
      baseArgs(control, { [control.arg_name]: Number(slider.value) }));
  });

  wrap.appendChild(caption);
  wrap.appendChild(slider);
  wrap.appendChild(readout);
  return wrap;
}

function readWidget(robotId, control) {
  const box = document.createElement('button');
  box.className = 'ctl-read';
  box.textContent = control.label;
  if (control.title) box.title = control.title;
  box.addEventListener('click', async () => {
    const result = await callTool(robotId, control.tool, baseArgs(control));
    if (result) box.textContent = summariseRead(result, control.label);
  });
  return box;
}

/* One line of whatever came back, without assuming which fields a tool
   returns — the tools differ per model and would otherwise need a formatter
   per tool in the frontend. */
function summariseRead(result, fallback) {
  if (typeof result !== 'object' || result === null) return String(result);
  const skip = new Set(['ok', 'robot', 'robot_id', 'note', 'hint', 'code']);
  const parts = [];
  for (const [key, value] of Object.entries(result)) {
    if (skip.has(key) || value === null || value === undefined) continue;
    if (typeof value === 'object') continue;
    parts.push(`${key}=${value}`);
    if (parts.length >= 6) break;
  }
  return parts.length ? parts.join('  ') : (result.note || fallback);
}

/* Which tab a control lives on. The backend decides what a control *is*
   (`group`); where it appears is presentation, so the mapping is here — a
   group the backend adds later still renders, on a tab named after it. */
const TAB_OF_GROUP = {
  posture: 'posture',
  gait: 'posture',
  motion_nudge: 'drive',
  motion: 'settings',
  lighting: 'settings',
  safety_net: 'sense',
  media: 'sense',
};

const TAB_LABELS = {
  drive: '驾驶',
  posture: '姿态',
  sense: '感知',
  settings: '设置',
  log: '记录',
};

const TAB_ORDER = ['drive', 'posture', 'sense', 'settings'];

function panelFor(robotId, tab) {
  const entry = state.focusMap[robotId];
  if (!entry || !entry.panels) return null;
  let panel = entry.panels.querySelector(`[data-tab="${CSS.escape(tab)}"]`);
  if (!panel) {
    panel = document.createElement('div');
    panel.className = 'panel';
    panel.dataset.tab = tab;
    entry.panels.appendChild(panel);
  }
  return panel;
}

/* The open tab lives in the URL hash, so a view can be linked, bookmarked or
 * reloaded without losing your place. Read once at boot; clicking updates it. */
function tabFromHash() {
  const match = /(?:^|[#&])tab=([a-z_]+)/.exec(location.hash || '');
  return match ? match[1] : null;
}

function showTab(robotId, tab) {
  const entry = state.focusMap[robotId];
  if (!entry) return;
  state.tab = state.tab || {};
  state.tab[robotId] = tab;
  if (location.hash !== `#tab=${tab}`) {
    history.replaceState(null, '', `#tab=${tab}`);
  }
  for (const button of entry.tabs.querySelectorAll('.tab')) {
    button.classList.toggle('active', button.dataset.tab === tab);
  }
  for (const panel of entry.panels.querySelectorAll('.panel')) {
    panel.classList.toggle('active', panel.dataset.tab === tab);
  }
}

function buildTabBar(robotId, tabs) {
  const entry = state.focusMap[robotId];
  if (!entry || !entry.tabs) return;
  entry.tabs.textContent = '';
  for (const tab of tabs) {
    const button = document.createElement('button');
    button.className = 'tab';
    button.dataset.tab = tab;
    button.textContent = TAB_LABELS[tab] || tab;
    const count = state.tabCounts && state.tabCounts[robotId] && state.tabCounts[robotId][tab];
    if (count) {
      const badge = document.createElement('span');
      badge.className = 'count';
      badge.textContent = String(count);
      button.appendChild(badge);
    }
    button.addEventListener('click', () => showTab(robotId, tab));
    entry.tabs.appendChild(button);
  }
}

/* What the robot is reporting, and what nobody has asked it.
 *
 * Three different absences, kept apart: `silent` means the console asked and
 * the topic did not answer; `not attempted` means the console does not read it
 * at all. Conflating them would answer "why is there no gait showing?" with
 * "this robot cannot report its gait" — which is a different claim and may be
 * false.
 */
const SOURCE_LABELS = {
  live: '有数据', silent: '未应答', offline: '未连接',
  starting: '启动中', stalled: '无画面', disabled: '已关闭',
};

function renderSources(robotId) {
  const entry = state.focusMap[robotId];
  const host = entry && entry.sources;
  if (!host) return;

  // Two halves, fetched at different rates on purpose: the *catalogue* (labels,
  // topic names, the reason a source is unread) is static and arrives once with
  // the control descriptors; the *statuses* change and ride the 4 Hz push as a
  // bare `{id: status}` map. Sending the strings four times a second was 21% of
  // the payload for text that changes when a topic starts answering and never
  // otherwise.
  const catalogue = (state.sourceCatalogue) || null;
  const statuses = (state.sources && state.sources[robotId]) || null;
  if (!catalogue || !statuses) { host.textContent = ''; return; }

  host.textContent = '';
  for (const source of catalogue.read || []) {
    const status = statuses.status[source.id] || 'offline';
    const chip = document.createElement('span');
    chip.className = 'source ' + status;
    chip.textContent = `${source.label} · ${SOURCE_LABELS[status] || status}`;
    chip.title = `${source.topic} —— ${SOURCE_LABELS[status] || status}`;
    host.appendChild(chip);
  }
  for (const source of catalogue.not_attempted || []) {
    const chip = document.createElement('span');
    chip.className = 'source untried';
    chip.textContent = `${source.label} · 未读取`;
    chip.title = `${source.topic} —— ${source.why}`;
    host.appendChild(chip);
  }
}

function renderControls(robotId) {
  const entry = state.focusMap[robotId];
  if (!entry || !entry.controls || !entry.tabs || !entry.panels) return;

  const spec = state.controls[robotId];
  entry.controls.textContent = '';
  entry.panels.textContent = '';
  entry.tabs.textContent = '';
  // The data-source list goes in 感知: it is what an operator wants when
  // something is not showing up, and that is the tab they will open. The
  // element itself comes from the template — see the comment there.
  const senseHost = panelFor(robotId, 'sense');
  senseHost.textContent = '';
  // Faults first: it is what the head badge points at, and a diagnostic below
  // a list of data sources is a diagnostic nobody scrolls to.
  if (entry.faultsWrap) senseHost.appendChild(entry.faultsWrap);
  if (entry.sources) senseHost.appendChild(entry.sources);
  entry.controls.classList.add('hidden');

  const controls = (spec && spec.controls) || [];
  if (!controls.length) {
    entry.tabs.classList.add('hidden');
    return;
  }
  entry.tabs.classList.remove('hidden');

  // The drive tab is never empty: nudges come from the server, but the pad
  // itself is always there, so the tab exists even if no nudge control did.
  const buckets = new Map();
  buckets.set('drive', []);
  for (const control of controls) {
    const tab = TAB_OF_GROUP[control.group] || control.group;
    if (!buckets.has(tab)) buckets.set(tab, []);
    buckets.get(tab).push(control);
  }

  const tabs = TAB_ORDER.filter((tab) => buckets.has(tab) && buckets.get(tab).length);
  for (const tab of buckets.keys()) {
    if (!tabs.includes(tab) && buckets.get(tab).length) tabs.push(tab);
  }

  state.tabCounts = state.tabCounts || {};
  state.tabCounts[robotId] = {};
  for (const tab of tabs) state.tabCounts[robotId][tab] = buckets.get(tab).length;

  const canBuzz = controls.some((c) => c.kind === 'button' && c.group === 'motion_nudge');
  buildTabBar(robotId, tabs);

  for (const tab of tabs) {
    const panel = panelFor(robotId, tab);
    if (tab !== 'drive') {
      const row = document.createElement('div');
      row.className = 'ctl-row';
      for (const control of buckets.get(tab)) row.appendChild(controlWidget(robotId, control));
      panel.appendChild(row);
      continue;
    }
    // Drive: nudges + turns, in two rows so "translate" and "rotate" read
    // apart. Split on `degrees` alone — a move control still carries
    // `omega: 0.0`, so testing for its absence dropped every move button out
    // of both buckets and the row rendered empty.
    const turns = buckets.get(tab).filter((c) => (c.args || {}).degrees !== undefined);
    const moves = buckets.get(tab).filter((c) => (c.args || {}).degrees === undefined);
    for (const group of [moves, turns]) {
      if (!group.length) continue;
      const row = document.createElement('div');
      row.className = 'ctl-row';
      for (const control of group) row.appendChild(controlWidget(robotId, control));
      panel.appendChild(row);
    }
  }

  const preferred = (state.tab && state.tab[robotId]) || tabFromHash();
  showTab(robotId, tabs.includes(preferred) ? preferred : tabs[0]);
}

/* ── loading the panel ─────────────────────────────────────────────── */

async function loadControls(robotId) {
  try {
    const spec = await api(`/api/console/controls?robot_id=${encodeURIComponent(robotId)}`);
    state.controls[robotId] = spec || { groups: [], controls: [] };
    if (spec && spec.flags) state.flags[robotId] = spec.flags;
    if (spec && spec.sources) state.sourceCatalogue = spec.sources;
  } catch (_) {
    // A console with no tool bridge still shows video and telemetry.
    state.controls[robotId] = { groups: [], controls: [] };
  }
  renderControls(robotId);
  renderFlags(robotId);
  renderSources(robotId);
}

/* ── live flags ────────────────────────────────────────────────────── */

/* What the robot says about itself. Populated from the read controls and from
   status, so a setting changed elsewhere is still visible here. */
const FLAG_ORDER = ['motion_mode', 'obstacle_avoid', 'brightness', 'volume', 'led'];

/* The server's ramp knows why a robot is or is not moving, and "I pressed W
 * and nothing happened" is the single most common thing an operator needs
 * answered. One word beats a toast. */
const DRIVE_LABELS = {
  idle: '空闲',
  driving: '行驶中',
  stopping: '减速中',
  watchdog: '看门狗停车',
  estopped: '已急停',
  stopped: '已停止',
  // The auto-stand gate, from unitree_harness: "I pressed W and nothing
  // happened" needs an answer, and "waiting for it to stand up" is one.
  standing_wait: '等待站立…',
  stand_timeout: '站立超时',
};

function renderDrive(robotId) {
  const entry = state.focusMap[robotId];
  if (!entry || !entry.drive) return;
  const motion = (state.motion && state.motion[robotId]) || {};
  const driveState = motion.state || 'idle';
  const commanded = motion.commanded || [0, 0, 0];
  const target = motion.target || [0, 0, 0];

  const busy = Math.abs(commanded[0]) + Math.abs(commanded[1]) + Math.abs(commanded[2]) > 0.01;
  entry.drive.textContent = DRIVE_LABELS[driveState] || driveState;
  const stuck = driveState === 'watchdog' || driveState === 'estopped'
    || driveState === 'stand_timeout';
  entry.drive.className = 'flag' + (stuck ? ' bad' : (busy ? ' on' : ''));
  entry.drive.title = busy
    ? `指令 ${commanded.map((v) => v.toFixed(2)).join(' / ')}`
      + `　目标 ${target.map((v) => v.toFixed(2)).join(' / ')}`
    : '没有速度指令';

  // A stick pushed hard while the commanded value stays near zero is a stall,
  // and it is invisible if only one of the two numbers is shown. Mark the dot
  // rather than printing both: the tooltip has the numbers, the colour has the
  // verdict.
  const asked = Math.hypot(target[0], target[1]);
  const sending = Math.hypot(commanded[0], commanded[1]);
  const stalled = asked > 0.25 && sending < asked * 0.35
    && driveState !== 'stopping' && driveState !== 'idle';
  if (entry.stickDot) {
    entry.stickDot.classList.toggle('stalled', stalled);
    if (stalled) entry.drive.title += '　—— 目标远大于实际指令，可能是卡住';
  }
}

/* Where the robot is, relative to where *it* started.
 *
 * Deliberately not a shared map: each robot's odometry has its own origin, so
 * plotting two dogs on one set of axes would invent a relationship that does
 * not exist. The chip shows its own displacement and heading, and says
 * "相对起点" so nobody reads it as a shared frame.
 *
 * Absent when the robot does not publish a position — the field rides on the
 * state topic, which some firmware never sends. A zero would look like a
 * measurement. */
function renderWhere(robotId) {
  const entry = state.focusMap[robotId];
  if (!entry || !entry.where) return;
  const spot = (state.where && state.where[robotId]) || {};

  if (!('x' in spot)) {
    entry.where.classList.add('hidden');
    return;
  }
  const arrow = ['→', '↘', '↓', '↙', '←', '↖', '↑', '↗'][
    ((Math.round((spot.yaw || 0) / (Math.PI / 4)) % 8) + 8) % 8
  ];
  entry.where.classList.remove('hidden');
  entry.where.textContent = `位移 ${spot.distance}m ${arrow}`;
  entry.where.title = `相对本机里程计原点：x=${spot.x} y=${spot.y} 朝向=${(spot.yaw * 57.3).toFixed(0)}°`;
}

/* The live body: attitude, foot contact, and actual speed beside commanded.
 *
 * All three ride on the topics measured arriving on real hardware, so this is
 * populated on the dog rather than only under the mock. Each answers a question
 * an operator actually asks and cannot otherwise see:
 *
 *   is it tipping?          the bubble
 *   which feet are down?    the four bars
 *   is the stick doing
 *   anything?               actual against commanded
 */
const TILT_WARN_DEG = 12;
const TILT_BAD_DEG = 25;

function renderBody(robotId) {
  const entry = state.focusMap[robotId];
  if (!entry || !entry.bodyStrip) return;
  const body = (state.body && state.body[robotId]) || {};
  const motion = (state.motion && state.motion[robotId]) || {};

  const hasAnything = body.attitude || body.feet || body.speed !== undefined
    || body.gait;
  entry.bodyStrip.classList.toggle('hidden', !hasAnything);
  if (!hasAnything) return;

  // ── attitude ──
  const attitude = body.attitude;
  if (attitude && entry.bubbleDot) {
    const roll = attitude.roll_deg || 0;
    const pitch = attitude.pitch_deg || 0;
    const worst = Math.max(Math.abs(roll), Math.abs(pitch));
    // 30 degrees fills the bubble; beyond that it is pinned at the rim, which
    // is the right reading anyway.
    const radius = 9;
    const x = Math.max(-radius, Math.min(radius, (roll / 30) * radius));
    const y = Math.max(-radius, Math.min(radius, (pitch / 30) * radius));
    entry.bubbleDot.style.transform = `translate(${x.toFixed(1)}px, ${y.toFixed(1)}px)`;
    entry.bubble.classList.toggle('tilted', worst >= TILT_WARN_DEG && worst < TILT_BAD_DEG);
    entry.bubble.classList.toggle('falling', worst >= TILT_BAD_DEG);
    entry.bubble.title = `横滚 ${roll}°　俯仰 ${pitch}°`;
    entry.bubble.classList.remove('hidden');
  } else if (entry.bubble) {
    entry.bubble.classList.add('hidden');
  }

  // ── feet ──
  if (Array.isArray(body.feet) && entry.feet) {
    const peak = Math.max(1, ...body.feet);
    entry.feet.textContent = '';
    body.feet.forEach((force, index) => {
      const bar = document.createElement('i');
      bar.style.height = `${Math.max(2, Math.round((force / peak) * 20))}px`;
      // A foot carrying almost nothing is in the air, which is normal in a
      // trot — but it is also what a stumble looks like, so it is drawn.
      bar.classList.toggle('airborne', force < peak * 0.15);
      bar.title = `足 ${index + 1}：${force}`;
      entry.feet.appendChild(bar);
    });
    entry.feet.title = `足端受力 ${body.feet.join(' / ')}`;
  }

  // ── actual against commanded ──
  if (entry.speed) {
    const actual = typeof body.speed === 'number' ? body.speed : null;
    const asked = Math.hypot((motion.commanded || [0, 0])[0], (motion.commanded || [0, 0])[1]);
    if (actual === null && asked < 0.01) {
      entry.speed.textContent = '';
    } else {
      // The two numbers are different units — the topic reports metres per
      // second, the ramp works in normalised stick units — so they are shown
      // side by side and labelled rather than subtracted from each other.
      //
      // Sustained before it is flagged. A robot that has just been told to
      // move has not moved *yet*, and an indicator that cries wolf every time
      // somebody presses W is one an operator learns to ignore — which is
      // worse than not having it.
      const looksStuck = asked > 0.25 && (actual === null || actual < 0.02)
        && motion.state === 'driving';
      state.stallSince = state.stallSince || {};
      const now = Date.now();
      if (!looksStuck) {
        delete state.stallSince[robotId];
      } else if (!state.stallSince[robotId]) {
        state.stallSince[robotId] = now;
      }
      const stalled = looksStuck
        && (now - state.stallSince[robotId]) > 1500;
      entry.speed.textContent = '';
      const label = document.createElement('span');
      label.textContent = '速度 ';
      const actualEl = document.createElement('b');
      actualEl.textContent = actual === null ? '—' : actual.toFixed(2);
      const tail = document.createElement('span');
      tail.textContent = ` m/s　指令 ${asked.toFixed(2)}`;
      entry.speed.appendChild(label);
      entry.speed.appendChild(actualEl);
      entry.speed.appendChild(tail);
      entry.speed.className = 'speed' + (stalled ? ' stalled' : '');
      // **The age, said out loud.** Telemetry refreshes at 1 Hz and the arena
      // camera at 10 Hz, so the panels routinely show numbers sampled a second
      // apart. Without this an operator compares a fresh picture against a
      // stale speed and concludes the dog is not moving, one second after it
      // started.
      const age = body.age_s;
      const stale = body.stale === true;
      if (age != null) {
        const ageEl = document.createElement('span');
        ageEl.className = stale ? 'age stale' : 'age';
        ageEl.textContent = stale ? `　${age}s 前的读数` : '';
        entry.speed.appendChild(ageEl);
      }
      entry.speed.title = stalled
        ? '摇杆推着但机器人没动 —— 可能没站好，或避障拦住了'
        : (stale
          ? `本体速度与斜坡指令（这组数字是 ${age} 秒前的，遥测 1Hz）`
          : '本体速度与斜坡指令');
    }
  }

  if (entry.gait) entry.gait.textContent = body.gait || '';
}

function renderFlags(robotId) {
  const entry = state.focusMap[robotId];
  if (!entry || !entry.flags) return;
  const values = state.flags[robotId] || {};
  entry.flags.textContent = '';

  const shown = FLAG_ORDER.filter((key) => values[key] !== undefined && values[key] !== null);
  for (const key of shown) {
    const chip = document.createElement('span');
    chip.className = 'flag' + (values[key] === true || (typeof values[key] === 'number' && values[key] > 0) ? ' on' : '');
    const label = document.createElement('span');
    label.textContent = FLAG_LABELS[key] || key;
    const value = document.createElement('b');
    value.textContent = typeof values[key] === 'boolean'
      ? (values[key] ? '开' : '关') : String(values[key]);
    chip.appendChild(label);
    chip.appendChild(value);
    entry.flags.appendChild(chip);
  }
}

const FLAG_LABELS = {
  motion_mode: '模式', obstacle_avoid: '避障', brightness: '亮度',
  volume: '音量', led: '灯光', lidar: '雷达',
};

function groupFor(robotId) {
  if (state.mode !== 'split') return null;
  return robotId === firstId() ? 'wasd' : 'ijkl';
}

function axesNow(robotId) {
  const axes = { lx: 0, ly: 0, rx: 0 };
  const group = groupFor(robotId);
  const maps = group ? [KEY_GROUPS[group]] : Object.values(KEY_GROUPS);

  for (const map of maps) {
    for (const key of state.held) {
      const entry = map[key];
      if (!entry) continue;
      axes[entry[0]] += entry[1];
    }
    if (group) break;
  }

  const pointer = state.pointer[robotId];
  if (pointer) {
    axes.lx += pointer.mx || 0;
    axes.ly += pointer.my || 0;
    axes.rx += pointer.turn || 0;
  }
  return {
    lx: Math.max(-1, Math.min(1, axes.lx)),
    ly: Math.max(-1, Math.min(1, axes.ly)),
    rx: Math.max(-1, Math.min(1, axes.rx)),
  };
}

function isIdle(axes) {
  return axes.lx === 0 && axes.ly === 0 && axes.rx === 0;
}

/* In split mode each key group is its own source, so the server can route the
 * two halves independently. In the other modes both groups collapse into one
 * logical operator source. */
/* Send one already-resolved frame for a named source. */
/* ── on-screen sticks ──────────────────────────────────────────────── */

function bindJoystick(robotId, el, kind) {
  let captured = null;

  const read = (event) => {
    const rect = el.getBoundingClientRect();
    const radiusX = rect.width / 2 - 6;
    const radiusY = rect.height / 2 - 6;
    let dx = (event.clientX - (rect.left + rect.width / 2)) / radiusX;
    let dy = (event.clientY - (rect.top + rect.height / 2)) / radiusY;
    if (kind === 'turn') dy = 0;
    // Clamp to the unit *disc*, not a square: a stick pushed into a corner
    // should give full deflection in that direction, not 1.41 of it.
    const length = Math.hypot(dx, dy);
    if (length > 1) { dx /= length; dy /= length; }

    const input = state.pointer[robotId] || (state.pointer[robotId] = { mx: 0, my: 0, turn: 0 });
    if (kind === 'move') {
      input.mx = dx;
      input.my = -dy;   // screen y grows downward; forward is up
    } else {
      input.turn = dx;
    }
    el.classList.add('active');
  };

  const release = () => {
    const input = state.pointer[robotId];
    if (input) {
      if (kind === 'move') { input.mx = 0; input.my = 0; } else { input.turn = 0; }
    }
    el.classList.remove('active');
  };

  el.addEventListener('pointerdown', (event) => {
    event.preventDefault();
    captured = event.pointerId;
    // Without capture a fast drag that leaves the circle stops steering.
    el.setPointerCapture(event.pointerId);
    read(event);
    setFocus(robotId, true);
  });
  el.addEventListener('pointermove', (event) => {
    if (captured === event.pointerId) read(event);
  });
  el.addEventListener('pointerup', release);
  el.addEventListener('pointercancel', release);
  el.addEventListener('lostpointercapture', release);
}

function releaseSticks() {
  for (const input of Object.values(state.pointer)) {
    input.mx = 0; input.my = 0; input.turn = 0;
  }
  for (const entry of Object.values(state.focusMap)) {
    if (entry.joyMove) entry.joyMove.classList.remove('active');
    if (entry.joyTurn) entry.joyTurn.classList.remove('active');
  }
}

/* ── response shaping ──────────────────────────────────────────────── */

/* Two shaping stages sit between the key and the robot: this one, and the
 * server's ramp. They do different jobs and both are needed. This one makes a
 * small stick movement mean a *small* speed — a squared curve is the difference
 * between fine control and a robot that is either stopped or sprinting, and
 * low speed is exactly where you manoeuvre two dogs in a room. The server's
 * ramp then bounds how fast any of it can change.
 *
 * The deadzone is not just noise rejection: real keys and real sticks are
 * digital-ish at the low end, and a robot that creeps when nobody is touching
 * anything is worse than one that ignores the first few percent of travel.
 */
const DEADZONE = 0.08;

function curve(value) {
  const magnitude = Math.abs(value || 0);
  if (magnitude < DEADZONE) return 0;
  const scaled = (magnitude - DEADZONE) / (1 - DEADZONE);
  return Math.sign(value) * scaled * scaled;
}

async function sendFrame(source, frame) {
  state.wasDriving = state.wasDriving || {};
  state.wasDriving[source] = true;
  // Trim scales every source, so it is the one place to make the whole console
  // gentler — which is what you want the first time you drive a real dog.
  const trim = state.trim === undefined ? 1 : state.trim;
  const scaled = (value) =>
    Math.max(-1, Math.min(1, curve(value) * trim));
  try {
    await api('/api/fleet/teleop', {
      method: 'POST',
      body: JSON.stringify({
        source,
        lx: scaled(frame.lx), ly: scaled(frame.ly), rx: scaled(frame.rx),
        ry: scaled(frame.ry), keys: frame.keys || 0,
      }),
    });
  } catch (err) {
    if (err.payload && err.payload.code === 'motion_blocked') {
      toast('急停锁存中，指令已拒绝', 'bad');
      state.held.clear();
    }
  }
}

/* Give up one source. Scoped on purpose: releasing everything here would zero
 * the other hand's dog in split mode. */
async function releaseSource(source) {
  if (!state.wasDriving || !state.wasDriving[source]) return;
  state.wasDriving[source] = false;
  try {
    await api('/api/fleet/release', {
      method: 'POST',
      body: JSON.stringify({ source }),
    });
  } catch (_) { /* the dead-man will catch it */ }
}

/* The gamepad is a source like any other, so all four arbitration modes apply
 * to it for free. While it is being used it takes precedence over the
 * keyboard — a pad at rest is not a request to drive, and a stick nudged by
 * accident should not fight the keys. */
function readGamepad() {
  const GP = window.UnitreeGamepad;
  if (!GP || !navigator.getGamepads) return null;
  const pads = GP.connectedPads(navigator.getGamepads());
  state.pads = pads;
  if (!pads.length) return null;
  const pad = navigator.getGamepads()[pads[0]];
  if (!pad) return null;
  state.padWarning = GP.mappingWarning(pad);
  return GP.frameFor(pad);
}

async function tick() {
  if (!state.fleet) return;

  const GP = window.UnitreeGamepad;
  const padFrame = readGamepad();
  if (GP && padFrame && !GP.isIdle(padFrame)) {
    await sendFrame('gamepad:0', padFrame);
    return;
  }
  await releaseSource('gamepad:0');

  // Show the live axes on every pen the current mode is driving, so "Both"
  // makes both sticks move and "Split" makes each follow its own keys.
  const shown = state.mode === 'both'
    ? Object.keys(state.focusMap)
    : [state.focused].filter(Boolean);
  for (const id of shown) renderStick(id, axesNow(id));

  const sources = state.mode === 'split' ? ['wasd', 'ijkl'] : ['keyboard'];

  for (const source of sources) {
    let axes = { lx: 0, ly: 0, rx: 0 };
    if (state.mode === 'split') {
      const group = KEY_GROUPS[source];
      for (const key of state.held) {
        const entry = group[key];
        if (!entry) continue;
        axes[entry[0]] += entry[1];
      }
      axes = {
        lx: Math.max(-1, Math.min(1, axes.lx)),
        ly: Math.max(-1, Math.min(1, axes.ly)),
        rx: Math.max(-1, Math.min(1, axes.rx)),
      };
    } else {
      for (const robotId of Object.keys(state.focusMap)) {
        const candidate = axesNow(robotId);
        if (!isIdle(candidate)) { axes = candidate; break; }
      }
    }

    // Only the split groups have on-screen pads of their own; the pad drives
    // its own robot directly, so skip here.
    if (isIdle(axes)) {
      await releaseSource(source);
      continue;
    }
    await sendFrame(source, axes);
  }
}

function keyFor(event) {
  const key = (event.key || '').toLowerCase();
  for (const group of Object.values(KEY_GROUPS)) {
    if (key in group) return key;
  }
  return null;
}

function wireInput() {
  window.addEventListener('keydown', (event) => {
    if (event.target && ['INPUT', 'SELECT', 'TEXTAREA'].includes(event.target.tagName)) return;
    const key = keyFor(event);
    if (!key) return;
    event.preventDefault();
    state.held.add(key);
  });

  window.addEventListener('keyup', (event) => {
    const key = keyFor(event);
    if (!key) return;
    state.held.delete(key);
  });

  // Losing focus while holding a key would otherwise leave the robot driving.
  window.addEventListener('blur', () => {
    // A drag that is interrupted by the window losing focus has no pointerup,
    // so the stick would stay deflected with nobody touching it.
    releaseSticks();
    state.held.clear();
    releaseAll();
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { state.held.clear(); releaseAll(); }
  });
}

async function releaseAll() {
  try { await api('/api/fleet/release', { method: 'POST' }); } catch (_) { /* dead-man */ }
}

function wireButtons() {
  document.getElementById('btn-stop-all').addEventListener('click', stopAll);
  document.getElementById('btn-estop').addEventListener('click', () => estop(''));
  document.getElementById('btn-estop-release').addEventListener('click', estopRelease);
  document.getElementById('input-mode').addEventListener('change', (event) => {
    releaseAll();
    setMode(event.target.value);
  });
}

/* ── live status ───────────────────────────────────────────────────── */

/* Status arrives over a WebSocket, but the console must not depend on it.
 *
 * Rendering the page in a real browser is what surfaced this: the socket was
 * closed, and every live field — link pills, battery, faults, the fleet
 * summary — sat on its initial placeholder text forever. A proxy that blocks
 * upgrades, a locked-down network, or a hiccup would leave an operator
 * staring at "连接中…" while the HTTP API was perfectly reachable.
 *
 * So polling is the fallback and the socket is the optimisation: the poll
 * loop runs always and simply stands down while the socket is open. */
function startPolling() {
  if (state.pollTimer) return;
  state.pollTimer = setInterval(async () => {
    if (state.wsOpen) return;
    try {
      renderStatus(await api('/api/fleet/status'));
    } catch (_) { /* keep trying; the next tick may succeed */ }
  }, 1000);
}

function wireSocket() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const suffix = TOKEN ? '?token=' + encodeURIComponent(TOKEN) : '';
  let socket;
  try {
    socket = new WebSocket(`${proto}//${location.host}/ws${suffix}`);
  } catch (_) {
    return;  // polling already covers us
  }

  socket.addEventListener('open', () => {
    state.wsOpen = true;
    document.getElementById('link-state').textContent = '';
  });
  socket.addEventListener('message', (event) => {
    try { renderStatus(JSON.parse(event.data)); } catch (_) { /* ignore */ }
  });
  socket.addEventListener('close', () => {
    state.wsOpen = false;
    startPolling();
    setTimeout(wireSocket, 2000);
  });
  socket.addEventListener('error', () => socket.close());
}

/* ── fleet actions ─────────────────────────────────────────────────── */

/* "Make them both do X" is one intent, so it is one click. The calls go out
 * concurrently server-side; doing it by clicking two per-dog buttons a second
 * apart is not the same thing as doing it together. */
/* ── the routine editor ────────────────────────────────────────────── */

/* A routine is a document, so it should be authorable from the page. The
 * branch this idea came from kept its sequences as Python lists, and the
 * consequence is on record: nobody but its author ever wrote one.
 *
 * Reordering is up/down buttons rather than drag-and-drop. Dragging is nicer
 * with a mouse and unusable with a keyboard or a touch screen, and a console
 * that two people share should not require one specific input device.
 */
const EDITOR_POLICIES = [
  { value: 'all', label: '全部做到' },
  { value: 'any', label: '任一做到' },
  { value: 'best_effort', label: '尽力而为' },
];

function editorSteps() {
  state.editor = state.editor || { steps: [], name: '', loop: false, editingId: '' };
  return state.editor.steps;
}

function renderEditorCount() {
  const count = document.querySelector('[data-role="editor-count"]');
  if (count) count.textContent = editorSteps().length ? `（${editorSteps().length} 步）` : '';
}

async function loadPalette() {
  const host = document.getElementById('editor-palette');
  if (!host) return;
  host.textContent = '';

  const entries = [];
  // Fleet actions first: they are already the right shape, and they are the
  // ones an operator reaches for when writing "both dogs do X".
  try {
    const fleet = await api('/api/fleet/actions');
    for (const action of [...(fleet.posture || []), ...(fleet.named || [])]) {
      entries.push({ label: action.label, tool: action.tool, args: action.args || {},
                     title: action.title || '' });
    }
  } catch (_) { /* a console with no bridge still gets the per-dog controls */ }

  // Then the per-dog controls that are one-shot actions rather than readers.
  const first = Object.keys(state.focusMap)[0];
  if (first) {
    try {
      const spec = await api(`/api/console/controls?robot_id=${encodeURIComponent(first)}`);
      for (const control of spec.controls || []) {
        if (control.read || control.kind === 'range') continue;
        if (control.kind === 'select' || control.kind === 'toggle') continue;
        entries.push({ label: control.label, tool: control.tool,
                       args: control.args || {}, title: control.title || '' });
      }
    } catch (_) { /* nothing to add */ }
  }

  for (const entry of entries) {
    const button = document.createElement('button');
    button.className = 'btn';
    button.textContent = entry.label;
    button.title = entry.title || `加入 ${entry.label}`;
    button.addEventListener('click', () => {
      editorSteps().push({
        label: entry.label, tool: entry.tool, args: { ...entry.args },
        policy: 'all', seconds: 3,
      });
      renderEditor();
    });
    host.appendChild(button);
  }
}

function renderEditor() {
  const host = document.getElementById('editor-steps');
  const empty = document.getElementById('editor-empty');
  if (!host) return;
  host.textContent = '';
  const steps = editorSteps();
  if (empty) empty.classList.toggle('hidden', steps.length > 0);
  renderEditorCount();

  steps.forEach((step, index) => {
    const row = document.createElement('li');
    row.className = 'step-row';
    row.draggable = true;
    row.dataset.index = String(index);

    // **Dragging is added, not substituted.** The existing comment on this
    // editor argued against drag-and-drop because it is "nicer with a mouse and
    // unusable with a keyboard or a touch screen, and a console that two people
    // share should not require one specific input device" — and that reasoning
    // still holds. So both exist: the handle and the arrows do the same thing,
    // and neither is the only way.
    const grip = document.createElement('span');
    grip.className = 'grip';
    grip.textContent = '⠿';
    grip.title = '拖动排序（也可以用 ↑ ↓ 按钮）';
    grip.setAttribute('aria-hidden', 'true');

    const order = document.createElement('span');
    order.className = 'order';
    order.textContent = String(index + 1);

    const what = document.createElement('span');
    what.className = 'what';
    what.textContent = step.label;
    what.title = `${step.tool} ${JSON.stringify(step.args)}`;

    const seconds = document.createElement('input');
    seconds.type = 'number';
    seconds.min = '0.2';
    seconds.max = '60';
    seconds.step = '0.5';
    seconds.value = String(step.seconds);
    seconds.title = '这一步之后等多久（秒）';
    seconds.addEventListener('change', () => {
      const value = Number(seconds.value);
      step.seconds = Number.isFinite(value) ? Math.max(0.2, Math.min(60, value)) : 3;
      seconds.value = String(step.seconds);
    });

    const policy = document.createElement('select');
    policy.title = '这一步要几只狗做到才算成功';
    for (const option of EDITOR_POLICIES) {
      const element = document.createElement('option');
      element.value = option.value;
      element.textContent = option.label;
      policy.appendChild(element);
    }
    policy.value = step.policy;
    policy.addEventListener('change', () => { step.policy = policy.value; });

    const up = document.createElement('button');
    up.className = 'icon';
    up.textContent = '↑';
    up.title = '上移';
    up.disabled = index === 0;
    up.addEventListener('click', () => {
      [steps[index - 1], steps[index]] = [steps[index], steps[index - 1]];
      renderEditor();
    });

    const down = document.createElement('button');
    down.className = 'icon';
    down.textContent = '↓';
    down.title = '下移';
    down.disabled = index === steps.length - 1;
    down.addEventListener('click', () => {
      [steps[index + 1], steps[index]] = [steps[index], steps[index + 1]];
      renderEditor();
    });

    const remove = document.createElement('button');
    remove.className = 'icon danger';
    remove.textContent = '✕';
    remove.title = '移除';
    remove.addEventListener('click', () => {
      steps.splice(index, 1);
      renderEditor();
    });

    row.appendChild(grip);
    row.appendChild(order);
    row.appendChild(what);
    row.appendChild(seconds);
    row.appendChild(policy);
    row.appendChild(up);
    row.appendChild(down);
    row.appendChild(remove);
    host.appendChild(row);
  });
  wireStepDrag();
}

/* Drag-to-reorder, on the rows the renderer just built.
 *
 * `renderEditor` rebuilds every row, so the listeners have to be re-bound each
 * time — which is exactly the pattern that leaked `#routine-stop` once (a new
 * handler per call, so N actions meant N stop requests). The difference is that
 * these handlers are attached to elements that are **thrown away and rebuilt**,
 * not to a long-lived element, so they cannot accumulate.
 */
function wireStepDrag() {
  const host = document.getElementById('editor-steps');
  if (!host) return;
  let dragging = null;

  host.querySelectorAll('.step-row').forEach((row) => {
    row.addEventListener('dragstart', (event) => {
      dragging = row;
      row.classList.add('dragging');
      // Firefox refuses to start a drag without data on the transfer.
      if (event.dataTransfer) event.dataTransfer.setData('text/plain', row.dataset.index);
    });
    row.addEventListener('dragend', () => {
      row.classList.remove('dragging');
      host.querySelectorAll('.step-row').forEach((r) => r.classList.remove('drop-before'));
      dragging = null;
    });
    row.addEventListener('dragover', (event) => {
      if (!dragging || dragging === row) return;
      event.preventDefault();
      row.classList.add('drop-before');
    });
    row.addEventListener('dragleave', () => row.classList.remove('drop-before'));
    row.addEventListener('drop', (event) => {
      event.preventDefault();
      if (!dragging || dragging === row) return;
      const steps = editorSteps();
      const from = Number(dragging.dataset.index);
      const to = Number(row.dataset.index);
      if (!Number.isFinite(from) || !Number.isFinite(to) || from === to) return;
      const [moved] = steps.splice(from, 1);
      steps.splice(to, 0, moved);
      renderEditor();
    });
  });
}

function editorStatus(message, kind) {
  const status = document.getElementById('editor-status');
  if (!status) return;
  status.textContent = message || '';
  status.className = kind === 'bad' ? 'muted warn' : 'muted';
}

/* A stable id from the name, so saving the same name twice updates rather than
 * accumulating near-duplicates. */
function editorRoutineId(name) {
  const ascii = (name || '').trim().toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '');
  if (ascii) return ascii.slice(0, 40);
  // A Chinese name has no ASCII to derive from; fall back to a hash so the id
  // is still stable across saves of the same name.
  let hash = 0;
  for (const char of name || '') hash = (hash * 31 + char.codePointAt(0)) >>> 0;
  return `routine_${hash.toString(36)}`;
}

function editorDocument() {
  const name = (document.getElementById('editor-name').value || '').trim();
  const steps = editorSteps();
  if (!name) throw new Error('先给它起个名字');
  if (!steps.length) throw new Error('序列是空的');
  // **An edited routine keeps its id.** The id is otherwise derived from the
  // name, so renaming a saved routine would create a second one and leave the
  // first behind — and `save_routine` would happily accept both, since the id
  // is new and does not shadow a built-in.
  const editing = (state.editor && state.editor.editingId) || '';
  return {
    id: editing || editorRoutineId(name),
    label: name,
    loop: Boolean(document.getElementById('editor-loop').checked),
    steps: steps.map((step) => ({
      label: step.label, tool: step.tool, args: step.args,
      policy: step.policy, seconds: step.seconds,
    })),
  };
}

async function saveEditor() {
  try {
    const saved = await api('/api/fleet/routines/save', {
      method: 'POST',
      body: JSON.stringify(editorDocument()),
    });
    editorStatus(`已保存「${saved.label}」`, 'ok');
    await loadRoutines();
    return saved;
  } catch (err) {
    editorStatus(err.message, 'bad');
    return null;
  }
}

/* Load a saved routine back into the editor.
 *
 * **This was the editor's real problem.** `state.editor` was only ever assigned
 * on boot and on clear, so a saved routine could be created and never changed or
 * removed — "edit yesterday's routine" was simply not possible, which makes an
 * editor a one-way pipe with a form on the end.
 *
 * A built-in can be opened too, and saving it under a new name is the intended
 * way to say "like 走正方形 but with three laps on a different day".
 */
function loadIntoEditor(routine) {
  state.editor = {
    steps: (routine.steps || []).map((step) => ({
      label: step.label,
      tool: step.tool,
      args: { ...(step.args || {}) },
      policy: step.policy || 'all',
      seconds: step.seconds || 3,
    })),
    name: routine.label || '',
    loop: Boolean(routine.loop),
    editingId: routine.builtin ? '' : routine.id,
  };
  document.getElementById('editor-name').value = state.editor.name;
  document.getElementById('editor-loop').checked = state.editor.loop;
  const panel = document.getElementById('editor');
  if (panel) panel.open = true;
  editorStatus(
    routine.builtin
      ? `已载入内置编排「${routine.label}」—— 改完保存会存成你自己的`
      : `正在编辑「${routine.label}」`,
    'ok',
  );
  renderEditor();
}

async function deleteCustomRoutine(routine) {
  // **A confirm, because there is no undo.** The editor can recreate a routine
  // but not the one you have just lost.
  const ok = window.confirm(`删除编排「${routine.label}」？删了就找不回来了。`);
  if (!ok) return;
  try {
    await api(`/api/fleet/routines/${encodeURIComponent(routine.id)}`, {
      method: 'DELETE',
    });
    if (state.editor && state.editor.editingId === routine.id) {
      state.editor = { steps: [], name: '', loop: false, editingId: '' };
      renderEditor();
    }
    editorStatus(`已删除「${routine.label}」`, 'ok');
    await loadRoutines();
  } catch (err) {
    editorStatus(`删除失败：${err.message}`, 'bad');
  }
}

/* The operator's own routines, each with 编辑 and 删除.
 *
 * A built-in cannot be edited in place — `save_routine` refuses to shadow one,
 * and that rule is right: the built-ins are the reference, and a fleet whose
 * 走正方形 means something different on each console is worse than one that
 * cannot change it.
 */
async function loadCustomRoutines() {
  const host = document.getElementById('fleet-custom');
  if (!host) return;
  let spec;
  try {
    spec = await api('/api/fleet/routines');
  } catch (_) { return; }
  const mine = (spec.routines || []).filter((routine) => !routine.builtin);
  host.textContent = '';
  host.classList.toggle('hidden', !mine.length);
  if (!mine.length) return;

  const label = document.createElement('span');
  label.className = 'group-label';
  label.textContent = '我的';
  host.appendChild(label);

  for (const routine of mine) {
    const chip = document.createElement('span');
    chip.className = 'routine-chip';

    const button = document.createElement('button');
    button.className = 'btn routine-btn';
    button.dataset.routine = routine.id;
    labelled(
      button,
      ROUTINE_ICONS[routine.id] || '📝',
      `${routine.label} · ${routine.steps.length}步/${Math.round(routine.seconds)}s`,
    );
    button.title = routine.title || routine.label;
    button.addEventListener('click', () => startRoutine(routine, button));
    chip.appendChild(button);

    const edit = document.createElement('button');
    edit.className = 'btn knob-btn';
    edit.textContent = '编辑';
    edit.title = `把「${routine.label}」载入编辑器`;
    edit.addEventListener('click', () => loadIntoEditor(routine));
    chip.appendChild(edit);

    const remove = document.createElement('button');
    remove.className = 'btn knob-btn danger';
    remove.textContent = '✕';
    remove.setAttribute('aria-label', `删除${routine.label}`);
    remove.title = `删除「${routine.label}」`;
    remove.addEventListener('click', () => deleteCustomRoutine(routine));
    chip.appendChild(remove);

    host.appendChild(chip);
  }
}

async function runEditor() {
  const saved = await saveEditor();
  if (!saved) return;
  try {
    await api('/api/fleet/routine', {
      method: 'POST',
      body: JSON.stringify({ id: saved.id }),
    });
    editorStatus(`正在运行「${saved.label}」`, 'ok');
  } catch (err) {
    editorStatus(err.message, 'bad');
  }
}

function wireEditor() {
  const panel = document.getElementById('editor');
  // Same deep-link affordance as the tabs: `#editor` opens it, so the editor
  // can be bookmarked and so a screenshot of it is possible at all.
  if (panel && /(?:^|[#&])editor\b/.test(location.hash || '')) panel.open = true;

  const save = document.getElementById('editor-save');
  const run = document.getElementById('editor-run');
  const clear = document.getElementById('editor-clear');
  if (save) save.addEventListener('click', saveEditor);
  if (run) run.addEventListener('click', runEditor);
  if (clear) clear.addEventListener('click', () => {
    state.editor = { steps: [], name: '', loop: false, editingId: '' };
    document.getElementById('editor-name').value = '';
    document.getElementById('editor-loop').checked = false;
    editorStatus('');
    renderEditor();
  });
  const restored = localStorage.getItem('unitree.editor.name');
  if (restored) document.getElementById('editor-name').value = restored;
  const nameInput = document.getElementById('editor-name');
  if (nameInput) {
    nameInput.addEventListener('change', () => {
      localStorage.setItem('unitree.editor.name', nameInput.value);
    });
  }
}

/* ── the arena map ─────────────────────────────────────────────────── */

/* A top-down view of the calibrated arena with both dogs in it.
 *
 * This is the one panel in the console that shows a *relationship* rather than
 * a value: the two dogs in a single coordinate system, which is what odometry
 * cannot produce and a camera cannot help producing.
 *
 * Drawn from the calibration's own bounds rather than a fixed extent — an arena
 * the drawing disagrees with is a map that lies about distances. */
const ARENA_STATES = {
  off: ['未配置', ''],
  starting: ['启动中', 'warn'],
  ok: ['实时', 'on'],
  camera_failed: ['相机打不开', 'bad'],
  camera_stale: ['画面停更', 'bad'],
  no_markers: ['没有登记标记', 'warn'],
  uncalibrated: ['未标定', 'warn'],
  error: ['检测出错', 'bad'],
};

const DOG_COLOURS = ['#58a6ff', '#d29922', '#3ecf8e', '#ef4444'];

function renderArena(arena) {
  const panel = document.getElementById('arena-panel');
  const stateEl = document.getElementById('arena-state');
  const detail = document.getElementById('arena-detail');
  const legend = document.getElementById('arena-legend');
  const svg = document.getElementById('arena-map');
  if (!panel || !svg) return;
  if (!arena) { panel.classList.add('hidden'); return; }
  panel.classList.remove('hidden');

  // Opened automatically when there is something to look at, and left alone
  // otherwise. A map that is working is the panel an operator wants; a map that
  // is not is a row they should be able to ignore.
  if (arena.state === 'ok' && !panel.dataset.touched) panel.open = true;
  // A deep link for the same reason the editor has one: `#arena` is how a
  // screenshot of it is possible at all, and how it gets bookmarked.
  if (/(?:^|[#&])arena\b/.test(location.hash || '')) {
    panel.open = true;
    panel.dataset.touched = '1';
  }

  const [label, tone] = ARENA_STATES[arena.state] || [arena.state, ''];
  stateEl.textContent = label;
  stateEl.className = 'flag' + (tone ? ' ' + tone : '');

  const calibration = arena.calibration;
  if (arena.state !== 'ok') {
    // Say which of the several reasons it is. "An empty field" answers none of
    // them, and two dogs out of shot look exactly like a camera that is off.
    const why = {
      off: arena.detail || '没有配置场地相机',
      starting: '正在等第一帧',
      camera_failed: arena.detail || '相机打不开',
      camera_stale: '超过两秒没有新画面',
      no_markers: arena.detail || '还没有登记标记',
      uncalibrated: arena.detail || '点「标定场地」对着一帧画面点四个角',
      error: arena.detail || '检测出错',
    }[arena.state] || arena.detail || '';
    detail.textContent = why;
    svg.textContent = '';
    legend.textContent = '';
    if (calibration && calibration.residual_m > 0) {
      const note = document.createElement('span');
      note.className = 'warn';
      note.textContent = `标定残差 ${calibration.residual_m} m`;
      legend.appendChild(note);
    }
    return;
  }

  const bounds = (calibration && calibration.bounds) || [0, 0, 4, 4];
  const [minX, minY, maxX, maxY] = bounds;
  const width = Math.max(0.1, maxX - minX);
  const height = Math.max(0.1, maxY - minY);
  const pad = 26;
  const size = 400;
  // One scale for both axes, so the map is not stretched. A stretched map makes
  // a square formation look rectangular and the operator cannot tell.
  const scale = Math.min((size - pad * 2) / width, (size - pad * 2) / height);
  const toX = (x) => pad + (x - minX) * scale;
  // Screen y grows downward and arena y grows upward.
  const toY = (y) => size - pad - (y - minY) * scale;

  svg.textContent = '';

  const grid = document.createElementNS('http://www.w3.org/2000/svg', 'g');
  for (let x = Math.ceil(minX); x <= maxX; x += 1) {
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line.setAttribute('x1', toX(x)); line.setAttribute('y1', toY(minY));
    line.setAttribute('x2', toX(x)); line.setAttribute('y2', toY(maxY));
    line.setAttribute('class', 'grid');
    grid.appendChild(line);
  }
  for (let y = Math.ceil(minY); y <= maxY; y += 1) {
    const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
    line.setAttribute('x1', toX(minX)); line.setAttribute('y1', toY(y));
    line.setAttribute('x2', toX(maxX)); line.setAttribute('y2', toY(y));
    line.setAttribute('class', 'grid');
    grid.appendChild(line);
  }
  svg.appendChild(grid);

  const edge = document.createElementNS('http://www.w3.org/2000/svg', 'rect');
  edge.setAttribute('x', toX(minX)); edge.setAttribute('y', toY(maxY));
  edge.setAttribute('width', width * scale); edge.setAttribute('height', height * scale);
  edge.setAttribute('class', 'edge');
  svg.appendChild(edge);

  const poses = arena.poses || {};
  const names = Object.keys(poses).sort();

  // The gap, drawn before the dogs so the arrows sit on top of it.
  if (arena.separation && names.length >= 2) {
    const a = poses[arena.separation.a];
    const b = poses[arena.separation.b];
    if (a && b) {
      const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      line.setAttribute('x1', toX(a.x)); line.setAttribute('y1', toY(a.y));
      line.setAttribute('x2', toX(b.x)); line.setAttribute('y2', toY(b.y));
      line.setAttribute('class', 'gap');
      svg.appendChild(line);
      const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
      text.setAttribute('x', (toX(a.x) + toX(b.x)) / 2);
      text.setAttribute('y', (toY(a.y) + toY(b.y)) / 2 - 6);
      text.setAttribute('class', 'gap-label');
      text.textContent = `${arena.separation.distance_m} m`;
      svg.appendChild(text);
    }
  }

  names.forEach((name, index) => {
    const pose = poses[name];
    const colour = DOG_COLOURS[index % DOG_COLOURS.length];
    // The arrow points where the nose points, in screen terms: arena +y is up.
    // 26 px is about 0.4 m at the scale a 4 m arena draws at — dog-sized, which
    // is the point: the arrows are the subject, and two dots would say less
    // than one arrow each.
    const length = 26;
    const rad = (pose.heading_deg * Math.PI) / 180;
    const tipX = toX(pose.x) + Math.cos(rad) * length;
    const tipY = toY(pose.y) - Math.sin(rad) * length;
    const leftX = toX(pose.x) + Math.cos(rad + 2.5) * length * 0.5;
    const leftY = toY(pose.y) - Math.sin(rad + 2.5) * length * 0.5;
    const rightX = toX(pose.x) + Math.cos(rad - 2.5) * length * 0.5;
    const rightY = toY(pose.y) - Math.sin(rad - 2.5) * length * 0.5;

    const arrow = document.createElementNS('http://www.w3.org/2000/svg', 'polygon');
    arrow.setAttribute('points', `${tipX},${tipY} ${leftX},${leftY} ${rightX},${rightY}`);
    arrow.setAttribute('class', 'dog');
    arrow.setAttribute('fill', colour);
    arrow.appendChild(document.createElementNS('http://www.w3.org/2000/svg', 'title'))
      .textContent = `${name}: (${pose.x}, ${pose.y}) 朝 ${pose.heading_deg}°`;
    svg.appendChild(arrow);

    const label = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    label.setAttribute('x', toX(pose.x) + 12);
    label.setAttribute('y', toY(pose.y) + 4);
    label.setAttribute('class', 'dog-label');
    label.setAttribute('fill', colour);
    label.textContent = name;
    svg.appendChild(label);
  });

  // Names the dogs the frame did not contain, rather than leaving a gap the
  // operator has to notice.
  for (const name of arena.missing || []) {
    const text = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    text.setAttribute('x', pad);
    text.setAttribute('y', size - 6);
    text.setAttribute('class', 'gap-label');
    text.textContent = `${name} 不在画面里`;
    svg.appendChild(text);
  }

  detail.textContent = arena.age_s != null ? `${arena.age_s}s 前` : '';
  legend.textContent = '';

  // **Say it on the map, not only when 对齐 refuses.**
  //
  // A tag below the 64 px floor is not a missing measurement — the position is
  // real and the *identity* is not certain. Two tags four bits apart in
  // `DICT_APRILTAG_36h11` decode to each other under blur, which puts one dog's
  // position on the other. The map looks completely normal in that state, so
  // the warning has to be on the map.
  // **Which tag belongs to which robot, and whether it was guessed.**
  //
  // The console derives tag→robot from roster order when no manifest is
  // configured, and a roster listing dog2 first attributes every pose to the
  // other dog — positions, headings and the separation all look plausible. The
  // warning about that existed on the server and no page code read it, so a
  // guessed mapping looked exactly like a printed one.
  if (arena.marker_source && arena.marker_source !== 'manifest') {
    const guess = document.createElement('span');
    guess.className = 'pair warn';
    guess.textContent = `⚠️ 标签对应关系是猜的（按名单顺序）：${arena.marker_note || ''}`;
    legend.appendChild(guess);
  } else if (arena.marker_note) {
    // A manifest is in force and disagrees with the roster.
    const clash = document.createElement('span');
    clash.className = 'pair warn';
    clash.textContent = `⚠️ ${arena.marker_note}`;
    legend.appendChild(clash);
  }

  if (arena.illegible && arena.illegible.length) {
    const warn = document.createElement('span');
    warn.className = 'pair warn';
    const px = arena.illegible
      .map((name) => {
        const pose = (arena.poses || {})[name] || {};
        return `${name} ${Math.round(pose.tag_px || 0)}px`;
      })
      .join('、');
    warn.textContent =
      `⚠️ 标签太小，认不出是哪只狗（${px}）—— 位置是真的，身份不一定。`
      + `读错 id 会把一只狗算到另一只身上。让狗离相机近一点，或者用更大的标签。`;
    legend.appendChild(warn);
  }

  if (arena.separation) {
    const row = document.createElement('span');
    row.className = 'pair';
    const dist = document.createElement('b');
    dist.textContent = `${arena.separation.distance_m} m`;
    const yaw = document.createElement('b');
    yaw.textContent = `${arena.separation.yaw_spread_deg}°`;
    row.append('间距 ', dist, '　朝向差 ', yaw);
    legend.appendChild(row);

    // Which way to turn to face the other dog, which is what a formation
    // command needs and a distance alone does not give: "close the gap" is
    // ambiguous without knowing which way the gap is.
    const bearing = document.createElement('span');
    bearing.className = 'pair';
    const arrow = ['→', '↗', '↑', '↖', '←', '↙', '↓', '↘'][
      ((Math.round(arena.separation.bearing_deg_from_a_to_b / 45) % 8) + 8) % 8
    ];
    bearing.textContent =
      `${arena.separation.b} 在 ${arena.separation.a} 的 ${arrow} `
      + `${arena.separation.bearing_deg_from_a_to_b}°`;
    legend.appendChild(bearing);

    // A separation measured from a floor calibration is inflated by the tag
    // height. Saying so beside the number is the difference between a
    // measurement and a number somebody trusts too much.
    if (calibration && calibration.scale_factor > 1.01) {
      const note = document.createElement('span');
      note.className = 'warn';
      const inflated = (arena.separation.distance_m / calibration.scale_factor).toFixed(2);
      note.textContent = `（地面标定，距离偏大 ×${calibration.scale_factor} —— 实际约 ${inflated} m）`;
      legend.appendChild(note);
    }
  }
  if (calibration && calibration.residual_m > 0) {
    const note = document.createElement('span');
    note.className = 'muted';
    note.textContent = `标定残差 ${calibration.residual_m} m`;
    legend.appendChild(note);
  }

  // Without a scale note the grid is decoration. With one it is a ruler, and
  // "how far apart are they" stops needing the number to be read.
  const ruler = document.createElement('span');
  ruler.className = 'muted';
  ruler.textContent = '每格 1 米 · 箭头指向狗头';
  legend.appendChild(ruler);
}

/* ── calibrating the arena ─────────────────────────────────────────── */

/* Click the four corners on a live frame.
 *
 * This is the last place that needed a hand-written JSON file, and hand-writing
 * one is how a session starts with a calibration that is subtly wrong: four
 * numbers copied from a phone photo, in an order nobody wrote down.
 *
 * **The click coordinates are converted back to native frame pixels.** The
 * frame is displayed at whatever width the panel gets, and recording the
 * display coordinates would make the calibration wrong by exactly the ratio
 * between the two — a mistake that produces a map which looks entirely
 * plausible and is off by a factor. The frame's own size comes from the server
 * beside it.
 */
const CALIB_ORDER = ['左上 (0)', '右上 (1)', '右下 (2)', '左下 (3)'];

function defaultWorldPoints(width, depth) {
  // The order the printed plan uses: anticlockwise from the far-left corner,
  // with arena +y away from the camera.
  return [[0, depth], [width, depth], [width, 0], [0, 0]];
}

function renderCalibrationPoints(state) {
  const list = document.getElementById('calib-points');
  if (!list) return;
  list.textContent = '';
  for (let index = 0; index < 4; index += 1) {
    const item = document.createElement('li');
    const point = state.points[index];
    const world = state.world[index];
    item.className = point ? 'done' : '';
    item.textContent = point
      ? `${CALIB_ORDER[index]} → 画面 (${Math.round(point[0])}, ${Math.round(point[1])})　场地 (${world[0]}, ${world[1]}) m`
      : `${CALIB_ORDER[index]} —— 还没点`;
    list.appendChild(item);
  }
}

function renderCalibrationOverlay(state) {
  const svg = document.getElementById('calib-overlay');
  const image = document.getElementById('calib-image');
  if (!svg || !image || !state.size[0]) return;
  // The overlay is positioned over the image and uses the image's own pixel
  // space as its viewBox, so the marks land where they were clicked whatever
  // width the panel happens to be.
  svg.setAttribute('viewBox', `0 0 ${state.size[0]} ${state.size[1]}`);
  svg.textContent = '';
  state.points.forEach((point, index) => {
    const ring = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    ring.setAttribute('cx', point[0]); ring.setAttribute('cy', point[1]);
    ring.setAttribute('r', 14); ring.setAttribute('class', 'marker-ring');
    svg.appendChild(ring);

    const dot = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    dot.setAttribute('cx', point[0]); dot.setAttribute('cy', point[1]);
    dot.setAttribute('r', 4); dot.setAttribute('class', 'marker-dot');
    svg.appendChild(dot);

    const number = document.createElementNS('http://www.w3.org/2000/svg', 'text');
    number.setAttribute('x', point[0]); number.setAttribute('y', point[1] - 20);
    number.setAttribute('class', 'marker-num');
    number.textContent = String(index);
    svg.appendChild(number);
  });
}

const calibration = { active: false, points: [], size: [0, 0], world: defaultWorldPoints(4, 4) };

function setCalibrationWorldFromInputs() {
  const width = Number(document.getElementById('calib-width').value) || 4;
  const depth = Number(document.getElementById('calib-depth').value) || 4;
  calibration.world = defaultWorldPoints(width, depth);
  renderCalibrationPoints(calibration);
}

function onCalibrationClick(event) {
  if (!calibration.active) return;
  const svg = document.getElementById('calib-overlay');
  const rect = svg.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  // Display pixels back to frame pixels — the conversion this whole flow turns
  // on. Clamped, because a click on the border rounds outside the frame and a
  // negative coordinate is a calibration nobody can debug.
  const x = Math.max(0, Math.min(calibration.size[0],
    ((event.clientX - rect.left) / rect.width) * calibration.size[0]));
  const y = Math.max(0, Math.min(calibration.size[1],
    ((event.clientY - rect.top) / rect.height) * calibration.size[1]));

  if (calibration.points.length >= 4) calibration.points = [];
  calibration.points.push([x, y]);
  renderCalibrationPoints(calibration);
  renderCalibrationOverlay(calibration);

  const save = document.getElementById('calib-save');
  if (save) save.disabled = calibration.points.length !== 4;
  const hint = document.getElementById('calib-hint');
  if (hint) {
    hint.textContent = calibration.points.length === 4
      ? '四个点齐了，点保存'
      : `已点 ${calibration.points.length}/4 —— 下一个是 ${CALIB_ORDER[calibration.points.length]}`;
  }
}

async function startCalibration() {
  const body = document.getElementById('calib-body');
  const hint = document.getElementById('calib-hint');
  try {
    const spec = await api('/api/arena/calibration');
    if (!spec.frame_size) {
      hint.textContent = '没有画面 —— 先让相机出图再标定';
      return;
    }
    calibration.size = spec.frame_size;
    calibration.points = [];
    calibration.active = true;
    setCalibrationWorldFromInputs();
    body.classList.remove('hidden');
    document.getElementById('calib-cancel').classList.remove('hidden');
    document.getElementById('calib-save').disabled = true;
    // A cache-busting query, because the browser will otherwise show the frame
    // it fetched the first time and the operator calibrates against a picture
    // of an arena that has since been rearranged.
    document.getElementById('calib-image').src = `/api/arena/frame?t=${Date.now()}`;
    renderCalibrationPoints(calibration);
    renderCalibrationOverlay(calibration);
    hint.textContent = `已点 0/4 —— 下一个是 ${CALIB_ORDER[0]}`;
  } catch (err) {
    hint.textContent = '拿不到画面: ' + err.message;
  }
}

function stopCalibration() {
  calibration.active = false;
  calibration.points = [];
  document.getElementById('calib-body').classList.add('hidden');
  document.getElementById('calib-cancel').classList.add('hidden');
  document.getElementById('calib-hint').textContent =
    '对着画面依次点四个角，顺序和打印的标定图一致';
}

async function saveCalibration() {
  const hint = document.getElementById('calib-hint');
  try {
    const result = await api('/api/arena/calibrate', {
      method: 'POST',
      body: JSON.stringify({
        image_points: calibration.points,
        world_points: calibration.world,
        tag_height_m: (Number(document.getElementById('calib-tag-height').value) || 0) / 100,
        camera_height_m: (Number(document.getElementById('calib-camera-height').value) || 0) / 100,
      }),
    });
    const residual = result.residual_m;
    hint.textContent = result.adequate
      ? `已保存 —— 视差可以忽略（${residual} m）`
      : `已保存，但视差 ${residual} m 偏大，位置会不准；把标定靶垫到狗背高度可以消除它`;
    stopCalibration();
    await refreshStatus();
  } catch (err) {
    hint.textContent = '保存失败: ' + err.message;
  }
}

function wireCalibration() {
  const start = document.getElementById('calib-start');
  const cancel = document.getElementById('calib-cancel');
  const save = document.getElementById('calib-save');
  const overlay = document.getElementById('calib-overlay');
  if (start) start.addEventListener('click', startCalibration);
  if (cancel) cancel.addEventListener('click', stopCalibration);
  if (save) save.addEventListener('click', saveCalibration);
  if (overlay) overlay.addEventListener('click', onCalibrationClick);
  for (const id of ['calib-width', 'calib-depth']) {
    const input = document.getElementById(id);
    if (input) input.addEventListener('change', setCalibrationWorldFromInputs);
  }
}

/* ── formation ─────────────────────────────────────────────────────── */

/* Say how far apart they should be, and watch it happen.
 *
 * The run takes seconds — up to six steps of up to 2.5 s each plus settling —
 * so it runs server-side and reports through the status push. An alignment that
 * goes quiet for twenty seconds is one the operator stops and does by hand,
 * which is why each measured step appears as it is taken rather than in a
 * summary afterwards. */
const FORMATION_STATES = {
  idle: ['', ''],
  running: ['编队中…', 'on'],
  converged: ['已到位', 'on'],
  no_progress: ['没有进展', 'warn'],
  no_measurement: ['测不到位置', 'warn'],
  // **A tag too small to trust its id, which is worse than not seeing it.**
  // Missing data stops the loop; wrong data drives it — a misread id puts one
  // dog's position on the other. The state used to fall through to the raw
  // English token, so the operator read `illegible` with no tone and no idea
  // what to do.
  illegible: ['标签太小认不出', 'bad'],
  // The planner found nothing it could command. Usually means the geometry and
  // the tolerances disagree, which is worth saying in those words.
  no_move: ['没有可执行的修正', 'warn'],
  budget: ['步数用完', 'warn'],
  estopped: ['急停中止', 'bad'],
  cancelled: ['已取消', ''],
  stopped: ['已停下', ''],
  error: ['出错', 'bad'],
};

function renderFormation(formation) {
  const status = document.getElementById('form-status');
  const start = document.getElementById('form-start');
  const stop = document.getElementById('form-stop');
  if (!status || !formation) return;
  if (!state.formationSteps) state.formationSteps = [];

  const running = formation.state === 'running';
  const [label, tone] = FORMATION_STATES[formation.state] || [formation.state, ''];
  status.textContent = formation.detail ? `${label} · ${formation.detail}` : label;
  status.className = 'muted' + (tone === 'bad' ? ' warn' : '');
  if (tone === 'bad') status.className = 'muted warn';

  start.disabled = running;
  start.textContent = formation.state === 'converged' ? '再来一次' : '对齐';
  stop.classList.toggle('hidden', !running);

  // The measured history, one chip per step. Drawn from the server's own record
  // rather than accumulated here, so a page refresh mid-run shows the steps
  // that already happened.
  const host = document.getElementById('form-steps');
  if (host) {
    host.textContent = '';
    for (const measured of formation.history || []) {
      const chip = document.createElement('span');
      const error = measured.distance_error_m;
      chip.className = 'form-step' + (Math.abs(error) <= 0.10 ? ' good' : ' bad');
      chip.textContent = `间距 ${measured.distance_m} m（${error > 0 ? '+' : ''}${error}）`;
      chip.title = `朝向差 ${measured.yaw_error_deg}°`;
      host.appendChild(chip);
    }
  }
}

async function startFormation() {
  const status = document.getElementById('form-status');
  try {
    await api('/api/fleet/formation', {
      method: 'POST',
      body: JSON.stringify({
        distance_m: Number(document.getElementById('form-distance').value) || 0,
        bearing_deg: Number(document.getElementById('form-bearing').value) || 0,
        heading_spread_deg: Number(document.getElementById('form-spread').value) || 0,
        mover: document.getElementById('form-mover').value,
      }),
    });
    status.textContent = '编队中…';
    await refreshStatus();
  } catch (err) {
    status.textContent = err.message;
    status.className = 'muted warn';
  }
}

async function stopFormation() {
  try {
    await api('/api/fleet/formation/stop', { method: 'POST', body: '{}' });
    await refreshStatus();
  } catch (err) {
    toast('停下失败: ' + err.message, 'bad');
  }
}

function wireFormation() {
  const start = document.getElementById('form-start');
  const stop = document.getElementById('form-stop');
  if (start) start.addEventListener('click', startFormation);
  if (stop) stop.addEventListener('click', stopFormation);

}

/* ── two-dog synchronisation ───────────────────────────────────────── */

/* The numbers are each dog's *change* since the marked reference, never its
 * raw pose. Two odometers have two origins — comparing them directly would
 * invent a relationship that does not exist — so the console asks the operator
 * for the one thing it cannot know: the moment the dogs were lined up. */
const SYNC_LABELS = {
  unmarked: '未标定',
  unknown: '无法判断',
  aligned: '同步',
  drifting: '有偏差',
  diverged: '已错位',
};

function renderSync(sync) {
  const stateEl = document.getElementById('sync-state');
  const detail = document.getElementById('sync-detail');
  if (!stateEl || !detail) return;
  if (!sync) return;

  const label = SYNC_LABELS[sync.state] || sync.state;
  stateEl.textContent = label;
  stateEl.className = 'flag' + (sync.state === 'aligned' ? ' on'
    : (sync.state === 'drifting' ? ' warn'
      : (sync.state === 'diverged' ? ' bad' : '')));

  detail.textContent = '';
  if (sync.state === 'unmarked') {
    detail.textContent = '按「标记对齐」把两只狗当前的位置和朝向记为基准';
    return;
  }
  if (sync.state === 'unknown') {
    // Marked, but nobody is reporting a pose. Not the same as aligned.
    detail.textContent = '已标定，但机器狗没有报告位姿 —— 无法判断';
    return;
  }

  for (const pair of sync.pairs || []) {
    const chip = document.createElement('span');
    chip.className = 'sync-pair ' + pair.state;
    const yaw = document.createElement('b');
    yaw.textContent = `${pair.yaw_spread_deg}°`;
    const dist = document.createElement('b');
    dist.textContent = `${pair.distance_spread_m} m`;
    chip.append(`${pair.a} ↔ ${pair.b}　朝向差 `, yaw, '　路程差 ', dist);
    chip.title = `${pair.a} 转了 ${pair.detail.a.turned_deg}°，`
      + `${pair.b} 转了 ${pair.detail.b.turned_deg}°；`
      + `走了 ${pair.detail.a.travelled} m / ${pair.detail.b.travelled} m`
      + (pair.state === 'aligned' ? '' : `　（${pair.laggard} 落后）`);
    detail.appendChild(chip);
  }
}

/* One status fetch, applied through the same path the socket uses. Two
 * renderers for one payload is how a panel ends up disagreeing with itself. */
async function refreshStatus() {
  try {
    renderStatus(await api('/api/fleet/status'));
  } catch (_) { /* the socket will bring it round anyway */ }
}

async function markSync() {
  try {
    const result = await api('/api/fleet/sync', { method: 'POST', body: '{}' });
    const parts = [`已以 ${result.marked.join('、')} 为基准`];
    if (result.skipped && result.skipped.length) {
      parts.push(`${result.skipped.join('、')} 没有位姿，未覆盖`);
    }
    toast(parts.join('；'), result.skipped && result.skipped.length ? 'warn' : 'ok');
    await refreshStatus();
  } catch (err) {
    toast('标记失败: ' + err.message, 'bad');
  }
}

function wireArena() {
  const panel = document.getElementById('arena-panel');
  // Once the operator has opened or closed it by hand, stop deciding for them.
  if (panel) panel.addEventListener('toggle', () => { panel.dataset.touched = '1'; });
}

function wireSync() {
  const mark = document.getElementById('sync-mark');
  const clear = document.getElementById('sync-clear');
  if (mark) mark.addEventListener('click', markSync);
  if (clear) clear.addEventListener('click', async () => {
    try {
      await api('/api/fleet/sync', { method: 'DELETE' });
      toast('已清除同步基准', 'ok');
      await refreshStatus();
    } catch (err) {
      toast('清除失败: ' + err.message, 'bad');
    }
  });
}

/* ── routines ──────────────────────────────────────────────────────── */

/* A sequence keeps sending commands after the operator has taken their hands
 * off the keyboard, so its progress and its stop button are always on screen
 * while it runs — not behind a panel, and not only on the card of whichever
 * dog happens to be focused. */
async function loadRoutines() {
  const host = document.getElementById('fleet-routines');
  let spec;
  try {
    spec = await api('/api/fleet/routines');
  } catch (_) { return; }
  // **Which panel was open, so a rebuild does not shut it.**
  //
  // `renderFleetTargets` re-fetches this list when the connected set changes —
  // which is when an operator has just plugged a dog in and is most likely to be
  // mid-edit. Clearing the host would close the panel under their hands and lose
  // nothing visible, which is the worst kind of bug to be told about.
  const openId = state.openRoutineParams || '';
  host.textContent = '';
  for (const routine of spec.routines || []) {
    if (routine.builtin) host.appendChild(routineButton(routine));
  }
  // The operator's own routines, in their own row: they are edited and deleted
  // rather than just pressed, so they carry more controls than a built-in.
  await loadCustomRoutines();
  if (openId) {
    const chip = host.querySelector(`.routine-chip .routine-btn[data-routine="${openId}"]`);
    const routine = (spec.routines || []).find((r) => r.id === openId);
    if (chip && routine) {
      chip.parentElement.appendChild(buildRoutineParams(routine));
    } else {
      state.openRoutineParams = '';
    }
  }
  renderRoutine(state.lastStatus && state.lastStatus.routine);
}

/* A built-in routine: press to run, plus a ⚙ when it has knobs. */
function routineButton(routine) {
  const wrap = document.createElement('span');
  wrap.className = 'routine-chip';

  const button = document.createElement('button');
  button.className = 'btn routine-btn';
  button.dataset.routine = routine.id;
  // The step count and the duration are computed from **the current
  // parameters**, so a routine with a 2 m side does not still claim 9 步/32s.
  const summary = `${routine.steps.length}步/${Math.round(routine.seconds)}s`;
  const hasKnobs = (routine.params || []).length > 0;
  labelled(
    button,
    ROUTINE_ICONS[routine.id] || '',
    `${routine.label} · ${summary}${hasKnobs ? ` ${KNOB_MARK}` : ''}`,
  );
  button.title = routine.title + (routine.needs_dangerous
    ? '（含需要危险动作许可的步骤）' : '');
  button.addEventListener('click', () => startRoutine(routine, button));
  wrap.appendChild(button);

  if (hasKnobs) {
    // The knob count, so a closed panel still shows there is something in it.
    const knobs = document.createElement('button');
    knobs.className = 'btn knob-btn';
    // The word, not just a marker: it has to be findable by someone who has
    // never seen the panel, and "设置" is what the button does.
    knobs.textContent = KNOB_MARK;
    knobs.title = `设置${routine.label}（${routine.params.map((p) => p.label).join('、')}）`;
    knobs.setAttribute(
      'aria-label', `设置${routine.label}`,
    );
    knobs.addEventListener('click', () => toggleRoutineParams(routine, wrap));
    wrap.appendChild(knobs);
  }
  return wrap;
}

/* The parameter panel for one routine.
 *
 * **The values live on the routine object in `state`**, keyed by id, so opening
 * and closing the panel does not lose what was typed — and so a reload of the
 * action list does not silently reset a distance somebody chose.
 */
function routineParams(routine) {
  state.routineParams = state.routineParams || {};
  if (!state.routineParams[routine.id]) {
    const values = {};
    for (const param of routine.params || []) values[param.key] = param.default;
    state.routineParams[routine.id] = values;
  }
  return state.routineParams[routine.id];
}

function toggleRoutineParams(routine, wrap) {
  const existing = wrap.querySelector('.routine-params');
  if (existing) {
    existing.remove();
    state.openRoutineParams = '';
    return;
  }
  // One panel at a time: two open panels are two places to look for the number
  // you are about to change.
  for (const open of document.querySelectorAll('.routine-params')) open.remove();
  state.openRoutineParams = routine.id;
  wrap.appendChild(buildRoutineParams(routine));
}

function buildRoutineParams(routine) {
  const panel = document.createElement('div');
  panel.className = 'routine-params';
  const values = routineParams(routine);

  for (const param of routine.params) {
    const row = document.createElement('label');
    row.className = 'param-row';
    const name = document.createElement('span');
    name.className = 'param-name';
    name.textContent = param.label;

    const input = document.createElement('input');
    input.type = 'number';
    input.min = String(param.min);
    input.max = String(param.max);
    input.step = String(param.step);
    input.value = String(values[param.key]);
    input.id = `param-${routine.id}-${param.key}`;
    input.addEventListener('input', () => {
      const next = Number(input.value);
      // Held locally even when out of range, so typing "0.05" on the way to
      // "0.5" is not rewritten mid-keystroke. The server is what refuses it.
      if (Number.isFinite(next)) values[param.key] = next;
      schedulePreview(routine);
    });
    // A number input that is out of range is drawn with a native warning, but
    // the refusal that matters comes from the server.
    row.append(name, input, unitSpan(param.unit));
    panel.appendChild(row);
  }

  const status = document.createElement('span');
  status.className = 'param-preview muted';
  status.id = `preview-${routine.id}`;
  panel.appendChild(status);

  const run = document.createElement('button');
  run.className = 'btn param-run';
  run.textContent = '按这个跑';
  run.addEventListener('click', () => startRoutineWithParams(routine, run));
  panel.appendChild(run);

  schedulePreview(routine);
  return panel;
}

function unitSpan(unit) {
  const span = document.createElement('span');
  span.className = 'param-unit';
  span.textContent = (unit || '').trim();
  return span;
}

/* Debounced preview of what the parameters currently produce.
 *
 * **The steps come from the server, not from arithmetic here.** Computing
 * "parameters → steps" in two places is two implementations of one fact, and
 * they drift — this project has already shipped a parallax formula disagreeing
 * with its own scale factor ten lines away, and a `converged()` disagreeing with
 * the `plan()` it was gating. A preview that has drifted from what runs is worse
 * than no preview, because it is a promise.
 *
 * Debounced because this fires on every keystroke and the answer only changes
 * when the number does.
 */
function schedulePreview(routine) {
  state.previewTimers = state.previewTimers || {};
  clearTimeout(state.previewTimers[routine.id]);
  state.previewTimers[routine.id] = setTimeout(
    () => refreshPreview(routine), 200,
  );
}

async function refreshPreview(routine) {
  const status = document.getElementById(`preview-${routine.id}`);
  if (!status) return;
  try {
    const preview = await api('/api/fleet/routine/preview', {
      method: 'POST',
      body: JSON.stringify({ id: routine.id, params: routineParams(routine) }),
    });
    const walk = preview.steps.find((step) => step.args
      && Object.prototype.hasOwnProperty.call(step.args, 'meters'));
    const reach = walk ? ` · 每步 ${walk.args.meters} 米` : '';
    status.className = 'param-preview muted';
    status.textContent = `${preview.total} 步 · 约 ${Math.round(preview.seconds)} 秒${reach}`;
    // The chip is updated too, so the summary on the closed panel is not
    // describing a routine that no longer exists.
    const chip = document.querySelector(
      `.routine-chip .routine-btn[data-routine="${routine.id}"]`,
    );
    if (chip) {
      const text = chip.querySelector('span:last-child');
      if (text) {
        text.textContent = `${routine.label} · ${preview.total}步/`
          + `${Math.round(preview.seconds)}s ${KNOB_MARK}`;
      }
    }
  } catch (err) {
    status.className = 'param-preview warn-text';
    status.textContent = err.message;
  }
}

async function startRoutine(routine, button) {
  button.disabled = true;
  try {
    await api('/api/fleet/routine', {
      method: 'POST',
      body: JSON.stringify({ id: routine.id }),
    });
  } catch (err) {
    toast(`${routine.label} 无法开始：${err.message}`, 'bad');
  } finally {
    button.disabled = false;
  }
}

/* Start with whatever is in the boxes. A parameter outside its range is refused
 * by the server with the field named, so there is no client-side copy of the
 * bounds to drift from the tool's own. */
async function startRoutineWithParams(routine, button) {
  button.disabled = true;
  try {
    await api('/api/fleet/routine', {
      method: 'POST',
      body: JSON.stringify({ id: routine.id, params: routineParams(routine) }),
    });
  } catch (err) {
    toast(`${routine.label} 无法开始：${err.message}`, 'bad');
  } finally {
    button.disabled = false;
  }
}

async function stopRoutine() {
  const button = document.getElementById('routine-stop');
  button.disabled = true;
  try {
    await api('/api/fleet/routine/stop', { method: 'POST' });
  } catch (err) {
    toast('停止编排失败：' + err.message, 'bad');
  } finally {
    button.disabled = false;
  }
}

const STEP_STATES = {
  pending: '待执行', running: '执行中', done: '完成',
  failed: '失败', cancelled: '已取消',
};
const POLICY_LABELS = { all: '全部', any: '任一', best_effort: '尽力' };

function renderRoutine(routine) {
  const wrap = document.getElementById('routine-run');
  const label = document.getElementById('routine-label');
  const bar = document.querySelector('[data-role="routine-progress"]');
  const pending = document.getElementById('routine-pending');
  if (!wrap) return;

  if (!routine || !routine.running) {
    wrap.classList.add('hidden');
    return;
  }
  wrap.classList.remove('hidden');
  const done = Math.max(0, routine.step);
  const total = routine.total || 1;
  bar.style.width = `${Math.round((done / total) * 100)}%`;
  label.textContent = `${routine.label} · ${done + 1}/${total} ${routine.step_label}`;

  const queued = (routine.pending || []).length;
  if (pending) {
    pending.textContent = queued ? `已排队 ${queued} 个动作，将在当前步骤结束后执行` : '';
    pending.className = queued ? 'muted warn' : 'muted';
  }
  renderTimeline(routine.steps || []);
}

/* Every step, its policy, and one dot per dog. The dots are the point: a card
 * that says "done" while one dog did nothing is exactly the reading the branch
 * this came from produced from `sum(results) > 0`, and it is wrong. */
function renderTimeline(steps) {
  const host = document.getElementById('routine-timeline');
  if (!host) return;
  host.textContent = '';

  for (const step of steps) {
    const card = document.createElement('li');
    card.className = `step-card ${step.state || 'pending'}`;

    const index = document.createElement('span');
    index.className = 'step-index';
    index.textContent = `${(step.index ?? 0) + 1} · ${STEP_STATES[step.state] || step.state}`;

    const text = document.createElement('span');
    text.className = 'step-label';
    text.textContent = step.label;
    text.title = `${step.label}（${step.tool}，${step.seconds}s）`;

    const meta = document.createElement('span');
    meta.className = 'step-meta';
    const policy = document.createElement('span');
    policy.className = 'policy';
    policy.textContent = POLICY_LABELS[step.policy] || step.policy;
    policy.title = step.policy === 'all'
      ? '每一只都必须做到，否则这一步算失败'
      : (step.policy === 'any' ? '任意一只做到即可' : '不因失败而中断');
    meta.appendChild(policy);

    const lanes = document.createElement('span');
    lanes.className = 'lanes';
    const perDog = step.per_dog || {};
    const names = Object.keys(perDog).length
      ? Object.keys(perDog)
      : (step.skipped || []).map((id) => id);
    for (const id of names) {
      const dot = document.createElement('span');
      const outcome = perDog[id];
      dot.className = 'lane ' + (outcome === undefined
        ? 'skipped' : (outcome && outcome.ok ? 'ok' : 'bad'));
      dot.title = outcome === undefined
        ? `${id}：未参与`
        : `${id}：${outcome.ok ? '成功' : (outcome.error || '失败')}`;
      lanes.appendChild(dot);
    }
    meta.appendChild(lanes);

    // A step that nudged a dog back into formation says so. Silence here would
    // mean the console turned a robot without the operator being told.
    if (step.correction && (step.correction.applied || []).length) {
      const fix = document.createElement('span');
      fix.className = 'step-fix';
      const nudge = step.correction.applied[0];
      fix.textContent = `已纠正 ${nudge.robot_id} ${nudge.degrees > 0 ? '+' : ''}${nudge.degrees}°`;
      fix.title = nudge.reason
        + `　（纠正前朝向差 ${step.correction.spread_before}°）`;
      meta.appendChild(fix);
    }

    card.appendChild(index);
    card.appendChild(text);
    card.appendChild(meta);
    host.appendChild(card);
  }
}

/* An icon per action, **for scanning and never for meaning**.
 *
 * The rule this table follows: **every icon is drawn next to its label, and
 * there are no icon-only buttons.** Emoji render differently on every platform
 * and can be missing entirely on a bare Linux box, where an icon-only button is
 * a blank square. The icon speeds up finding a button you already know; the
 * label is what tells you what it does.
 *
 * Keyed by the catalogue's action name, so it stays correct when the server
 * adds or reorders actions. An unknown name simply gets no icon, which is a
 * smaller failure than a wrong one.
 */
const ACTION_ICONS = {
  // posture
  stand: '🧍', recover: '🔄', lie_down: '⬇️',
  // the safe actions
  hello: '👋', stretch: '🙆', content: '😌', pose: '✨', scrape: '🙇',
  wiggle_hips: '🕺', finger_heart: '🫶', sit: '🪑', rise_sit: '🧍',
  // the hard ones
  dance_1: '💃', dance_2: '🕺', handstand: '🤸',
  front_jump: '⬆️', front_pounce: '🐆',
  bound: '🐇', cross_step: '🚶', onesided_step: '🦵', moon_walk: '🌙',
  front_flip: '🔄', back_flip: '🔃', left_flip: '↩️', right_flip: '↪️',
};

/* An icon per routine, by id. */
const ROUTINE_ICONS = {
  greet: '👋', dance: '💃', square: '⬛', formation: '➡️',
};

/* The marker for a routine that has knobs to turn.
 *
 * **A word, not a gear.** The gear was the obvious choice and it renders as an
 * unclear grey blob at 12px on this background — measured in a headless render
 * and again on screen. `设置` is unambiguous at any size, needs no emoji font,
 * and says what the button does rather than what category it belongs to.
 *
 * It still carries no colour: colour is spoken for by state (running / done /
 * failed), and a hint carried only by colour is invisible to a colour-blind
 * operator. */
const KNOB_MARK = '设置';
/* The needs-space marker, drawn next to the label of a flip or a jump.
 *
 * This one keeps its glyph because it is a *warning* — a word would compete
 * with the action's own label for attention, and `⚠` is universally recognised.
 * U+FE0F asks for the pictograph rather than the thin text glyph, which at
 * 12px on a dark background is a smudge. */
const SPACE_MARK = '\u26A0\uFE0F';

function iconFor(action) {
  return ACTION_ICONS[action.key] || ACTION_ICONS[action.id] || '';
}

function labelled(button, icon, label) {
  if (icon) {
    const mark = document.createElement('span');
    mark.className = 'btn-icon';
    // The icon is decorative: a screen reader announcing "dancer" before
    // "跳舞 1" is noise, and the label is already there.
    mark.setAttribute('aria-hidden', 'true');
    mark.textContent = icon;
    button.appendChild(mark);
  }
  const text = document.createElement('span');
  text.textContent = label;
  button.appendChild(text);
  return button;
}

async function loadFleetActions() {
  const posture = document.getElementById('fleet-posture');
  const named = document.getElementById('fleet-named');
  const hard = document.getElementById('fleet-hard');
  const targets = document.getElementById('fleet-targets');
  let spec;
  try {
    spec = await api('/api/fleet/actions');
  } catch (_) {
    document.getElementById('fleet-bar').classList.add('hidden');
    return;
  }
  state.fleetActions = spec;

  targets.textContent = spec.targets.length
    ? `作用于 ${spec.targets.join('、')}`
      + (spec.skipped.length ? ` · 未连接：${spec.skipped.join('、')}` : '')
    : '没有已连接的机器狗';

  const fill = (host, actions) => {
    host.textContent = '';
    for (const action of actions || []) host.appendChild(fleetButton(action));
  };
  fill(posture, spec.posture);
  fill(named, spec.named);
  fill(hard, spec.hard);

  // The count goes on the summary so a closed group still says how much is
  // behind it — otherwise "高难动作" with nothing after it reads as empty.
  const count = document.getElementById('fleet-hard-count');
  if (count) {
    count.textContent = (spec.hard || []).length ? `（${spec.hard.length} 个）` : '';
  }
  const wrap = document.getElementById('fleet-hard-wrap');
  if (wrap) wrap.classList.toggle('hidden', !(spec.hard || []).length);
}

function fleetButton(action) {
  const button = document.createElement('button');
  button.className = 'btn';
  const needsSpace = action.needs_space === true;
  labelled(
    button,
    iconFor(action),
    needsSpace ? `${action.label} ${SPACE_MARK}` : action.label,
  );
  button.title = action.title || action.label;
  if (needsSpace) button.classList.add('needs-space');
  button.addEventListener('click', () => runFleetAction(action, button));
  return button;
}

async function runFleetAction(action, button) {
  // **Not here.** Binding `#routine-stop` in this function registered a new
  // click listener on every fleet action, so after N of them one press on
  // 停止编排 sent N stop requests. Listeners belong in a `wire*` function that
  // runs once at boot; anything bound here runs once per call.
  const out = document.getElementById('fleet-result');
  const repeat = document.getElementById('fleet-repeat');
  button.disabled = true;
  out.className = 'fleet-result';

  // **A routine that is running gets queued into, not interrupted.**
  //
  // The server has had `/api/fleet/routine/insert` since the timeline work, and
  // the page has displayed `routine.pending` since the same commit — but
  // nothing could ever put anything in it, so the indicator could only light up
  // for an API client. Pressing 一起鞠躬 mid-sequence means "after this step",
  // not "throw away the rest of the run", and that is what this does.
  const running = state.lastStatus && state.lastStatus.routine
    && state.lastStatus.routine.running;
  if (running) {
    out.textContent = `排队中 · ${action.label}…`;
    try {
      const queued = await api('/api/fleet/routine/insert', {
        method: 'POST',
        body: JSON.stringify({
          tool: action.tool, args: action.args || {}, label: action.label,
        }),
      });
      out.className = 'fleet-result ok';
      out.textContent = `已排队 · ${action.label}（第 ${queued.pending} 个）—— 当前步骤结束后执行`;
    } catch (err) {
      out.className = 'fleet-result bad';
      out.textContent = `排队失败: ${err.message}`;
    } finally {
      button.disabled = false;
    }
    return;
  }

  out.textContent = `${action.label}…`;
  try {
    const result = await api('/api/fleet/action', {
      method: 'POST',
      body: JSON.stringify({ tool: action.tool, args: action.args || {} }),
    });
    const skipped = result.skipped_count
      ? ` · 跳过 ${result.skipped.join('、')}（未连接）` : '';
    if (result.all_ok) {
      out.className = 'fleet-result ok';
      out.textContent = `${action.label}：${result.ok_count}/${result.targets.length} 成功${skipped}`;
    } else {
      // Name the dogs that did not do it. "Done" over a partial result is the
      // one lie this console must not tell.
      out.className = 'fleet-result partial';
      out.textContent = `${action.label}：${result.ok_count}/${result.targets.length} 成功`
        + ` · 失败 ${result.failed.join('、')}${skipped}`;
    }
    state.lastFleetAction = action;
    repeat.classList.remove('hidden');
  } catch (err) {
    out.className = 'fleet-result bad';
    out.textContent = `${action.label} 失败：${err.message}`;
  } finally {
    button.disabled = false;
    renderFleetTargets();
  }
}

function renderFleetTargets() {
  const spec = state.fleetActions;
  const targets = document.getElementById('fleet-targets');
  if (!spec || !targets) return;
  const connected = (state.lastStatus && state.lastStatus.robots || [])
    .filter((r) => r.connected).map((r) => r.robot_id);
  spec.targets = connected;
  spec.skipped = Object.keys(state.focusMap).filter((id) => !connected.includes(id));
  targets.textContent = connected.length
    ? `作用于 ${connected.join('、')}`
      + (spec.skipped.length ? ` · 未连接：${spec.skipped.join('、')}` : '')
    : '没有已连接的机器狗（先点「连接」）';

  // **The catalogue needs a connected robot, so it has to be re-fetched when
  // one arrives.**
  //
  // `named` and `hard` come from `robot_list_actions` on a live robot. At boot
  // nothing is connected, so both come back empty — and nothing ever asked
  // again, because this function only rewrote the text above. The bar looked
  // right, the target line updated, and the buttons never appeared.
  //
  // It was survivable while only nine safe actions lived there. With thirteen
  // more behind it, "the actions are missing until you reload" is the whole
  // feature not working.
  const wanted = new Set(connected);
  const have = new Set(state.catalogueFor || []);
  const same = wanted.size === have.size
    && [...wanted].every((id) => have.has(id));
  if (!same) {
    state.catalogueFor = [...wanted];
    loadFleetActions();
    loadRoutines();
  }
}

/* ── boot ──────────────────────────────────────────────────────────── */

function wireFleetBar() {
  const trim = document.getElementById('trim');
  const value = document.getElementById('trim-value');
  if (trim) {
    const apply = () => {
      state.trim = Number(trim.value) / 100;
      value.textContent = `${trim.value}%`;
    };
    trim.addEventListener('input', apply);
    apply();
  }
  const repeat = document.getElementById('fleet-repeat');
  if (repeat) {
    repeat.addEventListener('click', () => {
      if (state.lastFleetAction) runFleetAction(state.lastFleetAction, repeat);
    });
  }
  const routineStop = document.getElementById('routine-stop');
  if (routineStop) routineStop.addEventListener('click', stopRoutine);
}

async function boot() {
  wireButtons();
  wireInput();
  wireFleetBar();
  wireSync();
  wireArena();
  wireCalibration();
  wireFormation();
  wireEditor();
  loadFleetActions();
  loadRoutines();
  try {
    const snapshot = await api('/api/fleet');
    state.fleet = snapshot.fleet;
    state.mode = (snapshot.state && snapshot.state.input_mode) || 'focus';
    state.focused = (snapshot.state && snapshot.state.focused) || firstId();
    document.getElementById('input-mode').value = state.mode;
    buildPens(snapshot.fleet);

    // Only offer operations this robot registered — capability gating happens
    // server-side, so an R1 never gets a Go2 LED button.
    try {
      state.tools = (await api('/api/console/tools')).tools || [];
    } catch (_) { /* read-only console */ }
    await Promise.all(snapshot.fleet.robots.map((robot) => loadControls(robot.robot_id)));
    renderFleetTargets();
    // Only now: the palette reads `focusMap` for the per-dog controls, and
    // until the pens exist that map is empty. Called earlier it appeared to
    // work — an `await` inside let the pens be built first — which is a race,
    // not a design.
    await loadPalette();
  } catch (err) {
    toast('无法读取机队信息: ' + err.message, 'bad');
    return;
  }
  wireSocket();
  startPolling();   // the socket stands it down once it is open
  setInterval(tick, TICK_MS);
}

document.addEventListener('DOMContentLoaded', boot);
