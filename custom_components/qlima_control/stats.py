"""Energy and cost today/this month, runtime, starts and defrost detection (persisted)."""

from __future__ import annotations

from datetime import timedelta
from time import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, SIGNAL_UPDATE
from .unit import QlimaUnit

_TICK = timedelta(seconds=30)
# Defrost: heating at speed, then the compressor drops out while it is cold outside.
_DEFROST_MAX_OUTDOOR = 6.0
_DEFROST_LOW_W = 120
_DEFROST_RUN_W = 300


class QlimaStats:
    """Counts what the unit used and did; saved so it survives restarts."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, unit: QlimaUnit) -> None:
        self.hass = hass
        self.entry = entry
        self.unit = unit
        self._store = Store(hass, 1, f"qlima_control.stats.{entry.entry_id}")
        self.data: dict[str, Any] = {}
        self._unsubs: list = []
        self._last_tick = time()
        self._running = False
        self._was_running_at_speed = 0.0
        self.defrosting = False

    async def async_start(self) -> None:
        self.data = await self._store.async_load() or {}
        self._roll()
        current = self.unit._float(self.unit.energy_sensor)
        if current is not None and self.data.get("last_energy") is None:
            self.data["last_energy"] = current  # count from here, not from the meter's lifetime total
        ids = [i for i in (self.unit.energy_sensor, self.unit.power_sensor) if i]
        if ids:
            self._unsubs.append(async_track_state_change_event(self.hass, ids, self._changed))
        self._unsubs.append(async_track_time_interval(self.hass, self._tick, _TICK))

    def stop(self) -> None:
        while self._unsubs:
            self._unsubs.pop()()
        self._store.async_delay_save(lambda: self.data, 0)

    # --- buckets -----------------------------------------------------------
    def _roll(self) -> None:
        now = dt_util.now()
        day, month = now.date().isoformat(), now.strftime("%Y-%m")
        if self.data.get("day") != day:
            self.data.update(day=day, energy_day=0.0, cost_day=0.0, runtime_day=0.0, starts_day=0, defrosts_day=0)
        if self.data.get("month") != month:
            self.data.update(month=month, energy_month=0.0, cost_month=0.0)

    def _add_energy(self, kwh: float) -> None:
        if kwh <= 0 or kwh > 5:  # ignore meter resets and jumps
            return
        self._roll()
        price = self.unit.price()
        self.data["energy_day"] = self.data.get("energy_day", 0.0) + kwh
        self.data["energy_month"] = self.data.get("energy_month", 0.0) + kwh
        if price is not None:
            self.data["cost_day"] = self.data.get("cost_day", 0.0) + kwh * price
            self.data["cost_month"] = self.data.get("cost_month", 0.0) + kwh * price
        self._save()

    def _save(self) -> None:
        self._store.async_delay_save(lambda: self.data, 30)
        async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(self.entry.entry_id))

    # --- events ------------------------------------------------------------
    @callback
    def _changed(self, event: Event) -> None:
        if event.data.get("entity_id") != self.unit.energy_sensor:
            return
        new = self.unit._float(self.unit.energy_sensor)
        last = self.data.get("last_energy")
        if new is not None:
            if last is not None:
                self._add_energy(new - last)
            self.data["last_energy"] = new

    @callback
    def _tick(self, _now) -> None:
        now = time()
        dt = now - self._last_tick
        self._last_tick = now
        self._roll()
        # Energy from power when there is no energy meter.
        if not self.unit.energy_sensor:
            power = self.unit.power()
            if power is not None and 0 < dt < 600:
                self._add_energy(power * dt / 3_600_000)
        mode = self.unit.pump_mode()
        running = self.unit.running("heat") or self.unit.running("cool")
        if running and 0 < dt < 600:
            self.data["runtime_day"] = self.data.get("runtime_day", 0.0) + dt / 3600
        if running and not self._running:
            self.data["starts_day"] = self.data.get("starts_day", 0) + 1
        self._running = running
        self._detect_defrost(mode)
        self._save()

    def _detect_defrost(self, mode: str | None) -> None:
        own = self.unit.unit_defrost()
        if own is not None:
            # The unit reports its defrost itself (service data): count each start.
            if own and not self.defrosting:
                self.data["defrosts_day"] = self.data.get("defrosts_day", 0) + 1
            self.defrosting = own
            return
        power, outdoor = self.unit.power(), self.unit.outdoor()
        if mode != "heat" or power is None or outdoor is None or outdoor > _DEFROST_MAX_OUTDOOR:
            self.defrosting = False
            return
        thermostat = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id)
        if thermostat is not None and thermostat.stop_requested():
            # The compressor stops because it was told to – that drop is no defrost.
            self.defrosting = False
            self._was_running_at_speed = 0.0
            return
        if power >= _DEFROST_RUN_W:
            self._was_running_at_speed = time()
            if self.defrosting:
                self.defrosting = False
            return
        recently_running = time() - self._was_running_at_speed < 120
        if power < _DEFROST_LOW_W and recently_running and not self.defrosting:
            self.defrosting = True
            self.data["defrosts_day"] = self.data.get("defrosts_day", 0) + 1
        elif not recently_running and time() - self._was_running_at_speed > 900:
            self.defrosting = False

    # --- values ------------------------------------------------------------
    def value(self, key: str) -> float | int | None:
        self._roll()
        v = self.data.get(key)
        if v is None:
            return None
        return round(v, 3) if isinstance(v, float) else v
