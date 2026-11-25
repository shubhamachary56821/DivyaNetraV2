from datetime import datetime
from threading import RLock
from copy import deepcopy


class EventBus:

    def __init__(self, logger=None, event_store=None):
        self._subscribers = {}
        self._lock = RLock()
        self._event_count = 0

        self.logger = logger
        self.event_store = event_store

        print("[EventBus] Initialized.")

        if self.logger is not None:
            self.logger.info("EventBus initialized.")

        if self.event_store is not None:
            print("[EventBus] Persistent event storage connected.")

            if self.logger is not None:
                self.logger.info(
                    "EventBus connected to EventStore."
                )

    def subscribe(self, event_name, callback):
        if not callable(callback):
            raise TypeError("Callback must be callable.")

        event_name = str(event_name).strip()

        if not event_name:
            raise ValueError("Event name cannot be empty.")

        with self._lock:
            listeners = self._subscribers.setdefault(
                event_name, []
            )

            if callback not in listeners:
                listeners.append(callback)

        if self.logger is not None:
            self.logger.debug(
                f"Subscriber registered for event: {event_name}"
            )

        return callback

    def unsubscribe(self, event_name, callback):
        event_name = str(event_name).strip()

        with self._lock:
            listeners = self._subscribers.get(event_name, [])

            if callback in listeners:
                listeners.remove(callback)

                if not listeners:
                    self._subscribers.pop(event_name, None)

                if self.logger is not None:
                    self.logger.debug(
                        f"Subscriber removed from: {event_name}"
                    )

                return True

            return False

    def publish(self, event_name, data=None):
        event_name = str(event_name).strip()

        if not event_name:
            raise ValueError("Event name cannot be empty.")

        with self._lock:
            listeners = list(
                self._subscribers.get(event_name, [])
            )

            self._event_count += 1
            event_id = self._event_count

        event = {
            "event_id": event_id,
            "name": event_name,
            "timestamp": datetime.now().isoformat(),
            "data": deepcopy(data) if data is not None else {},
        }

        successful = 0
        failed = 0
        persisted = None

        # --------------------------------------------------
        # PERSIST EVENT
        # --------------------------------------------------

        if self.event_store is not None:
            try:
                self.event_store.record_event(
                    event_name=event["name"],
                    data=event["data"],
                    severity="INFO",
                    source="EventBus",
                    timestamp=event["timestamp"],
                )

                persisted = True

                if self.logger is not None:
                    self.logger.debug(
                        f"Event persisted: {event_name} "
                        f"| ID: {event_id}"
                    )

            except Exception as error:
                persisted = False

                print(
                    f"[EventBus] Persistence failure "
                    f"({event_name}): {error}"
                )

                if self.logger is not None:
                    self.logger.error(
                        f"Event persistence failure | "
                        f"Event: {event_name} | Error: {error}"
                    )

        # --------------------------------------------------
        # NOTIFY SUBSCRIBERS
        # --------------------------------------------------

        if self.logger is not None:
            self.logger.debug(
                f"Event published: {event_name} | ID: {event_id}"
            )

        for callback in listeners:
            try:
                callback(deepcopy(event))
                successful += 1

            except Exception as error:
                failed += 1

                print(
                    f"[EventBus] Listener error "
                    f"({event_name}): {error}"
                )

                if self.logger is not None:
                    self.logger.error(
                        f"Event listener failure | "
                        f"Event: {event_name} | "
                        f"Listener: {callback!r} | Error: {error}"
                    )

        return {
            "event_id": event_id,
            "listeners_notified": successful,
            "listeners_failed": failed,
            "event_persisted": persisted,
        }

    def listener_count(self, event_name=None):
        with self._lock:
            if event_name is not None:
                return len(
                    self._subscribers.get(event_name, [])
                )

            return sum(
                len(listeners)
                for listeners in self._subscribers.values()
            )

    @property
    def event_count(self):
        with self._lock:
            return self._event_count

    def clear(self):
        with self._lock:
            self._subscribers.clear()

        print("[EventBus] All subscribers cleared.")

        if self.logger is not None:
            self.logger.info("EventBus subscribers cleared.")
