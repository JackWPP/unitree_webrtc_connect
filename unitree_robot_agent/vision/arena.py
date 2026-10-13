"""A camera watching the arena, and what it currently sees.

`detect.py` reads one frame. This is where frames come from, and it is a
surprisingly load-bearing piece of the design:

* **The camera stays open.** Opening a webcam takes most of a second — it is a
  USB negotiation, not a function call — so a console that opened one per frame
  would spend its whole time negotiating and none of it looking.
* **Only the newest frame is kept.** A decoder running faster than the consumer
  otherwise builds a backlog, and a map drawn from a three-second-old frame is
  worse than no map: it looks live.
* **A dead camera is a state, not an exception.** An unplugged camera, a busy
  one, a file that has run out — all of them have to leave the console running
  and saying which it is, because the console also drives robots.

## Why OpenCV and not PyAV

OpenCV is already a dependency of this package for the detector, so capture uses
it too and this feature stays on one video library instead of two.

That matters more than it sounds. On macOS both OpenCV and PyAV ship their own
FFmpeg, and loading both into one process makes them fight over registering the
same Objective-C classes:

    objc: Class AVFFrameReceiver is implemented in both
    cv2/.dylibs/libavdevice and av/.dylibs/libavdevice
    This may cause spurious casting failures and mysterious crashes.

Measured, not predicted: it appeared the first time both were loaded together.
**Every `cv2` import in this package is inside a function**, which is what keeps
it out of a session that never uses vision — a plain console start was checked
and does not load it. Moving one to module level would make every session pay
for a feature it is not using, and `test_cv2_is_never_imported_at_module_level`
is the test that fails if that happens.

## What this is not

Not the robot's own camera. A dog cannot see its own back, so the camera that
reads the markers has to be a third thing watching the pair — a webcam on a
shelf, a phone, a laptop. The robot's video is for seeing what the dog is
looking at, which is a different question with a different answer.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

#: How long a frame may go un-refreshed before the feed is called stale. A
#: webcam at 15 fps that has not produced a frame in two seconds is not slow,
#: it is gone.
STALE_AFTER_S = 2.0


@dataclass
class Frame:
    """One frame, and when it arrived."""

    image: Any
    at: float = field(default_factory=time.monotonic)

    @property
    def age_s(self) -> float:
        return time.monotonic() - self.at


class ArenaCamera:
    """A camera on a background thread, with the newest frame available.

    Stopping and starting is explicit and idempotent: a console that is closed
    while the camera is open has to be able to say so, and a camera left open
    holds the device against every other program on the machine.
    """

    def __init__(self, source: str = "0", *, width: int = 1280, fps: int = 15) -> None:
        self.source = str(source)
        self.width = width
        self.fps = fps
        self._frame: Optional[Frame] = None
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._error = ""
        #: Frames decoded since start, and frames dropped because a newer one
        #: arrived first. The ratio is the only honest way to know whether the
        #: consumer is keeping up.
        self._decoded = 0
        self._dropped = 0

    # ── lifecycle ────────────────────────────────────────────────────

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._error = ""
        self._thread = threading.Thread(
            target=self._run, name="arena-camera", daemon=True
        )
        self._thread.start()

    async def aclose(self) -> None:
        self.close()

    def close(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=3.0)
        self._thread = None

    # ── reading ──────────────────────────────────────────────────────

    def latest(self) -> Optional[Frame]:
        with self._lock:
            return self._frame

    def status(self) -> Dict[str, Any]:
        frame = self.latest()
        age = frame.age_s if frame is not None else None
        if not self.running and not self._error:
            state = "stopped"
        elif self._error and frame is None:
            state = "failed"
        elif age is not None and age > STALE_AFTER_S:
            state = "stale"
        elif frame is None:
            state = "starting"
        else:
            state = "ok"
        return {
            "source": self.source,
            "state": state,
            "error": self._error,
            "age_s": round(age, 2) if age is not None else None,
            "size": list(frame.image.shape[:2]) if frame is not None else None,
            "decoded": self._decoded,
            "dropped": self._dropped,
        }

    # ── the thread ───────────────────────────────────────────────────

    def _open(self):
        """Open the camera or file through OpenCV, not PyAV.

        **Deliberately not PyAV, which the rest of this project uses for video.**
        On macOS both OpenCV and PyAV ship their own FFmpeg, and loading both
        into one process makes them fight over registering the same Objective-C
        classes:

            objc: Class AVFFrameReceiver is implemented in both
            cv2/.dylibs/libavdevice and av/.dylibs/libavdevice
            This may cause spurious casting failures and mysterious crashes.

        A console that drives robots cannot carry "mysterious crashes" in its
        camera path, and OpenCV is already a dependency of this package for the
        detector — so the camera uses it too and PyAV is never imported here.
        The warning was measured, not predicted: it appeared the first time this
        class ran with both libraries loaded.
        """
        import cv2

        source: Any = self.source
        if isinstance(source, str) and source.isdigit():
            source = int(source)  # VideoCapture wants an index for a device
        capture = cv2.VideoCapture(source)
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        capture.set(cv2.CAP_PROP_FPS, self.fps)
        return capture

    def _run(self) -> None:
        import cv2

        is_file = Path(self.source).exists()
        while not self._stop.is_set():
            capture = self._open()
            if not capture.isOpened():
                self._error = f"could not open {self.source!r}"
                logger.info("arena camera %s did not open", self.source)
                capture.release()
                # Waiting before retrying: a camera that is unplugged will not
                # appear because we asked again a millisecond later, and a tight
                # retry loop on a USB device is how a machine stops responding.
                self._stop.wait(2.0)
                continue

            self._error = ""
            try:
                self._read_loop(capture)
            except Exception as exc:  # noqa: BLE001
                self._error = f"{type(exc).__name__}: {str(exc)[:120]}"
                logger.info("arena camera stream ended: %s", self._error)
            finally:
                capture.release()

            if not is_file:
                # A device that stopped producing is usually gone. Reopening
                # immediately would spin.
                self._stop.wait(1.0)

    def _read_loop(self, capture) -> None:
        import cv2

        while not self._stop.is_set():
            ok, frame = capture.read()
            if not ok:
                # A file that has ended is rewound rather than reopened, so a
                # recorded clip stays useful as a live stand-in between
                # sessions — and a device that returns nothing is treated as
                # gone by the caller.
                if Path(self.source).exists():
                    capture.set(cv2.CAP_PROP_POS_FRAMES, 0)
                    continue
                return
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
            with self._lock:
                if self._frame is not None:
                    self._dropped += 1
                self._frame = Frame(image=gray)
                self._decoded += 1


class ReplayCamera(ArenaCamera):
    """A camera that replays a sequence of frames — the tests' stand-in.

    Not a mock in the usual sense: it is the same class, driven by the same
    `latest()`, and it exists so the pipeline downstream of the camera can be
    tested without one. Also useful in a session for demonstrating the map from
    a recorded clip.
    """

    def __init__(self, images, *, fps: int = 15, loop: bool = True) -> None:
        super().__init__(source="replay", fps=fps)
        self._images = list(images)
        self._loop = loop

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._error = ""
        self._thread = threading.Thread(
            target=self._replay, name="arena-replay", daemon=True
        )
        self._thread.start()

    def _replay(self) -> None:
        interval = 1.0 / max(1, self.fps)
        while not self._stop.is_set():
            for image in self._images:
                if self._stop.is_set():
                    return
                with self._lock:
                    # Counted the same way the live path counts it: this class
                    # exists to be the same camera driven differently, and a
                    # consumer that cannot tell whether it is keeping up is a
                    # consumer with a backlog it does not know about.
                    if self._frame is not None:
                        self._dropped += 1
                    self._frame = Frame(image=image)
                    self._decoded += 1
                self._stop.wait(interval)
            if not self._loop:
                return

    def status(self) -> Dict[str, Any]:
        status = super().status()
        status["replay"] = True
        return status


def frame_provider(camera: ArenaCamera) -> Callable[[], Optional[Any]]:
    """`latest()` as a plain function, for the detector's benefit."""

    def provide():
        frame = camera.latest()
        return frame.image if frame is not None else None

    return provide
