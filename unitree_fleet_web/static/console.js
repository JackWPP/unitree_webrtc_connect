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
    };
    updateVideo(robot.robot_id, true);
  }
  updateKeysHint();
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

    entry.link.textContent = connected ? '在线' : '离线';
    entry.link.className = 'pill ' + (connected ? 'up' : 'down');

    const session = robot.session || {};
    entry.state.textContent = session.state !== undefined && session.state !== null
      ? String(session.state) : (connected ? '已连接' : '—');

    const st = robot.state || {};
    const battery = st.battery !== undefined && st.battery !== null
      ? `${st.battery}%` : '—';
    entry.battery.textContent = battery;

    entry.pen.classList.toggle('focused', robot.robot_id === state.focused);
    entry.pen.classList.toggle('driving', state.driving ? state.driving.has(robot.robot_id) : false);
  }

  document.getElementById('fleet-summary').textContent =
    `${online}/${payload.robots.length} 在线`;
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

async function robotStop(robotId) {
  try {
    const res = await api(`/api/robot/${encodeURIComponent(robotId)}/stop`, { method: 'POST' });
    if (!res.delivered) toast(`${robotId} 未确认停止（${res.link}）`, 'bad');
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

/* ── input ─────────────────────────────────────────────────────────── */

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
async function tick() {
  if (!state.fleet) return;
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
      if (state.wasDriving && state.wasDriving[source]) {
        state.wasDriving[source] = false;
        try {
          // Scoped to this source. Releasing everything here would zero the
          // other hand's dog in split mode.
          await api('/api/fleet/release', {
            method: 'POST',
            body: JSON.stringify({ source }),
          });
        } catch (_) { /* the dead-man will catch it */ }
      }
      continue;
    }

    state.wasDriving = state.wasDriving || {};
    state.wasDriving[source] = true;
    try {
      await api('/api/fleet/teleop', {
        method: 'POST',
        body: JSON.stringify({ source, lx: axes.lx, ly: axes.ly, rx: axes.rx }),
      });
    } catch (err) {
      if (err.payload && err.payload.code === 'motion_blocked') {
        toast('急停锁存中，指令已拒绝', 'bad');
        state.held.clear();
      }
    }
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

function wireSocket() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const suffix = TOKEN ? '?token=' + encodeURIComponent(TOKEN) : '';
  const socket = new WebSocket(`${proto}//${location.host}/ws${suffix}`);

  socket.addEventListener('message', (event) => {
    try { renderStatus(JSON.parse(event.data)); } catch (_) { /* ignore */ }
  });
  socket.addEventListener('close', () => {
    document.getElementById('link-state').textContent = '状态推送已断开，重连中…';
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
  } catch (err) {
    toast('无法读取机队信息: ' + err.message, 'bad');
    return;
  }
  wireSocket();
  setInterval(tick, TICK_MS);
}

document.addEventListener('DOMContentLoaded', boot);
