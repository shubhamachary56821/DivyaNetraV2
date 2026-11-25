"""
DivyaNetra V2 - Camera Manager

Independent camera management with:
- Explicit activation only
- Independent Camera A / Camera B
- Background capture threads
- Latest-frame buffering
- Thread-safe access
- Bounded startup/recovery attempts
- Lifecycle events
- Timeout-aware shutdown
- Safe cleanup

Camera hardware is never opened during construction.
"""

import time
import threading
from copy import deepcopy

import cv2


class CameraError(Exception):
    """Base exception for camera subsystem errors."""


class CameraNotConfiguredError(CameraError):
    """Raised when a camera is unknown, missing, or disabled."""


class CameraManager:

    CAMERA_NAMES = ("camera_a", "camera_b")

    VALID_STATES = (
        "STOPPED",
        "STARTING",
        "RUNNING",
        "STOPPING",
        "ERROR",
    )

    def __init__(
        self,
        config_manager,
        event_bus=None,
        logger=None,
        reconnect_attempts=3,
        reconnect_delay=2.0,
        startup_timeout=8.0,
    ):

        self.config = config_manager
        self.event_bus = event_bus
        self.logger = logger

        self.reconnect_attempts = max(
            1, int(reconnect_attempts)
        )

        self.reconnect_delay = max(
            0.1, float(reconnect_delay)
        )

        self.startup_timeout = max(
            1.0, float(startup_timeout)
        )

        self._lock = threading.RLock()
        self._shutdown = False
        self._cameras = {}

        for name in self.CAMERA_NAMES:

            self._cameras[name] = {
                "name": name,
                "state": "STOPPED",
                "thread": None,
                "stop_event": threading.Event(),
                "ready_event": threading.Event(),
                "capture": None,
                "frame": None,
                "frame_timestamp": None,
                "frame_count": 0,
                "failure_count": 0,
                "reconnect_count": 0,
                "last_error": None,
                "started_at": None,
                "last_frame_at": None,
                "device_index": None,
                "purpose": None,
                "resolution": None,
                "fps": None,
            }

        self._log(
            "info",
            "CameraManager initialized. Hardware remains inactive."
        )

    # ==================================================
    # LOGGING
    # ==================================================

    def _log(self, level, message):

        if self.logger is not None:

            method = getattr(self.logger, level, None)

            if callable(method):
                method(message)

        else:
            print(f"[CameraManager] {message}")

    # ==================================================
    # EVENTS
    # ==================================================

    def _publish(self, event_name, data=None):

        if self.event_bus is None:
            return

        try:

            self.event_bus.publish(
                event_name,
                deepcopy(data) if data else {},
            )

        except Exception as error:

            self._log(
                "error",
                f"Event publishing failed: "
                f"{event_name} | {error}",
            )

    # ==================================================
    # CONFIGURATION
    # ==================================================

    def _get_camera_config(self, camera_name):

        if camera_name not in self.CAMERA_NAMES:

            raise CameraNotConfiguredError(
                f"Unknown camera: {camera_name}"
            )

        camera_config = self.config.get(
            f"cameras.{camera_name}"
        )

        if not camera_config:

            raise CameraNotConfiguredError(
                f"Configuration missing for {camera_name}"
            )

        if not camera_config.get("enabled", False):

            raise CameraNotConfiguredError(
                f"Camera is disabled: {camera_name}"
            )

        required = ("device_index",)

        for key in required:

            if key not in camera_config:

                raise CameraNotConfiguredError(
                    f"{camera_name} missing configuration: {key}"
                )

        return camera_config

    # ==================================================
    # CAMERA ACTIVATION
    # ==================================================

    def start_camera(self, camera_name):

        camera_name = str(camera_name).lower()

        camera_config = self._get_camera_config(
            camera_name
        )

        with self._lock:

            if self._shutdown:

                raise CameraError(
                    "CameraManager has been shut down."
                )

            camera = self._cameras[camera_name]

            if camera["state"] in ("STARTING", "RUNNING"):

                self._log(
                    "warning",
                    f"{camera_name} is already "
                    f"{camera['state']}.",
                )

                return False

            old_thread = camera["thread"]

            if old_thread is not None and old_thread.is_alive():

                raise CameraError(
                    f"Previous worker is still active: {camera_name}"
                )

            # Reset state for a new activation.
            camera["stop_event"] = threading.Event()
            camera["ready_event"] = threading.Event()

            camera["frame"] = None
            camera["frame_timestamp"] = None

            camera["frame_count"] = 0
            camera["failure_count"] = 0
            camera["reconnect_count"] = 0
            camera["last_error"] = None

            camera["device_index"] = camera_config[
                "device_index"
            ]

            camera["purpose"] = camera_config.get(
                "purpose", "UNSPECIFIED"
            )

            camera["resolution"] = (
                camera_config.get("width", 640),
                camera_config.get("height", 480),
            )

            camera["fps"] = camera_config.get(
                "fps", 15
            )

            camera["started_at"] = None
            camera["last_frame_at"] = None

            camera["state"] = "STARTING"

            thread = threading.Thread(
                target=self._capture_worker,
                args=(camera_name, deepcopy(camera_config)),
                name=f"DivyaNetra-{camera_name}",
                daemon=False,
            )

            camera["thread"] = thread

            thread.start()

            ready_event = camera["ready_event"]
            stop_event = camera["stop_event"]

        self._log(
            "info",
            f"Activation requested: {camera_name} "
            f"| Device: {camera_config['device_index']}",
        )

        ready = ready_event.wait(
            timeout=self.startup_timeout
        )

        if not ready:

            self._log(
                "warning",
                f"Startup timeout: {camera_name}. "
                f"Requesting cancellation.",
            )

            stop_event.set()

            with self._lock:

                if camera["state"] not in ("ERROR", "STOPPED"):
                    camera["state"] = "STOPPING"

            thread.join(timeout=1.0)

            if not thread.is_alive():

                with self._lock:

                    camera["thread"] = None
                    camera["state"] = "STOPPED"
                    camera["frame"] = None

            return False

        with self._lock:

            return camera["state"] == "RUNNING"

    # ==================================================
    # CAMERA SETUP
    # ==================================================

    def _open_camera(
        self,
        device_index,
        width,
        height,
        target_fps,
    ):

        cap = cv2.VideoCapture(
            device_index,
            cv2.CAP_V4L2,
        )

        if not cap.isOpened():

            cap.release()

            raise CameraError(
                f"Unable to open device {device_index}"
            )

        try:

            cap.set(cv2.CAP_PROP_FRAME_WIDTH, width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, height)
            cap.set(cv2.CAP_PROP_FPS, target_fps)

            # Reduce buffering latency.
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            return cap

        except Exception:

            cap.release()
            raise

    # ==================================================
    # CAPTURE WORKER
    # ==================================================

    def _capture_worker(
        self,
        camera_name,
        camera_config,
    ):

        camera = self._cameras[camera_name]

        stop_event = camera["stop_event"]

        device_index = camera_config["device_index"]

        width = camera_config.get("width", 640)
        height = camera_config.get("height", 480)

        target_fps = max(
            0,
            float(camera_config.get("fps", 15)),
        )

        frame_interval = (
            1.0 / target_fps
            if target_fps > 0
            else 0.0
        )

        cap = None

        try:

            # ------------------------------------------
            # INITIAL CONNECTION
            # ------------------------------------------

            for attempt in range(
                1,
                self.reconnect_attempts + 1,
            ):

                if stop_event.is_set():
                    break

                self._log(
                    "info",
                    f"{camera_name}: opening device "
                    f"{device_index} "
                    f"(attempt {attempt}/"
                    f"{self.reconnect_attempts})",
                )

                try:

                    cap = self._open_camera(
                        device_index,
                        width,
                        height,
                        target_fps,
                    )

                    ret, frame = cap.read()

                    if stop_event.is_set():

                        cap.release()
                        cap = None
                        break

                    if not ret or frame is None:

                        raise CameraError(
                            "Initial frame capture failed."
                        )

                    now = time.time()

                    with self._lock:

                        camera["capture"] = cap
                        camera["frame"] = frame.copy()

                        camera["frame_timestamp"] = now
                        camera["last_frame_at"] = now

                        camera["frame_count"] = 1
                        camera["started_at"] = now

                        camera["state"] = "RUNNING"
                        camera["last_error"] = None

                    camera["ready_event"].set()

                    self._log(
                        "info",
                        f"{camera_name} RUNNING | "
                        f"{frame.shape[1]}x{frame.shape[0]}",
                    )

                    self._publish(
                        "camera.started",
                        {
                            "camera": camera_name,
                            "device_index": device_index,
                            "purpose": camera["purpose"],
                            "width": frame.shape[1],
                            "height": frame.shape[0],
                            "target_fps": target_fps,
                        },
                    )

                    break

                except Exception as error:

                    if cap is not None:

                        try:
                            cap.release()
                        except Exception:
                            pass

                        cap = None

                    with self._lock:

                        camera["failure_count"] += 1
                        camera["last_error"] = str(error)

                    self._log(
                        "error",
                        f"{camera_name} opening failure: {error}",
                    )

                    if attempt < self.reconnect_attempts:

                        with self._lock:
                            camera["reconnect_count"] += 1

                        self._publish(
                            "camera.reconnecting",
                            {
                                "camera": camera_name,
                                "attempt": attempt,
                                "error": str(error),
                            },
                        )

                        stop_event.wait(
                            self.reconnect_delay
                        )

            # ------------------------------------------
            # STARTUP FAILURE
            # ------------------------------------------

            if cap is None:

                with self._lock:

                    camera["state"] = (
                        "STOPPED"
                        if stop_event.is_set()
                        else "ERROR"
                    )

                camera["ready_event"].set()

                if not stop_event.is_set():

                    self._publish(
                        "camera.error",
                        {
                            "camera": camera_name,
                            "device_index": device_index,
                            "error": camera["last_error"],
                        },
                    )

                return

            # ------------------------------------------
            # MAIN CAPTURE LOOP
            # ------------------------------------------

            while not stop_event.is_set():

                frame_start = time.monotonic()

                ret, frame = cap.read()

                if stop_event.is_set():
                    break

                # --------------------------------------
                # FRAME FAILURE / RECOVERY
                # --------------------------------------

                if not ret or frame is None:

                    with self._lock:

                        camera["failure_count"] += 1
                        camera["state"] = "STARTING"

                    self._log(
                        "warning",
                        f"{camera_name}: frame capture failed.",
                    )

                    self._publish(
                        "camera.frame_failed",
                        {
                            "camera": camera_name,
                            "device_index": device_index,
                        },
                    )

                    try:
                        cap.release()
                    except Exception:
                        pass

                    cap = None

                    with self._lock:
                        camera["capture"] = None

                    recovered = False

                    for attempt in range(
                        1,
                        self.reconnect_attempts + 1,
                    ):

                        if stop_event.is_set():
                            break

                        stop_event.wait(
                            self.reconnect_delay
                        )

                        if stop_event.is_set():
                            break

                        self._log(
                            "warning",
                            f"{camera_name}: recovery attempt "
                            f"{attempt}/{self.reconnect_attempts}",
                        )

                        try:

                            cap = self._open_camera(
                                device_index,
                                width,
                                height,
                                target_fps,
                            )

                            ret, frame = cap.read()

                            if stop_event.is_set():

                                cap.release()
                                cap = None
                                break

                            if not ret or frame is None:

                                raise CameraError(
                                    "Reconnected device "
                                    "returned no frame."
                                )

                            now = time.time()

                            with self._lock:

                                camera["capture"] = cap
                                camera["frame"] = frame.copy()

                                camera["frame_timestamp"] = now
                                camera["last_frame_at"] = now

                                camera["state"] = "RUNNING"
                                camera["last_error"] = None

                                camera["reconnect_count"] += 1
                                camera["frame_count"] += 1

                            recovered = True

                            self._publish(
                                "camera.reconnected",
                                {
                                    "camera": camera_name,
                                    "device_index": device_index,
                                },
                            )

                            self._log(
                                "info",
                                f"{camera_name} successfully reconnected.",
                            )

                            break

                        except Exception as error:

                            if cap is not None:

                                try:
                                    cap.release()
                                except Exception:
                                    pass

                                cap = None

                            with self._lock:

                                camera["last_error"] = str(error)
                                camera["failure_count"] += 1

                            self._log(
                                "error",
                                f"{camera_name} recovery failed: "
                                f"{error}",
                            )

                    if not recovered:

                        with self._lock:

                            camera["state"] = (
                                "STOPPED"
                                if stop_event.is_set()
                                else "ERROR"
                            )

                        if not stop_event.is_set():

                            self._publish(
                                "camera.error",
                                {
                                    "camera": camera_name,
                                    "device_index": device_index,
                                    "error": camera["last_error"],
                                    "reason": (
                                        "Reconnection attempts exhausted"
                                    ),
                                },
                            )

                        break

                    continue

                # --------------------------------------
                # STORE LATEST FRAME
                # --------------------------------------

                now = time.time()

                with self._lock:

                    camera["frame"] = frame.copy()
                    camera["frame_timestamp"] = now
                    camera["last_frame_at"] = now

                    camera["frame_count"] += 1

                # --------------------------------------
                # FPS LIMIT
                # --------------------------------------

                if frame_interval > 0:

                    elapsed = (
                        time.monotonic() - frame_start
                    )

                    remaining = frame_interval - elapsed

                    if remaining > 0:
                        stop_event.wait(remaining)

        except Exception as error:

            with self._lock:

                camera["last_error"] = str(error)
                camera["state"] = "ERROR"

            self._log(
                "exception",
                f"{camera_name} worker exception: {error}",
            )

            self._publish(
                "camera.error",
                {
                    "camera": camera_name,
                    "error": str(error),
                },
            )

        finally:

            if cap is not None:

                try:
                    cap.release()
                except Exception as error:

                    self._log(
                        "warning",
                        f"{camera_name} release warning: {error}",
                    )

            with self._lock:

                camera["capture"] = None
                camera["frame"] = None
                camera["frame_timestamp"] = None

                if stop_event.is_set():

                    camera["state"] = "STOPPED"

                elif camera["state"] != "ERROR":

                    camera["state"] = "STOPPED"

            camera["ready_event"].set()

            self._log(
                "info",
                f"{camera_name} capture worker exited.",
            )

    # ==================================================
    # CAMERA DEACTIVATION
    # ==================================================

    def stop_camera(
        self,
        camera_name,
        timeout=5.0,
    ):

        camera_name = str(camera_name).lower()

        if camera_name not in self.CAMERA_NAMES:

            raise CameraNotConfiguredError(
                f"Unknown camera: {camera_name}"
            )

        timeout = max(
            0.1,
            float(timeout),
        )

        with self._lock:

            camera = self._cameras[camera_name]
            thread = camera["thread"]

            if thread is None:

                camera["state"] = "STOPPED"
                camera["frame"] = None
                camera["frame_timestamp"] = None

                return True

            if not thread.is_alive():

                camera["thread"] = None
                camera["state"] = "STOPPED"

                camera["frame"] = None
                camera["frame_timestamp"] = None

                camera["capture"] = None

                return True

            camera["state"] = "STOPPING"
            camera["stop_event"].set()

        self._log(
            "info",
            f"Stopping {camera_name}...",
        )

        thread.join(timeout=timeout)

        if thread.is_alive():

            self._log(
                "error",
                f"{camera_name} shutdown timed out "
                f"after {timeout:.1f}s. "
                f"Worker remains tracked.",
            )

            return False

        with self._lock:

            camera["thread"] = None
            camera["capture"] = None

            camera["frame"] = None
            camera["frame_timestamp"] = None

            camera["state"] = "STOPPED"

        self._publish(
            "camera.stopped",
            {
                "camera": camera_name,
            },
        )

        self._log(
            "info",
            f"{camera_name} stopped.",
        )

        return True

    def stop_all(self, timeout=5.0):

        results = {}

        for camera_name in self.CAMERA_NAMES:

            results[camera_name] = self.stop_camera(
                camera_name,
                timeout=timeout,
            )

        return all(results.values())

    # ==================================================
    # FRAME ACCESS
    # ==================================================

    def get_frame(self, camera_name):

        camera_name = str(camera_name).lower()

        if camera_name not in self.CAMERA_NAMES:

            raise CameraNotConfiguredError(
                f"Unknown camera: {camera_name}"
            )

        with self._lock:

            frame = self._cameras[camera_name]["frame"]

            if frame is None:
                return None

            return frame.copy()

    def get_frame_info(self, camera_name):

        camera_name = str(camera_name).lower()

        if camera_name not in self.CAMERA_NAMES:

            raise CameraNotConfiguredError(
                f"Unknown camera: {camera_name}"
            )

        with self._lock:

            camera = self._cameras[camera_name]

            if camera["frame"] is None:
                return None

            return {
                "timestamp": camera["frame_timestamp"],
                "shape": camera["frame"].shape,
                "frame_count": camera["frame_count"],
            }

    # ==================================================
    # STATUS
    # ==================================================

    def get_camera_status(self, camera_name):

        camera_name = str(camera_name).lower()

        if camera_name not in self.CAMERA_NAMES:

            raise CameraNotConfiguredError(
                f"Unknown camera: {camera_name}"
            )

        with self._lock:

            camera = self._cameras[camera_name]
            thread = camera["thread"]

            return {
                "name": camera_name,
                "state": camera["state"],
                "device_index": camera["device_index"],
                "purpose": camera["purpose"],
                "resolution": camera["resolution"],
                "fps": camera["fps"],
                "frame_count": camera["frame_count"],
                "failure_count": camera["failure_count"],
                "reconnect_count": camera["reconnect_count"],
                "last_error": camera["last_error"],
                "started_at": camera["started_at"],
                "last_frame_at": camera["last_frame_at"],
                "thread_alive": (
                    thread.is_alive()
                    if thread is not None
                    else False
                ),
            }

    def get_status(self):

        return {
            camera_name: self.get_camera_status(camera_name)
            for camera_name in self.CAMERA_NAMES
        }

    # ==================================================
    # SHUTDOWN
    # ==================================================

    def shutdown(self, timeout=5.0):

        with self._lock:

            # Do not return early merely because shutdown
            # was requested before. A previous attempt may
            # have timed out and still need cleanup.
            self._shutdown = True

        self._log(
            "info",
            "CameraManager shutdown requested.",
        )

        success = self.stop_all(
            timeout=timeout,
        )

        if success:

            self._log(
                "info",
                "CameraManager shutdown completed.",
            )

        else:

            self._log(
                "error",
                "CameraManager shutdown incomplete. "
                "One or more worker threads remain active.",
            )

        return success
