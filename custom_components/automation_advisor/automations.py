"""Read, create, change and remove automations.

Home Assistant has no public Python API for this, so this mirrors what the automation editor's
HTTP view does (homeassistant/components/config/automation.py): validate with the automation
integration's own validator, rewrite automations.yaml atomically, then reload just that
automation. Only automations stored in automations.yaml (the UI-managed ones, which have an id)
can be edited or deleted; YAML-defined ones can only be turned off.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import time
from typing import Any

from homeassistant.components.automation import DOMAIN as AUTOMATION_DOMAIN
from homeassistant.config import AUTOMATION_CONFIG_PATH
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util.file import write_utf8_file_atomic
from homeassistant.util.yaml import dump, load_yaml

from . import analysis as A

_LOGGER = logging.getLogger(__name__)
_LOCK = asyncio.Lock()
RELOAD_WAIT_S = 10


class AdvisorError(HomeAssistantError):
    """Something the user should be told about."""


# --------------------------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------------------------


def _collect_entities(node: Any, out: set[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "entity_id":
                if isinstance(value, str):
                    out.update(v.strip() for v in value.split(","))
                elif isinstance(value, list):
                    out.update(v for v in value if isinstance(v, str))
            else:
                _collect_entities(value, out)
    elif isinstance(node, list):
        for item in node:
            _collect_entities(item, out)


def _section(config: dict, plural: str, singular: str) -> list:
    value = config.get(plural, config.get(singular)) or []
    return value if isinstance(value, list) else [value]


def summarize_config(config: dict | None) -> tuple[set[str], set[str], set[str]]:
    """(trigger entities, trigger platforms, action entities) of an automation config."""
    if not config:
        return set(), set(), set()
    triggers = _section(config, "triggers", "trigger")
    actions = _section(config, "actions", "action")
    t_ent: set[str] = set()
    a_ent: set[str] = set()
    _collect_entities(triggers, t_ent)
    _collect_entities(actions, a_ent)
    platforms = {
        str(t.get("trigger", t.get("platform"))) for t in triggers if isinstance(t, dict)
    }
    return t_ent, platforms, a_ent


def _read_file(path: str) -> list[dict]:
    if not os.path.isfile(path):
        return []
    data = load_yaml(path)
    return data if isinstance(data, list) else []


async def read_ui_automations(hass: HomeAssistant) -> list[dict]:
    return await hass.async_add_executor_job(_read_file, hass.config.path(AUTOMATION_CONFIG_PATH))


async def existing_automations(hass: HomeAssistant) -> list[A.ExistingAutomation]:
    """Every automation entity Home Assistant has loaded."""
    try:
        from homeassistant.components.automation import DATA_COMPONENT
    except ImportError:  # pragma: no cover - very old cores
        return []
    component = hass.data.get(DATA_COMPONENT)
    if component is None:
        return []
    ui_ids = {str(c.get("id")) for c in await read_ui_automations(hass) if c.get("id")}
    out = []
    for entity in component.entities:
        raw = getattr(entity, "raw_config", None)
        config_id = getattr(entity, "unique_id", None)
        t_ent, platforms, a_ent = summarize_config(raw)
        state = hass.states.get(entity.entity_id)
        out.append(
            A.ExistingAutomation(
                entity_id=entity.entity_id,
                alias=(raw or {}).get("alias") or (state.name if state else entity.entity_id),
                config_id=str(config_id) if config_id else None,
                editable=config_id is not None and str(config_id) in ui_ids,
                enabled=state is not None and state.state == "on",
                trigger_entities=frozenset(t_ent),
                trigger_platforms=frozenset(platforms),
                action_entities=frozenset(a_ent),
                config=copy.deepcopy(dict(raw)) if raw else None,
            )
        )
    return out


# --------------------------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------------------------


async def validate(hass: HomeAssistant, config: dict) -> None:
    """Raise AdvisorError if Home Assistant would reject this automation."""
    from homeassistant.components.automation.config import async_validate_config_item

    try:
        await async_validate_config_item(hass, config.get("id", "advisor_check"), config)
    except Exception as err:  # probatio.Invalid / HomeAssistantError
        raise AdvisorError(f"Home Assistant rejected the automation: {err}") from err


def _ordered(config_id: str, config: dict) -> dict:
    out: dict[str, Any] = {"id": config_id}
    for key in ("alias", "description", "triggers", "trigger", "conditions", "condition",
                "actions", "action", "mode"):
        if key in config:
            out[key] = config[key]
    out.update({k: v for k, v in config.items() if k not in out})
    return out


def _write_file(path: str, data: list[dict]) -> None:
    contents = dump(data)
    write_utf8_file_atomic(path, contents)


async def _reload(hass: HomeAssistant, config_id: str) -> None:
    await hass.services.async_call(
        AUTOMATION_DOMAIN, "reload", {"id": config_id}, blocking=True
    )


async def _wait_for_entity(hass: HomeAssistant, config_id: str) -> str | None:
    registry = er.async_get(hass)
    deadline = time.monotonic() + RELOAD_WAIT_S
    while time.monotonic() < deadline:
        entity_id = registry.async_get_entity_id(AUTOMATION_DOMAIN, AUTOMATION_DOMAIN, config_id)
        if entity_id and hass.states.get(entity_id) is not None:
            return entity_id
        await asyncio.sleep(0.2)
    return None


async def create(hass: HomeAssistant, config: dict) -> str:
    """Add an automation to automations.yaml, load it, and return its (enabled) entity id."""
    config_id = str(int(time.time() * 1000))
    config = _ordered(config_id, config)
    await validate(hass, config)
    path = hass.config.path(AUTOMATION_CONFIG_PATH)
    async with _LOCK:
        data = await hass.async_add_executor_job(_read_file, path)
        data.append(config)
        await hass.async_add_executor_job(_write_file, path, data)
    await _reload(hass, config_id)
    entity_id = await _wait_for_entity(hass, config_id)
    if entity_id is None:
        async with _LOCK:  # take it out again, so a retry does not create a duplicate
            data = await hass.async_add_executor_job(_read_file, path)
            data = [c for c in data if str(c.get("id")) != config_id]
            await hass.async_add_executor_job(_write_file, path, data)
        raise AdvisorError(
            "The automation was saved to automations.yaml but Home Assistant did not load it. "
            "Make sure configuration.yaml contains `automation: !include automations.yaml`."
        )
    state = hass.states.get(entity_id)
    if state is not None and state.state != "on":
        await hass.services.async_call(
            AUTOMATION_DOMAIN, "turn_on", {"entity_id": entity_id}, blocking=True
        )
    return entity_id


async def update(hass: HomeAssistant, config_id: str, config: dict) -> dict:
    """Replace an automation in automations.yaml; returns the previous config."""
    config = _ordered(config_id, {k: v for k, v in config.items() if k != "id"})
    await validate(hass, config)
    path = hass.config.path(AUTOMATION_CONFIG_PATH)
    async with _LOCK:
        data = await hass.async_add_executor_job(_read_file, path)
        for i, cur in enumerate(data):
            if str(cur.get("id")) == config_id:
                previous = cur
                data[i] = config
                break
        else:
            raise AdvisorError("That automation is no longer in automations.yaml.")
        await hass.async_add_executor_job(_write_file, path, data)
    await _reload(hass, config_id)
    return previous


async def delete(hass: HomeAssistant, config_id: str) -> dict:
    """Remove an automation from automations.yaml; returns the deleted config."""
    path = hass.config.path(AUTOMATION_CONFIG_PATH)
    async with _LOCK:
        data = await hass.async_add_executor_job(_read_file, path)
        keep = [c for c in data if str(c.get("id")) != config_id]
        if len(keep) == len(data):
            raise AdvisorError("That automation is no longer in automations.yaml.")
        previous = next(c for c in data if str(c.get("id")) == config_id)
        await hass.async_add_executor_job(_write_file, path, keep)
    await _reload(hass, config_id)
    registry = er.async_get(hass)
    if entity_id := registry.async_get_entity_id(AUTOMATION_DOMAIN, AUTOMATION_DOMAIN, config_id):
        registry.async_remove(entity_id)
    return previous


async def disable(hass: HomeAssistant, entity_id: str) -> None:
    if hass.states.get(entity_id) is None:
        raise AdvisorError(f"{entity_id} no longer exists.")
    await hass.services.async_call(
        AUTOMATION_DOMAIN, "turn_off", {"entity_id": entity_id}, blocking=True
    )
