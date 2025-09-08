"""Config flow for Romania Dynamic Tariff integration."""
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_NAME
import homeassistant.helpers.config_validation as cv
from datetime import date

DOMAIN = "romania_dynamic_tariff"

class RomaniaDynamicConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Romania Dynamic Tariff."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle the initial step."""
        errors = {}

        if user_input is not None:
            return self.async_create_entry(
                title="Romania Dynamic Electricity Prices",
                data=user_input
            )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({
                vol.Required("start_date", default="2023-12-14"): str,
                vol.Optional("name", default="Romania Dynamic Tariff"): str,
            }),
            errors=errors,
        )