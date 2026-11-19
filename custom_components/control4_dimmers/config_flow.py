"""Config flow for Control4 Dimmers."""

from __future__ import annotations

from typing import TYPE_CHECKING

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    AUDIT_MODES,
    CONF_AUDIT_INTERVAL,
    CONF_AUDIT_MODE,
    CONF_MQTT_TOPIC,
    DEFAULT_AUDIT_INTERVAL,
    DEFAULT_AUDIT_MODE,
    DEFAULT_MQTT_TOPIC,
    DOMAIN,
    MAX_AUDIT_INTERVAL,
)

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry


def valid_base_topic(topic: object) -> bool:
    """
    Return whether a Zigbee2MQTT base topic is usable.

    The integration subscribes to <topic>/+ and <topic>/bridge/devices and
    slices device names off the front of incoming topics, so the topic
    must be non-empty, free of MQTT wildcards and NUL, and must not start
    or end with a slash.
    """
    if not isinstance(topic, str) or not topic.strip():
        return False
    if any(ch in topic for ch in "+#\0"):
        return False
    return not topic.startswith("/") and not topic.endswith("/")


def _topic_errors(user_input: dict) -> dict[str, str]:
    """Return form errors for an invalid MQTT base topic."""
    if valid_base_topic(user_input.get(CONF_MQTT_TOPIC)):
        return {}
    return {CONF_MQTT_TOPIC: "invalid_topic"}


class Control4DimmersFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Config flow for Control4 Dimmers."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,  # noqa: ARG004
    ) -> Control4DimmersOptionsFlow:
        """Return the options flow handler."""
        return Control4DimmersOptionsFlow()

    async def async_step_user(
        self,
        user_input: dict | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Handle a flow initialized by the user."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _topic_errors(user_input)
        if user_input is not None and not errors:
            await self.async_set_unique_id(DOMAIN)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title="Control4 Zigbee",
                data=user_input,
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_MQTT_TOPIC,
                        default=DEFAULT_MQTT_TOPIC,
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.TEXT,
                        ),
                    ),
                },
            ),
            errors=errors,
        )


class Control4DimmersOptionsFlow(config_entries.OptionsFlowWithReload):
    """
    Options flow: MQTT topic plus the periodic config audit sweep settings.

    OptionsFlowWithReload reloads the entry when options change, so the
    manager picks up a new interval or mode without an update listener.
    """

    async def async_step_init(
        self,
        user_input: dict | None = None,
    ) -> config_entries.ConfigFlowResult:
        """Show and handle the single options form."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _topic_errors(user_input)
        if user_input is not None and not errors:
            user_input[CONF_AUDIT_INTERVAL] = int(user_input[CONF_AUDIT_INTERVAL])
            return self.async_create_entry(title="", data=user_input)

        options = self.config_entry.options
        data = self.config_entry.data
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_MQTT_TOPIC,
                        default=options.get(
                            CONF_MQTT_TOPIC,
                            data.get(CONF_MQTT_TOPIC, DEFAULT_MQTT_TOPIC),
                        ),
                    ): selector.TextSelector(
                        selector.TextSelectorConfig(
                            type=selector.TextSelectorType.TEXT,
                        ),
                    ),
                    vol.Required(
                        CONF_AUDIT_INTERVAL,
                        default=options.get(
                            CONF_AUDIT_INTERVAL, DEFAULT_AUDIT_INTERVAL
                        ),
                    ): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=MAX_AUDIT_INTERVAL,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                            unit_of_measurement="min",
                        ),
                    ),
                    vol.Required(
                        CONF_AUDIT_MODE,
                        default=options.get(CONF_AUDIT_MODE, DEFAULT_AUDIT_MODE),
                    ): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=AUDIT_MODES,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                            translation_key="audit_mode",
                        ),
                    ),
                },
            ),
            errors=errors,
        )
