"""OPCOM Historical Price Integration."""
import asyncio
import logging
from datetime import datetime, timedelta
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

_LOGGER = logging.getLogger(__name__)

DOMAIN = "opcom_historical"
PLATFORMS = [Platform.SENSOR]

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Schema({
            vol.Required("start_date"): cv.date,
            vol.Optional("download_interval", default=3600): cv.positive_int,
        })
    },
    extra=vol.ALLOW_EXTRA,
)

async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the OPCOM Historical component."""
    _LOGGER.info("Setting up OPCOM Historical integration")
    
    # Initialize domain data
    hass.data.setdefault(DOMAIN, {})
    
    return True

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up OPCOM Historical from a config entry."""
    _LOGGER.info(f"Setting up OPCOM Historical config entry: {entry.entry_id}")
    
    # Initialize domain data if not exists
    hass.data.setdefault(DOMAIN, {})
    
    # Store entry data
    hass.data[DOMAIN][entry.entry_id] = {
        "config": entry.data,
        "options": entry.options,
    }
    
    # Forward setup to platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    
    # Set up options update listener
    entry.async_on_unload(entry.add_update_listener(async_update_options))
    
    _LOGGER.info("OPCOM Historical integration setup completed")
    return True

async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    _LOGGER.info(f"Unloading OPCOM Historical config entry: {entry.entry_id}")
    
    # Unload platforms
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    
    # Remove entry data
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        
        # Clean up domain data if no more entries
        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN, None)
    
    _LOGGER.info(f"OPCOM Historical unload result: {unload_ok}")
    return unload_ok

async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update options."""
    _LOGGER.info(f"Updating options for OPCOM Historical: {entry.entry_id}")
    
    # Update stored options
    if DOMAIN in hass.data and entry.entry_id in hass.data[DOMAIN]:
        hass.data[DOMAIN][entry.entry_id]["options"] = entry.options
    
    # Reload the entry to apply new options
    await hass.config_entries.async_reload(entry.entry_id)

async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old entry."""
    _LOGGER.info(f"Migrating OPCOM Historical entry from version {config_entry.version}")
    
    if config_entry.version == 1:
        # Migration logic for version 1 to 2 (if needed in future)
        # For now, no migration needed
        pass
    
    _LOGGER.info(f"Migration to version {config_entry.version} successful")
    return True