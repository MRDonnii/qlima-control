"""Qlima Control: local control of Qlima/Midea heat pumps through an ESP32 in the unit."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, PLATFORMS
from .feeder import RoomFeeder
from .notify import QlimaNotifier
from .unit import QlimaUnit


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one heat pump without issuing device commands."""
    hass.data.setdefault(DOMAIN, {})
    unit = QlimaUnit(hass, {**entry.data, **entry.options})
    hass.data[DOMAIN][f"{entry.entry_id}_unit"] = unit
    notifier = QlimaNotifier(hass, entry, unit)
    hass.data[DOMAIN][f"{entry.entry_id}_notifier"] = notifier
    feeder = RoomFeeder(hass, entry, unit)
    hass.data[DOMAIN][f"{entry.entry_id}_feeder"] = feeder
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    notifier.start()
    feeder.start()
    entry.async_on_unload(entry.add_update_listener(_options_updated))
    return True


async def _options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Linked sensors may have changed; rebuild everything."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an entry."""
    notifier = hass.data.get(DOMAIN, {}).get(f"{entry.entry_id}_notifier")
    if notifier is not None:
        notifier.stop()
    feeder = hass.data.get(DOMAIN, {}).get(f"{entry.entry_id}_feeder")
    if feeder is not None:
        feeder.stop()
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        for key in (entry.entry_id, f"{entry.entry_id}_unit", f"{entry.entry_id}_notifier", f"{entry.entry_id}_feeder"):
            hass.data.get(DOMAIN, {}).pop(key, None)
    return unloaded
