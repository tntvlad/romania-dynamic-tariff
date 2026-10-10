"""Config flow for Romania Dynamic Tariff integration."""
from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.core import callback
import homeassistant.helpers.config_validation as cv

from .const import (
    DOMAIN,
    OPT_RETENTION_CUSTOM_DAYS,
    OPT_RETENTION_MODE,
    OPT_KEEP_RAW_CSV,
    OPT_DELETE_LEGACY_FILES,
    OPT_BACKFILL_HISTORY,
    RETENTION_MODES,
    DEFAULT_RETENTION_MODE,
    DEFAULT_RETENTION_CUSTOM_DAYS,
)

DEFAULT_START_DATE = "2025-08-14"
DEFAULT_ENTRY_NAME = "Romania Dynamic Tariff"


class RomaniaDynamicConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Romania Dynamic Tariff."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            return self.async_create_entry(
                title="Romania Dynamic Electricity Prices",
                data=user_input,
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required("start_date", default=DEFAULT_START_DATE): str,
                    vol.Optional(CONF_NAME, default=DEFAULT_ENTRY_NAME): str,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        """Return the options flow for this integration.

        Since HA 2024.11 the framework injects ``config_entry`` into the
        options flow, so it must NOT be passed to the constructor.
        """
        return RomaniaDynamicOptionsFlow()


class RomaniaDynamicOptionsFlow(config_entries.OptionsFlow):
    """Handle options (retention / raw csv / legacy files)."""

    async def async_step_init(self, user_input=None):
        """Manage the options."""
        if user_input is not None:
            # Re-read values from the form each time so nothing is lost if
            # new options are added in the future.
            options = dict(self.config_entry.options)
            options.update(user_input)
            return self.async_create_entry(title="", data=options)

        options = self.config_entry.options

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        OPT_RETENTION_MODE,
                        default=options.get(
                            OPT_RETENTION_MODE, DEFAULT_RETENTION_MODE
                        ),
                    ): vol.In(RETENTION_MODES),
                    vol.Required(
                        OPT_RETENTION_CUSTOM_DAYS,
                        default=options.get(
                            OPT_RETENTION_CUSTOM_DAYS, DEFAULT_RETENTION_CUSTOM_DAYS
                        ),
                    ): cv.positive_int,
                    vol.Optional(
                        OPT_KEEP_RAW_CSV,
                        default=options.get(OPT_KEEP_RAW_CSV, False),
                    ): bool,
                    vol.Optional(
                        OPT_BACKFILL_HISTORY,
                        default=options.get(OPT_BACKFILL_HISTORY, True),
                    ): bool,
                    vol.Optional(
                        OPT_DELETE_LEGACY_FILES,
                        default=options.get(OPT_DELETE_LEGACY_FILES, False),
                    ): bool,
                }
            ),
        )