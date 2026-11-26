"""Energy and cost today/this month, runtime, starts and defrost detection (persisted)."""

from __future__ import annotations

from datetime import timedelta
import math
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
_STARTUP_GRACE = 10 * 60  # s after a start whose dips are the soft start, not defrosts
_DEFROST_MAX = 15 * 60    # s: a longer drop is a stop
_DEFROST_MIN_INTERVAL = 10   # heating minutes between defrosts that count for the forecast
_DEFROST_MAX_INTERVAL = 600


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
        self._run_start: float | None = None
        self._dip_at: float | None = None
        self._dip_step: str | None = None
        self._dip_pushed = False

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
            if mode == "heat":
                self.data["defrost_heat_min"] = self.data.get("defrost_heat_min", 0.0) + dt / 60
        if running and not self._running:
            self.data["starts_day"] = self.data.get("starts_day", 0) + 1
        self._running = running
        self._detect_defrost(mode)
        self._save()

    def _detect_defrost(self, mode: str | None) -> None:
        """A defrost: heating at speed, a drop while cold outside, and the unit resumes by itself.

        Not counted (1.11): drops within _STARTUP_GRACE of a start (the soft start dips twice),
        stops the thermostat asked for (stop step, off, door), drops that end only because the
        thermostat pushed a start, and drops longer than _DEFROST_MAX (a stop, not a defrost).
        A drop is counted once it ends. `defrosting` is set from the drop on, so the Follow Me
        learning pauses during it.
        """
        own = self.unit.unit_defrost()
        if own is not None:
            # The unit reports its defrost itself (service data): count each start.
            if own and not self.defrosting:
                self._count_defrost()
            self.defrosting = own
            return
        power, outdoor = self.unit.power(), self.unit.outdoor()
        if mode != "heat" or power is None or outdoor is None or outdoor > _DEFROST_MAX_OUTDOOR:
            self.defrosting, self._dip_at = False, None
            return
        thermostat = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id)
        if thermostat is not None and thermostat.stop_requested():
            # The compressor stops because it was told to – that drop is no defrost.
            self.defrosting, self._dip_at = False, None
            self._was_running_at_speed = 0.0
            return
        now = time()
        step = getattr(thermostat, "follow_me_step", lambda: None)() if thermostat is not None else None
        if self._dip_at is not None and step == "start" and self._dip_step != "start":
            self._dip_pushed = True  # it only comes back because the thermostat pushed a start
        recently_running = now - self._was_running_at_speed < 120
        if power >= _DEFROST_RUN_W:
            if not recently_running:
                self._run_start = now  # a (re)start: its soft-start dips follow
            self._was_running_at_speed = now
            if self.defrosting:
                self.defrosting = False
                if self._dip_at is not None and now - self._dip_at <= _DEFROST_MAX and not self._dip_pushed:
                    self._count_defrost()
            self._dip_at = None
            return
        if power < _DEFROST_LOW_W and recently_running and not self.defrosting:
            if self._run_start is not None and now - self._run_start < _STARTUP_GRACE:
                return  # the soft start dips: no defrost
            self.defrosting, self._dip_at, self._dip_step, self._dip_pushed = True, now, step, False
        elif self.defrosting and self._dip_at is not None and now - self._dip_at > _DEFROST_MAX:
            self.defrosting, self._dip_at = False, None  # too long for a defrost: it was a stop
        elif not recently_running and now - self._was_running_at_speed > 900:
            self.defrosting, self._dip_at = False, None

    def _count_defrost(self) -> None:
        """One defrost: counted, and the heating minutes since the last one learned for this
        outdoor band (1.12, for the forecast)."""
        self.data["defrosts_day"] = self.data.get("defrosts_day", 0) + 1
        minutes, outdoor = self.data.get("defrost_heat_min", 0.0), self.unit.outdoor()
        if outdoor is not None and _DEFROST_MIN_INTERVAL <= minutes <= _DEFROST_MAX_INTERVAL:
            band = str(int(math.floor(outdoor / 5) * 5))
            intervals = self.data.setdefault("defrost_interval", {})
            old = intervals.get(band)
            intervals[band] = round(minutes if old is None else old + (minutes - old) * 0.3, 1)
        self.data["defrost_heat_min"] = 0.0
        self.data["last_defrost"] = dt_util.utcnow().isoformat(timespec="seconds")

    def defrost_forecast(self) -> dict[str, Any]:
        """Heating minutes until the next defrost is expected, from what this band has shown."""
        outdoor = self.unit.outdoor()
        band = None if outdoor is None else str(int(math.floor(outdoor / 5) * 5))
        interval = (self.data.get("defrost_interval") or {}).get(band) if band is not None else None
        since = round(self.data.get("defrost_heat_min", 0.0))
        minutes = None
        if interval is not None and outdoor is not None and outdoor <= _DEFROST_MAX_OUTDOOR:
            minutes = max(0, round(interval - since))
        return {"minutes": minutes, "interval_min": interval, "heating_since_min": since,
                "last_defrost": self.data.get("last_defrost"), "outdoor_band": band,
                "intervals": dict(self.data.get("defrost_interval") or {})}

    # --- values ------------------------------------------------------------
    def value(self, key: str) -> float | int | None:
        self._roll()
        v = self.data.get(key)
        if v is None:
            return None
        return round(v, 3) if isinstance(v, float) else v
