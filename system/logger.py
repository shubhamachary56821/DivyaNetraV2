import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import RLock


class SystemLogger:

    _instance = None
    _instance_lock = RLock()

    def __new__(cls, config_manager=None):
        with cls._instance_lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
        return cls._instance

    def __init__(self, config_manager=None):
        if self._initialized:
            return

        self._initialized = True
        self._lock = RLock()

        self.config = config_manager

        project_root = Path(__file__).resolve().parent.parent
        self.log_directory = project_root / "data" / "logs"
        self.log_directory.mkdir(parents=True, exist_ok=True)

        self.log_file = self.log_directory / "divyanetra.log"

        max_size_mb = 10
        backups = 3

        if config_manager is not None:
            max_size_mb = config_manager.get(
                "storage.maximum_log_size_mb", 10
            )

            backup_enabled = config_manager.get(
                "storage.backup_enabled", True
            )

            backups = 3 if backup_enabled else 0

        self.logger = logging.getLogger("DivyaNetra")
        self.logger.setLevel(logging.DEBUG)
        self.logger.propagate = False

        # Avoid duplicate handlers during repeated initialization.
        if not self.logger.handlers:
            formatter = logging.Formatter(
                "%(asctime)s | %(levelname)-8s | "
                "%(name)s | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S"
            )

            console_handler = logging.StreamHandler()
            console_handler.setLevel(logging.INFO)
            console_handler.setFormatter(formatter)

            file_handler = RotatingFileHandler(
                filename=self.log_file,
                maxBytes=max_size_mb * 1024 * 1024,
                backupCount=backups,
                encoding="utf-8"
            )

            file_handler.setLevel(logging.DEBUG)
            file_handler.setFormatter(formatter)

            self.logger.addHandler(console_handler)
            self.logger.addHandler(file_handler)

        self.logger.info("DivyaNetra logging system initialized.")

    def debug(self, message):
        self.logger.debug(message)

    def info(self, message):
        self.logger.info(message)

    def warning(self, message):
        self.logger.warning(message)

    def error(self, message):
        self.logger.error(message)

    def critical(self, message):
        self.logger.critical(message)

    def exception(self, message):
        self.logger.exception(message)

    def get_logger(self):
        return self.logger

    def shutdown(self):
        with self._lock:
            for handler in list(self.logger.handlers):
                handler.flush()
                handler.close()
                self.logger.removeHandler(handler)

        self._initialized = False
        SystemLogger._instance = None
