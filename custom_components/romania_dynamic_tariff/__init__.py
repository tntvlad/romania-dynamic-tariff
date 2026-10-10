"""Romania Dynamic Electricity Tariff integration."""
from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .const import (
    DB_FILENAME,
    DB_FOLDER,
    DOMAIN,
)
from .database import PriceDatabase

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the integration (empty on purpose; setup happens via config entry)."""
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up from a config entry."""
    hass.data.setdefault(DOMAIN, {})

    db_path = (
        Path(hass.config.config_dir)
        / DB_FOLDER
        / DB_FILENAME
    )
    database = PriceDatabase(hass, db_path)
    await database.async_init()

    hass.data[DOMAIN][entry.entry_id] = {
        "database": database,
        "coordinator": None,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(async_update_options))

    _LOGGER.info("Romania Dynamic Tariff setup completed for entry %s", entry.entry_id)
    return True


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options change (retention, raw csv, legacy)."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinator = data.get("coordinator")
    if coordinator is not None:
        try:
            await coordinator.async_shutdown()
        except Exception as err:  # noqa: BLE001 - never block unload
            _LOGGER.debug("Coordinator shutdown error: %s", err)

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN, None)
    return unload_ok


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old entry versions."""
    if config_entry.version == 1:
        # No data-schema migration required yet; options are optional.
        config_entry.version = 1
    return True