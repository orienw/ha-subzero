"""Appliance state filtering, concurrent read merging, and event deduplication."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo

from homeassistant.util import dt as dt_util

from .api import StateUpdate, notification_records
from .const import MAX_EVENT_HISTORY, STATE_KEYS
from .controls import appliance_datetime, appliance_type, excluded_properties, uptime_seconds

CONFLICT_IGNORED_KEYS = frozenset({"notifs", "time", "uptime", "ap_rssi", "appliance_type"})


@dataclass(frozen=True)
class StateRead:
    request: int
    confirmed_at: datetime | None
    error: Exception | None = None
    conflicted: bool = False
    epoch: int = 0


def snapshot_is_older(properties: dict, reference: dict, zone: tzinfo | None) -> bool:
    if properties.get("appliance_model") != reference.get("appliance_model"):
        return False
    observed = appliance_datetime(properties.get("time"), zone)
    previous = appliance_datetime(reference.get("time"), zone)
    uptime = properties.get("uptime")
    previous_uptime = reference.get("uptime")
    if (
        observed is None
        or previous is None
        or not isinstance(uptime, str)
        or not isinstance(previous_uptime, str)
    ):
        return False
    uptime_s = uptime_seconds(uptime)
    previous_uptime_s = uptime_seconds(previous_uptime)
    # Truncated appliance uptime can have minute precision. Requiring both
    # clocks to regress also allows clock adjustments and ordinary reboots.
    return (
        uptime_s is not None
        and previous_uptime_s is not None
        and uptime_s <= previous_uptime_s - 60
        and observed <= previous - timedelta(minutes=1)
    )


def with_known_type(data: dict, known: dict) -> dict:
    """Like the app, an observation without a usable type keeps the type already
    known for the same appliance model."""
    if (
        appliance_type(data) is None
        and appliance_type(known) is not None
        and data.get("appliance_model") == known.get("appliance_model")
    ):
        return {**data, "appliance_type": known["appliance_type"]}
    return data


def discard_excluded(data: dict, known: dict) -> dict:
    """Drop the properties the app discards for the appliance type."""
    data = with_known_type(data, known)
    excluded = excluded_properties(data)
    return {key: value for key, value in data.items() if key not in excluded}


class PendingRead:
    """Collect pushed updates arriving during a status read and merge them over the reply."""

    def __init__(self, epoch: int) -> None:
        self.epoch = epoch
        self.updates: list[StateUpdate] = []
        self.error: Exception | None = None

    def fail(self, error: Exception) -> None:
        self.error = error

    def clear_error(self) -> None:
        self.error = None

    def record_update(self, properties: dict, *, full: bool) -> None:
        self.error = None
        self.updates.append(StateUpdate(properties, full=full))

    def merge(self, reply: dict, known: dict, zone: tzinfo | None) -> tuple[dict, bool]:
        """Merge pushes received during a read over its reply.

        Returns (published_data, conflicted). Arrival during a GET does not
        establish when a partial change happened, so it is published immediately
        but marks the read conflicted when it disagrees with the reply.
        """
        pushed: dict = {}
        partial_keys: set[str] = set()
        reference = reply
        for update in self.updates:
            if update.full:
                if snapshot_is_older(update.properties, reference, zone):
                    continue
                if update.properties.get("appliance_model") != reference.get("appliance_model"):
                    pushed.clear()
                    partial_keys.clear()
                    reference = {}
                else:
                    partial_keys.difference_update(update.properties)
            else:
                partial_keys.update(update.properties)
            pushed.update(update.properties)
            reference = {**reference, **update.properties}

        model = pushed.get("appliance_model")
        other_model = bool(
            isinstance(model, str) and model and model != reply.get("appliance_model")
        )
        base = {} if other_model else reply
        published_for_type = with_known_type({**base, **pushed}, known)
        conflicted = any(
            key not in reply
            or key not in pushed
            or pushed[key] != reply[key]
            or type(pushed[key]) is not type(reply[key])
            for key in partial_keys
            - CONFLICT_IGNORED_KEYS
            - excluded_properties(published_for_type)
        )
        history = notification_records(base) if "notifs" in base else None
        merged = {
            **{key: value for key, value in base.items() if key in STATE_KEYS},
            **pushed,
        }
        if history is not None:
            merged["notifs"] = [*history, *pushed.get("notifs", [])]
        return discard_excluded(merged, known), conflicted


class EventTracker:
    """Deduplicate appliance events across state history, stream, and Firebase copies."""

    def __init__(self, cutoff: datetime) -> None:
        self._cutoff = cutoff
        self.seen: dict[tuple[datetime, int, int], str] = {}
        self._paired: set[tuple[datetime, int, int]] = set()
        self._history_seen = False

    def extract_records(
        self, properties: dict, zone: tzinfo | None, *, history: bool
    ) -> tuple[bool, list[tuple[datetime, int, int]]]:
        """Return whether records should be delivered and their sorted event tuples."""
        # The first snapshot or status read sets the baseline, even without history.
        # Later history still passes through the startup timestamp cutoff.
        deliver = self._history_seen or not history
        self._history_seen |= history
        events = {}
        for record in notification_records(properties):
            timestamp = appliance_datetime(record["timestamp"], zone)
            if timestamp is not None:
                # Identify by instant: a local time in a repeated hour never equals a UTC time.
                identity = (timestamp.astimezone(UTC), record["notif_seq"], record["notif_type"])
                events[identity] = timestamp
        return deliver, [
            (events[identity], identity[1], identity[2]) for identity in sorted(events)
        ]

    def record(
        self,
        timestamp: datetime,
        sequence: int,
        code: int,
        *,
        deliver: bool = True,
        source: str = "channel",
    ) -> dict | None:
        """Record an event and return its payload if it should be delivered."""
        identity = (timestamp.astimezone(UTC), sequence, code)
        if identity[0] < self._cutoff:
            return None
        if (known := self.seen.get(identity)) is not None:
            if (known == "firebase") != (source == "firebase"):
                self._paired.add(identity)
            return None
        # Firebase rounds event time to whole seconds; stream and history keep
        # milliseconds. Pair each event with at most one copy from the other path.
        counterpart = min(
            (
                old
                for old, origin in self.seen.items()
                if (origin == "firebase") != (source == "firebase")
                and old not in self._paired
                and old[1:] == (sequence, code)
                and abs(old[0] - identity[0]) < timedelta(seconds=1)
            ),
            key=lambda old: abs(old[0] - identity[0]),
            default=None,
        )
        self.seen[identity] = source
        if counterpart is not None:
            self._paired.update((identity, counterpart))
        if len(self.seen) > MAX_EVENT_HISTORY:
            oldest = min(self.seen)
            del self.seen[oldest]
            self._paired.discard(oldest)
            self._cutoff = max(self._cutoff, oldest[0] + timedelta(microseconds=1))
        if counterpart is not None or not deliver:
            return None
        return {
            "code": code,
            "sequence": sequence,
            "appliance_timestamp": timestamp.isoformat(),
            "received_at": dt_util.utcnow().isoformat(),
            "source": source,
        }
