"""'Pending suggestions' sensor, handy for a dashboard badge or a notification automation."""

from __future__ import annotations

from datetime import datetime, timezone

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN, SIGNAL_UPDATED


async def async_setup_entry(hass: HomeAssistant, entry, async_add_entities: AddConfigEntryEntitiesCallback) -> None:
    async_add_entities([PendingSensor(entry)])


class PendingSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_translation_key = "pending"
    _attr_icon = "mdi:lightbulb-auto-outline"
    _attr_native_unit_of_measurement = "suggestions"
    _attr_should_poll = False

    def __init__(self, entry) -> None:
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_pending"
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": "Automation Advisor",
            "entry_type": "service",
        }

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_UPDATED, self._update))

    @callback
    def _update(self) -> None:
        self.async_write_ha_state()

    @property
    def native_value(self) -> int:
        return len(self._entry.runtime_data.pending())

    @property
    def extra_state_attributes(self) -> dict:
        manager = self._entry.runtime_data
        last = manager.data.get("last_run")
        return {
            "titles": [r["suggestion"]["title"] for r in manager.pending()][:10],
            "last_run": datetime.fromtimestamp(last, timezone.utc).isoformat() if last else None,
            "running": manager.running,
        }
