import tempfile
import unittest
from pathlib import Path

from system.system_manager import (
    SystemManager,
    SystemManagerError,
)


class TestSystemManager(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self.temp_dir.name) / "events.db"
        )

        self.manager = SystemManager(
            database_path=str(self.database_path)
        )

    def tearDown(self):
        if self.manager.is_running:
            self.manager.shutdown("Test cleanup")

        self.temp_dir.cleanup()

    def test_startup_initializes_all_components(self):
        self.assertTrue(self.manager.start())

        health = self.manager.get_health()

        self.assertTrue(health["running"])
        self.assertFalse(health["failed"])

        for initialized in health[
            "components_initialized"
        ].values():
            self.assertTrue(initialized)

    def test_startup_remains_in_standby(self):
        self.manager.start()

        health = self.manager.get_health()

        self.assertEqual(
            health["mode"]["mode"],
            "STANDBY",
        )

        self.assertEqual(
            health["cameras"]["camera_a"]["state"],
            "STOPPED",
        )

        self.assertEqual(
            health["cameras"]["camera_b"]["state"],
            "STOPPED",
        )

    def test_start_is_idempotent(self):
        self.manager.start()

        first_camera_manager = self.manager.camera_manager
        first_event_store = self.manager.event_store

        self.assertTrue(self.manager.start())

        self.assertIs(
            self.manager.camera_manager,
            first_camera_manager,
        )

        self.assertIs(
            self.manager.event_store,
            first_event_store,
        )

    def test_health_reports_correct_status(self):
        self.manager.start()

        health = self.manager.get_health()

        self.assertTrue(health["running"])
        self.assertFalse(health["stopping"])
        self.assertFalse(health["failed"])
        self.assertIsNone(health["last_error"])

        self.assertEqual(
            health["mode"]["mode"],
            "STANDBY",
        )

        self.assertIsNotNone(
            health["mode_controller"]
        )

        self.assertIsNotNone(
            health["cameras"]
        )

    def test_shutdown_cleans_up_components(self):
        self.manager.start()

        self.assertTrue(
            self.manager.shutdown("Unit test")
        )

        self.assertFalse(self.manager.is_running)
        self.assertFalse(self.manager.is_stopping)
        self.assertFalse(self.manager.is_failed)

        self.assertIsNone(self.manager.config_manager)
        self.assertIsNone(self.manager.logger)
        self.assertIsNone(self.manager.event_store)
        self.assertIsNone(self.manager.event_bus)
        self.assertIsNone(self.manager.state_manager)
        self.assertIsNone(self.manager.camera_manager)
        self.assertIsNone(self.manager.mode_controller)

    def test_shutdown_is_idempotent(self):
        self.manager.start()

        self.assertTrue(self.manager.shutdown())
        self.assertTrue(self.manager.shutdown())

    def test_invalid_config_fails_safely(self):
        manager = SystemManager(
            config_path="/tmp/divyanetra_invalid_config.json",
            database_path=str(self.database_path),
        )

        with self.assertRaises(SystemManagerError):
            manager.start()

        self.assertFalse(manager.is_running)
        self.assertTrue(manager.is_failed)

        self.assertIsNone(manager.config_manager)
        self.assertIsNone(manager.logger)
        self.assertIsNone(manager.event_store)
        self.assertIsNone(manager.camera_manager)

        self.assertIsNotNone(manager._last_error)


if __name__ == "__main__":
    unittest.main()
