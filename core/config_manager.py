"""
DivyaNetra V2 - Configuration Manager

Loads, validates and safely provides access to application settings.
No hardware or AI initialization occurs here.
"""

import json
from pathlib import Path
from copy import deepcopy


class ConfigError(Exception):
    """Raised when configuration is invalid or cannot be loaded."""


class ConfigManager:

    def __init__(self, config_path=None):
        self.config_path = (
            Path(config_path) if config_path
            else Path(__file__).resolve().parent.parent / "config" / "settings.json"
        )

        self._config = {}
        self.load()

    def load(self):
        """Load and validate configuration from JSON."""

        try:
            with self.config_path.open("r", encoding="utf-8") as file:
                config = json.load(file)

        except FileNotFoundError:
            raise ConfigError(f"Configuration file not found: {self.config_path}")

        except json.JSONDecodeError as error:
            raise ConfigError(f"Invalid JSON: {error}")

        self._validate(config)
        self._config = config

        return True

    def _validate(self, config):

        required_sections = [
            "application",
            "system",
            "modes",
            "cameras",
            "perception",
            "voice",
            "alerts",
            "severity_actions",
            "sensors",
            "storage",
        ]

        for section in required_sections:
            if section not in config:
                raise ConfigError(f"Missing configuration section: {section}")

        # Application validation
        if config["application"].get("manual_activation") is not True:
            raise ConfigError("Manual activation must remain enabled.")

        default_mode = config["application"].get("default_mode")

        if default_mode not in config["modes"]:
            raise ConfigError(f"Invalid default mode: {default_mode}")

        if not config["modes"][default_mode].get("enabled", False):
            raise ConfigError(f"Default mode is disabled: {default_mode}")

        # Camera validation
        for camera_name in ("camera_a", "camera_b"):
            camera = config["cameras"].get(camera_name)

            if camera is None:
                raise ConfigError(
                    f"Camera configuration missing: {camera_name}"
                )

            index = camera.get("device_index")

            if not isinstance(index, int) or index < 0:
                raise ConfigError(
                    f"Invalid device index for {camera_name}"
                )

            fps = camera.get("fps", 15)

            if not isinstance(fps, (int, float)) or not 1 <= fps <= 60:
                raise ConfigError(
                    f"Invalid FPS for {camera_name}"
                )

            width = camera.get("width", 640)
            height = camera.get("height", 480)

            if width <= 0 or height <= 0:
                raise ConfigError(
                    f"Invalid resolution for {camera_name}"
                )

        # Perception confidence validation
        for name, module in config["perception"].items():

            if "confidence" in module:
                confidence = module["confidence"]

                if not isinstance(confidence, (int, float)):
                    raise ConfigError(f"Invalid confidence: {name}")

                if not 0.0 <= confidence <= 1.0:
                    raise ConfigError(f"Confidence out of range: {name}")

        # Severity validation
        required_severities = [
            "INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"
        ]

        for severity in required_severities:
            if severity not in config["severity_actions"]:
                raise ConfigError(f"Missing severity level: {severity}")

        print("[ConfigManager] Configuration validation passed.")

    def get(self, key, default=None):
        """
        Access configuration using dotted paths.

        Example:
            get("cameras.A.index")
            get("application.default_mode")
        """

        value = self._config

        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return deepcopy(default)

            value = value[part]

        return deepcopy(value)

    def get_section(self, section):
        """Return an entire configuration section safely."""

        if section not in self._config:
            raise ConfigError(f"Unknown section: {section}")

        return deepcopy(self._config[section])

    def reload(self):
        """Reload configuration from disk."""

        return self.load()

    @property
    def config_path(self):
        return self._config_path

    @config_path.setter
    def config_path(self, value):
        self._config_path = Path(value)

    @property
    def loaded(self):
        return bool(self._config)
