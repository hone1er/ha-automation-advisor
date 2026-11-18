"""Config and options flow. Setup needs no input; everything is tunable later in Options."""

from __future__ import annotations

from typing import Any

from ._compat import vol

from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .sources import RemoteAuthError, async_test_connection
from .const import (
    CONF_ALLOW_REMOTE_WRITE,
    CONF_MODE,
    CONF_TOKEN,
    CONF_URL,
    CONF_VERIFY_SSL,
    MODE_LOCAL,
    MODE_REMOTE,
    CONF_AI_TASK_ENTITY,
    CONF_EXCLUDED,
    CONF_IGNORE_EXISTING,
    CONF_INTERVAL_HOURS,
    CONF_LOOKBACK_DAYS,
    CONF_MIN_CONFIDENCE,
    CONF_MIN_OCCURRENCES,
    CONF_NOTIFY,
    CONF_OVERRIDE_WINDOW,
    CONF_RETENTION_DAYS,
    DEFAULTS,
    DOMAIN,
    TITLE,
)


def _number(lo: float, hi: float, step: float = 1, unit: str | None = None) -> selector.NumberSelector:
    config = selector.NumberSelectorConfig(
        min=lo, max=hi, step=step, mode=selector.NumberSelectorMode.BOX
    )
    if unit:
        config["unit_of_measurement"] = unit
    return selector.NumberSelector(config)


OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_INTERVAL_HOURS): _number(0, 168, 1, "h"),
        vol.Required(CONF_LOOKBACK_DAYS): _number(3, 60, 1, "days"),
        vol.Required(CONF_MIN_OCCURRENCES): _number(3, 50, 1),
        vol.Required(CONF_MIN_CONFIDENCE): _number(0.5, 0.95, 0.05),
        vol.Required(CONF_OVERRIDE_WINDOW): _number(15, 900, 5, "s"),
        vol.Required(CONF_RETENTION_DAYS): _number(1, 365, 1, "days"),
        vol.Optional(CONF_AI_TASK_ENTITY): selector.EntitySelector(
            selector.EntitySelectorConfig(domain="ai_task")
        ),
        vol.Required(CONF_EXCLUDED): selector.EntitySelector(
            selector.EntitySelectorConfig(multiple=True)
        ),
        vol.Required(CONF_NOTIFY): selector.BooleanSelector(),
        vol.Required(CONF_IGNORE_EXISTING): selector.BooleanSelector(),
    }
)
REMOTE_OPTIONS = OPTIONS_SCHEMA.extend(
    {vol.Required(CONF_ALLOW_REMOTE_WRITE): selector.BooleanSelector()}
)


AI_FIELD = {
    vol.Optional(CONF_AI_TASK_ENTITY): selector.EntitySelector(
        selector.EntitySelectorConfig(domain="ai_task")
    )
}


class AdvisorConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        return self.async_show_menu(step_id="user", menu_options=["local", "remote"])

    async def async_step_local(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._create(TITLE, {CONF_MODE: MODE_LOCAL}, user_input)
        return self.async_show_form(step_id="local", data_schema=vol.Schema(AI_FIELD))

    async def async_step_remote(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        placeholders = {"error": "", "url_example": "http://homeassistant.local:8123"}
        if user_input is not None:
            url = user_input[CONF_URL].strip().rstrip("/")
            if not url.startswith(("http://", "https://")):
                url = "http://" + url
            try:
                info = await async_test_connection(
                    self.hass, url, user_input[CONF_TOKEN], user_input.get(CONF_VERIFY_SSL, True)
                )
            except RemoteAuthError:
                errors["base"] = "invalid_auth"
            except Exception as err:  # noqa: BLE001 - shown to the user
                errors["base"] = "cannot_connect"
                placeholders["error"] = f"({type(err).__name__}: {err})"
            else:
                data = {
                    CONF_MODE: MODE_REMOTE,
                    CONF_URL: url,
                    CONF_TOKEN: user_input[CONF_TOKEN],
                    CONF_VERIFY_SSL: user_input.get(CONF_VERIFY_SSL, True),
                }
                title = f"{TITLE} ({info.get('location') or url}, read-only)"
                return self._create(title, data, user_input)
        schema = vol.Schema(
            {
                vol.Required(CONF_URL): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.URL)
                ),
                vol.Required(CONF_TOKEN): selector.TextSelector(
                    selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
                ),
                vol.Required(CONF_VERIFY_SSL, default=True): selector.BooleanSelector(),
                **AI_FIELD,
            }
        )
        return self.async_show_form(
            step_id="remote",
            data_schema=self.add_suggested_values_to_schema(schema, user_input or {}),
            errors=errors,
            description_placeholders=placeholders,
        )

    def _create(self, title: str, data: dict, user_input: dict) -> ConfigFlowResult:
        options = dict(DEFAULTS)
        options[CONF_AI_TASK_ENTITY] = user_input.get(CONF_AI_TASK_ENTITY) or None
        return self.async_create_entry(title=title, data=data, options=options)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return AdvisorOptionsFlow()


class AdvisorOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            data = dict(user_input)
            data.setdefault(CONF_AI_TASK_ENTITY, None)
            for key in (CONF_INTERVAL_HOURS, CONF_LOOKBACK_DAYS, CONF_MIN_OCCURRENCES,
                        CONF_OVERRIDE_WINDOW, CONF_RETENTION_DAYS):
                data[key] = int(data[key])
            return self.async_create_entry(data=data)
        current = {**DEFAULTS, **self.config_entry.options}
        if current.get(CONF_AI_TASK_ENTITY) is None:
            current.pop(CONF_AI_TASK_ENTITY, None)
        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                REMOTE_OPTIONS if self.config_entry.data.get(CONF_MODE) == MODE_REMOTE
                else OPTIONS_SCHEMA,
                current,
            ),
        )
