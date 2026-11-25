from datetime import datetime
from threading import RLock


class StateTransitionError(Exception):
    pass


class StateManager:

    VALID_MODES = {
        "STANDBY",
        "INTERACTIVE",
        "MONITORING",
        "CUSTOM",
    }

    def __init__(self, config_manager, event_bus=None, logger=None):
        self.config = config_manager
        self.event_bus = event_bus
        self.logger = logger
        self._lock = RLock()
        self._listeners = []

        self._current_mode = "STANDBY"
        self._entered_at = datetime.now()
        self._transition_reason = "Application initialization"

        print("[StateManager] Initialized in STANDBY.")

    @property
    def current_mode(self):
        with self._lock:
            return self._current_mode

    @property
    def is_standby(self):
        return self.current_mode == "STANDBY"

    def get_state(self):
        with self._lock:
            return {
                "mode": self._current_mode,
                "is_standby": self._current_mode == "STANDBY",
                "entered_at": self._entered_at.isoformat(),
                "reason": self._transition_reason,
            }

    def add_listener(self, callback):
        if not callable(callback):
            raise TypeError("Listener must be callable.")

        with self._lock:
            if callback not in self._listeners:
                self._listeners.append(callback)

    def remove_listener(self, callback):
        with self._lock:
            if callback in self._listeners:
                self._listeners.remove(callback)

    def set_mode(self, new_mode, reason="Manual mode change"):
        new_mode = str(new_mode).upper()

        if new_mode not in self.VALID_MODES:
            raise StateTransitionError(f"Unknown mode: {new_mode}")

        if new_mode != "STANDBY":
            if not self.config.get(
                f"modes.{new_mode}.enabled", False
            ):
                raise StateTransitionError(
                    f"Mode is disabled: {new_mode}"
                )

        with self._lock:
            old_mode = self._current_mode

            if old_mode == new_mode:
                return False

            self._current_mode = new_mode
            self._entered_at = datetime.now()
            self._transition_reason = str(reason)
            listeners = list(self._listeners)

            event = {
                "previous_mode": old_mode,
                "current_mode": new_mode,
                "timestamp": self._entered_at.isoformat(),
                "reason": str(reason),
            }

        print(f"[StateManager] {old_mode} -> {new_mode}")

        if self.logger is not None:
            self.logger.info(
                f"Mode transition: {old_mode} -> {new_mode} | "
                f"Reason: {reason}"
            )

        for listener in listeners:
            try:
                listener(event.copy())
            except Exception as error:
                print(f"[StateManager] Listener error: {error}")

                if self.logger is not None:
                    self.logger.error(
                        f"State listener failed: {error}"
                    )

        if self.event_bus is not None:
            self.event_bus.publish(
                "system.mode_changed",
                event.copy()
            )

        return True

    def standby(self, reason="System deactivated"):
        return self.set_mode("STANDBY", reason)

    def activate_interactive(self):
        return self.set_mode("INTERACTIVE", "Manual activation")

    def activate_monitoring(self):
        return self.set_mode("MONITORING", "Manual activation")

    def activate_custom(self):
        return self.set_mode("CUSTOM", "Manual activation")
