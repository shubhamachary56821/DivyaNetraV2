"""
DivyaNetra V2 - Mode Controller

Coordinates operating modes with hardware activation.

Safety:
- STANDBY always requests all cameras stopped.
- Hardware activation is explicit.
- Camera activation follows configuration.
- Failed transitions attempt to return to STANDBY.
- No hardware is activated during construction.
"""

from threading import RLock


class ModeControllerError(Exception):
    """Raised when an operating mode transition fails."""


class ModeController:

    CAMERA_NAMES = ("camera_a", "camera_b")

    MODE_CAMERA_MAP = {
        "STANDBY": {
            "camera_a": False,
            "camera_b": False,
        },
        "INTERACTIVE": {
            "camera_a": True,
            "camera_b": False,
        },
        "MONITORING": {
            "camera_a": False,
            "camera_b": True,
        },
    }

    def __init__(
        self,
        config_manager,
        state_manager,
        camera_manager,
        event_bus=None,
        logger=None,
    ):
        self.config = config_manager
        self.state_manager = state_manager
        self.camera_manager = camera_manager
        self.event_bus = event_bus
        self.logger = logger

        self._lock = RLock()
        self._transitioning = False
        self._last_error = None
        self._custom_config = None
        self._fallback_error = None
        self._hardware_stopped = True

        print(
            "[ModeController] Initialized. "
            "Hardware remains inactive."
        )

    @property
    def transitioning(self):
        with self._lock:
            return self._transitioning

    @property
    def last_error(self):
        with self._lock:
            return self._last_error

    def get_status(self):
        with self._lock:
            return {
                "current_mode": self.state_manager.current_mode,
                "transitioning": self._transitioning,
                "last_error": self._last_error,
                "custom_config": (
                    dict(self._custom_config)
                    if self._custom_config is not None
                    else None
                ),
                "fallback_error": self._fallback_error,
                "hardware_stopped": self._hardware_stopped,
            }

    def _log(self, level, message):
        print(f"[ModeController] {message}")

        if self.logger is not None:
            method = getattr(self.logger, level, None)

            if callable(method):
                method(message)

    def _publish(self, event_name, data):
        if self.event_bus is None:
            return

        try:
            self.event_bus.publish(event_name, data)
        except Exception as error:
            self._log(
                "warning",
                f"Event publication failed: {error}",
            )

    def _get_camera_config(self, camera_name):
        return self.config.get(
            f"cameras.{camera_name}",
            {},
        )

    def _get_mode_config(self, mode):
        return self.config.get(
            f"modes.{mode}",
            {},
        )

    def _resolve_camera_targets(
        self,
        mode,
        custom_config=None,
    ):
        mode = str(mode).upper()

        if mode == "STANDBY":
            return {
                "camera_a": False,
                "camera_b": False,
            }

        if mode == "CUSTOM":
            from_mode_settings = custom_config is None

            if from_mode_settings:
                custom_config = self.config.get(
                    "modes.CUSTOM",
                    {},
                )

            if not isinstance(custom_config, dict):
                raise ModeControllerError(
                    "CUSTOM configuration must be a dictionary."
                )

            if from_mode_settings:
                # Mode metadata is not a camera target.
                custom_config = {
                    key: value
                    for key, value in custom_config.items()
                    if key in self.CAMERA_NAMES
                }

            unknown_keys = set(custom_config) - set(
                self.CAMERA_NAMES
            )

            if unknown_keys:
                raise ModeControllerError(
                    "Unknown CUSTOM configuration keys: "
                    f"{sorted(unknown_keys)}"
                )

            targets = {}

            for camera_name in self.CAMERA_NAMES:
                value = custom_config.get(camera_name, False)

                if not isinstance(value, bool):
                    raise ModeControllerError(
                        f"CUSTOM {camera_name} must be "
                        "a boolean (True/False)."
                    )

                targets[camera_name] = value

            return targets

        if mode not in self.MODE_CAMERA_MAP:
            raise ModeControllerError(
                f"Unsupported mode: {mode}"
            )

        mode_config = self._get_mode_config(mode)

        return {
            camera_name: bool(
                mode_config.get(
                    camera_name,
                    self.MODE_CAMERA_MAP[mode][camera_name],
                )
            )
            for camera_name in self.CAMERA_NAMES
        }

    def _validate_mode(self, mode):
        mode = str(mode).upper()

        if mode not in self.state_manager.VALID_MODES:
            raise ModeControllerError(
                f"Invalid operating mode: {mode}"
            )

        if mode != "STANDBY":
            mode_config = self._get_mode_config(mode)

            if not mode_config.get("enabled", False):
                raise ModeControllerError(
                    f"Mode is disabled in configuration: {mode}"
                )

        return mode

    def _validate_camera_targets(self, targets):
        for camera_name, should_activate in targets.items():
            if not should_activate:
                continue

            camera_config = self._get_camera_config(
                camera_name
            )

            if not camera_config.get("enabled", False):
                raise ModeControllerError(
                    f"{camera_name} is disabled in configuration."
                )

    def _stop_all_cameras(self):
        result = self.camera_manager.stop_all()

        if result is False:
            self._hardware_stopped = False
            raise ModeControllerError(
                "CameraManager failed to stop all cameras."
            )

        failures = []

        for camera_name in self.CAMERA_NAMES:
            status = self.camera_manager.get_camera_status(
                camera_name
            )

            if status.get("state") != "STOPPED":
                failures.append(
                    f"{camera_name}: {status.get('state')}"
                )

        if failures:
            self._hardware_stopped = False
            raise ModeControllerError(
                "Cameras failed to stop: "
                + ", ".join(failures)
            )

        self._hardware_stopped = True
        return True

    def _activate_targets(self, targets):
        for camera_name, should_activate in targets.items():
            if not should_activate:
                continue

            self._log(
                "info",
                f"Activating {camera_name}...",
            )

            result = self.camera_manager.start_camera(
                camera_name
            )

            if result is False:
                raise ModeControllerError(
                    f"Failed to activate {camera_name}."
                )

            status = self.camera_manager.get_camera_status(
                camera_name
            )

            if status["state"] != "RUNNING":
                raise ModeControllerError(
                    f"{camera_name} activation incomplete. "
                    f"State: {status['state']}"
                )

    def _safe_fallback(self, reason):
        self._log(
            "error",
            f"Transition failure. Attempting safe fallback: "
            f"{reason}",
        )

        fallback_errors = []

        try:
            self._stop_all_cameras()
        except Exception as error:
            fallback_errors.append(
                f"Camera shutdown: {error}"
            )
            self._hardware_stopped = False

        try:
            self.state_manager.standby(
                reason=f"Mode transition failure: {reason}"
            )
        except Exception as error:
            fallback_errors.append(
                f"State transition: {error}"
            )

        self._fallback_error = (
            "; ".join(fallback_errors)
            if fallback_errors
            else None
        )

        if self._fallback_error:
            self._log(
                "critical",
                "SAFE FALLBACK INCOMPLETE: "
                + self._fallback_error,
            )
        else:
            self._log(
                "warning",
                "Fallback completed. Cameras stopped; "
                "logical state is STANDBY.",
            )

    def transition_to(
        self,
        mode,
        reason="Manual mode activation",
        custom_config=None,
    ):
        mode = self._validate_mode(mode)

        with self._lock:
            if self._transitioning:
                raise ModeControllerError(
                    "Another mode transition is in progress."
                )

            if self.state_manager.current_mode == mode:
                self._log(
                    "info",
                    f"Already in {mode} mode.",
                )
                return False

            self._transitioning = True
            self._last_error = None
            self._fallback_error = None

        transition_data = {
            "target_mode": mode,
            "reason": str(reason),
        }

        self._publish(
            "system.mode_transition_started",
            transition_data,
        )

        try:
            targets = self._resolve_camera_targets(
                mode,
                custom_config,
            )

            self._validate_camera_targets(targets)

            # First, release currently active hardware.
            self._stop_all_cameras()

            # Then activate hardware required by target mode.
            self._activate_targets(targets)
            self._hardware_stopped = not any(targets.values())

            # Change logical state only after hardware succeeds.
            self.state_manager.set_mode(
                mode,
                reason=str(reason),
            )

            if mode == "CUSTOM":
                self._custom_config = dict(targets)
            else:
                self._custom_config = None

            self._log(
                "info",
                f"Mode transition completed: {mode}",
            )

            self._publish(
                "system.mode_transition_completed",
                {
                    "mode": mode,
                    "cameras": targets,
                    "reason": str(reason),
                },
            )

            return True

        except Exception as error:
            self._last_error = str(error)

            self._safe_fallback(str(error))

            self._publish(
                "system.mode_transition_failed",
                {
                    "target_mode": mode,
                    "error": str(error),
                    "fallback_mode": "STANDBY",
                },
            )

            raise ModeControllerError(
                f"Failed to transition to {mode}: {error}"
            ) from error

        finally:
            with self._lock:
                self._transitioning = False

    def standby(self, reason="Manual standby"):
        return self.transition_to(
            "STANDBY",
            reason=reason,
        )

    def activate_interactive(self):
        return self.transition_to(
            "INTERACTIVE",
            reason="Manual interactive activation",
        )

    def activate_monitoring(self):
        return self.transition_to(
            "MONITORING",
            reason="Manual monitoring activation",
        )

    def activate_custom(self, custom_config):
        return self.transition_to(
            "CUSTOM",
            reason="Manual custom activation",
            custom_config=custom_config,
        )
