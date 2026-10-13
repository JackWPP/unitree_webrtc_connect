# Hardware bring-up: first run on two real Go2s

Everything here is verified offline against mock robots and a real browser.
What has **never** been exercised is a real `data2=3` handshake, real video, and
real teleoperation — those need the dogs.

Run the steps in order. Each one is worth its own result: if step 3 fails, steps
1 and 2 have already told you whether the problem is the key, the network, or
the connection slot.

---

## 0. Fill in the roster

The roster on disk is a demo with placeholder serials and no keys. Replace it.
You need, per dog: an `id`, the model, its IP, its serial number, and — for
firmware ≥ 1.1.15 — its per-device AES-128 key.

```sh
cat > ~/fleet.json <<'JSON'
{
  "default": "dog1",
  "robots": [
    {"id": "dog1", "model": "Go2", "ip": "192.168.31.245", "sn": "<DOG1_SN>", "role": "lead"},
    {"id": "dog2", "model": "Go2", "ip": "192.168.31.246", "sn": "<DOG2_SN>", "role": "wing"}
  ]
}
JSON
export UNITREE_AGENT_ROSTER=$HOME/fleet.json
```

Get a serial number from the dog itself (it prints the SN it hears back):

```sh
.venv/bin/python -c "
from unitree_webrtc_connect.multicast_scanner import discover_ip_sn
print(discover_ip_sn(timeout=3, device_type='Go2'))"
```

Fetch a per-device key — **each key is bound to one serial**, which is the whole
reason the fleet keys them by SN:

```sh
.venv/bin/unitree-fetch-aes-key --device-type Go2 --sn <DOG1_SN> --quiet
.venv/bin/unitree-fetch-aes-key --device-type Go2 --sn <DOG2_SN> --quiet
```

If that returns HTTP 567 or a JS challenge, the Unitree cloud WAF is blocking
it (upstream issue #65; the error is now typed as `UnitreeCloudBlockedError`).
Try `.venv/bin/unitree-fetch-aes-key --impersonate safari ...`. Without the key,
step 1 below will tell you so explicitly rather than failing obscurely.

Do **not** put a key in the roster file by hand unless you know which dog it
came from — assigning dog1's key to dog2 is the failure the preflight exists to
name, and it looks like a hardware fault from the inside.

---

## 1. Preflight — read-only, claims no connection slot

```sh
.venv/bin/unitree-fleet-console --preflight
```

Expected, per dog:

```
[OK  ] dog1     Go2  192.168.31.245   ok
         data2=3: per-device key accepted
```

| Output | What it means | What to do |
|---|---|---|
| `ok` + `data2=3: per-device key accepted` | Reachable, key is this dog's | → step 2 |
| `no_key` | Firmware needs a key and none is configured | fetch it (step 0) |
| `key_rejected` | A key is present but this dog refused it — almost certainly dog1's key on dog2 | re-fetch per SN |
| `unreachable` | Neither 9991 nor 8081 answered | check power / network / firewall |
| `legacy` + port 8081 | Pre-1.1.11 firmware | needs no key, some features unavailable |

This is safe to run while the official app holds the link: it posts
`con_notify` and stops, never an SDP. **Report the full output of this step.**

---

## 2. Do the two dogs each really get their own transport?

```sh
.venv/bin/python scripts/probe_dual_connection.py --ips 192.168.31.245,192.168.31.246
```

Measured offline: three concurrent connections produced three independent
transports with all-distinct host-candidate ports, and closing one left the
others alive. Confirm it on hardware.

**What matters:** both connect, and the per-connection candidate ports are
all-distinct. **Report the port table.**

---

## 3. Does connecting one dog freeze the other?

This is the last structural unknown, and the one that would force a redesign if
it goes the wrong way. The handshake awaits `conn.connect()` on the shared event
loop, and inside the driver that path does blocking I/O.

```sh
.venv/bin/python scripts/measure_connect_interference.py --ips 192.168.31.245,192.168.31.246
```

Read `during.max_ms` against the baseline. **A joystick frame is due every 50 ms
at 20 Hz**, so a stall at or above that is the hazard, and the script says so.

Expected if healthy:

```
baseline worst wakeup lateness : ~2 ms
during the connect             : < 50 ms
clean: worst wakeup lateness N ms against a N ms baseline.
```

If it reports `stalled`, that is a genuine finding, not a script bug — the fix
is to move the handshake off the shared loop (one thread with its own loop per
robot). **Report the JSON block.**

---

## 4. The console

```sh
.venv/bin/unitree-fleet-console            # -> http://127.0.0.1:8765
```

Open it and check, in this order:

1. **Both pens show 在线** and the video panels show pictures. If a panel says
   `无画面 —— 相机不可用或视频通道未开启`, the link is up but the video channel
   is not — that is a real signature, not a cosmetic message.
2. **电量 and 姿态** show real values (`87%`, `trot`). If they show `—`,
   the telemetry sampler is getting nothing.
3. **遥操作**: select dog1, hold `W` — **only dog1 moves**. Then `Split` mode:
   `WASD+QE` drives dog1, `IJKL+UO` drives dog2, both at once.
4. **急停**: press it. Both dogs should refuse motion with `409 motion_blocked`,
   and keep refusing until you release it explicitly.
5. **Release the tab** (close it). Both dogs should stop, and ~10 s later the
   log should say:

   ```
   released dog1, dog2: nothing has been watching for 10s, so the official app can take over
   ```

   Then confirm the **official app can connect** — that is the takeover test.
6. **Hand the link back**: press 断开, then connect the official app. It should
   get in immediately.

**Be ready to cut power.** Start with the dogs on blocks or in open space, and
keep a hand near the battery. Step 3 in particular triggers connects while
another dog is live.

---

## Safety questions this project cannot answer without hardware

Two things are designed-around rather than verified, and both matter:

- **Does a Go2 stop when the WebRTC link drops?** The dead-man watchdog zeroes
  the sticks when *frames* stop arriving, and the lease stops the robot before
  releasing a link. But if this process is killed outright (`kill -9`), nothing
  in it can send a stop. Test it deliberately: drive slowly, pull the network
  cable, and see whether the dog halts. **Whatever the answer, it belongs in the
  docs.**
- **Does the robot's own remote-control topic have a timeout?** If it does, a
  dead console is self-limiting. Unknown.

---

## What to send back

For each step: the command, the full output, and what the dogs physically did.
Especially:

1. Step 1's verdicts (this tells us whether the keys are usable at all)
2. Step 3's JSON (the one open design question)
3. Anything in step 4 that behaved differently from the description above
4. The answer to the link-drop question, if you are willing to test it

Failures are the useful part. Every expensive bug in this project so far was a
diagnostic one — the code worked, the message lied.
