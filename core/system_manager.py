"""
DivyaNetra V2 - System Manager

Central lifecycle controller for core system components.

Safety:
- Always starts in STANDBY.
- Does not initialize hardware.
- Does not open cameras.
- Does not load AI models.
- Does not activate microphone.
- Startup is transactional.
"""

from threading import RLock

from core.config_manager import ConfigManager
from core.event_bus import EventBus
from core.event_store import EventStore
from core.state_manager import StateManager
from system.logger import SystemLogger


class SystemManagerError(Exception):
    """Raised when system lifecycle operations fail."""


class SystemManager:

    def __init__(self, config_path=None, database_path=None):
        self._lock = RLock()

        self.config_path = config_path
        self.database_path = database_path

        self.config = None
        self.logger = None
        self.event_store = None
        self.event_bus = None
        self.state_manager = None

        self._running = False
        self._stopping = False
        self._failed = False

        self._initialized_components = []

        print("[SystemManager] Created.")

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

    @property
    def status(self):
        with self._lock:
            if self._failed:
                current_status = "FAILED"
            elif self._stopping:
                current_status = "STOPPING"
            elif self._running:
                current_status = "RUNNING"
            else:
                current_status = "STOPPED"

        mode = (
            self.state_manager.current_mode
            if self.state_manager is not None
            else None
        )

        return {
            "status": current_status,
            "mode": mode,
            "initialized_components": list(
                self._initialized_components
            ),
        }

    def start(self):
        """Initialize core components without activating hardware."""

        with self._lock:
            if self._running:
                print("[SystemManager] Already running.")
                return False

            if self._stopping:
                raise SystemManagerError(
                    "Cannot start while shutdown is in progress."
                )

            self._failed = False
            self._initialized_components = []

        print("\n========== DIVYANETRA STARTUP ==========")

        try:
            # 1. Configuration
            self.config = ConfigManager(self.config_path)

            self._initialized_components.append("ConfigManager")

            # 2. Logging
            self.logger = SystemLogger(self.config)

            self._initialized_components.append("SystemLogger")

            self.logger.info("System startup initiated.")

            # 3. Persistent event storage
            self.event_store = EventStore(
                config_manager=self.config,
                database_path=self.database_path
            )

            self._initialized_components.append("EventStore")

            # 4. Event communication
            self.event_bus = EventBus(
                logger=self.logger,
                event_store=self.event_store
            )

            self._initialized_components.append("EventBus")

            # 5. State management
            self.state_manager = StateManager(
                config_manager=self.config,
                event_bus=self.event_bus,
                logger=self.logger
            )

            self._initialized_components.append("StateManager")

            # Enforce safe startup mode
            if not self.state_manager.is_standby:
                self.state_manager.standby(
                    reason="Safe startup enforcement"
                )

            # Mark running before publishing startup event
            with self._lock:
                self._running = True

            # 6. Record startup event
            self.event_bus.publish(
                "system.started",
                {
                    "status": "RUNNING",
                    "mode": self.state_manager.current_mode,
                    "components": list(
                        self._initialized_components
                    ),
                }
            )

            self.logger.info(
                "System startup completed successfully."
            )

            print("\n========== STARTUP COMPLETE ==========")
            print("Status: RUNNING")
            print("Mode: STANDBY")
            print("Hardware: NOT ACTIVATED")
            print("AI: NOT ACTIVATED")
            print("Microphone: NOT ACTIVATED")

            return True

        except Exception as error:
            print(
                f"\n[SystemManager] Startup failure: {error}"
            )

            if self.logger is not None:
                self.logger.exception(
                    "System startup failed."
                )

            self._cleanup_startup_failure()

            with self._lock:
                self._running = False
                self._stopping = False
                self._failed = True

            raise SystemManagerError(
                f"System startup failed: {error}"
            ) from error

    def _cleanup_startup_failure(self):
        """Release any components initialized before failure."""

        print("[SystemManager] Cleaning up failed startup.")

        if self.event_store is not None:
            try:
                self.event_store.close()
            except Exception as error:
                print(
                    f"[SystemManager] EventStore cleanup error: "
                    f"{error}"
                )

            self.event_store = None

        if self.logger is not None:
            try:
                self.logger.error(
                    "Startup cleanup completed."
                )
                self.logger.shutdown()
            except Exception as error:
                print(
                    f"[SystemManager] Logger cleanup error: "
                    f"{error}"
                )

            self.logger = None

        self.event_bus = None
        self.state_manager = None
        self.config = None

        self._initialized_components = []

    def shutdown(self, reason="Manual shutdown"):
        """Safely stop the core system."""

        with self._lock:
            if not self._running:
                print("[SystemManager] Not running.")
                return False

            if self._stopping:
                print("[SystemManager] Shutdown already in progress.")
                return False

            self._stopping = True

        print("\n========== DIVYANETRA SHUTDOWN ==========")

        shutdown_errors = []

        # 1. Return to safe standby
        if self.state_manager is not None:
            try:
                self.state_manager.standby(
                    reason=f"Shutdown: {reason}"
                )
            except Exception as error:
                shutdown_errors.append(
                    f"State transition: {error}"
                )

        # 2. Publish shutdown while database is still available
        if self.event_bus is not None:
            try:
                self.event_bus.publish(
                    "system.stopping",
                    {
                        "reason": str(reason),
                        "mode": (
                            self.state_manager.current_mode
                            if self.state_manager is not None
                            else None
                        ),
                    }
                )
            except Exception as error:
                shutdown_errors.append(
                    f"Shutdown event: {error}"
                )

        # 3. Close database
        if self.event_store is not None:
            try:
                self.event_store.close()
            except Exception as error:
                shutdown_errors.append(
                    f"EventStore shutdown: {error}"
                )

        # 4. Close logger last
        if self.logger is not None:
            try:
                if shutdown_errors:
                    self.logger.error(
                        "Shutdown completed with errors: "
                        + "; ".join(shutdown_errors)
                    )
                else:
                    self.logger.info(
                        "System shutdown completed successfully."
                    )

                self.logger.shutdown()

            except Exception as error:
                shutdown_errors.append(
                    f"Logger shutdown: {error}"
                )

        # Release references
        self.event_store = None
        self.event_bus = None
        self.state_manager = None
        self.config = None
        self.logger = None

        self._initialized_components = []

        with self._lock:
            self._running = False
            self._stopping = False

        print("Status: STOPPED")

        if shutdown_errors:
            print("Shutdown completed with errors:")

            for error in shutdown_errors:
                print("-", error)

        else:
            print("Shutdown completed cleanly.")

        return True

    def get_health(self):
        """Return a basic health snapshot of core components."""

        if not self._running:
            return {
                "healthy": False,
                "status": self.status["status"],
                "reason": "System is not running.",
            }

        components = {
            "config": self.config is not None
                      and self.config.loaded,

            "logger": self.logger is not None,

            "event_store": self.event_store is not None
                           and not self.event_store._closed,

            "event_bus": self.event_bus is not None,

            "state_manager": self.state_manager is not None,
        }

        healthy = all(components.values())

        return {
            "healthy": healthy,
            "status": self.status["status"],
            "mode": self.state_manager.current_mode,
            "components": components,
        }
