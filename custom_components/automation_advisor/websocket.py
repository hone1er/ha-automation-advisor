"""Websocket commands used by the sidebar panel."""

from __future__ import annotations

from typing import Any

from ._compat import vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import ACTION_DISMISS, ACTION_RESTORE, APPLY_ACTIONS, DOMAIN, SIGNAL_LIVE, SIGNAL_UPDATED


def _manager(hass: HomeAssistant):
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    return entries[0].runtime_data if entries else None


@callback
def async_register_commands(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_list)
    websocket_api.async_register_command(hass, ws_decide)
    websocket_api.async_register_command(hass, ws_run)
    websocket_api.async_register_command(hass, ws_subscribe)
    websocket_api.async_register_command(hass, ws_clear)
    websocket_api.async_register_command(hass, ws_live)
    websocket_api.async_register_command(hass, ws_edit)
    websocket_api.async_register_command(hass, ws_automations)
    websocket_api.async_register_command(hass, ws_fix)
    websocket_api.async_register_command(hass, ws_devices)
    websocket_api.async_register_command(hass, ws_build)


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/list"})
@callback
def ws_list(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    manager = _manager(hass)
    if manager is None:
        connection.send_error(msg["id"], "not_loaded", "Automation Advisor is not set up")
        return
    connection.send_result(msg["id"], manager.snapshot())


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/decide",
        vol.Required("suggestion_id"): str,
        vol.Required("action"): vol.In([*APPLY_ACTIONS, ACTION_DISMISS, ACTION_RESTORE]),
    }
)
@websocket_api.async_response
async def ws_decide(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    manager = _manager(hass)
    if manager is None:
        connection.send_error(msg["id"], "not_loaded", "Automation Advisor is not set up")
        return
    try:
        rec = await manager.async_decide(msg["suggestion_id"], msg["action"])
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "failed", str(err))
        return
    connection.send_result(msg["id"], rec)


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/run"})
@websocket_api.async_response
async def ws_run(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    manager = _manager(hass)
    if manager is None:
        connection.send_error(msg["id"], "not_loaded", "Automation Advisor is not set up")
        return
    try:
        stats = await manager.async_run()
    except Exception as err:  # surface analysis errors in the panel
        connection.send_error(msg["id"], "failed", str(err))
        return
    connection.send_result(msg["id"], {**stats, "report": manager.data.get("last_report")})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/subscribe"})
@callback
def ws_subscribe(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    @callback
    def updated() -> None:
        connection.send_message(websocket_api.event_message(msg["id"], {"updated": True}))

    connection.subscriptions[msg["id"]] = async_dispatcher_connect(hass, SIGNAL_UPDATED, updated)
    connection.send_result(msg["id"])


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/clear"})
@websocket_api.async_response
async def ws_clear(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    manager = _manager(hass)
    if manager is None:
        connection.send_error(msg["id"], "not_loaded", "Automation Advisor is not set up")
        return
    if manager.running:
        connection.send_error(msg["id"], "busy", "An analysis is running; try again when it finishes")
        return
    connection.send_result(msg["id"], {"removed": await manager.async_clear()})


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/live"})
@callback
def ws_live(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Stream the running analysis: steps, and the AI's thinking and answer as they arrive."""

    @callback
    def send() -> None:
        manager = _manager(hass)
        connection.send_message(websocket_api.event_message(
            msg["id"], {"live": manager.live if manager else None}))

    connection.subscriptions[msg["id"]] = async_dispatcher_connect(hass, SIGNAL_LIVE, send)
    connection.send_result(msg["id"])
    send()


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/edit",
        vol.Required("suggestion_id"): str,
        vol.Exclusive("yaml", "how"): str,
        vol.Exclusive("instruction", "how"): str,
    }
)
@websocket_api.async_response
async def ws_edit(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Edit one suggestion: {"yaml": "..."} by hand, or {"instruction": "..."} via the AI."""
    manager = _manager(hass)
    if manager is None:
        connection.send_error(msg["id"], "not_loaded", "Automation Advisor is not set up")
        return
    try:
        rec = await manager.async_edit(msg["suggestion_id"], yaml_text=msg.get("yaml"),
                                       instruction=msg.get("instruction"))
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "failed", str(err))
        return
    except Exception as err:  # AI or network trouble: tell the panel
        connection.send_error(msg["id"], "failed", f"{type(err).__name__}: {err}")
        return
    connection.send_result(msg["id"], rec)


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/automations"})
@websocket_api.async_response
async def ws_automations(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    manager = _manager(hass)
    if manager is None:
        connection.send_error(msg["id"], "not_loaded", "Automation Advisor is not set up")
        return
    connection.send_result(msg["id"], await manager.async_automation_list())


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/fix",
        vol.Required("automation"): str,
        vol.Required("problem"): str,
    }
)
@websocket_api.async_response
async def ws_fix(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """'This automation does not do what I want: ...' -> a fix suggestion from the AI."""
    manager = _manager(hass)
    if manager is None:
        connection.send_error(msg["id"], "not_loaded", "Automation Advisor is not set up")
        return
    try:
        rec = await manager.async_fix(msg["automation"], msg["problem"])
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "failed", str(err))
        return
    except Exception as err:  # AI or network trouble
        connection.send_error(msg["id"], "failed", f"{type(err).__name__}: {err}")
        return
    connection.send_result(msg["id"], rec)


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/devices"})
@websocket_api.async_response
async def ws_devices(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    manager = _manager(hass)
    if manager is None:
        connection.send_error(msg["id"], "not_loaded", "Automation Advisor is not set up")
        return
    connection.send_result(msg["id"], await manager.async_device_list())


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/build",
        vol.Required("request"): str,
        vol.Optional("device_id"): vol.Any(str, None),
    }
)
@websocket_api.async_response
async def ws_build(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """'Set this up for me' -> automation suggestion(s) from the AI."""
    manager = _manager(hass)
    if manager is None:
        connection.send_error(msg["id"], "not_loaded", "Automation Advisor is not set up")
        return
    try:
        result = await manager.async_build(msg["request"], msg.get("device_id") or None)
    except HomeAssistantError as err:
        connection.send_error(msg["id"], "failed", str(err))
        return
    except Exception as err:
        connection.send_error(msg["id"], "failed", f"{type(err).__name__}: {err}")
        return
    connection.send_result(msg["id"], result)
