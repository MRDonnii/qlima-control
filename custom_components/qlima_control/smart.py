"""Price steering, wake-up from the unit's low-effort hold, and a 30-minute turbo.

Price steering: the target goes up a degree in cheap periods and down a degree
in expensive ones (heat; the other way round for cool), within the limits you
set. The level is held at least 30 minutes so the unit is not retargeted often.

Wake-up: Midea-type units drop to a low compressor grade after a couple of
hours of hard work, and only a mains power-cycle makes them try hard again.
When the room stays clearly below target while the unit draws little power
for a long time, the supply relay is cycled. Off by default; never more than a
few times a day; only while the thermostat wants the unit running.

Turbo: the unit's own boost preset for 30 minutes, then back to normal.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from time import time
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.util import dt as dt_util

from .const import (
    CONF_AUTO_WAKE,
    CONF_PRICE_CHEAP_OFFSET,
    CONF_PRICE_CONTROL,
    CONF_PRICE_EXPENSIVE_OFFSET,
    CONF_PRICE_RESOLUTION,
    CONF_PRICE_RESOLUTION_ENTITY,
    CONF_WAKE_DEFICIT,
    CONF_WAKE_MINUTES,
    CONF_WAKE_POWER_W,
    DEFAULT_PRICE_CHEAP_OFFSET,
    DEFAULT_PRICE_EXPENSIVE_OFFSET,
    DEFAULT_WAKE_DEFICIT,
    DEFAULT_WAKE_MINUTES,
    DEFAULT_WAKE_POWER_W,
    DOMAIN,
    SIGNAL_UPDATE,
    TURBO_MINUTES,
    WAKE_COOLDOWN_MINUTES,
    WAKE_MAX_PER_DAY,
)
from .prices import LEVEL_CHEAP, LEVEL_EXPENSIVE, LEVEL_UNKNOWN, PriceRank, parse_prices, rank_now, to_hours
from .unit import QlimaUnit

_LOGGER = logging.getLogger(__name__)
PRICE_MIN_HOLD = timedelta(minutes=30)
LEVEL_OFF = "off"
# Midea units hold at their lowest compressor grade (~20 Hz) in the low-effort state.
_WAKE_HOLD_MAX_HZ = 25


def _cfg(entry: ConfigEntry) -> dict[str, Any]:
    return {**entry.data, **entry.options}


def hourly_wanted(hass: HomeAssistant, cfg: dict[str, Any]) -> bool:
    """Whether prices should be ranked per hour instead of per quarter."""
    mode = cfg.get(CONF_PRICE_RESOLUTION, "auto")
    if mode in ("hour", "quarter"):
        return mode == "hour"
    entity = cfg.get(CONF_PRICE_RESOLUTION_ENTITY)
    state = hass.states.get(entity) if entity else None
    if state is None:
        return False
    text = str(state.state).lower()
    return any(word in text for word in ("time", "hour", "60"))


class PriceControl:
    """Holds the current price level and turns it into a target offset."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, unit: QlimaUnit) -> None:
        self.hass = hass
        self.entry = entry
        self.unit = unit
        self.level = LEVEL_OFF
        self.rank: PriceRank | None = None
        self._since: datetime | None = None
        self._unsub = None

    @property
    def enabled(self) -> bool:
        return bool(_cfg(self.entry).get(CONF_PRICE_CONTROL, False)) and bool(self.unit.price_sensor)

    def offset(self, cool: bool = False) -> int:
        """Degrees to add to the heat target (or the cool target when ``cool``)."""
        cfg = _cfg(self.entry)
        if not self.enabled:
            return 0
        if self.level == LEVEL_CHEAP:
            step = int(cfg.get(CONF_PRICE_CHEAP_OFFSET, DEFAULT_PRICE_CHEAP_OFFSET))
            return -step if cool else step
        if self.level == LEVEL_EXPENSIVE:
            step = int(cfg.get(CONF_PRICE_EXPENSIVE_OFFSET, DEFAULT_PRICE_EXPENSIVE_OFFSET))
            return step if cool else -step
        return 0

    def start(self) -> None:
        self._unsub = async_track_time_interval(self.hass, self._tick, timedelta(minutes=1))
        self.refresh()

    def stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    @callback
    def _tick(self, _now) -> None:
        self.refresh()

    @callback
    def refresh(self, force: bool = False) -> None:
        """Re-rank the price now; change level only after the minimum hold."""
        old = self.level
        if not self.enabled:
            self.level, self.rank, self._since = LEVEL_OFF, None, None
        else:
            state = self.hass.states.get(self.unit.price_sensor)
            periods = parse_prices(dict(state.attributes)) if state is not None else []
            if hourly_wanted(self.hass, _cfg(self.entry)):
                periods = to_hours(periods)
            now = dt_util.utcnow()
            self.rank = rank_now(periods, now)
            new = self.rank.level
            held = self._since is None or now - self._since >= PRICE_MIN_HOLD
            if new != old and (held or force or old in (LEVEL_OFF, LEVEL_UNKNOWN) or new == LEVEL_UNKNOWN):
                self.level = new
                self._since = now
        if self.level != old:
            _LOGGER.info("%s: price level %s -> %s", self.entry.title, old, self.level)
            async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(self.entry.entry_id))
            thermostat = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id)
            if thermostat is not None:
                self.hass.async_create_task(thermostat.async_price_changed())

    def attributes(self) -> dict[str, Any]:
        rank = self.rank
        return {
            "offset_heat": self.offset(False),
            "offset_cool": self.offset(True),
            "price": rank.price if rank else None,
            "percentile": rank.percentile if rank else None,
            "period_end": rank.period_end.isoformat() if rank and rank.period_end else None,
            "known_periods": rank.periods if rank else 0,
            "level_since": self._since.isoformat() if self._since else None,
        }


class WakeControl:
    """Cycles the supply when the unit idles along while the room stays cold."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, unit: QlimaUnit) -> None:
        self.hass = hass
        self.entry = entry
        self.unit = unit
        self.stalled_since: float | None = None
        self.last_wake: float | None = None
        self._day = dt_util.now().date()
        self.wakes_today = 0
        self.reason = "off"
        self._unsub = None

    @property
    def enabled(self) -> bool:
        return bool(_cfg(self.entry).get(CONF_AUTO_WAKE, False)) and bool(self.unit.power_switch)

    def start(self) -> None:
        if self.unit.power_switch:
            self._unsub = async_track_time_interval(self.hass, self._tick, timedelta(minutes=1))

    def stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    @property
    def wake_at(self) -> float | None:
        """When the wake-up happens if nothing changes (epoch seconds)."""
        if self.stalled_since is None or not self.enabled:
            return None
        return self.stalled_since + float(_cfg(self.entry).get(CONF_WAKE_MINUTES, DEFAULT_WAKE_MINUTES)) * 60

    def check(self, now: float) -> str:
        """Why the unit is (not) considered stalled; updates ``stalled_since``."""
        cfg = _cfg(self.entry)
        reason = self._condition(cfg)
        if reason != "stalled":
            self.stalled_since = None
            return reason
        self.stalled_since = self.stalled_since or now
        return reason

    def _condition(self, cfg: dict[str, Any]) -> str:
        if not self.enabled:
            return "off"
        thermostat = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id)
        power = self.hass.data.get(DOMAIN, {}).get(f"{self.entry.entry_id}_power")
        stats = self.hass.data.get(DOMAIN, {}).get(f"{self.entry.entry_id}_stats")
        if thermostat is None or (power is not None and power.cycling):
            return "busy"
        status = thermostat.status()
        if status not in ("heating", "cooling"):
            return "not_running"
        mode = "heat" if status == "heating" else "cool"
        if self.unit.pump_mode() != mode or self.unit.online() is False:
            return "unit_not_on"
        if stats is not None and getattr(stats, "defrosting", False):
            return "defrosting"
        room = self.unit.room_temperature().value
        watts = self.unit.power()
        hz = self.unit.compressor_hz()
        if room is None or (watts is None and hz is None):
            return "no_data"
        target = thermostat.effective_target(mode)
        deficit = (target - room) if mode == "heat" else (room - target)
        if deficit < float(cfg.get(CONF_WAKE_DEFICIT, DEFAULT_WAKE_DEFICIT)):
            return "on_target"
        if hz is not None:
            # The compressor frequency tells the low-effort hold directly (~20 Hz or stopped).
            if hz > _WAKE_HOLD_MAX_HZ:
                return "working"
        elif watts >= float(cfg.get(CONF_WAKE_POWER_W, DEFAULT_WAKE_POWER_W)):
            return "working"
        return "stalled"

    @callback
    def refresh(self) -> None:
        """Re-check now (e.g. right after the switch changed)."""
        self.reason = self.check(time())
        async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(self.entry.entry_id))

    @callback
    def _tick(self, _now) -> None:
        now = time()
        today = dt_util.now().date()
        if today != self._day:
            self._day, self.wakes_today = today, 0
        old = (self.reason, self.stalled_since)
        self.reason = self.check(now)
        wake_at = self.wake_at
        if (
            wake_at is not None
            and now >= wake_at
            and self.wakes_today < WAKE_MAX_PER_DAY
            and (self.last_wake is None or now - self.last_wake >= WAKE_COOLDOWN_MINUTES * 60)
        ):
            self.hass.async_create_task(self.async_wake())
        if (self.reason, self.stalled_since) != old:
            async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(self.entry.entry_id))

    async def async_wake(self) -> None:
        power = self.hass.data.get(DOMAIN, {}).get(f"{self.entry.entry_id}_power")
        if power is None:
            return
        self.last_wake = time()
        self.wakes_today += 1
        self.stalled_since = None
        _LOGGER.warning("%s: unit idles while the room is below target – waking it with a power-cycle", self.entry.title)
        notifier = self.hass.data.get(DOMAIN, {}).get(f"{self.entry.entry_id}_notifier")
        if notifier is not None:
            notifier.notify_wake()
        await power.async_power_cycle("wake from low-effort hold")
        async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(self.entry.entry_id))

    def attributes(self) -> dict[str, Any]:
        wake_at = self.wake_at
        return {
            "reason": self.reason,
            "stalled_since": datetime.fromtimestamp(self.stalled_since, dt_util.UTC).isoformat() if self.stalled_since else None,
            "wake_at": datetime.fromtimestamp(wake_at, dt_util.UTC).isoformat() if wake_at else None,
            "last_wake": datetime.fromtimestamp(self.last_wake, dt_util.UTC).isoformat() if self.last_wake else None,
            "max_per_day": WAKE_MAX_PER_DAY,
        }


class TurboControl:
    """The unit's boost preset for a fixed time."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, unit: QlimaUnit) -> None:
        self.hass = hass
        self.entry = entry
        self.unit = unit
        self.until: float | None = None
        self._cancel = None

    @property
    def active(self) -> bool:
        return self.until is not None and self.until > time()

    def available(self) -> bool:
        state = self.hass.states.get(self.unit.heat_pump) if self.unit.heat_pump else None
        return state is not None and "boost" in (state.attributes.get("preset_modes") or [])

    async def async_start(self) -> None:
        if not self.available():
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="turbo_no_boost")
        if self.unit.pump_mode() not in ("heat", "cool"):
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="turbo_not_running")
        await self._preset("boost")
        self.until = time() + TURBO_MINUTES * 60
        if self._cancel:
            self._cancel()
        self._cancel = async_call_later(self.hass, TURBO_MINUTES * 60, self._expired)
        async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(self.entry.entry_id))

    async def async_stop(self) -> None:
        if self._cancel:
            self._cancel()
            self._cancel = None
        self.until = None
        state = self.hass.states.get(self.unit.heat_pump)
        if state is not None and state.attributes.get("preset_mode") == "boost":
            await self._preset("none")
        async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(self.entry.entry_id))

    async def _preset(self, preset: str) -> None:
        await self.hass.services.async_call(
            "climate", "set_preset_mode", {"entity_id": self.unit.heat_pump, "preset_mode": preset}, blocking=True
        )

    @callback
    def _expired(self, _now) -> None:
        self._cancel = None
        self.hass.async_create_task(self.async_stop())

    def stop(self) -> None:
        if self._cancel:
            self._cancel()
            self._cancel = None
