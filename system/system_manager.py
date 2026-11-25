"""
DivyaNetra V2 - System Manager

Central lifecycle coordinator for application subsystems.

Safety:
- Startup is software-only and leaves the system in STANDBY.
- Camera hardware is never activated during startup.
- Startup is transactional: partial initialization is rolled back.
- Shutdown requests STANDBY and stops cameras before closing storage/logging.
"""

from threading import RLock


class SystemManagerError(Exception):
    """Raised when system lifecycle operations fail."""


class SystemManager:

    def __init__(self, config_path=None, database_path=None):
        self.config_path = config_path
        self.database_path = database_path

        self.config_manager = None
        self.logger = None
        self.event_store = None
        self.event_bus = None
        self.state_manager = None
        self.camera_manager = None
        self.mode_controller = None

        self._lock = RLock()
        self._running = False
        self._stopping = False
        self._failed = False
        self._last_error = None
        self._started_at = None
        self._shutdown_at = None

    @property
    def is_running(self):
        with self._lock:
            return self._running

    @property
    def is_stopping(self):
        with self._lock:
            return self._stopping

    @property
    def is_failed(self):
        with self._lock:
            return self._failed

    def _log(self, level, message):
        if self.logger is not None:
            method = getattr(self.logger, level, None)
            if callable(method):
                method(message)

    def _publish(self, event_name, data=None):
        if self.event_bus is None:
            return

        try:
            self.event_bus.publish(event_name, data or {})
        except Exception as error:
            self._log(
                "warning",
                f"Event publication failed ({event_name}): {error}",
            )

    @staticmethod
    def _now():
        from datetime import datetime
        return datetime.now().isoformat()

    def start(self):
        """Initialize subsystems without activating hardware."""

        with self._lock:

            if self._running:
                return True

            if self._stopping:
                raise SystemManagerError(
                    "Cannot start while shutdown is in progress."
                )

            self._failed = False
            self._last_error = None

            try:

                from core.config_manager import ConfigManager
                from system.logger import SystemLogger
                from core.event_store import EventStore
                from core.event_bus import EventBus
                from core.state_manager import StateManager
                from cameras.camera_manager import CameraManager
                from system.mode_controller import ModeController

                # 1. Configuration
                self.config_manager = ConfigManager(
                    self.config_path
                )

                # 2. Logging
                self.logger = SystemLogger(
                    self.config_manager
                )

                # 3. Persistent storage
                self.event_store = EventStore(
                    self.config_manager,
                    database_path=self.database_path,
                )

                # 4. Event infrastructure
                self.event_bus = EventBus(
                    logger=self.logger,
                    event_store=self.event_store,
                )

                # 5. Logical state
                self.state_manager = StateManager(
                    self.config_manager,
                    event_bus=self.event_bus,
                    logger=self.logger,
                )

                # 6. Hardware manager (construction is inert)
                self.camera_manager = CameraManager(
                    self.config_manager,
                    event_bus=self.event_bus,
                    logger=self.logger,
                )

                # 7. Hardware/mode coordinator (construction is inert)
                self.mode_controller = ModeController(
                    self.config_manager,
                    self.state_manager,
                    self.camera_manager,
                    event_bus=self.event_bus,
                    logger=self.logger,
                )

                # Startup safety invariant
                if self.state_manager.current_mode != "STANDBY":
                    self.state_manager.standby(
                        "System startup safety initialization"
                    )

                self._running = True
                self._started_at = self._now()
                self._shutdown_at = None

                self._publish(
                    "system.started",
                    {
                        "mode": self.state_manager.current_mode,
                        "hardware_activated": False,
                        "manual_activation_required": True,
                    },
                )

                self._log(
                    "info",
                    "DivyaNetra system started in STANDBY.",
                )

                return True

            except Exception as error:

                self._last_error = str(error)
                self._failed = True
                self._running = False

                self._rollback_startup()

                raise SystemManagerError(
                    f"System startup failed: {error}"
                ) from error

    def _rollback_startup(self):
        """Best-effort cleanup of partially initialized components."""

        # Stop hardware first.
        if self.camera_manager is not None:
            try:
                self.camera_manager.shutdown()
            except Exception as error:
                self._log(
                    "error",
                    f"Camera cleanup during rollback failed: {error}",
                )

        # Close database.
        if self.event_store is not None:
            try:
                self.event_store.close()
            except Exception as error:
                self._log(
                    "error",
                    f"EventStore cleanup during rollback failed: {error}",
                )

        # Close logging handlers.
        if self.logger is not None:
            try:
                self.logger.shutdown()
            except Exception:
                pass

        self._clear_components()

    def _clear_components(self):
        self.config_manager = None
        self.logger = None
        self.event_store = None
        self.event_bus = None
        self.state_manager = None
        self.camera_manager = None
        self.mode_controller = None

    def shutdown(self, reason="Manual shutdown"):
        """Stop hardware and close resources in safe dependency order."""

        with self._lock:

            if not self._running and all(
                component is None
                for component in (
                    self.config_manager,
                    self.logger,
                    self.event_store,
                    self.event_bus,
                    self.state_manager,
                    self.camera_manager,
                    self.mode_controller,
                )
            ):
                return True

            self._stopping = True
            errors = []

            # Record shutdown request before closing storage.
            self._publish(
                "system.stopping",
                {"reason": str(reason)},
            )

            # Request STANDBY via ModeController.
            if self.mode_controller is not None:
                try:
                    self.mode_controller.standby(
                        reason=str(reason)
                    )
                except Exception as error:
                    errors.append(
                        f"ModeController standby: {error}"
                    )

            # Explicit shutdown even if mode transition failed.
            if self.camera_manager is not None:
                try:
                    result = self.camera_manager.shutdown()

                    if result is False:
                        errors.append(
                            "CameraManager shutdown returned False"
                        )

                except Exception as error:
                    errors.append(
                        f"Camera shutdown: {error}"
                    )

            # Storage remains available until camera shutdown attempts finish.
            if self.event_store is not None:
                try:
                    self.event_store.close()
                except Exception as error:
                    errors.append(
                        f"EventStore close: {error}"
                    )

            # Logger is the final resource to close.
            if self.logger is not None:
                try:
                    self.logger.info(
                        "DivyaNetra system shutdown complete."
                    )
                    self.logger.shutdown()
                except Exception as error:
                    errors.append(
                        f"Logger shutdown: {error}"
                    )

            self._running = False
            self._stopping = False
            self._shutdown_at = self._now()

            if errors:

                self._failed = True
                self._last_error = "; ".join(errors)

                self._clear_components()

                raise SystemManagerError(
                    "Shutdown completed with errors: "
                    + self._last_error
                )

            self._failed = False
            self._last_error = None

            self._clear_components()

            return True

    def get_health(self):
        """Return a snapshot of lifecycle and subsystem health."""

        with self._lock:

            camera_status = None

            if self.camera_manager is not None:
                try:
                    camera_status = (
                        self.camera_manager.get_status()
                    )
                except Exception as error:
                    camera_status = {
                        "error": str(error)
                    }

            mode_status = None

            if self.mode_controller is not None:
                try:
                    mode_status = (
                        self.mode_controller.get_status()
                    )
                except Exception as error:
                    mode_status = {
                        "error": str(error)
                    }

            state = None

            if self.state_manager is not None:
                try:
                    state = self.state_manager.get_state()
                except Exception as error:
                    state = {
                        "error": str(error)
                    }

            return {
                "running": self._running,
                "stopping": self._stopping,
                "failed": self._failed,
                "last_error": self._last_error,
                "started_at": self._started_at,
                "shutdown_at": self._shutdown_at,
                "mode": state,
                "mode_controller": mode_status,
                "cameras": camera_status,
                "components_initialized": {
                    "config": self.config_manager is not None,
                    "logger": self.logger is not None,
                    "event_store": self.event_store is not None,
                    "event_bus": self.event_bus is not None,
                    "state_manager": self.state_manager is not None,
                    "camera_manager": self.camera_manager is not None,
                    "mode_controller": self.mode_controller is not None,
                },
            }
