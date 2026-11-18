"""Where the advisor gets its data and where it applies changes.

LocalSource  - the Home Assistant this integration runs in (normal install).
RemoteSource - another Home Assistant on the network, through its websocket and REST APIs with a
               long-lived access token. Lets you try the advisor against your real home from a
               separate (test) instance. Read-only unless "allow changes" is switched on: in
               read-only mode nothing is ever written to the remote Home Assistant.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import date, datetime, timedelta, tzinfo
import logging
import time
from typing import Any
from zoneinfo import ZoneInfo

import aiohttp

from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.sun import get_astral_event_date
from homeassistant.util import dt as dt_util

from . import analysis as A, automations as autos
from .archive import Archive
from .collector import Collector, _LastValue, backfill_from_recorder, is_tracked

_LOGGER = logging.getLogger(__name__)
DAY = 86400


class Source:
    """Interface shared by both sources."""

    can_write = True
    label = "this Home Assistant"

    async def async_start(self, collector: Collector, archive: Archive, lookback_s: float) -> None: ...
    async def async_stop(self) -> None: ...
    async def async_prepare(self, since: float) -> None:
        """Called before every analysis run."""

    @property
    def tz(self) -> tzinfo: ...
    def status(self) -> dict: return {"mode": "local", "label": self.label, "can_write": self.can_write}
    def entity_exists(self, entity_id: str) -> bool: ...
    def entity_ids(self) -> set[str]: ...
    def entity_info(self, entity_ids: set[str]) -> dict[str, A.EntityInfo]: ...
    async def async_automations(self) -> list[A.ExistingAutomation]: ...
    def sun_table(self, days: set[date]) -> dict[date, tuple[float, float]]: ...
    async def async_create(self, config: dict) -> str: ...
    async def async_update(self, config_id: str, config: dict) -> dict: ...
    async def async_delete(self, config_id: str) -> dict: ...
    async def async_disable(self, entity_id: str) -> None: ...
    async def async_traces(self, auto: A.ExistingAutomation) -> list[dict]:
        """Short summaries of the automation's last runs (Home Assistant traces)."""
        return []
    async def async_catalog(self) -> dict:
        """Areas, devices (newest first) and entities, for building automations on request."""
        return {"areas": [], "devices": [], "entities": {}}
    async def async_device_triggers(self, device_id: str) -> list[dict]:
        """What a device can trigger (e.g. a remote's button presses)."""
        return []


# --------------------------------------------------------------------------------------------
# Local
# --------------------------------------------------------------------------------------------


class LocalSource(Source):
    def __init__(self, hass: HomeAssistant, excluded: set[str]) -> None:
        self.hass = hass
        self.excluded = excluded
        self._archive: Archive | None = None

    async def async_start(self, collector, archive, lookback_s) -> None:
        self._archive = archive
        collector.async_listen_local()

    async def async_prepare(self, since: float) -> None:
        """One-time backfill from the recorder database."""
        archive = self._archive
        if await self.hass.async_add_executor_job(archive.get_meta, "backfilled"):
            return
        if "recorder" not in self.hass.config.components:
            return
        ids = [s.entity_id for s in self.hass.states.async_all()
               if is_tracked(s.entity_id, s.attributes.get("device_class"), self.excluded)]
        try:
            from homeassistant.helpers.recorder import get_instance

            events = await get_instance(self.hass).async_add_executor_job(
                backfill_from_recorder, self.hass, ids, since
            )
            added = await self.hass.async_add_executor_job(archive.add, events)
            _LOGGER.info("Backfilled %s events from the recorder", added)
        except Exception as err:  # internal schema; never fatal
            _LOGGER.warning("Recorder backfill failed, starting with an empty log: %s", err)
        await self.hass.async_add_executor_job(archive.set_meta, "backfilled", str(time.time()))

    @property
    def tz(self) -> tzinfo:
        return dt_util.get_default_time_zone()

    def entity_exists(self, entity_id: str) -> bool:
        return (self.hass.states.get(entity_id) is not None
                or er.async_get(self.hass).async_get(entity_id) is not None)

    def entity_ids(self) -> set[str]:
        return {s.entity_id for s in self.hass.states.async_all()}

    def entity_info(self, entity_ids: set[str]) -> dict[str, A.EntityInfo]:
        ent_reg, dev_reg, area_reg = er.async_get(self.hass), dr.async_get(self.hass), ar.async_get(self.hass)
        out = {}
        for eid in entity_ids:
            state = self.hass.states.get(eid)
            if state is None:  # entity gone: nothing to suggest for it
                continue
            area_id = None
            if entry := ent_reg.async_get(eid):
                area_id = entry.area_id
                if area_id is None and entry.device_id and (dev := dev_reg.async_get(entry.device_id)):
                    area_id = dev.area_id
            area = area_reg.async_get_area(area_id) if area_id else None
            out[eid] = A.EntityInfo(eid, state.name, state.attributes.get("device_class"),
                                    area.name if area else None)
        return out

    async def async_automations(self) -> list[A.ExistingAutomation]:
        return await autos.existing_automations(self.hass)

    def sun_table(self, days: set[date]) -> dict[date, tuple[float, float]]:
        out = {}
        for d in days:
            rise = get_astral_event_date(self.hass, "sunrise", d)
            sset = get_astral_event_date(self.hass, "sunset", d)
            if rise and sset:
                out[d] = (rise.timestamp(), sset.timestamp())
        return out

    async def async_create(self, config: dict) -> str:
        return await autos.create(self.hass, config)

    async def async_update(self, config_id: str, config: dict) -> dict:
        return await autos.update(self.hass, config_id, config)

    async def async_delete(self, config_id: str) -> dict:
        return await autos.delete(self.hass, config_id)

    async def async_disable(self, entity_id: str) -> None:
        await autos.disable(self.hass, entity_id)

    async def async_catalog(self) -> dict:
        ent_reg, dev_reg, area_reg = er.async_get(self.hass), dr.async_get(self.hass), ar.async_get(self.hass)
        areas = [{"id": a.id, "name": a.name} for a in area_reg.async_list_areas()]
        area_name = {a["id"]: a["name"] for a in areas}
        devices = []
        # Newer Home Assistant: iterating `devices` yields the entries (and `.values()` is
        # deprecated); older: it is a mapping of id -> entry.
        all_devices = dev_reg.devices
        for d in (all_devices if hasattr(all_devices, "_report_deprecated_use")
                  else all_devices.values()):
            if d.disabled_by or d.entry_type is not None:  # skip services/helpers
                continue
            devices.append({
                "id": d.id, "name": d.name_by_user or d.name or d.id, "area": area_name.get(d.area_id),
                "manufacturer": d.manufacturer, "model": d.model,
                "created": d.created_at.isoformat() if getattr(d, "created_at", None) else "",
                "entities": [e.entity_id for e in er.async_entries_for_device(ent_reg, d.id)],
            })
        devices.sort(key=lambda d: d["created"], reverse=True)
        entities = {}
        for st in self.hass.states.async_all():
            entry = ent_reg.async_get(st.entity_id)
            area_id = entry.area_id if entry else None
            if area_id is None and entry and entry.device_id and (dev := dev_reg.async_get(entry.device_id)):
                area_id = dev.area_id
            entities[st.entity_id] = {
                "name": st.name, "area": area_name.get(area_id), "area_id": area_id,
                "device_class": st.attributes.get("device_class"),
                "event_types": st.attributes.get("event_types"),
                "device_id": entry.device_id if entry else None,
            }
        return {"areas": areas, "devices": devices, "entities": entities}

    async def async_device_triggers(self, device_id: str) -> list[dict]:
        try:
            from homeassistant.components.device_automation import (
                DeviceAutomationType,
                async_get_device_automations,
            )

            found = await async_get_device_automations(self.hass, DeviceAutomationType.TRIGGER, [device_id])
            return [dict(t) for t in found.get(device_id, [])]
        except Exception as err:
            _LOGGER.debug("No device triggers for %s: %s", device_id, err)
            return []

    async def async_traces(self, auto: A.ExistingAutomation) -> list[dict]:
        if not auto.config_id or "trace" not in self.hass.config.components:
            return []
        try:
            from homeassistant.components.trace.util import async_list_traces

            return await async_list_traces(self.hass, "automation", f"automation.{auto.config_id}")
        except Exception as err:  # internal API; never fatal
            _LOGGER.debug("No traces for %s: %s", auto.entity_id, err)
            return []


# --------------------------------------------------------------------------------------------
# Remote
# --------------------------------------------------------------------------------------------


class RemoteAuthError(Exception):
    """The token was rejected."""


class RemoteClient:
    """Minimal Home Assistant websocket client."""

    def __init__(self, hass: HomeAssistant, url: str, token: str, verify_ssl: bool = True) -> None:
        self.hass = hass
        self.url = url.rstrip("/")
        self.token = token
        self.session = async_get_clientsession(hass, verify_ssl=verify_ssl)
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.version: str | None = None
        self._id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._handlers: dict[int, Callable[[dict], None]] = {}
        self._reader: asyncio.Task | None = None
        self.closed = asyncio.Event()

    @property
    def headers(self) -> dict:
        return {"Authorization": f"Bearer {self.token}"}

    async def connect(self) -> None:
        ws_url = self.url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
        self.ws = await self.session.ws_connect(
            f"{ws_url}/api/websocket", heartbeat=30, max_msg_size=0, timeout=aiohttp.ClientWSTimeout(ws_close=10)
        )
        hello = await self.ws.receive_json(timeout=15)
        if hello.get("type") != "auth_required":
            raise ConnectionError(f"Unexpected greeting: {hello}")
        await self.ws.send_json({"type": "auth", "access_token": self.token})
        reply = await self.ws.receive_json(timeout=15)
        if reply.get("type") != "auth_ok":
            await self.ws.close()
            raise RemoteAuthError(reply.get("message", "authentication failed"))
        self.version = reply.get("ha_version")
        self.closed.clear()
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        assert self.ws is not None
        try:
            async for msg in self.ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                for item in _as_list(msg.json()):
                    mid = item.get("id")
                    if item.get("type") == "result" and mid in self._pending:
                        fut = self._pending.pop(mid)
                        if not fut.done():
                            if item.get("success"):
                                fut.set_result(item.get("result"))
                            else:
                                err = item.get("error") or {}
                                fut.set_exception(RuntimeError(f"{err.get('code')}: {err.get('message')}"))
                    elif item.get("type") == "event" and mid in self._handlers:
                        try:
                            self._handlers[mid](item["event"])
                        except Exception:  # a bad event must not kill the reader
                            _LOGGER.exception("Error handling remote event")
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionError("connection closed"))
            self._pending.clear()
            self.closed.set()

    async def call(self, msg: dict, timeout: float = 120) -> Any:
        assert self.ws is not None
        self._id += 1
        mid = self._id
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        await self.ws.send_json({**msg, "id": mid})
        return await asyncio.wait_for(fut, timeout)

    async def subscribe(self, event_type: str, handler: Callable[[dict], None]) -> None:
        self._id += 1
        mid = self._id
        self._handlers[mid] = handler
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        await self.ws.send_json({"id": mid, "type": "subscribe_events", "event_type": event_type})
        await asyncio.wait_for(fut, 30)

    async def close(self) -> None:
        if self.ws is not None and not self.ws.closed:
            await self.ws.close()
        if self._reader is not None:
            await asyncio.gather(self._reader, return_exceptions=True)

    async def rest(self, method: str, path: str, json: Any = None) -> tuple[int, Any]:
        async with self.session.request(
            method, f"{self.url}{path}", headers=self.headers, json=json,
            timeout=aiohttp.ClientTimeout(total=60),
        ) as resp:
            try:
                body = await resp.json(content_type=None)
            except ValueError:
                body = await resp.text()
            return resp.status, body


def _as_list(x: Any) -> list:
    return x if isinstance(x, list) else [x]


def _ts(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    parsed = dt_util.parse_datetime(str(value))
    return parsed.timestamp() if parsed else time.time()


def logbook_to_events(entries: list[dict], device_classes: dict[str, str | None],
                      excluded: set[str]) -> list[A.Event]:
    """Convert logbook entries (which carry 'what caused this') into archive events.

    The logbook attaches the *origin* of a change's context: context_event_type
    "automation_triggered" + context_entity_id for an automation, context_user_id for a person
    using Home Assistant. A change with no context fields originated at the device itself.
    """
    out: list[A.Event] = []
    last = _LastValue()
    for item in sorted(entries, key=lambda e: _ts(e.get("when"))):
        entity_id = item.get("entity_id")
        if not entity_id:
            continue
        ts = _ts(item.get("when"))
        if "state" not in item:
            if entity_id.startswith("automation.") and (item.get("source") or "triggered" in str(item.get("message", ""))):
                out.append(A.Event(ts, entity_id, None, "triggered", A.CAUSE_OTHER))
            continue
        if not is_tracked(entity_id, device_classes.get(entity_id), excluded):
            continue
        change = last.update(entity_id, item["state"], None, device_classes.get(entity_id))
        if change is None:
            continue
        if item.get("context_event_type") == "automation_triggered" and item.get("context_entity_id"):
            cause, ref = A.CAUSE_AUTOMATION, item["context_entity_id"]
        elif item.get("context_user_id") and item.get("context_event_type") == "call_service":
            cause, ref = A.CAUSE_USER, None  # a person used a dashboard, app or voice
        elif item.get("context_user_id"):
            # A user started something upstream, but the logbook cannot say what (it happens
            # for a while after Home Assistant starts, before automations describe their
            # events). Could be an automation's action: never count it as a manual habit.
            cause, ref = A.CAUSE_OTHER, None
        elif item.get("context_event_type") or item.get("context_entity_id") or item.get("context_domain"):
            cause, ref = A.CAUSE_OTHER, None
        else:
            cause, ref = A.CAUSE_MANUAL, None
        out.append(A.Event(ts, entity_id, change[0], change[1], cause, ref))
    return out


class RemoteSource(Source):
    def __init__(self, hass: HomeAssistant, url: str, token: str, verify_ssl: bool,
                 excluded: set[str], allow_write: bool, lookback_days: int) -> None:
        self.hass = hass
        self.client = RemoteClient(hass, url, token, verify_ssl)
        self.excluded = excluded
        self.can_write = allow_write
        self.label = url
        self.lookback_s = lookback_days * DAY
        self.states: dict[str, dict] = {}
        self.areas: dict[str, str] = {}  # entity_id -> area name
        self.registry: dict[str, list] = {"areas": [], "devices": [], "entities": []}
        self.config: dict = {}
        self.connected = False
        self.last_error: str | None = None
        self._collector: Collector | None = None
        self._archive: Archive | None = None
        self._task: asyncio.Task | None = None
        self._stopping = False
        self._ready = asyncio.Event()

    # -- lifecycle -----------------------------------------------------------------------------

    async def async_start(self, collector, archive, lookback_s) -> None:
        self._collector, self._archive = collector, archive
        self._task = self.hass.async_create_background_task(self._loop(), "automation_advisor_remote")

    async def async_stop(self) -> None:
        self._stopping = True
        await self.client.close()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)

    async def _loop(self) -> None:
        delay = 5
        while not self._stopping:
            try:
                await self.client.connect()
                await self._load_metadata()
                await self.client.subscribe("state_changed", self._on_state)
                await self.client.subscribe("automation_triggered", self._on_automation)
                await self._fill_gap()
                self.connected, self.last_error, delay = True, None, 5
                self._ready.set()
                _LOGGER.info("Connected to remote Home Assistant %s (%s)", self.label, self.client.version)
                await self.client.closed.wait()
                _LOGGER.warning("Connection to %s closed; reconnecting", self.label)
            except asyncio.CancelledError:
                raise
            except RemoteAuthError as err:
                self.last_error = f"The access token was rejected: {err}"
                _LOGGER.error("Remote Home Assistant %s: %s", self.label, self.last_error)
                delay = 1800
            except Exception as err:
                self.last_error = f"{type(err).__name__}: {err}"
                _LOGGER.warning("Remote Home Assistant %s unreachable: %s", self.label, self.last_error)
            finally:
                self.connected = False
                await self.client.close()
            if self._stopping:
                return
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)

    async def async_prepare(self, since: float) -> None:
        try:
            await asyncio.wait_for(self._ready.wait(), 30)
        except TimeoutError as err:
            raise autos.AdvisorError(
                f"Cannot reach {self.label}: {self.last_error or 'no connection yet'}"
            ) from err
        if self.connected:
            await self._load_metadata()

    # -- data ----------------------------------------------------------------------------------

    async def _load_metadata(self) -> None:
        self.config = await self.client.call({"type": "get_config"})
        states = await self.client.call({"type": "get_states"})
        self.states = {s["entity_id"]: s for s in states}
        try:
            ents = await self.client.call({"type": "config/entity_registry/list"})
            devs = await self.client.call({"type": "config/device_registry/list"})
            areas = await self.client.call({"type": "config/area_registry/list"})
        except Exception as err:  # needs an admin token; areas are only a nicety
            _LOGGER.debug("Registries unavailable on remote: %s", err)
            return
        area_names = {a["area_id"]: a["name"] for a in areas}
        dev_area = {d["id"]: d.get("area_id") for d in devs}
        self.registry = {"areas": areas, "devices": devs, "entities": ents}
        self.areas = {}
        for e in ents:
            area_id = e.get("area_id") or dev_area.get(e.get("device_id"))
            if area_id in area_names:
                self.areas[e["entity_id"]] = area_names[area_id]

    def _device_classes(self) -> dict[str, str | None]:
        return {eid: s.get("attributes", {}).get("device_class") for eid, s in self.states.items()}

    async def _fill_gap(self) -> None:
        """Read the logbook from the newest archived event (or the lookback) until now, so a Mac
        that slept or a dropped connection leaves no hole."""
        assert self._archive is not None and self._collector is not None
        await self._collector.async_flush()
        stats = await self.hass.async_add_executor_job(self._archive.stats)
        now = time.time()
        start = max(now - self.lookback_s, (stats["last"] or 0) - 60)
        classes = self._device_classes()
        ids = sorted(e for e in self.states
                     if is_tracked(e, classes.get(e), self.excluded) or e.startswith("automation."))
        if not ids:
            return
        entries: list[dict] = []
        t = start
        while t < now:
            end = min(t + DAY, now)
            try:
                chunk = await self.client.call({
                    "type": "logbook/get_events",
                    "start_time": dt_util.utc_from_timestamp(t).isoformat(),
                    "end_time": dt_util.utc_from_timestamp(end).isoformat(),
                    "entity_ids": ids,
                })
            except RuntimeError as err:
                if "unknown_command" in str(err):
                    _LOGGER.warning("%s has no logbook; collecting live events only", self.label)
                    return
                raise
            entries.extend(chunk or [])
            t = end
        events = logbook_to_events(entries, classes, self.excluded)
        added = await self.hass.async_add_executor_job(self._archive.add, events)
        _LOGGER.info("Read %s logbook entries from %s, %s new events", len(entries), self.label, added)

    @callback
    def _on_state(self, event: dict) -> None:
        data = event.get("data", {})
        new, old = data.get("new_state"), data.get("old_state")
        entity_id = data.get("entity_id")
        if new is None:
            self.states.pop(entity_id, None)
            return
        self.states[entity_id] = new
        if old is None or self._collector is None:
            return
        ctx = event.get("context") or {}
        self._collector.ingest_state(
            entity_id, old.get("state"), new.get("state"),
            (new.get("attributes") or {}).get("device_class"), _ts(event.get("time_fired")),
            ctx.get("id"), ctx.get("user_id"), ctx.get("parent_id"),
        )

    @callback
    def _on_automation(self, event: dict) -> None:
        if self._collector is not None:
            self._collector.ingest_automation(
                event.get("data", {}).get("entity_id"), (event.get("context") or {}).get("id"),
                _ts(event.get("time_fired")),
            )

    # -- Source interface --------------------------------------------------------------------

    @property
    def tz(self) -> tzinfo:
        name = self.config.get("time_zone")
        try:
            return ZoneInfo(name) if name else dt_util.get_default_time_zone()
        except Exception:
            return dt_util.get_default_time_zone()

    def status(self) -> dict:
        return {"mode": "remote", "label": self.label, "can_write": self.can_write,
                "connected": self.connected, "version": self.client.version,
                "error": self.last_error, "location": self.config.get("location_name")}

    def entity_exists(self, entity_id: str) -> bool:
        return entity_id in self.states

    def entity_ids(self) -> set[str]:
        return set(self.states)

    def entity_info(self, entity_ids: set[str]) -> dict[str, A.EntityInfo]:
        out = {}
        for eid in entity_ids:
            st = self.states.get(eid)
            if st is None:
                continue
            attrs = st.get("attributes") or {}
            out[eid] = A.EntityInfo(eid, attrs.get("friendly_name") or eid,
                                    attrs.get("device_class"), self.areas.get(eid))
        return out

    async def async_automations(self) -> list[A.ExistingAutomation]:
        sem = asyncio.Semaphore(8)

        async def one(eid: str, st: dict) -> A.ExistingAutomation:
            attrs = st.get("attributes") or {}
            config_id = attrs.get("id")
            config, editable = None, False
            async with sem:
                try:
                    config = (await self.client.call({"type": "automation/config", "entity_id": eid}))["config"]
                except Exception as err:
                    _LOGGER.debug("No config for %s: %s", eid, err)
                if config_id:
                    status, _ = await self.client.rest("GET", f"/api/config/automation/config/{config_id}")
                    editable = status == 200
            t_ent, platforms, a_ent = autos.summarize_config(config)
            return A.ExistingAutomation(
                entity_id=eid,
                alias=(config or {}).get("alias") or attrs.get("friendly_name") or eid,
                config_id=str(config_id) if config_id else None,
                editable=editable,
                enabled=st.get("state") == "on",
                trigger_entities=frozenset(t_ent),
                trigger_platforms=frozenset(platforms),
                action_entities=frozenset(a_ent),
                config=config,
            )

        items = [(e, s) for e, s in self.states.items() if e.startswith("automation.")]
        return list(await asyncio.gather(*(one(e, s) for e, s in items)))

    def sun_table(self, days: set[date]) -> dict[date, tuple[float, float]]:
        from astral import LocationInfo
        from astral.sun import sun

        lat, lon = self.config.get("latitude"), self.config.get("longitude")
        if lat is None or lon is None:
            return {}
        observer = LocationInfo("home", "", str(self.tz), lat, lon).observer
        out = {}
        for d in days:
            try:
                s = sun(observer, date=d, tzinfo=self.tz)
            except ValueError:  # polar day/night
                continue
            out[d] = (s["sunrise"].timestamp(), s["sunset"].timestamp())
        return out

    # -- writes (only with allow_write) --------------------------------------------------------

    def _check_write(self) -> None:
        if not self.can_write:
            raise autos.AdvisorError("Read-only connection: nothing is changed on " + self.label)

    async def async_create(self, config: dict) -> str:
        self._check_write()
        config_id = str(int(time.time() * 1000))
        await autos.validate(self.hass, {**config, "id": config_id})
        status, body = await self.client.rest("POST", f"/api/config/automation/config/{config_id}", config)
        if status != 200:
            raise autos.AdvisorError(f"{self.label} refused the automation: {body}")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            for eid, st in self.states.items():
                if eid.startswith("automation.") and str((st.get("attributes") or {}).get("id")) == config_id:
                    if st.get("state") != "on":
                        await self.async_enable(eid)
                    return eid
            await asyncio.sleep(0.3)
        raise autos.AdvisorError(
            f"Saved on {self.label}, but it did not load. Check that its configuration.yaml has "
            "`automation: !include automations.yaml`."
        )

    async def async_enable(self, entity_id: str) -> None:
        await self.client.call({"type": "call_service", "domain": "automation",
                                "service": "turn_on", "target": {"entity_id": entity_id}})

    async def async_update(self, config_id: str, config: dict) -> dict:
        self._check_write()
        status, previous = await self.client.rest("GET", f"/api/config/automation/config/{config_id}")
        if status != 200:
            raise autos.AdvisorError("That automation is no longer editable on " + self.label)
        body = {k: v for k, v in config.items() if k != "id"}
        await autos.validate(self.hass, {**body, "id": config_id})
        status, reply = await self.client.rest("POST", f"/api/config/automation/config/{config_id}", body)
        if status != 200:
            raise autos.AdvisorError(f"{self.label} refused the change: {reply}")
        return previous

    async def async_delete(self, config_id: str) -> dict:
        self._check_write()
        status, previous = await self.client.rest("GET", f"/api/config/automation/config/{config_id}")
        if status != 200:
            raise autos.AdvisorError("That automation is no longer editable on " + self.label)
        status, reply = await self.client.rest("DELETE", f"/api/config/automation/config/{config_id}")
        if status != 200:
            raise autos.AdvisorError(f"{self.label} refused to delete it: {reply}")
        return previous

    async def async_catalog(self) -> dict:
        if self.connected:
            await self._load_metadata()
        reg = self.registry
        areas = [{"id": a["area_id"], "name": a["name"]} for a in reg["areas"]]
        area_name = {a["id"]: a["name"] for a in areas}
        by_device: dict[str, list[str]] = {}
        ent_reg = {e["entity_id"]: e for e in reg["entities"]}
        for e in reg["entities"]:
            if e.get("device_id"):
                by_device.setdefault(e["device_id"], []).append(e["entity_id"])
        devices = [{
            "id": d["id"], "name": d.get("name_by_user") or d.get("name") or d["id"],
            "area": area_name.get(d.get("area_id")), "manufacturer": d.get("manufacturer"),
            "model": d.get("model"), "created": str(d.get("created_at") or ""),
            "entities": by_device.get(d["id"], []),
        } for d in reg["devices"] if not d.get("disabled_by") and not d.get("entry_type")]
        devices.sort(key=lambda d: d["created"], reverse=True)
        dev_area = {d["id"]: d.get("area_id") for d in reg["devices"]}
        entities = {}
        for eid, st in self.states.items():
            e = ent_reg.get(eid, {})
            area_id = e.get("area_id") or dev_area.get(e.get("device_id"))
            attrs = st.get("attributes") or {}
            entities[eid] = {"name": attrs.get("friendly_name") or eid, "area": area_name.get(area_id),
                             "area_id": area_id, "device_class": attrs.get("device_class"),
                             "event_types": attrs.get("event_types"), "device_id": e.get("device_id")}
        return {"areas": areas, "devices": devices, "entities": entities}

    async def async_device_triggers(self, device_id: str) -> list[dict]:
        try:
            return await self.client.call({"type": "device_automation/trigger/list",
                                           "device_id": device_id}) or []
        except Exception as err:
            _LOGGER.debug("No device triggers for %s: %s", device_id, err)
            return []

    async def async_traces(self, auto: A.ExistingAutomation) -> list[dict]:
        if not auto.config_id:
            return []
        try:
            return await self.client.call({"type": "trace/list", "domain": "automation",
                                           "item_id": auto.config_id}) or []
        except Exception as err:
            _LOGGER.debug("No traces for %s: %s", auto.entity_id, err)
            return []

    async def async_disable(self, entity_id: str) -> None:
        self._check_write()
        await self.client.call({"type": "call_service", "domain": "automation",
                                "service": "turn_off", "target": {"entity_id": entity_id}})


async def async_test_connection(hass: HomeAssistant, url: str, token: str, verify_ssl: bool) -> dict:
    """Used by the config flow: connect, authenticate, read the basics, disconnect."""
    client = RemoteClient(hass, url, token, verify_ssl)
    try:
        await client.connect()
        config = await client.call({"type": "get_config"})
        states = await client.call({"type": "get_states"})
        return {"version": client.version, "location": config.get("location_name"),
                "entities": len(states)}
    finally:
        await client.close()
