"""Mains power to the unit through a relay (e.g. a Shelly): switch, restart and auto-restart."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import logging
from time import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .const import CONF_AUTO_POWER_CYCLE_MINUTES, DOMAIN, POWER_CYCLE_GUARD_SECONDS, POWER_CYCLE_OFF_SECONDS
from .unit import QlimaUnit

_LOGGER = logging.getLogger(__name__)
# Never auto-restart more often than this.
_AUTO_COOLDOWN_SECONDS = 3600


class PowerControl:
    """Restarts the unit by cutting its supply, on request or when it stops answering."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, unit: QlimaUnit) -> None:
        self.hass = hass
        self.entry = entry
        self.unit = unit
        cfg = {**entry.data, **entry.options}
        self.auto_minutes = float(cfg.get(CONF_AUTO_POWER_CYCLE_MINUTES) or 0)
        self._offline_since: float | None = None
        self._last_cycle = 0.0
        self._cycling = False
        self._unsub = None

    def start(self) -> None:
        if self.unit.power_switch and self.auto_minutes > 0:
            self._unsub = async_track_time_interval(self.hass, self._check, timedelta(minutes=1))

    def stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    @property
    def cycling(self) -> bool:
        return self._cycling

    async def async_power_cycle(self, reason: str = "user") -> None:
        """Switch the supply off for 30 s and back on."""
        if not self.unit.power_switch or self._cycling:
            return
        self._cycling = True
        self._last_cycle = time()
        thermostat = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id)
        if thermostat is not None:
            thermostat.suppress_manual_detection(POWER_CYCLE_OFF_SECONDS + POWER_CYCLE_GUARD_SECONDS)
        _LOGGER.warning("%s: power-cycling the unit (%s)", self.entry.title, reason)
        try:
            await self.hass.services.async_call("homeassistant", "turn_off", {"entity_id": self.unit.power_switch}, blocking=True)
            await asyncio.sleep(POWER_CYCLE_OFF_SECONDS)
        finally:
            await self.hass.services.async_call("homeassistant", "turn_on", {"entity_id": self.unit.power_switch}, blocking=True)
            self._cycling = False

    @callback
    def _check(self, _now) -> None:
        if self.unit.online() is False:
            self._offline_since = self._offline_since or time()
            if (
                time() - self._offline_since >= self.auto_minutes * 60
                and time() - self._last_cycle >= _AUTO_COOLDOWN_SECONDS
                and not self._cycling
            ):
                self.hass.async_create_task(self.async_power_cycle("unit not responding"))
        else:
            self._offline_since = None
