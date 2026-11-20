"""Appliance state confirmation, concurrent read replay, and event deduplication."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo

from homeassistant.util import dt as dt_util

from .api import StateUpdate, notification_records
from .const import MAX_EVENT_HISTORY, STATE_KEYS
from .controls import appliance_datetime, appliance_type, excluded_properties, uptime_seconds


@dataclass(frozen=True)
class StateRead:
    request: int
    confirmed_at: datetime | None
    error: Exception | None = None
    published: int = 0
    conflicted: bool = False
    # State keys status reads have reported that the published state lacks.
    missing_keys: tuple[str, ...] = ()
    # The reply was discarded for a snapshot that replaced state after a reconnect.
    superseded: bool = False


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
    """Track pushed updates, complete snapshots, and auth errors during a status read."""

    def __init__(self, transport_generation: int) -> None:
        self.transport_generation = transport_generation
        # Updates during a status read, each with whether it was a confirming snapshot
        # and whether it replaced cached state.
        self.updates: list[tuple[StateUpdate, bool, bool]] = []
        self.snapshot: dict | None = None
        self.error: Exception | None = None

    def fail(self, error: Exception) -> None:
        self.error = error

    def clear_error(self) -> None:
        self.error = None

    def record_update(
        self,
        properties: dict,
        *,
        full: bool,
        confirmed_snapshot: bool,
        replace: bool,
        complete: bool,
    ) -> None:
        self.error = None
        self.updates.append((StateUpdate(properties, full=full), confirmed_snapshot, replace))
        if confirmed_snapshot and complete:
            # The reference a failed request is rescued and checked against.
            self.snapshot = properties

    def replay(self, reference: dict, zone: tzinfo | None) -> tuple[dict, set[str], dict, bool]:
        """Replay the updates received during a read over its reference state.

        Returns the properties to publish, the keys whose timing against the
        reference is unknown, the values a replacing snapshot dropped, and whether
        such a snapshot restarted the replay.
        """
        properties = {}
        partial_keys: set[str] = set()
        dropped = {}
        restarted = False
        for update, confirmed_snapshot, replaced in self.updates:
            if update.full:
                if snapshot_is_older(update.properties, reference, zone):
                    continue
                if update.properties["appliance_model"] != reference.get("appliance_model"):
                    properties.clear()
                    partial_keys.clear()
                    dropped.clear()
                    reference = {}
                elif replaced:
                    # The replay starts over from a snapshot that replaced state. What it
                    # drops was still observed, so it stays recorded for the conflict
                    # check, and the reference keeps its clocks to order later snapshots.
                    partial_keys.update(properties)
                    dropped.update(properties)
                    properties.clear()
                    restarted = True
            if confirmed_snapshot:
                partial_keys.difference_update(update.properties)
            else:
                partial_keys.update(update.properties)
            properties.update(update.properties)
            reference = {**reference, **update.properties}
        return properties, partial_keys, dropped, restarted


class StateConfirmation:
    """Track per-source reported state keys and state confirmation metadata."""

    def __init__(self) -> None:
        self.confirmed_at: datetime | None = None
        self.count = 0
        self.transport_generation = 0
        self._model: str | None = None
        # The state keys each source has reported for the model. Status reads and
        # pushed snapshots can report different sets, so each is held to its own.
        self.required_keys: dict[str, set[str]] = {"snapshot": set(), "read": set()}
        # What each source's latest observation lacked of the keys it is held to, and
        # what the observation that last confirmed state lacked. Recorded, not enforced.
        self.missing_keys: dict[str, list[str]] = {"snapshot": [], "read": []}
        self.confirmed_missing_keys: list[str] = []

    def confirm(
        self,
        properties: dict,
        current: dict,
        source: str,
        transport_generation: int,
        *,
        certify: bool = True,
    ) -> bool:
        """Record what the source reports and lacks, and certify the state.

        A key the observation lacks is noted in missing_keys, for diagnostics
        and the State confirmed sensor; it does not keep the observation from
        confirming state. Sources do omit keys the integration has seen (the fridge's
        status reply lacks the notification history its snapshots carry, an oven's
        snapshots vary in `cav_remote_ready`), and refusing to confirm left appliances
        unavailable and controls refused for one missing key.
        """
        model = properties.get("appliance_model")
        if not isinstance(model, str) or not model:
            self.missing_keys[source] = ["appliance_model"]
            return False
        if model != self._model:
            if not certify:
                # A reply that concurrent updates contradict does not switch models.
                return False
            self._model = model
            for reported in self.required_keys.values():
                reported.clear()
        # Like discard_excluded, an observation without a usable type keeps the
        # known one; properties the app discards for it are not state.
        typed = properties if appliance_type(properties) is not None else current
        excluded = excluded_properties(typed) if typed.get("appliance_model") == model else set()
        # A read replaces state, so it is held to what reads report. A snapshot
        # merges into that state, so also to what snapshots report. A key only
        # ever pushed as a partial update is never expected, since no read or
        # snapshot can refresh it.
        required = set(self.required_keys["read"])
        if source == "snapshot":
            required |= self.required_keys["snapshot"]
        missing = self.missing_keys[source] = sorted(required - properties.keys() - excluded)
        reported = properties.keys() & (STATE_KEYS - {"notifs"} - excluded)
        if appliance_type(properties) is None:
            # An unusable type never reaches state.
            reported.discard("appliance_type")
        self.required_keys[source].update(reported)
        if not certify:
            return False
        self.confirmed_at = dt_util.utcnow()
        self.confirmed_missing_keys = missing
        self.count += 1
        self.transport_generation = transport_generation
        return True

    def missing_read_keys(self, data: dict) -> list[str]:
        """The keys status reads have reported for the model that this state lacks."""
        return sorted(self.required_keys["read"] - data.keys() - excluded_properties(data))


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
