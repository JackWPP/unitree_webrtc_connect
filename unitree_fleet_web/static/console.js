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
  sticky: {},             // robot_id -> {axis: bool} from on-screen pad
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

    // On-screen pad: pointer down drives that axis for as long as it is held.
    pen.querySelectorAll('.pad-key').forEach((key) => {
      const axis = key.dataset.axis;
      const press = (on) => {
        state.sticky[robot.robot_id] = state.sticky[robot.robot_id] || {};
        state.sticky[robot.robot_id][axis] = on;
        key.classList.toggle('active', on);
        if (on) setFocus(robot.robot_id, true);
      };
      key.addEventListener('pointerdown', (ev) => { ev.preventDefault(); press(true); });
      key.addEventListener('pointerup', () => press(false));
      key.addEventListener('pointerleave', () => press(false));
      key.addEventListener('pointercancel', () => press(false));
    });

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
      stickDot: host.querySelector(`.pen[data-robot="${CSS.escape(robot.robot_id)}"] [data-role="stick-dot"]`),
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
  if (entry.stickDot) {
    entry.stickDot.style.transform =
      `translate(calc(-50% + ${(lx * 15).toFixed(1)}px), calc(-50% + ${(-ly * 15).toFixed(1)}px))`;
  }
  renderPadKeys(robotId, axes);
}

/* The on-screen pad is a control, so it should also be a readout: a key held
 * down lights the same arrow the mouse would. Without this there is no way to
 * tell "my key is stuck" from "the robot is ignoring me". */
function renderPadKeys(robotId, axes) {
  const entry = state.focusMap[robotId];
  if (!entry) return;
  const sticky = state.sticky[robotId] || {};
  for (const key of entry.pen.querySelectorAll('.pad-key')) {
    const axis = key.dataset.axis || '';
    const value = axes[axis.slice(0, 2)] || 0;
    const lit = axis.endsWith('+') ? value > 0.01 : value < -0.01;
    // Never clear a key the pointer is holding — that press owns the class.
    if (lit) key.classList.add('active');
    else if (!sticky[axis]) key.classList.remove('active');
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
    renderFaults(robot.robot_id, robot);
    renderPadKeys(robot.robot_id, axesNow(robot.robot_id));
  }

  state.connecting = payload.connecting || [];
  state.telemetry = payload.telemetry || {};
  // Named `videoHealth` rather than `video`: `focusMap[id].video` is the
  // <img> element, and two things called video would invite a mix-up.
  state.videoHealth = payload.video || {};
  state.lastStatus = payload;
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
  const placeholder = document.createElement('option');
  placeholder.value = '';
  placeholder.textContent = control.label;
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

function showTab(robotId, tab) {
  const entry = state.focusMap[robotId];
  if (!entry) return;
  state.tab = state.tab || {};
  state.tab[robotId] = tab;
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

function renderControls(robotId) {
  const entry = state.focusMap[robotId];
  if (!entry || !entry.controls || !entry.tabs || !entry.panels) return;

  const spec = state.controls[robotId];
  entry.controls.textContent = '';
  entry.panels.textContent = '';
  entry.tabs.textContent = '';
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

  const remembered = state.tab && state.tab[robotId];
  showTab(robotId, tabs.includes(remembered) ? remembered : tabs[0]);
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

  const sticky = state.sticky[robotId] || {};
  for (const axis of Object.keys(sticky)) {
    if (!sticky[axis]) continue;
    const sign = axis.endsWith('+') ? 1 : -1;
    axes[axis.slice(0, 2)] += sign;
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
async function sendFrame(source, frame) {
  state.wasDriving = state.wasDriving || {};
  state.wasDriving[source] = true;
  // Trim scales every source, so it is the one place to make the whole console
  // gentler — which is what you want the first time you drive a real dog.
  const trim = state.trim === undefined ? 1 : state.trim;
  const scaled = (value) => Math.max(-1, Math.min(1, (value || 0) * trim));
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

function renderRoutine(routine) {
  const wrap = document.getElementById('routine-run');
  const label = document.getElementById('routine-label');
  const bar = document.querySelector('[data-role="routine-progress"]');
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
  } catch (err) {
    toast('无法读取机队信息: ' + err.message, 'bad');
    return;
  }
  wireSocket();
  startPolling();   // the socket stands it down once it is open
  setInterval(tick, TICK_MS);
}

document.addEventListener('DOMContentLoaded', boot);
