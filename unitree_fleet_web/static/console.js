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

function updateKeysHint() {
  const hint = {
    focus: 'WASD 前进/平移 · QE 转向',
    both: 'WASD 同时驱动所有已选狗',
    split: 'Dog A = WASD+QE · Dog B = IJKL+UO',
    per_source: '按输入设备绑定（下方 bind）',
  }[state.mode] || '';
  for (const id of Object.keys(state.focusMap)) {
    const el = state.focusMap[id].keys;
    if (!el) continue;
    if (state.mode === 'split') {
      el.textContent = id === firstId() ? 'WASD + QE' : 'IJKL + UO';
    } else {
      el.textContent = hint;
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
    const telemetry = (state.telemetry && state.telemetry[robot.robot_id]) || {};
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
    if (!telemetry.battery && health.error) {
      entry.battery.title = health.error;
      entry.state.title = health.error;
    }

    const posture = telemetry.posture || {};
    const sport = telemetry.sport_state || {};
    const parts = [];
    if (posture.state && posture.state !== 'n/a' && posture.state !== 'unknown') {
      parts.push(posture.state);
    }
    if (sport.gait_type) parts.push(sport.gait_type);
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
  state.telemetry = payload.telemetry || {};
  // Named `videoHealth` rather than `video`: `focusMap[id].video` is the
  // <img> element, and two things called video would invite a mix-up.
  state.videoHealth = payload.video || {};
  state.lastStatus = payload;
  state.motion = payload.motion || {};
  state.where = payload.where || {};
  state.body = payload.body || {};
  state.sources = payload.sources || {};
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
  const spec = (state.sources && state.sources[robotId]) || null;
  if (!spec) { host.textContent = ''; return; }

  host.textContent = '';
  for (const source of spec.read || []) {
    const chip = document.createElement('span');
    chip.className = 'source ' + source.status;
    chip.textContent = `${source.label} · ${SOURCE_LABELS[source.status] || source.status}`;
    chip.title = `${source.topic} —— ${SOURCE_LABELS[source.status] || source.status}`;
    host.appendChild(chip);
  }
  for (const source of spec.not_attempted || []) {
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
  if (entry.sources) {
    const senseHost = panelFor(robotId, 'sense');
    senseHost.textContent = '';
    senseHost.appendChild(entry.sources);
  }
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
  } catch (_) {
    // A console with no tool bridge still shows video and telemetry.
    state.controls[robotId] = { groups: [], controls: [] };
  }
  renderControls(robotId);
  renderFlags(robotId);
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
      entry.speed.title = stalled
        ? '摇杆推着但机器人没动 —— 可能没站好，或避障拦住了'
        : '本体速度与斜坡指令';
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
  state.editor = state.editor || { steps: [], name: '', loop: false };
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

    row.appendChild(order);
    row.appendChild(what);
    row.appendChild(seconds);
    row.appendChild(policy);
    row.appendChild(up);
    row.appendChild(down);
    row.appendChild(remove);
    host.appendChild(row);
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
  return {
    id: editorRoutineId(name),
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
    state.editor = { steps: [], name: '', loop: false };
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
  host.textContent = '';
  for (const routine of spec.routines || []) {
    const button = document.createElement('button');
    button.className = 'btn';
    // The step count and the expected duration go on the button: a routine is
    // a commitment of time as well as motion, and the operator should know
    // both before pressing it rather than after.
    button.textContent = `${routine.label} · ${routine.steps.length}步/${Math.round(routine.seconds)}s`;
    button.title = routine.title + (routine.needs_dangerous
      ? '（含需要危险动作许可的步骤）' : '');
    button.addEventListener('click', () => startRoutine(routine, button));
    host.appendChild(button);
  }
  renderRoutine(state.lastStatus && state.lastStatus.routine);
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

    card.appendChild(index);
    card.appendChild(text);
    card.appendChild(meta);
    host.appendChild(card);
  }
}

async function loadFleetActions() {
  const host = document.getElementById('fleet-posture');
  const named = document.getElementById('fleet-named');
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

  host.textContent = '';
  host.classList.toggle('posture', true);
  for (const action of spec.posture || []) {
    host.appendChild(fleetButton(action));
  }
  named.textContent = '';
  for (const action of spec.named || []) {
    named.appendChild(fleetButton(action));
  }
}

function fleetButton(action) {
  const button = document.createElement('button');
  button.className = 'btn';
  button.textContent = action.label;
  button.title = action.title || action.label;
  button.addEventListener('click', () => runFleetAction(action, button));
  return button;
}

async function runFleetAction(action, button) {
  const out = document.getElementById('fleet-result');
  const routineStop = document.getElementById('routine-stop');
  if (routineStop) routineStop.addEventListener('click', stopRoutine);
  const repeat = document.getElementById('fleet-repeat');
  button.disabled = true;
  out.className = 'fleet-result';
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
}

async function boot() {
  wireButtons();
  wireInput();
  wireFleetBar();
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
