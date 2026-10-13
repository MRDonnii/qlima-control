"""Qlima Control: local control of Qlima/Midea heat pumps through an ESP32 in the unit."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN, LIVE_KEYS, PLATFORMS
from .feeder import RoomFeeder
from .notify import QlimaNotifier
from .power import PowerControl
from .repairs import schedule_checks
from .stats import QlimaStats
from .unit import QlimaUnit


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up one heat pump without issuing device commands."""
    hass.data.setdefault(DOMAIN, {})
    unit = QlimaUnit(hass, {**entry.data, **entry.options})
    hass.data[DOMAIN][f"{entry.entry_id}_unit"] = unit
    notifier = QlimaNotifier(hass, entry, unit)
    hass.data[DOMAIN][f"{entry.entry_id}_notifier"] = notifier
    stats = QlimaStats(hass, entry, unit)
    await stats.async_start()
    hass.data[DOMAIN][f"{entry.entry_id}_stats"] = stats
    power = PowerControl(hass, entry, unit)
    hass.data[DOMAIN][f"{entry.entry_id}_power"] = power
    feeder = RoomFeeder(hass, entry, unit)
    hass.data[DOMAIN][f"{entry.entry_id}_feeder"] = feeder
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    notifier.start()
    feeder.start()
    power.start()
    entry.async_on_unload(schedule_checks(hass, entry))
    entry.async_on_unload(entry.add_update_listener(_options_updated))
    return True


async def _options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply live settings in place; anything else (linked sensors) rebuilds the entry."""
    unit = hass.data[DOMAIN].get(f"{entry.entry_id}_unit")
    old = unit.config if unit is not None else {}
    new = {**entry.data, **entry.options}
    changed = {k for k in set(old) | set(new) if old.get(k) != new.get(k)}
    thermostat = hass.data[DOMAIN].get(entry.entry_id)
    if changed and changed <= LIVE_KEYS and thermostat is not None:
        unit.config = new
        thermostat.apply_live_settings()
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload an entry."""
    notifier = hass.data.get(DOMAIN, {}).get(f"{entry.entry_id}_notifier")
    if notifier is not None:
        notifier.stop()
    for key in ("power", "stats"):
        obj = hass.data.get(DOMAIN, {}).get(f"{entry.entry_id}_{key}")
        if obj is not None:
            obj.stop()
    feeder = hass.data.get(DOMAIN, {}).get(f"{entry.entry_id}_feeder")
    if feeder is not None:
        feeder.stop()
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        for key in (entry.entry_id, f"{entry.entry_id}_unit", f"{entry.entry_id}_notifier", f"{entry.entry_id}_feeder", f"{entry.entry_id}_power", f"{entry.entry_id}_stats"):
            hass.data.get(DOMAIN, {}).pop(key, None)
    return unloaded
