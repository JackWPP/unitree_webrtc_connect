"""Real backend, wrapping ``unitree_webrtc_connect``.

Three upstream behaviours are handled here so nothing above has to know
about them:

1. ``publish_request_new`` has **no timeout** — the future resolves only
   when a matching reply arrives, and several api_ids on the humanoids
   have no handler at all, so the call would hang forever. Every request
   is wrapped in ``asyncio.wait_for``.
2. The reply envelope is ``{data: {header: {status: {code}}, data: <often a
   JSON string>}}``. It is normalised into :class:`ApiResponse` with a
   decoded payload so tools never touch the envelope.
3. The upstream constructor performs a blocking cloud login for STA-T, and
   ``discover_ip_sn`` blocks on multicast. Both run in a worker thread.

The upstream package is imported lazily inside the methods, so the agent
package can be imported (and the mock backend used) even when the driver's
dependencies are not installed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any, Dict, Optional, Set

from ..errors import (
    AesKeyMissing,
    UnsupportedConnection,
    classify_exception,
)
from ..fleet.connect import exclusive_connect, resolve_target
from .base import ApiResponse

logger = logging.getLogger("unitree_robot_agent.backend.webrtc")

_METHOD_NAMES = {
    "ap": "LocalAP",
    "sta-l": "LocalSTA",
    "sta-t": "Remote",
}


class WebRTCBackend:
    """A live WebRTC link to one robot."""

    name = "webrtc"

    def __init__(self, config, audit=None, auth=None) -> None:
        self.config = config
        self.audit = audit
        self.auth = auth
        self.robot_name = config.robot
        self.conn = None
        self._cache: Dict[str, dict] = {}
        self._subscribed: Set[str] = set()
        self._video_started = False
        self._video_track = None
        self._frame_waiter: Optional[asyncio.Future] = None
        self._errors: list = []
        #: Optional push listener for error notifications; see
        #: `set_error_listener`. Set by `create_runtime` to feed the robot's
        #: FaultStore.
        self._error_listener = None

    # ── lifecycle ────────────────────────────────────────────────────

    @property
    def is_connected(self) -> bool:
        if self.conn is None:
            return False
        return bool(getattr(self.conn, "isConnected", False))

    async def connect(self) -> None:
        from unitree_webrtc_connect import UnitreeWebRTCConnection, WebRTCConnectionMethod

        self.config.require_target()
        method_name = _METHOD_NAMES.get(self.config.connection)
        if method_name is None:
            raise UnsupportedConnection(f"Unknown connection method {self.config.connection!r}")

        key, key_note = await self._resolve_key()
        if key is None and self.config.needs_aes_key:
            detail = {"target": self.config.target_label()}
            if key_note:
                detail["cloud_lookup"] = key_note
            raise AesKeyMissing(
                f"No AES-128 key available for {self.config.target_label()}."
                + (f" Cloud lookup said: {key_note}" if key_note else ""),
                detail=detail,
            )

        method = getattr(WebRTCConnectionMethod, method_name)
        kwargs: Dict[str, Any] = {
            "aes_128_key": key,
            # `connection_region` is never "auto": CloudAuth pins the working
            # endpoint during the key lookup, so STA-T signs against the same
            # region that accepted the account.
            "region": self.config.connection_region,
            "device_type": self.config.device_type,
        }

        if self.config.connection == "ap":
            pass  # the driver hard-codes 192.168.12.1
        elif self.config.connection == "sta-l":
            if self.config.ip:
                kwargs["ip"] = self.config.ip
            else:
                # Resolve SN -> address here, off the loop. Doing it inside
                # the driver would block the shared loop for a full discovery
                # timeout, freezing every other robot's control loop; and two
                # concurrent discoveries cross-talk on port 10134 because the
                # scanner does not correlate replies with requests.
                resolved = await resolve_target(self.config)
                if resolved:
                    kwargs["ip"] = resolved
                else:
                    # Fall back to the driver's own scan. A miss here is not
                    # proof the robot is absent, just that this scan was quiet.
                    kwargs["serialNumber"] = self.config.serial_number
        else:  # sta-t
            kwargs.update(
                serialNumber=self.config.serial_number,
                username=self.config.username,
                password=self.config.password,
            )

        def _build():
            # The constructor may perform a blocking cloud login (STA-T).
            return UnitreeWebRTCConnection(method, **kwargs)

        conn = await asyncio.to_thread(_build)

        try:
            # One handshake at a time, process-wide. Two robots connecting at
            # once would contend for the multicast socket and for the loop.
            async with exclusive_connect(self.config.robot_id):
                await conn.connect()
        except BaseException as exc:
            with contextlib.suppress(Exception):
                await conn.disconnect()
            if isinstance(exc, Exception):
                raise classify_exception(exc) from exc
            raise

        self.conn = conn
        self._cache.clear()
        self._subscribed.clear()
        self._video_started = False
        self._video_track = None
        self._frame_waiter = None
        self._errors = []
        self._install_error_hook(conn)

    async def _resolve_key(self):
        """AES-128 key lookup. Returns ``(key, note)``.

        Prefers :class:`CloudAuth` (region detection + session cache); falls
        back to the config-only path when no auth helper is wired in.
        """
        if self.auth is not None:
            try:
                lookup = await self.auth.resolve_aes_key()
                return lookup.key, lookup.note
            except Exception as exc:
                logger.debug("auth key lookup failed: %s", exc)
                return None, f"{exc.__class__.__name__}: {exc}"
        key = await asyncio.to_thread(self.config.resolve_aes_key)
        return key, ""

    async def disconnect(self) -> None:
        conn, self.conn = self.conn, None
        self._subscribed.clear()
        self._video_started = False
        self._video_track = None
        self._frame_waiter = None
        if conn is None:
            return
        try:
            await conn.disconnect()
        except Exception as exc:  # teardown must never raise
            logger.debug("disconnect raised: %s", exc)

    async def aclose(self) -> None:
        await self.disconnect()

    # ── requests ─────────────────────────────────────────────────────

    async def api_request(
        self,
        topic: str,
        api_id: int,
        parameter: Any = None,
        *,
        timeout: Optional[float] = None,
    ) -> ApiResponse:
        if self.conn is None or not self.is_connected:
            return ApiResponse(code=-1, timed_out=True, message="not connected")

        options: Dict[str, Any] = {"api_id": api_id}
        if parameter is not None:
            options["parameter"] = parameter

        effective = timeout if timeout is not None else self.config.request_timeout_s
        try:
            raw = await asyncio.wait_for(
                self.conn.datachannel.pub_sub.publish_request_new(topic, options),
                timeout=effective,
            )
        except asyncio.TimeoutError:
            return ApiResponse(
                code=-1,
                timed_out=True,
                message=f"no reply within {effective:g}s",
            )
        except Exception as exc:
            # A closed channel / dead peer must not look like a refusal.
            text = str(exc) or exc.__class__.__name__
            if "not open" in text.lower():
                return ApiResponse(code=-1, timed_out=True, message=text)
            raise classify_exception(exc) from exc

        return self._parse(raw, topic=topic, api_id=api_id)

    @staticmethod
    def _parse(raw: Any, *, topic: str, api_id: int) -> ApiResponse:
        if not isinstance(raw, dict):
            return ApiResponse(
                code=-1, timed_out=True, message="malformed reply", raw={"raw": repr(raw)[:200]}
            )
        envelope = raw.get("data")
        if not isinstance(envelope, dict):
            return ApiResponse(code=-1, timed_out=True, message="reply without data", raw=raw)

        header = envelope.get("header")
        status = (header or {}).get("status") if isinstance(header, dict) else None
        if not isinstance(status, dict) or "code" not in status:
            return ApiResponse(
                code=-1,
                timed_out=True,
                message="reply without a status code",
                raw=raw,
            )
        try:
            code = int(status.get("code"))
        except (TypeError, ValueError):
            return ApiResponse(code=-1, timed_out=True, message="non-numeric status code", raw=raw)

        message = str(status.get("msg") or status.get("info") or "")
        return ApiResponse(
            code=code,
            data=envelope.get("data", ""),
            raw=raw,
            message=message,
        )

    # ── publish / subscribe ──────────────────────────────────────────

    def publish(self, topic: str, data: Any = None, msg_type: Optional[str] = None) -> bool:
        if self.conn is None:
            return False
        datachannel = getattr(self.conn, "datachannel", None)
        if not getattr(datachannel, "data_channel_opened", False):
            return False
        self.conn.datachannel.pub_sub.publish_without_callback(topic, data, msg_type)
        return True

    async def latest(
        self,
        topic: str,
        *,
        subscribe: bool = True,
        timeout: Optional[float] = None,
        max_age: Optional[float] = None,
    ) -> Optional[dict]:
        if self.conn is None:
            return None

        if subscribe:
            self._subscribe(topic)

        cached = self._get_cached(topic, max_age=max_age)
        if cached is not None:
            return cached

        if timeout and timeout > 0:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                await asyncio.sleep(0.05)
                cached = self._get_cached(topic, max_age=max_age)
                if cached is not None:
                    return cached
        return None

    def _subscribe(self, topic: str) -> None:
        if topic in self._subscribed or self.conn is None:
            return
        pub_sub = self.conn.datachannel.pub_sub

        def _store(message: Any) -> None:
            if isinstance(message, dict):
                self._cache[topic] = {"msg": message, "at": time.time()}

        try:
            pub_sub.subscribe(topic, _store)
        except Exception as exc:
            logger.debug("subscribe(%s) failed: %s", topic, exc)
            return
        if getattr(pub_sub, "subscriptions", {}).get(topic) is not _store:
            # The driver prints-and-returns when the channel is not open;
            # retry on the next read instead of going blind for the session.
            logger.debug("subscribe(%s) did not register; will retry", topic)
            return
        self._subscribed.add(topic)

    def _get_cached(self, topic: str, *, max_age: Optional[float]) -> Optional[dict]:
        entry = self._cache.get(topic)
        if entry is None:
            return None
        if max_age is not None and time.time() - entry["at"] > max_age:
            return None
        payload = entry["msg"].get("data")
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except ValueError:
                return entry["msg"]
        return payload if isinstance(payload, dict) else entry["msg"]

    # ── media ────────────────────────────────────────────────────────

    async def snapshot_frame(self, *, timeout: float = 5.0) -> Optional[bytes]:
        """Return a single JPEG frame, or None when the camera isn't available."""
        conn = self.conn
        if conn is None or not self.is_connected:
            return None

        if not self._video_started:
            loop = asyncio.get_running_loop()
            self._frame_waiter = loop.create_future()

            async def _on_track(track) -> None:
                self._video_track = track
                self._video_started = True
                while True:
                    try:
                        frame = await track.recv()
                    except Exception:
                        return
                    waiter = self._frame_waiter
                    if waiter is not None and not waiter.done():
                        waiter.set_result(frame)
                        self._frame_waiter = loop.create_future()

            conn.video.add_track_callback(_on_track)
            conn.video.switchVideoChannel(True)
        elif self._frame_waiter is None or self._frame_waiter.done():
            self._frame_waiter = asyncio.get_running_loop().create_future()

        waiter = self._frame_waiter
        try:
            frame = await asyncio.wait_for(asyncio.shield(waiter), timeout=timeout)
        except asyncio.TimeoutError:
            logger.info("no video frame within %.1fs", timeout)
            return None
        except Exception as exc:
            logger.debug("video frame failed: %s", exc)
            return None

        return await asyncio.to_thread(self._encode_jpeg, frame)

    @staticmethod
    def _encode_jpeg(frame) -> Optional[bytes]:
        """Encode an ``av.VideoFrame`` as a bare JPEG.

        Delegates to :mod:`unitree_robot_agent.vision.encode`, which is the
        single place that knows how to do this correctly (and is covered by
        offline tests, unlike the old inline version)."""
        from ..vision.encode import encode_frame

        payload, _, _ = encode_frame(frame)
        return payload

    # ── error notifications ──────────────────────────────────────────

    ERROR_TYPES = ("errors", "add_error", "rm_error")

    def _install_error_hook(self, conn) -> None:
        """Capture ``errors`` / ``add_error`` / ``rm_error`` messages.

        The driver consumes these types internally (``handle_error`` just
        prints them), so we attach a second listener to the underlying
        pyee EventEmitter instead of patching the library. pyee supports
        multiple listeners per event, and this keeps the upstream diff at
        zero."""
        channel = getattr(getattr(conn, "datachannel", None), "channel", None)
        if channel is None:
            return

        def _capture(message) -> None:
            try:
                if isinstance(message, (bytes, bytearray)):
                    return
                payload = json.loads(message) if isinstance(message, str) else message
                if not isinstance(payload, dict):
                    return
                if payload.get("type") not in self.ERROR_TYPES:
                    return
                self._errors.append(
                    {
                        "type": payload.get("type"),
                        "data": payload.get("data"),
                        "at": time.time(),
                    }
                )
                del self._errors[:-200]
                listener = self._error_listener
                if listener is not None:
                    # Pushed straight through, so the active-fault set is
                    # reconciled as messages arrive rather than by polling a
                    # ring that may already have been trimmed.
                    with contextlib.suppress(Exception):
                        listener(self._errors[-1])
            except Exception:
                pass

        try:
            channel.on("message", _capture)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("could not attach error hook: %s", exc)

    def error_events(self) -> list:
        return list(self._errors)

    def set_error_listener(self, callback) -> None:
        """Receive each error notification as it arrives.

        Called from the data-channel hook thread, so the callback must be
        cheap and must not assume a running event loop.
        """
        self._error_listener = callback

    # ── introspection ────────────────────────────────────────────────

    def extra_status(self) -> dict:
        out: Dict[str, Any] = {}
        conn = self.conn
        if conn is None:
            return out
        try:
            network = conn.datachannel.rtc_inner_req.network_status.network_status
            if network:
                out["network_mode"] = network
        except Exception:
            pass
        try:
            if getattr(conn.datachannel, "data_channel_opened", False):
                out["data_channel_open"] = True
        except Exception:
            pass
        return out


async def discover(
    *,
    device_type: str = "R1",
    sn: Optional[str] = None,
    timeout: float = 3.0,
) -> Dict[str, str]:
    """Multicast discovery, run off the event loop.

    Humanoids do not announce on the Go2 multicast group, and V3 firmware
    only answers a query carrying its own serial number — so passing ``sn``
    is what turns this from "usually empty" into "actually useful".
    """
    from unitree_webrtc_connect import discover_ip_sn

    def _run() -> Dict[str, str]:
        return discover_ip_sn(device_type=device_type, sn=sn, timeout=timeout)

    result = await asyncio.to_thread(_run)
    return result or {}
