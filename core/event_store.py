import json
import sqlite3
from datetime import datetime
from pathlib import Path
from threading import RLock


class EventStoreError(Exception):
    pass


class EventStore:

    VALID_SEVERITIES = {
        "DEBUG",
        "INFO",
        "WARNING",
        "ERROR",
        "CRITICAL",
    }

    def __init__(self, config_manager=None, database_path=None):
        self._lock = RLock()
        self._closed = False

        project_root = Path(__file__).resolve().parent.parent

        if database_path is None:
            if config_manager is not None:
                configured_path = config_manager.get(
                    "storage.database_path",
                    "data/events.db"
                )
            else:
                configured_path = "data/events.db"

            database_path = Path(configured_path)

            if not database_path.is_absolute():
                database_path = project_root / database_path

        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(
            parents=True,
            exist_ok=True
        )

        self._connection = sqlite3.connect(
            str(self.database_path),
            timeout=10,
            check_same_thread=False
        )

        self._connection.row_factory = sqlite3.Row

        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA foreign_keys=ON")
            self._connection.execute("PRAGMA busy_timeout=10000")

            self._create_schema()

        print(
            f"[EventStore] Database ready: "
            f"{self.database_path}"
        )

    def _create_schema(self):
        with self._connection:
            self._connection.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    event_name TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    source TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
            """)

            self._connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_events_timestamp
                ON events(timestamp)
            """)

            self._connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_events_name
                ON events(event_name)
            """)

            self._connection.execute("""
                CREATE INDEX IF NOT EXISTS idx_events_severity
                ON events(severity)
            """)

    def record_event(
        self,
        event_name,
        data=None,
        severity="INFO",
        source="system",
        timestamp=None
    ):
        event_name = str(event_name).strip()
        severity = str(severity).upper()
        source = str(source).strip()

        if not event_name:
            raise ValueError("Event name cannot be empty.")

        if severity not in self.VALID_SEVERITIES:
            raise ValueError(f"Invalid severity: {severity}")

        if not source:
            raise ValueError("Event source cannot be empty.")

        timestamp = timestamp or datetime.now().isoformat()

        try:
            payload = json.dumps(
                data if data is not None else {},
                ensure_ascii=False
            )
        except (TypeError, ValueError) as error:
            raise EventStoreError(
                f"Event payload is not JSON serializable: {error}"
            ) from error

        with self._lock:
            if self._closed:
                raise EventStoreError("EventStore is closed.")

            try:
                cursor = self._connection.execute(
                    """
                    INSERT INTO events (
                        timestamp,
                        event_name,
                        severity,
                        source,
                        payload
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        timestamp,
                        event_name,
                        severity,
                        source,
                        payload
                    )
                )

                self._connection.commit()

                return cursor.lastrowid

            except sqlite3.Error as error:
                self._connection.rollback()
                raise EventStoreError(
                    f"Database write failed: {error}"
                ) from error

    def get_recent(
        self,
        limit=100,
        event_name=None,
        severity=None
    ):
        if not isinstance(limit, int) or limit <= 0:
            raise ValueError("Limit must be a positive integer.")

        query = "SELECT * FROM events"
        conditions = []
        parameters = []

        if event_name is not None:
            conditions.append("event_name = ?")
            parameters.append(str(event_name))

        if severity is not None:
            severity = str(severity).upper()

            if severity not in self.VALID_SEVERITIES:
                raise ValueError(f"Invalid severity: {severity}")

            conditions.append("severity = ?")
            parameters.append(severity)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY id DESC LIMIT ?"
        parameters.append(limit)

        with self._lock:
            if self._closed:
                raise EventStoreError("EventStore is closed.")

            rows = self._connection.execute(
                query,
                parameters
            ).fetchall()

        results = []

        for row in rows:
            event = dict(row)

            try:
                event["payload"] = json.loads(event["payload"])
            except json.JSONDecodeError:
                event["payload"] = {}

            results.append(event)

        return results

    def count_events(self):
        with self._lock:
            if self._closed:
                raise EventStoreError("EventStore is closed.")

            row = self._connection.execute(
                "SELECT COUNT(*) AS total FROM events"
            ).fetchone()

            return row["total"]

    def integrity_check(self):
        with self._lock:
            if self._closed:
                raise EventStoreError("EventStore is closed.")

            result = self._connection.execute(
                "PRAGMA integrity_check"
            ).fetchone()

            return result[0]

    def close(self):
        with self._lock:
            if self._closed:
                return

            self._connection.close()
            self._closed = True

        print("[EventStore] Database connection closed.")
