"""Automation Advisor: learns from your manual switching and suggests automations."""

from __future__ import annotations

import logging
from pathlib import Path

from ._compat import vol

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import (
    APPLY_ACTIONS,
    ACTION_DISMISS,
    ACTION_RESTORE,
    DOMAIN,
    PANEL_COMPONENT,
    PANEL_JS_URL,
    PANEL_URL,
    TITLE,
)
from .manager import AdvisorManager
from .websocket import async_register_commands

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.SENSOR]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

type AdvisorConfigEntry = ConfigEntry[AdvisorManager]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    async_register_commands(hass)

    async def run(call: ServiceCall) -> ServiceResponse:
        return await _manager(hass).async_run()

    async def decide(call: ServiceCall) -> ServiceResponse:
        try:
            rec = await _manager(hass).async_decide(call.data["suggestion_id"], call.data["action"])
        except HomeAssistantError:
            raise
        return {"status": rec["status"], "result": rec.get("result")}

    hass.services.async_register(
        DOMAIN, "run_analysis", run, supports_response=SupportsResponse.OPTIONAL
    )
    hass.services.async_register(
        DOMAIN,
        "decide",
        decide,
        schema=vol.Schema(
            {
                vol.Required("suggestion_id"): cv.string,
                vol.Required("action"): vol.In([*APPLY_ACTIONS, ACTION_DISMISS, ACTION_RESTORE]),
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    return True


def _manager(hass: HomeAssistant) -> AdvisorManager:
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    if not entries:
        raise HomeAssistantError("Automation Advisor is not set up")
    return entries[0].runtime_data


async def async_setup_entry(hass: HomeAssistant, entry: AdvisorConfigEntry) -> bool:
    manager = AdvisorManager(hass, entry)
    await manager.async_start()
    entry.runtime_data = manager
    await _register_panel(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_options_updated))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: AdvisorConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    await entry.runtime_data.async_stop()
    frontend.async_remove_panel(hass, PANEL_URL, warn_if_unknown=False)
    return ok


async def _options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def _register_panel(hass: HomeAssistant) -> None:
    key = f"{DOMAIN}_static"
    if not hass.data.get(key):
        path = Path(__file__).parent / "frontend" / "automation-advisor-panel.js"
        await hass.http.async_register_static_paths(
            [StaticPathConfig(PANEL_JS_URL, str(path), cache_headers=False)]
        )
        hass.data[key] = True
    if PANEL_URL in hass.data.get(frontend.DATA_PANELS, {}):
        return
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL,
        webcomponent_name=PANEL_COMPONENT,
        sidebar_title=TITLE,
        sidebar_icon="mdi:lightbulb-auto-outline",
        module_url=PANEL_JS_URL,
        require_admin=True,
    )
