import unittest

from system.mode_controller import ModeController, ModeControllerError


class FakeConfig:
    def __init__(self):
        self.data = {
            "cameras": {
                "camera_a": {"enabled": True},
                "camera_b": {"enabled": True},
            },
            "modes": {
                "STANDBY": {"enabled": True},
                "INTERACTIVE": {"enabled": True, "camera_a": True},
                "MONITORING": {"enabled": True, "camera_b": True},
                "CUSTOM": {"enabled": True},
            },
        }

    def get(self, dotted_key, default=None):
        value = self.data
        for part in dotted_key.split("."):
            if not isinstance(value, dict) or part not in value:
                return default
            value = value[part]
        return value


class FakeStateManager:
    VALID_MODES = {"STANDBY", "INTERACTIVE", "MONITORING", "CUSTOM"}

    def __init__(self):
        self.current_mode = "STANDBY"
        self.transitions = []

    def set_mode(self, mode, reason=""):
        if mode not in self.VALID_MODES:
            raise ValueError(f"Invalid mode: {mode}")
        self.current_mode = mode
        self.transitions.append((mode, reason))
        return True

    def standby(self, reason=""):
        return self.set_mode("STANDBY", reason)


class FakeCameraManager:
    def __init__(self):
        self.states = {
            "camera_a": "STOPPED",
            "camera_b": "STOPPED",
        }
        self.start_calls = []
        self.stop_calls = 0
        self.fail_start = set()
        self.fail_stop = False

    def start_camera(self, name):
        self.start_calls.append(name)
        if name in self.fail_start:
            return False
        self.states[name] = "RUNNING"
        return True

    def stop_all(self, timeout=5.0):
        self.stop_calls += 1
        if self.fail_stop:
            return False
        for name in self.states:
            self.states[name] = "STOPPED"
        return True

    def get_camera_status(self, name):
        return {"state": self.states[name]}


class FakeEventBus:
    def __init__(self):
        self.events = []

    def publish(self, name, data):
        self.events.append((name, data))


class ModeControllerTests(unittest.TestCase):
    def setUp(self):
        self.config = FakeConfig()
        self.state = FakeStateManager()
        self.cameras = FakeCameraManager()
        self.events = FakeEventBus()

        self.controller = ModeController(
            self.config,
            self.state,
            self.cameras,
            event_bus=self.events,
        )

    def test_construction_does_not_activate_hardware(self):
        self.assertEqual(self.cameras.start_calls, [])
        self.assertEqual(self.cameras.stop_calls, 0)
        self.assertEqual(self.state.current_mode, "STANDBY")

    def test_interactive_activates_only_camera_a(self):
        self.assertTrue(self.controller.activate_interactive())
        self.assertEqual(self.state.current_mode, "INTERACTIVE")
        self.assertEqual(self.cameras.states["camera_a"], "RUNNING")
        self.assertEqual(self.cameras.states["camera_b"], "STOPPED")
        self.assertEqual(self.cameras.start_calls, ["camera_a"])

    def test_monitoring_activates_only_camera_b(self):
        self.assertTrue(self.controller.activate_monitoring())
        self.assertEqual(self.state.current_mode, "MONITORING")
        self.assertEqual(self.cameras.states["camera_a"], "STOPPED")
        self.assertEqual(self.cameras.states["camera_b"], "RUNNING")
        self.assertEqual(self.cameras.start_calls, ["camera_b"])

    def test_switching_modes_stops_previous_camera(self):
        self.controller.activate_interactive()
        self.controller.activate_monitoring()
        self.assertEqual(self.cameras.states["camera_a"], "STOPPED")
        self.assertEqual(self.cameras.states["camera_b"], "RUNNING")
        self.assertEqual(self.state.current_mode, "MONITORING")

    def test_custom_mode_can_activate_both_cameras(self):
        self.assertTrue(
            self.controller.activate_custom(
                {"camera_a": True, "camera_b": True}
            )
        )
        self.assertEqual(self.state.current_mode, "CUSTOM")
        self.assertEqual(self.cameras.states["camera_a"], "RUNNING")
        self.assertEqual(self.cameras.states["camera_b"], "RUNNING")
        self.assertEqual(
            self.controller.get_status()["custom_config"],
            {"camera_a": True, "camera_b": True},
        )

    def test_custom_config_rejects_non_boolean_values(self):
        with self.assertRaises(ModeControllerError):
            self.controller.activate_custom({"camera_a": "false"})
        self.assertEqual(self.state.current_mode, "STANDBY")
        self.assertEqual(self.cameras.states["camera_a"], "STOPPED")

    def test_custom_config_rejects_unknown_keys(self):
        with self.assertRaises(ModeControllerError):
            self.controller.activate_custom({"camera_c": True})
        self.assertEqual(self.state.current_mode, "STANDBY")

    def test_custom_mode_default_config_is_valid(self):
        self.assertTrue(self.controller.activate_custom(None))
        self.assertEqual(self.state.current_mode, "CUSTOM")
        self.assertEqual(self.cameras.start_calls, [])

    def test_disabled_camera_cannot_be_activated(self):
        self.config.data["cameras"]["camera_a"]["enabled"] = False

        with self.assertRaises(ModeControllerError):
            self.controller.activate_interactive()

        self.assertEqual(self.state.current_mode, "STANDBY")
        self.assertEqual(self.cameras.start_calls, [])

    def test_camera_activation_failure_falls_back_to_standby(self):
        self.cameras.fail_start.add("camera_a")

        with self.assertRaises(ModeControllerError):
            self.controller.activate_interactive()

        self.assertEqual(self.state.current_mode, "STANDBY")
        self.assertEqual(self.cameras.states["camera_a"], "STOPPED")

        status = self.controller.get_status()
        self.assertIsNotNone(status["last_error"])
        self.assertTrue(status["hardware_stopped"])
        self.assertIsNone(status["fallback_error"])

    def test_camera_stop_failure_is_reported(self):
        self.cameras.fail_stop = True

        with self.assertRaises(ModeControllerError):
            self.controller.activate_interactive()

        status = self.controller.get_status()
        self.assertFalse(status["hardware_stopped"])
        self.assertIsNotNone(status["fallback_error"])
        self.assertEqual(self.state.current_mode, "STANDBY")

    def test_transition_events_are_published(self):
        self.controller.activate_interactive()

        event_names = [name for name, _ in self.events.events]
        self.assertIn("system.mode_transition_started", event_names)
        self.assertIn("system.mode_transition_completed", event_names)

    def test_same_mode_request_is_noop(self):
        self.assertFalse(self.controller.transition_to("STANDBY"))
        self.assertEqual(self.cameras.stop_calls, 0)
        self.assertEqual(self.cameras.start_calls, [])

    def test_invalid_mode_is_rejected_without_hardware_activity(self):
        with self.assertRaises(ModeControllerError):
            self.controller.transition_to("NOT_A_MODE")

        self.assertEqual(self.cameras.stop_calls, 0)
        self.assertEqual(self.cameras.start_calls, [])

    def test_disabled_mode_is_rejected(self):
        self.config.data["modes"]["MONITORING"]["enabled"] = False

        with self.assertRaises(ModeControllerError):
            self.controller.activate_monitoring()

        self.assertEqual(self.cameras.start_calls, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
