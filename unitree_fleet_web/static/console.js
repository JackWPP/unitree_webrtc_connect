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
      posture: host.querySelector(`.pen[data-robot="${CSS.escape(robot.robot_id)}"] [data-role="posture"]`),
      linkToggle: host.querySelector(`.pen[data-robot="${CSS.escape(robot.robot_id)}"] [data-role="link-toggle"]`),
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
    renderFaults(robot.robot_id, robot);
  }

  state.connecting = payload.connecting || [];
  state.telemetry = payload.telemetry || {};
  // Named `videoHealth` rather than `video`: `focusMap[id].video` is the
  // <img> element, and two things called video would invite a mix-up.
  state.videoHealth = payload.video || {};
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

/* ── posture / actions ─────────────────────────────────────────────── */

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

function postureButton(robotId, label, tool, args) {
  const button = document.createElement('button');
  button.className = 'btn';
  button.textContent = label;
  button.addEventListener('click', () => callTool(robotId, tool, args));
  return button;
}

async function buildPosture(robotId, available) {
  const entry = state.focusMap[robotId];
  if (!entry || !entry.posture) return;
  entry.posture.textContent = '';

  if (available.includes('robot_stand')) {
    entry.posture.appendChild(
      postureButton(robotId, '站立', 'robot_stand', { confirm: true }));
  }
  if (available.includes('robot_lie_down')) {
    entry.posture.appendChild(
      postureButton(robotId, '趴下', 'robot_lie_down', { confirm: true }));
  }

  if (!available.includes('robot_play_action')) {
    if (!entry.posture.childElementCount) entry.posture.classList.add('hidden');
    return;
  }

  // Ask the robot what it can actually do rather than hard-coding a list.
  let actions = [];
  try {
    const listed = await api(`/api/robot/${encodeURIComponent(robotId)}/tool`, {
      method: 'POST',
      body: JSON.stringify({ tool: 'robot_list_actions', args: { include_dangerous: false } }),
    });
    actions = (listed && listed.actions) || [];
  } catch (_) { /* leave the picker empty */ }

  if (!actions.length) return;

  const picker = document.createElement('select');
  const placeholder = document.createElement('option');
  placeholder.value = '';
  placeholder.textContent = '选择动作…';
  picker.appendChild(placeholder);

  for (const action of actions) {
    const option = document.createElement('option');
    option.value = action.name;
    option.textContent = action.name;
    picker.appendChild(option);
  }
  picker.addEventListener('change', () => {
    const name = picker.value;
    picker.value = '';
    if (name) callTool(robotId, 'robot_play_action', { name, confirm: true });
  });
  entry.posture.appendChild(picker);
}

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
  try {
    await api('/api/fleet/teleop', {
      method: 'POST',
      body: JSON.stringify({
        source,
        lx: frame.lx, ly: frame.ly, rx: frame.rx,
        ry: frame.ry || 0, keys: frame.keys || 0,
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

/* ── boot ──────────────────────────────────────────────────────────── */

async function boot() {
  wireButtons();
  wireInput();
  try {
    const snapshot = await api('/api/fleet');
    state.fleet = snapshot.fleet;
    state.mode = (snapshot.state && snapshot.state.input_mode) || 'focus';
    state.focused = (snapshot.state && snapshot.state.focused) || firstId();
    document.getElementById('input-mode').value = state.mode;
    buildPens(snapshot.fleet);

    // Only offer operations this robot registered — capability gating happens
    // server-side, so an R1 never gets a Go2 LED button.
    let available = [];
    try {
      available = (await api('/api/console/tools')).tools || [];
    } catch (_) { /* read-only console */ }
    state.tools = available;
    for (const robot of snapshot.fleet.robots) {
      buildPosture(robot.robot_id, available);
    }
  } catch (err) {
    toast('无法读取机队信息: ' + err.message, 'bad');
    return;
  }
  wireSocket();
  startPolling();   // the socket stands it down once it is open
  setInterval(tick, TICK_MS);
}

document.addEventListener('DOMContentLoaded', boot);
