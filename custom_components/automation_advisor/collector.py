"""Live event collection and one-time recorder backfill.

Cause attribution: when an automation runs, Home Assistant fires `automation_triggered` with a
fresh Context, and every service call in that automation's actions runs with the same Context.
So a state change whose context id (or parent id, for scripts the automation started) belongs to
a recent `automation_triggered` event was caused by that automation. A change with a user id
came from a Home Assistant user (dashboard, app, voice); a change with neither came from the
device itself, typically a wall switch or remote.
"""

from __future__ import annotations

from collections.abc import Callable
import json
import logging
import time
from typing import Any

from homeassistant.const import EVENT_HOMEASSISTANT_STOP, EVENT_STATE_CHANGED, STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import Context, Event as HAEvent, HomeAssistant, State, callback
from homeassistant.helpers.event import async_track_time_interval

from . import analysis as A
from .archive import Archive

_LOGGER = logging.getLogger(__name__)

EVENT_AUTOMATION_TRIGGERED = "automation_triggered"
AUTOMATION_CONTEXT_TTL_S = 6 * 3600
FLUSH_INTERVAL_S = 60
IGNORED_STATES = {STATE_UNAVAILABLE, STATE_UNKNOWN, None}
# Cover transitions through "opening"/"closing"; only end states are interesting.
COVER_STATES = {"open", "closed"}


# Besides the switchable targets: context the AI and the backtester can use in conditions.
CONTEXT_DOMAINS = frozenset({"lock", "media_player", "climate", "scene", "script"})
BINARY_CLASSES = frozenset({
    "motion", "occupancy", "presence", "door", "garage_door", "opening", "window",
    "lock", "moisture", "plug", "power", "running", "sound", "vibration",
})
# Light level is logged only when it crosses one of these (lux), with the value.
LUX_STEPS = (5, 10, 20, 50, 100, 200, 500, 1000, 5000)


def is_tracked(entity_id: str, device_class: str | None, excluded: set[str]) -> bool:
    if entity_id in excluded:
        return False
    domain = entity_id.split(".", 1)[0]
    if domain in A.TARGET_DOMAINS or domain in A.PRESENCE_DOMAINS or domain in CONTEXT_DOMAINS:
        return True
    if domain == "binary_sensor":
        return device_class in BINARY_CLASSES
    if domain == "sensor":
        return device_class == "illuminance"
    return False


def _lux_step(value: float) -> int:
    return sum(value >= step for step in LUX_STEPS)


class _LastValue:
    """Last real value per entity. A device that drops out (unavailable/unknown) forgets it, so
    coming back is not mistaken for someone switching it; in-between cover states
    (opening/closing) are skipped without forgetting, so open -> closing -> closed is one
    open -> closed transition."""

    def __init__(self) -> None:
        self._last: dict[str, str | None] = {}

    def update(self, entity_id: str, raw: str | None, old_raw: str | None = None,
               device_class: str | None = None) -> tuple[str, str] | None:
        """Feed a raw state; returns (old, new) if this is a real transition."""
        domain = entity_id.split(".", 1)[0]
        if domain == "scene":  # state is the time of the last activation
            if raw in IGNORED_STATES:
                return None
            prev = self._last.get(entity_id, old_raw)
            self._last[entity_id] = raw
            return ("", "activated") if prev not in (None, raw) else None
        if domain == "sensor":  # illuminance: report crossings of LUX_STEPS
            try:
                value = float(raw)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                self._last[entity_id] = None
                return None
            prev = self._last.get(entity_id)
            if prev is None and old_raw is not None:
                try:
                    prev = str(float(old_raw))
                except ValueError:
                    prev = None
                self._last[entity_id] = prev
            if prev is not None and _lux_step(float(prev)) == _lux_step(value):
                return None
            self._last[entity_id] = str(round(value, 1))
            return (prev, str(round(value, 1))) if prev is not None else None
        if entity_id not in self._last and old_raw is not None:
            self._last[entity_id] = _normalise(entity_id, old_raw)
        if raw in IGNORED_STATES:
            self._last[entity_id] = None
            return None
        value = _normalise(entity_id, raw)
        if value is None:  # intermediate state
            return None
        last = self._last.get(entity_id)
        self._last[entity_id] = value
        if last is None or last == value:
            return None
        return last, value


def _normalise(entity_id: str, value: str | None) -> str | None:
    """Fold states so the analysis sees clean transitions (person zones -> not_home)."""
    if value in IGNORED_STATES:
        return None
    domain = entity_id.split(".", 1)[0]
    if domain in A.PRESENCE_DOMAINS:
        return "home" if value == "home" else "not_home"
    if domain == "media_player":
        return value if value in ("playing", "paused", "off") else "idle"
    if domain == "lock" and value in ("locking", "unlocking", "opening"):
        return None
    if domain == "cover" and value not in COVER_STATES:
        return None
    return value


def classify(
    context_id: Any, user_id: Any, parent_id: Any, automation_of: Callable[[Any], str | None]
) -> tuple[str, str | None]:
    """(cause, cause_ref) for a state change, see the module docstring."""
    ref = automation_of(context_id)
    if ref is None and parent_id is not None:
        ref = automation_of(parent_id)
    if ref is not None:
        return A.CAUSE_AUTOMATION, ref
    if user_id:
        return A.CAUSE_USER, None
    if parent_id:
        return A.CAUSE_OTHER, None
    return A.CAUSE_MANUAL, None


class Collector:
    """Turns state changes and automation runs into archive rows. Fed either by this Home
    Assistant's event bus (async_listen_local) or by a remote Home Assistant (remote.py)."""

    def __init__(self, hass: HomeAssistant, archive: Archive, excluded: set[str]) -> None:
        self.hass = hass
        self.archive = archive
        self.excluded = excluded
        self._buffer: list[A.Event] = []
        self._auto_ctx: dict[str, tuple[str, float]] = {}
        self._unsubs: list[Callable[[], None]] = []
        self._last = _LastValue()

    @callback
    def async_start(self) -> None:
        """Start the periodic flush (any source)."""
        self._unsubs.append(
            async_track_time_interval(self.hass, self._flush_cb, _seconds(FLUSH_INTERVAL_S))
        )
        self._unsubs.append(
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._flush_cb)
        )

    @callback
    def async_listen_local(self) -> None:
        """Collect from this Home Assistant's own event bus."""
        bus = self.hass.bus
        self._unsubs.append(bus.async_listen(EVENT_AUTOMATION_TRIGGERED, self._on_automation))
        self._unsubs.append(bus.async_listen(EVENT_STATE_CHANGED, self._on_state))

    async def async_stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        await self.async_flush()

    async def async_flush(self) -> None:
        if not self._buffer:
            return
        batch, self._buffer = self._buffer, []
        await self.hass.async_add_executor_job(self.archive.add, batch)

    async def _flush_cb(self, *_: Any) -> None:
        await self.async_flush()
        cutoff = time.time() - AUTOMATION_CONTEXT_TTL_S
        self._auto_ctx = {k: v for k, v in self._auto_ctx.items() if v[1] >= cutoff}

    def _automation_of(self, context_id: str | None) -> str | None:
        if context_id is None:
            return None
        hit = self._auto_ctx.get(context_id)
        return hit[0] if hit else None

    # -- source-independent ingestion -------------------------------------------------------

    @callback
    def ingest_automation(self, entity_id: str | None, context_id: str | None, ts: float) -> None:
        if not entity_id:
            return
        if context_id:
            self._auto_ctx[context_id] = (entity_id, time.time())
        self._buffer.append(A.Event(ts, entity_id, None, "triggered", A.CAUSE_OTHER))

    @callback
    def ingest_state(
        self,
        entity_id: str,
        old_raw: str | None,
        new_raw: str | None,
        device_class: str | None,
        ts: float,
        context_id: str | None,
        user_id: str | None,
        parent_id: str | None,
    ) -> None:
        if not is_tracked(entity_id, device_class, self.excluded):
            return
        change = self._last.update(entity_id, new_raw, old_raw, device_class)
        if change is None:
            return
        cause, ref = classify(context_id, user_id, parent_id, self._automation_of)
        self._buffer.append(A.Event(ts, entity_id, change[0], change[1], cause, ref))

    # -- local bus ---------------------------------------------------------------------------

    @callback
    def _on_automation(self, event: HAEvent) -> None:
        self.ingest_automation(
            event.data.get("entity_id"), event.context.id, event.time_fired_timestamp
        )

    @callback
    def _on_state(self, event: HAEvent) -> None:
        new: State | None = event.data.get("new_state")
        old: State | None = event.data.get("old_state")
        if new is None or old is None:
            return
        ctx: Context = event.context
        self.ingest_state(
            new.entity_id, old.state, new.state, new.attributes.get("device_class"),
            event.time_fired_timestamp, ctx.id, ctx.user_id, ctx.parent_id,
        )


def _seconds(s: float):
    from datetime import timedelta

    return timedelta(seconds=s)


# --------------------------------------------------------------------------------------------
# Recorder backfill
# --------------------------------------------------------------------------------------------


def backfill_from_recorder(
    hass: HomeAssistant, entity_ids: list[str], since: float
) -> list[A.Event]:
    """Read state changes (with context) and automation runs straight from the recorder
    database. Blocking; run in the recorder's executor. Best effort: the schema is internal,
    so any failure just means the advisor starts with an empty log."""
    from sqlalchemy import select

    from homeassistant.components.recorder.db_schema import (
        EventData,
        Events,
        EventTypes,
        States,
        StatesMeta,
    )
    from homeassistant.helpers.recorder import session_scope

    out: list[A.Event] = []
    with session_scope(hass=hass, read_only=True) as session:
        auto_ctx: dict[bytes, str] = {}
        type_id = session.execute(
            select(EventTypes.event_type_id).where(
                EventTypes.event_type == EVENT_AUTOMATION_TRIGGERED
            )
        ).scalar()
        if type_id is not None:
            rows = session.execute(
                select(Events.time_fired_ts, Events.context_id_bin, EventData.shared_data)
                .outerjoin(EventData, Events.data_id == EventData.data_id)
                .where(Events.event_type_id == type_id, Events.time_fired_ts >= since)
            ).all()
            for fired, ctx_bin, data in rows:
                try:
                    auto_entity = json.loads(data or "{}").get("entity_id")
                except ValueError:
                    auto_entity = None
                if not auto_entity:
                    continue
                if ctx_bin:
                    auto_ctx[ctx_bin] = auto_entity
                out.append(A.Event(fired, auto_entity, None, "triggered", A.CAUSE_OTHER))

        meta = dict(
            session.execute(
                select(StatesMeta.metadata_id, StatesMeta.entity_id).where(
                    StatesMeta.entity_id.in_(entity_ids)
                )
            ).all()
        )
        if not meta:
            return out
        rows = session.execute(
            select(
                States.metadata_id,
                States.state,
                States.last_updated_ts,
                States.context_id_bin,
                States.context_user_id_bin,
                States.context_parent_id_bin,
            )
            .where(States.metadata_id.in_(list(meta)), States.last_updated_ts >= since)
            .order_by(States.metadata_id, States.last_updated_ts)
        ).all()

    last = _LastValue()
    for meta_id, state, ts, ctx_bin, user_bin, parent_bin in rows:
        entity_id = meta[meta_id]
        change = last.update(entity_id, state)
        if change is None:
            continue
        cause, ref = classify(ctx_bin, user_bin, parent_bin, auto_ctx.get)
        out.append(A.Event(ts, entity_id, change[0], change[1], cause, ref))
    return out
