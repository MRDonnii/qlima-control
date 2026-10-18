"""A low-chatter external-sensor heat-pump thermostat."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import math
from time import time
from typing import Any

from homeassistant.components.climate import ClimateEntity
from homeassistant.components.climate.const import ClimateEntityFeature, HVACAction, HVACMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    ATTR_TEMPERATURE,
    STATE_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfTemperature,
)
from homeassistant.core import Context, Event, HomeAssistant, State, callback
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.restore_state import RestoreEntity

from .const import (
    AWAY_STATES,
    CONF_COOL_TARGET,
    CONF_HEAT_PUMP,
    CONF_HEAT_TARGET,
    CONF_HYSTERESIS,
    CONF_START_HYSTERESIS,
    CONF_MANUAL_OVERRIDE_MINUTES,
    CONF_MIN_OFF_MINUTES,
    CONF_MIN_RUN_MINUTES,
    CONF_OPENING_SENSORS,
    CONF_OPENING_MIN_DELAY_MINUTES,
    CONF_OPENING_OFF_DELAY_MINUTES,
    CONF_OPENING_REDUCE_TO,
    CONF_REGULATION,
    CONF_OPENING_RESUME_DELAY_MINUTES,
    CONF_SENSOR_STABLE_SECONDS,
    CONF_SENSOR_LOSS_GRACE_SECONDS,
    CONF_SETPOINT_COMPENSATION,
    CONF_TEMPERATURE_SENSOR,
    CONF_THERMOSTATS_TO_DISABLE,
    COUNTDOWN_MANUAL_OVERRIDE,
    COUNTDOWN_MIN_RUN,
    COUNTDOWN_OPENING_OFF,
    COUNTDOWN_OPENING_REDUCE,
    COUNTDOWN_OPENING_RESUME,
    COUNTDOWN_SENSOR_LOSS,
    COUNTDOWN_START_ALLOWED,
    COUNTDOWNS,
    DEFAULT_COOL_TARGET,
    DEFAULT_HEAT_TARGET,
    DEFAULT_HYSTERESIS,
    DEFAULT_START_HYSTERESIS,
    DEFAULT_MANUAL_OVERRIDE_MINUTES,
    DEFAULT_MIN_OFF_MINUTES,
    DEFAULT_MIN_RUN_MINUTES,
    DEFAULT_OPENING_MIN_DELAY_MINUTES,
    DEFAULT_OPENING_OFF_DELAY_MINUTES,
    DEFAULT_OPENING_REDUCE_TO,
    DEFAULT_REGULATION,
    DEFAULT_OPENING_RESUME_DELAY_MINUTES,
    DEFAULT_SENSOR_STABLE_SECONDS,
    DEFAULT_SENSOR_LOSS_GRACE_SECONDS,
    DOMAIN,
    OPENING_REDUCE_SETPOINT,
    REGULATION_UNIT,
    OPENING_STAGE_MIN,
    OPENING_STAGE_NONE,
    OPENING_STAGE_OFF,
    PRESET_AUTO,
    PRESET_MANUAL,
    SIGNAL_UPDATE,
)

_LOGGER = logging.getLogger(__name__)
_COMMAND_ECHO_SECONDS = 90
_EVALUATION_SECONDS = 30
_OPENING_STARTUP_GRACE_SECONDS = 90
# How long to wait for the heat pump to report a new target before treating it
# as rejected. Some units silently ignore their advertised minimum/maximum.
_TARGET_CONFIRM_SECONDS = 10
_TARGET_CONFIRM_ATTEMPTS = 4
# While heating/cooling, re-check the compensated unit target this often.
_RETARGET_SECONDS = 300
# Follow Me reaches the unit in whole degrees. While the unit regulates on it, the
# value sent is steered from the room sensor (0.1 K) instead of passed through:
#   start – room _FM_START below the target: the unit is told the room is at least a
#           degree below its target (further when the room is further off), so it
#           works – kept until the target is reached, no turning down before;
#   hold  – at the target: the unit is told the room is exactly at its target and
#           modulates (a stopped unit stays off);
#   stop  – room _FM_STOP above the target: the unit is told the room is at least a
#           degree above its target (further when it is), so the compressor stops –
#           kept until the room is _FM_START below the target again.
# Midea units do not all act on the Follow Me value as it is: some add their own
# compensation in heat mode (an S 5232 told "a degree above" kept running at
# ~630 W). So every value is shifted by a learned offset per unit and mode, learned
# from whether the unit really works (compressor, else power) when it should:
#   - in stop it must stop: still working after _FM_STOP_CHECK → offset +1 (and the
#     offset is known to need at least that);
#   - in hold a running unit must keep running: stopped for _FM_STOP_CHECK → the
#     offset is too large → −1;
#   - in start it must start: idle for _FM_IDLE_CHECK → the offset is too large (−1)
#     if it may be lowered, else the start is pushed a degree further for good (the
#     unit's own start/stop hysteresis is wider);
#   - in start the room must come up: no _FM_PROGRESS within _FM_RISE_CHECK while
#     _FM_START_SHORT short → the offset −1 if it may be lowered, else this start
#     only is pushed a degree further (boost).
# Each step counts as confirmed once the unit did it (stopped in stop, started in
# start); follow_me_learning() reports the check running, its progress and history.
# If even _FM_OFFSET_MAX does not stop it, the unit's target is parked at its
# neutral value during stop (guaranteed off) and restored at the next start.
# Minimum run and off times keep the compressor from short cycling unless the room
# is _FM_FORCE past the target. The unit's own target is the room target rounded.
_FM_START = 0.2
_FM_STOP = 0.3
_FM_FORCE = 0.5
_FM_OFFSET_MAX = 4
_FM_BOOST_MAX = 3
_FM_STOP_CHECK = 6 * 60
_FM_IDLE_CHECK = 10 * 60
_FM_RISE_CHECK = 15 * 60
_FM_HISTORY = 10
_FM_CHECKS = {"stop": _FM_STOP_CHECK, "hold": _FM_STOP_CHECK, "start": _FM_IDLE_CHECK, "rise": _FM_RISE_CHECK}
_FM_START_SHORT = 0.5
_FM_PROGRESS = 0.2
# Midea UART (ESPHome) silently drops a command that arrives while the
# previous one is still waiting for the unit's reply, so pump commands are
# serialised with a gap between them.
_PUMP_COMMAND_GAP_SECONDS = 3
# Evaluations that come from a target change and must reach the unit at once.
_TARGET_REASONS = ("target_change", "price", "resume_auto", "release", "settings", "thermostat_mode")
# Modes passed straight to the unit without temperature regulation.
_DIRECT_MODES = (HVACMode.DRY, HVACMode.FAN_ONLY)
_SWING_HORIZONTAL = getattr(ClimateEntityFeature, "SWING_HORIZONTAL_MODE", None)


def _whole(value: Any) -> float:
    """Round to a whole degree, halves up (the heat pumps only take whole degrees)."""
    return float(math.floor(float(value) + 0.5))


def _half(value: Any) -> float:
    """Round a room target to half a degree: the room sensor steers at 0.1 K."""
    return math.floor(float(value) * 2 + 0.5) / 2


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    """Set up one controller entity."""
    entity = QlimaThermostat(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = entity
    async_add_entities([entity])


class QlimaThermostat(ClimateEntity, RestoreEntity):
    """Control a heat pump from an external sensor without command chatter."""

    _attr_has_entity_name = True
    _attr_name = None
    _attr_translation_key = "thermostat"
    _attr_hvac_modes = [
        HVACMode.OFF,
        HVACMode.HEAT,
        HVACMode.COOL,
        HVACMode.HEAT_COOL,
        HVACMode.DRY,
        HVACMode.FAN_ONLY,
    ]
    _attr_preset_modes = [PRESET_AUTO, PRESET_MANUAL]
    _base_features = (
        ClimateEntityFeature.PRESET_MODE
        | ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.TURN_ON
    )
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = 0.5

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._attr_unique_id = entry.entry_id
        self._attr_device_info = {
            "identifiers": {(DOMAIN, entry.entry_id)},
            "name": entry.title,
            "manufacturer": "Custom",
            "model": "External sensor heat-pump controller",
        }
        self._hvac_mode = HVACMode.OFF
        self._active_mode = HVACMode.OFF
        self._neutral_mode: HVACMode | None = None
        self._preset_mode = PRESET_AUTO
        self._heat_target = DEFAULT_HEAT_TARGET
        self._cool_target = DEFAULT_COOL_TARGET
        self._last_sensor_value: float | None = None
        self._sensor_stable_since = time()
        self._last_transition = time()
        self._last_command = 0.0
        self._last_retarget = 0.0
        self._follow_me_sent: float | None = None
        self._follow_me_step: str = "room"
        self._fm_step_since = time()
        self._fm_run_since: float | None = None
        self._fm_offset: dict[str, int] = {"heat": 0, "cool": 0}
        self._fm_min: dict[str, int] = {"heat": 0, "cool": 0}  # a stop failed one below
        self._fm_start: dict[str, int] = {"heat": 0, "cool": 0}  # start pushed further
        self._fm_park: dict[str, bool] = {"heat": False, "cool": False}
        self._fm_parked = False
        self._fm_boost = 0
        self._fm_hold_worked = False
        self._fm_mode: HVACMode | None = None
        self._fm_verified: dict[str, dict[str, bool]] = {m: {"stop": False, "start": False} for m in ("heat", "cool")}
        self._fm_history: list[dict[str, Any]] = []
        self._fm_mismatch: str | None = None
        self._fm_mismatch_since = 0.0
        self._fm_mismatch_room = 0.0
        self._fm_short = 0.0
        self._neutral_value: float | None = None
        self._command_guard_until = 0.0
        self._manual_until = 0.0
        self._sensor_unavailable_since: float | None = None
        self._pending_mode: HVACMode | None = None
        self._opening_episode_start: float | None = None
        self._opening_open_accum = 0.0
        self._opening_open_since: float | None = None
        self._opening_closed_since: float | None = None
        self._opening_stage = OPENING_STAGE_NONE
        self._min_heat_accepted: float | None = None
        self._max_cool_accepted: float | None = None
        self._eval_lock = asyncio.Lock()
        self._user_start = False
        self._disabled_thermostats: dict[str, dict[str, Any]] = {}
        self._command_count_mode = 0
        self._command_count_temperature = 0
        self._last_command_reason = "none"
        self._command_context = Context()
        self._pump_lock = asyncio.Lock()
        self._last_pump_command = 0.0
        self._observed_pump_target: float | None = None
        self._startup_time = time()
        self._power_guard_until = 0.0
        self._started = False
        self._load_config()

    def _load_config(self) -> None:
        cfg = {**self.entry.data, **self.entry.options}
        self.unit = self.hass.data[DOMAIN][f"{self.entry.entry_id}_unit"]
        self._heat_pump = self.unit.heat_pump or cfg.get(CONF_HEAT_PUMP)
        self._sensors = self.unit.temperature_sensors
        self._thermostats = list(cfg.get(CONF_THERMOSTATS_TO_DISABLE, []))
        self._openings = list(cfg.get(CONF_OPENING_SENSORS, []))
        self._openings_enabled = bool(cfg.get("openings_enabled", True))
        self._presence = cfg.get("presence_entity") or None
        self._away_setback = float(cfg.get("away_setback", 3))
        self._opening_min_delay = float(
            cfg.get(CONF_OPENING_MIN_DELAY_MINUTES, DEFAULT_OPENING_MIN_DELAY_MINUTES)
        ) * 60
        self._opening_off_delay = max(
            self._opening_min_delay,
            float(cfg.get(CONF_OPENING_OFF_DELAY_MINUTES, DEFAULT_OPENING_OFF_DELAY_MINUTES))
            * 60,
        )
        self._opening_resume_delay = float(
            cfg.get(
                CONF_OPENING_RESUME_DELAY_MINUTES,
                DEFAULT_OPENING_RESUME_DELAY_MINUTES,
            )
        ) * 60
        self._heat_target = _half(cfg.get(CONF_HEAT_TARGET, self._heat_target))
        self._cool_target = _half(cfg.get(CONF_COOL_TARGET, self._cool_target))
        self._hysteresis = float(cfg.get(CONF_HYSTERESIS, DEFAULT_HYSTERESIS))
        self._start_hysteresis = float(cfg.get(CONF_START_HYSTERESIS, DEFAULT_START_HYSTERESIS))
        self._min_run = float(cfg.get(CONF_MIN_RUN_MINUTES, DEFAULT_MIN_RUN_MINUTES)) * 60
        self._min_off = float(cfg.get(CONF_MIN_OFF_MINUTES, DEFAULT_MIN_OFF_MINUTES)) * 60
        self._stable_seconds = float(
            cfg.get(CONF_SENSOR_STABLE_SECONDS, DEFAULT_SENSOR_STABLE_SECONDS)
        )
        self._sensor_loss_grace = float(
            cfg.get(
                CONF_SENSOR_LOSS_GRACE_SECONDS,
                DEFAULT_SENSOR_LOSS_GRACE_SECONDS,
            )
        )
        self._override_seconds = float(
            cfg.get(CONF_MANUAL_OVERRIDE_MINUTES, DEFAULT_MANUAL_OVERRIDE_MINUTES)
        ) * 60
        self._compensate = bool(cfg.get(CONF_SETPOINT_COMPENSATION, True))
        self._opening_reduce_to = cfg.get(CONF_OPENING_REDUCE_TO, DEFAULT_OPENING_REDUCE_TO)
        self._regulation = cfg.get(CONF_REGULATION, DEFAULT_REGULATION)

    @property
    def hvac_mode(self):
        return self._hvac_mode

    @property
    def hvac_modes(self):
        """Offer dry/fan-only only when the physical unit supports them."""
        modes = list(self._attr_hvac_modes)
        pump_modes = self._pump_attr("hvac_modes")
        if isinstance(pump_modes, list):
            modes = [m for m in modes if m not in _DIRECT_MODES or m.value in pump_modes]
        return modes

    @property
    def supported_features(self):
        # Show a single setpoint, and the heat/cool range only in heat_cool.
        features = self._base_features | (
            ClimateEntityFeature.TARGET_TEMPERATURE_RANGE
            if self._hvac_mode == HVACMode.HEAT_COOL
            else ClimateEntityFeature.TARGET_TEMPERATURE
        )
        if self._pump_attr("fan_modes"):
            features |= ClimateEntityFeature.FAN_MODE
        if self._pump_attr("swing_modes"):
            features |= ClimateEntityFeature.SWING_MODE
        if self._pump_attr("swing_horizontal_modes") and _SWING_HORIZONTAL is not None:
            features |= _SWING_HORIZONTAL
        return features

    def _pump_attr(self, key: str):
        pump = self.hass.states.get(self._heat_pump) if self.hass else None
        return pump.attributes.get(key) if pump is not None else None

    @property
    def fan_modes(self):
        return self._pump_attr("fan_modes")

    @property
    def fan_mode(self):
        return self._pump_attr("fan_mode")

    @property
    def swing_modes(self):
        return self._pump_attr("swing_modes")

    @property
    def swing_mode(self):
        return self._pump_attr("swing_mode")

    @property
    def swing_horizontal_modes(self):
        return self._pump_attr("swing_horizontal_modes")

    @property
    def swing_horizontal_mode(self):
        return self._pump_attr("swing_horizontal_mode")

    async def _pump_call(self, service: str, data: dict[str, Any]) -> None:
        """Send one command to the physical unit, spaced from the previous one."""
        async with self._pump_lock:
            wait = self._last_pump_command + _PUMP_COMMAND_GAP_SECONDS - time()
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                await self.hass.services.async_call(
                    "climate",
                    service,
                    {"entity_id": self._heat_pump, **data},
                    blocking=True,
                    context=self._command_context,
                )
            finally:
                self._last_pump_command = time()

    async def _forward(self, service: str, data: dict[str, Any]) -> None:
        """Pass a setting straight through to the physical unit."""
        self._command_guard_until = time() + _COMMAND_ECHO_SECONDS
        await self._pump_call(service, data)
        self.async_write_ha_state()

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        await self._forward("set_fan_mode", {"fan_mode": fan_mode})

    async def async_set_swing_mode(self, swing_mode: str) -> None:
        await self._forward("set_swing_mode", {"swing_mode": swing_mode})

    async def async_set_swing_horizontal_mode(self, swing_horizontal_mode: str) -> None:
        await self._forward(
            "set_swing_horizontal_mode", {"swing_horizontal_mode": swing_horizontal_mode}
        )

    @property
    def hvac_action(self):
        if self._active_mode == HVACMode.DRY:
            return HVACAction.DRYING
        if self._active_mode == HVACMode.FAN_ONLY:
            return HVACAction.FAN
        if self._active_mode in (HVACMode.HEAT, HVACMode.COOL) and self._unit_regulates():
            # The unit holds the target itself: show whether it is working right now.
            if not self.unit.working(self._active_mode.value):
                return HVACAction.IDLE
        if self._active_mode == HVACMode.HEAT:
            return HVACAction.HEATING
        if self._active_mode == HVACMode.COOL:
            return HVACAction.COOLING
        return HVACAction.OFF if self._hvac_mode == HVACMode.OFF else HVACAction.IDLE

    @property
    def preset_mode(self):
        return self._preset_mode

    @property
    def current_temperature(self):
        return self.unit.room_temperature().value

    @property
    def current_humidity(self):
        value = self.unit.room_humidity().value
        return round(value) if value is not None else None

    @property
    def target_temperature(self):
        if self._hvac_mode == HVACMode.HEAT_COOL:
            return None
        if self._hvac_mode == HVACMode.COOL:
            return self._cool_target
        return self._heat_target

    @property
    def target_temperature_low(self):
        return self._heat_target if self._hvac_mode == HVACMode.HEAT_COOL else None

    @property
    def target_temperature_high(self):
        return self._cool_target if self._hvac_mode == HVACMode.HEAT_COOL else None

    @property
    def extra_state_attributes(self):
        remaining = max(0, int((self._manual_until - time()) / 60))
        next_event, next_at = self.next_change()
        return {
            "status": self.status(),
            "next_event": next_event,
            "next_change_timestamp": next_at,
            "heat_pump": self._heat_pump,
            "temperature_sensors": self._sensors,
            "away": self.is_away(),
            "effective_heat_target": self._eff_heat(),
            "follow_me_sent": self._follow_me_sent,
            "follow_me_step": self._follow_me_step,
            "follow_me_offset_heat": self._fm_offset["heat"],
            "follow_me_offset_cool": self._fm_offset["cool"],
            "follow_me_offset_min_heat": self._fm_min["heat"],
            "follow_me_offset_min_cool": self._fm_min["cool"],
            "follow_me_start_offset_heat": self._fm_start["heat"],
            "follow_me_start_offset_cool": self._fm_start["cool"],
            "follow_me_boost": self._fm_boost,
            "follow_me_verified": self._fm_verified,
            "follow_me_learning": self.follow_me_learning(),
            "follow_me_history": self._fm_history,
            "follow_me_park_heat": self._fm_park["heat"],
            "follow_me_park_cool": self._fm_park["cool"],
            "follow_me_parked": self._fm_parked,
            "effective_cool_target": self._eff_cool(),
            "price_offset": self._price_offset(self._active_mode == HVACMode.COOL),
            "active_mode": self._active_mode,
            "neutral_mode": self._neutral_mode,
            "pending_mode": self._pending_mode,
            "start_wait_minutes": self._start_wait_minutes(),
            "manual_override_remaining_minutes": remaining,
            "mode_command_count": self._command_count_mode,
            "temperature_command_count": self._command_count_temperature,
            "last_command_reason": self._last_command_reason,
            "thermostats_to_disable": self._thermostats,
            "paused_thermostats": list(self._disabled_thermostats),
            "opening_sensors": self._openings,
            "opening_stage": self._opening_stage,
            "opening_pause_active": self._opening_stage != OPENING_STAGE_NONE,
            "opening_pause_reason": self._opening_pause_reason(),
            "opening_open_minutes": round(self._opening_open_total() / 60, 1),
            "opening_min_delay_minutes": self._opening_min_delay / 60,
            "opening_off_delay_minutes": self._opening_off_delay / 60,
            "opening_resume_delay_minutes": self._opening_resume_delay / 60,
            "min_heat_target_accepted": self._min_heat_accepted,
            "max_cool_target_accepted": self._max_cool_accepted,
            "disabled_thermostat_snapshot": self._disabled_thermostats,
            "last_transition_timestamp": self._last_transition,
            "manual_override_until_timestamp": self._manual_until,
            "sensor_stable_since_timestamp": self._sensor_stable_since,
            "sensor_unavailable_since_timestamp": self._sensor_unavailable_since,
            "opening_episode_start_timestamp": self._opening_episode_start,
            "opening_open_accum_seconds": self._opening_open_accum,
            "opening_open_since_timestamp": self._opening_open_since,
            "opening_closed_since_timestamp": self._opening_closed_since,
            "sensor_available": self.current_temperature is not None,
            "sensor_loss_grace_seconds": self._sensor_loss_grace,
            "heat_target_saved": self._heat_target,
            "cool_target_saved": self._cool_target,
            "command_count_mode_saved": self._command_count_mode,
            "command_count_temperature_saved": self._command_count_temperature,
        }

    @callback
    def async_write_ha_state(self) -> None:
        """Write the state and let the status/countdown/release entities follow."""
        super().async_write_ha_state()
        if self.hass is not None:
            async_dispatcher_send(self.hass, SIGNAL_UPDATE.format(self.entry.entry_id))

    def status(self) -> str:
        """One word for what the thermostat is doing right now."""
        if self._hvac_mode == HVACMode.OFF:
            return "off"
        if self._manual_until > time():
            return "manual"
        reason = self._opening_pause_reason()
        if reason == "off":
            return "opening_off"
        if reason == "reduced":
            return "opening_reduced"
        if reason == "waiting_to_resume":
            return "opening_resume_wait"
        if self._sensor_unavailable_since is not None:
            return "sensor_lost"
        if self._start_wait_minutes() > 0:
            return "start_wait"
        if reason == "waiting_to_reduce":
            return "opening_reduce_wait"
        return {
            HVACMode.HEAT: "heating",
            HVACMode.COOL: "cooling",
            HVACMode.DRY: "drying",
            HVACMode.FAN_ONLY: "fan_only",
        }.get(self._active_mode, "idle")

    def countdowns(self) -> dict[str, float | None]:
        """When each running timer ends (epoch seconds), or None when it is not running.

        The values are derived from the stored start times, so they stay
        constant while a timer runs instead of being recomputed from now.
        """
        now = time()
        out: dict[str, float | None] = dict.fromkeys(COUNTDOWNS)

        def future(value: float) -> float | None:
            return value if value > now else None

        if self._manual_until > now:
            out[COUNTDOWN_MANUAL_OVERRIDE] = self._manual_until
        if self._hvac_mode != HVACMode.OFF:
            if self._opening_open_since is not None:
                # Open time only accrues while an opening is open.
                accum = self._opening_open_accum
                if self._opening_stage == OPENING_STAGE_NONE:
                    out[COUNTDOWN_OPENING_REDUCE] = future(
                        self._opening_open_since + self._opening_min_delay - accum
                    )
                if self._opening_stage != OPENING_STAGE_OFF:
                    out[COUNTDOWN_OPENING_OFF] = future(
                        self._opening_open_since + self._opening_off_delay - accum
                    )
            elif self._opening_closed_since is not None and self._opening_stage != OPENING_STAGE_NONE:
                out[COUNTDOWN_OPENING_RESUME] = future(
                    self._opening_closed_since + self._opening_resume_delay
                )
            if self._pending_mode is not None and self._active_mode == HVACMode.OFF:
                out[COUNTDOWN_START_ALLOWED] = future(self._last_transition + self._min_off)
            if self._active_mode in (HVACMode.HEAT, HVACMode.COOL):
                out[COUNTDOWN_MIN_RUN] = future(self._last_transition + self._min_run)
            if self._sensor_unavailable_since is not None:
                out[COUNTDOWN_SENSOR_LOSS] = future(
                    self._sensor_unavailable_since + self._sensor_loss_grace
                )
        return out

    def next_change(self) -> tuple[str | None, float | None]:
        """The first timer to run out: (countdown key, epoch seconds)."""
        running = [(at, key) for key, at in self.countdowns().items() if at is not None]
        if not running:
            return None, None
        at, key = min(running)
        return key, at

    async def async_release(self) -> None:
        """Release every hold now: manual override, door/window pause and start waits.

        The thermostat goes straight back to automatic control. A door or
        window that is still open starts a fresh episode with fresh timers.
        """
        _LOGGER.info("%s: released by user", self.entry.title)
        self._manual_until = 0
        self._preset_mode = PRESET_AUTO
        if self._hvac_mode != HVACMode.OFF:
            self._reset_opening_episode()
            self._user_start = True
            self._sensor_stable_since = 0.0
        self.async_write_ha_state()
        await self._evaluate("release")

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        old = await self.async_get_last_state()
        if old is None:
            # First start: take over what the unit is doing now instead of
            # switching a running unit off.
            pump = self.hass.states.get(self._heat_pump)
            if pump is not None and pump.state in {m.value for m in HVACMode} and pump.state != HVACMode.HEAT_COOL:
                self._hvac_mode = HVACMode(pump.state)
        if old is not None:
            try:
                self._hvac_mode = HVACMode(old.state)
            except ValueError:
                self._hvac_mode = HVACMode.OFF
            attrs = old.attributes
            self._heat_target = _half(
                attrs.get("heat_target_saved", attrs.get("target_temp_low", self._heat_target))
            )
            self._cool_target = _half(
                attrs.get("cool_target_saved", attrs.get("target_temp_high", self._cool_target))
            )
            for mode in ("heat", "cool"):
                for store, key in ((self._fm_offset, "follow_me_offset"), (self._fm_min, "follow_me_offset_min")):
                    try:
                        store[mode] = max(0, min(_FM_OFFSET_MAX, int(attrs.get(f"{key}_{mode}", 0))))
                    except (TypeError, ValueError):
                        store[mode] = 0
                self._fm_min[mode] = min(self._fm_min[mode], self._fm_offset[mode])
                try:
                    self._fm_start[mode] = max(0, min(_FM_BOOST_MAX, int(attrs.get(f"follow_me_start_offset_{mode}", 0))))
                except (TypeError, ValueError):
                    self._fm_start[mode] = 0
                self._fm_park[mode] = bool(attrs.get(f"follow_me_park_{mode}", False))
                verified = (attrs.get("follow_me_verified") or {}).get(mode) or {}
                self._fm_verified[mode] = {k: bool(verified.get(k, False)) for k in ("stop", "start")}
            history = attrs.get("follow_me_history")
            if isinstance(history, list):
                self._fm_history = [dict(h) for h in history if isinstance(h, dict)][-_FM_HISTORY:]
            try:
                self._active_mode = HVACMode(attrs.get("active_mode", HVACMode.OFF))
            except ValueError:
                self._active_mode = HVACMode.OFF
            neutral_mode = attrs.get("neutral_mode")
            try:
                self._neutral_mode = HVACMode(neutral_mode) if neutral_mode else None
            except ValueError:
                self._neutral_mode = None
            restored_preset = attrs.get("preset_mode", PRESET_AUTO)
            if restored_preset in self._attr_preset_modes:
                self._preset_mode = restored_preset
            pending_mode = attrs.get("pending_mode")
            try:
                self._pending_mode = HVACMode(pending_mode) if pending_mode else None
            except ValueError:
                self._pending_mode = None
            snapshot = attrs.get("disabled_thermostat_snapshot", {})
            if isinstance(snapshot, dict):
                self._disabled_thermostats = snapshot
            self._last_transition = float(
                attrs.get("last_transition_timestamp", self._last_transition)
            )
            self._manual_until = float(
                attrs.get("manual_override_until_timestamp", self._manual_until)
            )
            self._sensor_stable_since = float(
                attrs.get("sensor_stable_since_timestamp", self._sensor_stable_since)
            )
            unavailable_since = attrs.get("sensor_unavailable_since_timestamp")
            self._sensor_unavailable_since = (
                float(unavailable_since) if unavailable_since is not None else None
            )
            open_since = attrs.get("opening_open_since_timestamp")
            closed_since = attrs.get("opening_closed_since_timestamp")
            episode_start = attrs.get("opening_episode_start_timestamp")
            self._opening_open_since = float(open_since) if open_since is not None else None
            self._opening_closed_since = (
                float(closed_since) if closed_since is not None else None
            )
            self._opening_episode_start = (
                float(episode_start) if episode_start is not None else None
            )
            self._opening_open_accum = float(attrs.get("opening_open_accum_seconds") or 0.0)
            stage = attrs.get("opening_stage", OPENING_STAGE_NONE)
            if stage in (OPENING_STAGE_NONE, OPENING_STAGE_MIN, OPENING_STAGE_OFF):
                self._opening_stage = stage
            if self._opening_episode_start is None:
                # Upgrading from <= 0.1.x or no episode in progress.
                self._opening_stage = OPENING_STAGE_NONE
                self._opening_open_since = None
                self._opening_closed_since = None
                self._opening_open_accum = 0.0
            for key, attr in (
                ("_min_heat_accepted", "min_heat_target_accepted"),
                ("_max_cool_accepted", "max_cool_target_accepted"),
            ):
                try:
                    setattr(self, key, float(attrs[attr]) if attrs.get(attr) is not None else None)
                except (TypeError, ValueError):
                    setattr(self, key, None)
            self._command_count_mode = int(
                attrs.get("command_count_mode_saved", self._command_count_mode)
            )
            self._command_count_temperature = int(
                attrs.get(
                    "command_count_temperature_saved",
                    self._command_count_temperature,
                )
            )

        watched = list(dict.fromkeys([*self._sensors, self._heat_pump, *self._openings, *([self._presence] if self._presence else [])]))
        self.async_on_remove(async_track_state_change_event(self.hass, watched, self._state_changed))
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._periodic_update, timedelta(seconds=_EVALUATION_SECONDS)
            )
        )
        self._last_sensor_value = self.current_temperature
        self._update_opening()
        pump_state = self.hass.states.get(self._heat_pump)
        if pump_state is not None:
            try:
                physical_mode = HVACMode(pump_state.state)
                if self._neutral_mode == physical_mode:
                    self._active_mode = HVACMode.OFF
                elif physical_mode in (HVACMode.OFF, HVACMode.HEAT, HVACMode.COOL):
                    self._active_mode = physical_mode
            except ValueError:
                pass
            try:
                self._observed_pump_target = float(pump_state.attributes.get("temperature"))
            except (TypeError, ValueError):
                self._observed_pump_target = None
        self._started = True
        self.async_write_ha_state()
        # Deliberately do not evaluate or command hardware during entity startup.

    async def _entry_updated(self, hass, entry) -> None:
        self._load_config()
        self.async_write_ha_state()

    @callback
    def _state_changed(self, event: Event) -> None:
        entity_id = event.data.get("entity_id")
        if entity_id in self._sensors:
            value = self.current_temperature
            if value != self._last_sensor_value:
                self._last_sensor_value = value
                self._sensor_stable_since = time()
        if entity_id == self._heat_pump:
            new_state = event.data.get("new_state")
            self._detect_manual_change(new_state)
            self.async_write_ha_state()  # fan/swing are mirrored from the unit
            self.hass.async_create_task(self._sync_manual_thermostats(new_state))
        if entity_id in self._openings:
            self._update_opening()
        self.hass.async_create_task(self._evaluate("state_change"))

    @callback
    def _periodic_update(self, _now) -> None:
        self._update_opening()
        self.async_write_ha_state()
        self.hass.async_create_task(self._evaluate("periodic"))

    def _detect_manual_change(self, new_state: State | None) -> None:
        if new_state is None:
            return
        try:
            new_target = float(new_state.attributes.get("temperature"))
        except (TypeError, ValueError):
            new_target = None
        target_changed = (
            new_target is not None
            and self._observed_pump_target is not None
            and abs(new_target - self._observed_pump_target) >= 1
        )
        self._observed_pump_target = new_target
        # Physical climate entities commonly pass through OFF while their
        # integration is still restoring after a Home Assistant restart. Do
        # not interpret that transient startup state as a handheld-remote
        # command.
        if time() - self._startup_time < _OPENING_STARTUP_GRACE_SECONDS:
            return
        if time() < self._power_guard_until:
            # The supply was cut by us; the mode is repaired when it is back.
            return
        context = new_state.context
        user_command = (
            context is not None
            and context.user_id is not None
            and context.id != self._command_context.id
        )
        if time() <= self._command_guard_until and not user_command:
            # Changes right after our own commands are usually their echo. A
            # change a person made from Home Assistant carries their user id,
            # so it is always taken as a manual change.
            return
        if new_state.state not in {mode.value for mode in HVACMode}:
            return
        expected_mode = self._neutral_mode or self._active_mode
        manual_mode = HVACMode(new_state.state)
        if self._opening_stage == OPENING_STAGE_OFF and manual_mode == HVACMode.OFF:
            # We switched the unit off because a door/window stayed open. A
            # late OFF echo must not be mistaken for a remote-control stop.
            return
        if manual_mode == HVACMode.OFF and expected_mode != HVACMode.OFF:
            # A physical OFF (for example from the handheld remote) is an
            # explicit stop request, not a temporary setpoint override. Mirror
            # it to the virtual thermostat so restore-state keeps the unit off
            # until the user explicitly turns it on again.
            self._hvac_mode = HVACMode.OFF
            self._active_mode = HVACMode.OFF
            self._neutral_mode = None
            self._manual_until = 0
            self._preset_mode = PRESET_AUTO
            self._pending_mode = None
            _LOGGER.info("%s: external heat-pump off mirrored to thermostat", self.entry.title)
            return
        if (
            new_state.state != expected_mode.value or target_changed
        ) and self._override_seconds > 0:
            if manual_mode in _DIRECT_MODES:
                # Dry/fan-only chosen on the unit becomes the thermostat mode.
                self._hvac_mode = manual_mode
                self._active_mode = manual_mode
                self._neutral_mode = None
                self._manual_until = 0
                self._preset_mode = PRESET_AUTO
                self._pending_mode = None
                return
            if manual_mode in (HVACMode.HEAT, HVACMode.COOL):
                if self._hvac_mode == HVACMode.OFF or self._hvac_mode in _DIRECT_MODES:
                    self._hvac_mode = manual_mode
                self._active_mode = manual_mode
            self._neutral_mode = None
            self._manual_until = time() + self._override_seconds
            self._preset_mode = PRESET_MANUAL
            self._pending_mode = None
            _LOGGER.info("%s: external heat-pump change started manual override", self.entry.title)

    def is_away(self) -> bool:
        """Nobody home according to the linked presence entity."""
        state = self.hass.states.get(self._presence) if self._presence else None
        return state is not None and state.state in AWAY_STATES

    def _price_offset(self, cool: bool) -> int:
        price = self.hass.data.get(DOMAIN, {}).get(f"{self.entry.entry_id}_price")
        return price.offset(cool) if price is not None else 0

    def _eff_heat(self) -> float:
        return self._heat_target - (self._away_setback if self.is_away() else 0) + self._price_offset(False)

    def _eff_cool(self) -> float:
        return self._cool_target + (self._away_setback if self.is_away() else 0) + self._price_offset(True)

    def effective_target(self, mode: str) -> float:
        """The target the thermostat regulates to right now (away and price included)."""
        return self._eff_cool() if mode == HVACMode.COOL.value else self._eff_heat()

    async def async_price_changed(self) -> None:
        """The price level moved the effective target: regulate to the new one now."""
        self._last_retarget = 0.0
        self.async_write_ha_state()
        await self._evaluate("price")

    def suppress_manual_detection(self, seconds: float) -> None:
        """Our own power-cycle: the unit may come back in another mode – that is not the remote."""
        self._power_guard_until = time() + seconds

    def apply_live_settings(self) -> None:
        """Re-read settings changed from the switch/number entities without a reload."""
        self._load_config()
        if not self._openings_enabled:
            self._reset_opening_episode()
        self.async_write_ha_state()
        self.hass.async_create_task(self._evaluate("settings"))

    def _opening_is_open(self) -> bool:
        if not self._openings_enabled:
            return False
        return any(
            (state := self.hass.states.get(entity_id)) is not None and state.state == STATE_ON
            for entity_id in self._openings
        )

    def _start_wait_minutes(self) -> float:
        """Minutes left of the minimum off time while a start is pending."""
        if self._pending_mode is None or self._active_mode != HVACMode.OFF:
            return 0
        return round(max(0.0, self._min_off - (time() - self._last_transition)) / 60, 1)

    def _opening_open_total(self) -> float:
        """Seconds an opening has been open during the current episode."""
        total = self._opening_open_accum
        if self._opening_open_since is not None:
            total += time() - self._opening_open_since
        return total

    def _update_opening(self) -> str:
        """Advance the door/window state machine and return the current stage.

        An episode starts when any configured opening opens. Open time is
        accumulated within the episode: after the "minimum" delay the heat pump
        is turned down to its lowest accepted target, after the "off" delay it
        is switched off completely. The stage never steps back while the
        episode lasts; the episode only ends once every opening has stayed
        closed for the resume delay.
        """
        now = time()
        if self._opening_is_open():
            if self._opening_episode_start is None:
                self._opening_episode_start = now
                self._opening_open_accum = 0.0
                self._opening_stage = OPENING_STAGE_NONE
            if self._opening_open_since is None:
                self._opening_open_since = now
            self._opening_closed_since = None
        else:
            if self._opening_open_since is not None:
                self._opening_open_accum += now - self._opening_open_since
                self._opening_open_since = None
            if self._opening_episode_start is not None:
                if self._opening_closed_since is None:
                    self._opening_closed_since = now
                elif now - self._opening_closed_since >= self._opening_resume_delay:
                    if self._opening_stage != OPENING_STAGE_NONE:
                        _LOGGER.info(
                            "%s: openings closed for %.0f min, resuming",
                            self.entry.title,
                            self._opening_resume_delay / 60,
                        )
                    self._opening_episode_start = None
                    self._opening_closed_since = None
                    self._opening_open_accum = 0.0
                    self._opening_stage = OPENING_STAGE_NONE
        if self._opening_episode_start is not None:
            open_total = self._opening_open_total()
            if open_total >= self._opening_off_delay:
                if self._opening_stage != OPENING_STAGE_OFF:
                    _LOGGER.info("%s: opening open %.0f min, switching off", self.entry.title, open_total / 60)
                self._opening_stage = OPENING_STAGE_OFF
            elif open_total >= self._opening_min_delay and self._opening_stage == OPENING_STAGE_NONE:
                _LOGGER.info("%s: opening open %.0f min, turning down", self.entry.title, open_total / 60)
                self._opening_stage = OPENING_STAGE_MIN
        return self._opening_stage

    def _reset_opening_episode(self) -> None:
        """Forget the current door/window pause after an explicit user command.

        The user is there and asked for heat/cool now. If an opening is still
        open, a fresh episode (with fresh timers) starts from this moment.
        """
        if self._opening_episode_start is not None:
            _LOGGER.info("%s: user command clears the opening pause", self.entry.title)
        self._opening_episode_start = None
        self._opening_open_accum = 0.0
        self._opening_open_since = None
        self._opening_closed_since = None
        self._opening_stage = OPENING_STAGE_NONE
        self._update_opening()

    def _opening_pause_reason(self) -> str:
        is_open = self._opening_is_open()
        if self._opening_stage == OPENING_STAGE_OFF:
            return "off" if is_open else "waiting_to_resume"
        if self._opening_stage == OPENING_STAGE_MIN:
            return "reduced" if is_open else "waiting_to_resume"
        if is_open:
            return "waiting_to_reduce"
        return "none"

    async def _sync_manual_thermostats(self, pump_state: State | None) -> None:
        """Apply radiator coordination even when the physical remote was used."""
        if pump_state is None:
            return
        if self._neutral_mode is not None and pump_state.state == self._neutral_mode.value:
            await self._restore_thermostats()
        elif pump_state.state in (HVACMode.HEAT, HVACMode.COOL):
            await self._disable_thermostats()
        elif pump_state.state == HVACMode.OFF:
            await self._restore_thermostats()

    def follow_me_value(self, room: float) -> float:
        """The temperature to send as Follow Me: steered while the unit regulates."""
        now = time()
        step, value = "room", room
        mode = self._active_mode
        pump = self.hass.states.get(self._heat_pump) if self._heat_pump else None
        if (
            self._unit_regulates()
            and mode in (HVACMode.HEAT, HVACMode.COOL)
            and self._opening_stage == OPENING_STAGE_NONE
            and self._manual_until <= now  # a remote-control target is held on the true room
            and pump is not None
            and pump.state == mode.value
        ):
            if mode != self._fm_mode:
                # Heat and cool (heat_cool) each start from scratch.
                self._fm_mode, self._follow_me_step = mode, "room"
            target = self._eff_heat() if mode == HVACMode.HEAT else self._eff_cool()
            unit_target = self._setpoint_target(mode, pump)
            sign = 1 if mode == HVACMode.HEAT else -1
            short = round((target - room) * sign, 2)  # > 0: the room still needs heat (cool)
            self._fm_short = short
            step = self._next_fm_step(short, now)
            heat = mode == HVACMode.HEAT
            if step == "start":
                # Never closer to the target than the room is: far off, the unit must
                # see the whole shortfall or it idles along.
                base = min(math.floor(room), unit_target - 1) if heat else max(math.ceil(room), unit_target + 1)
            elif step == "stop":
                base = max(math.ceil(room), unit_target + 1) if heat else min(math.floor(room), unit_target - 1)
            else:
                base = unit_target
            if step != self._follow_me_step:
                self._fm_boost = 0
            shift = self._fm_offset[mode.value]
            if step == "start":
                shift -= self._fm_start[mode.value] + self._fm_boost
            value = float(base + sign * shift)
        if step != self._follow_me_step:
            if step == "start" and self._follow_me_step in ("stop", "room"):
                self._fm_run_since = now
            elif step == "hold" and self._follow_me_step == "room":
                self._fm_run_since = now
            self._fm_step_since = now
            self._fm_mismatch = None
            self._fm_hold_worked = False
            self._fm_boost = 0
        if (step, value) != (self._follow_me_step, self._follow_me_sent):
            self._follow_me_step, self._follow_me_sent = step, value
            if self.entity_id:
                self.async_write_ha_state()
        return value

    def _next_fm_step(self, short: float, now: float) -> str:
        """start / hold / stop from how far the room is from the target (0.1 K)."""
        last = self._follow_me_step
        if last == "start":
            return "start" if short > 0 else "hold"
        if last == "stop":
            rested = now - self._fm_step_since >= self._min_off
            if short >= _FM_START and (rested or short >= _FM_FORCE):
                return "start"
            return "stop"
        if short >= _FM_START:
            return "start"
        if short <= -_FM_STOP:
            ran = self._fm_run_since is None or now - self._fm_run_since >= self._min_run
            if ran or short <= -_FM_FORCE:
                return "stop"
        return "hold"

    def _fm_outcome(self, cond: str | None, key: str) -> tuple[str, int | None] | None:
        """What a check that runs out changes: (what, new value), None when nothing can."""
        offset = self._fm_offset[key]
        if cond == "stop":
            if offset < _FM_OFFSET_MAX:
                return "offset_up", offset + 1
            return None if self._fm_park[key] else ("park", None)
        if cond == "hold":
            return ("offset_down", offset - 1) if offset > 0 else None
        if cond in ("start", "rise") and offset > self._fm_min[key]:
            return "offset_down", offset - 1
        if cond == "start" and self._fm_start[key] < _FM_BOOST_MAX:
            return "start_earlier", self._fm_start[key] + 1
        if cond == "rise" and self._fm_boost < _FM_BOOST_MAX:
            return "boost", self._fm_boost + 1
        return None

    def _check_follow_me(self, now: float) -> None:
        """Learn the unit's own Follow Me offset from whether it stops, holds and starts."""
        mode = self._active_mode
        step = self._follow_me_step
        room = self.current_temperature
        if (
            mode not in (HVACMode.HEAT, HVACMode.COOL)
            or step not in ("start", "hold", "stop")
            or room is None
            or self._fm_paused()
        ):
            self._fm_mismatch = None
            return
        key = mode.value
        sign = 1 if mode == HVACMode.HEAT else -1
        working = self.unit.working(key)
        verified = self._fm_verified[key]
        if step == "stop" and not working and not verified["stop"]:
            verified["stop"] = True
            self.async_write_ha_state()
        elif step == "start" and working and not verified["start"]:
            verified["start"] = True
            self.async_write_ha_state()
        cond = None
        if step == "stop":
            if working and not self._fm_parked:
                cond = "stop"
        elif step == "hold":
            if working:
                self._fm_hold_worked = True
            elif self._fm_hold_worked:
                cond = "hold"
        elif not working:
            cond = "start" if self._fm_short >= _FM_START else None
        elif self._fm_short >= _FM_START_SHORT:
            cond = "rise"
        if cond != self._fm_mismatch:
            self._fm_mismatch, self._fm_mismatch_since, self._fm_mismatch_room = cond, now, room
            self.async_write_ha_state()
            return
        if cond is None:
            return
        if cond == "rise" and (room - self._fm_mismatch_room) * sign >= _FM_PROGRESS:
            # The room comes up: the unit does its job.
            self._fm_mismatch_since, self._fm_mismatch_room = now, room
            return
        if now - self._fm_mismatch_since < _FM_CHECKS[cond]:
            return
        self._fm_mismatch_since, self._fm_mismatch_room = now, room
        outcome = self._fm_outcome(cond, key)
        if cond == "hold":
            self._fm_hold_worked = False  # once per stop in hold
        if outcome is None:
            return
        what, value = outcome
        if what == "offset_up":
            self._fm_offset[key] = self._fm_min[key] = value
            verified["stop"] = verified["start"] = False
        elif what == "park":
            self._fm_park[key] = True
            self._fm_min[key] = self._fm_offset[key]
            verified["stop"] = False
        elif what == "offset_down":
            self._fm_offset[key] = value
            self._fm_park[key] = False
            if cond == "hold":
                self._fm_min[key] = min(self._fm_min[key], value)
                self._fm_start[key] = max(0, self._fm_start[key] - 1)  # it may have made up for it
            verified["stop"] = verified["start"] = False
        elif what == "start_earlier":
            self._fm_start[key] = value
            verified["start"] = False
        elif what == "boost":
            self._fm_boost = value
        self._fm_learned(key, cond, what, value)
        self.async_write_ha_state()  # the feeder sends the new value

    def _fm_learned(self, key: str, cond: str, what: str, value: int | None) -> None:
        if what != "boost":
            self._fm_history = [*self._fm_history, {
                "at": datetime.fromtimestamp(time(), timezone.utc).isoformat(timespec="seconds"),
                "mode": key, "check": cond, "change": what, "value": value,
            }][-_FM_HISTORY:]
        log = _LOGGER.info if what == "boost" else _LOGGER.warning
        log(
            "%s: Follow Me %s – %s %s (%s)", self.entry.title, key,
            {"stop": "the unit kept working after stop", "hold": "the unit stopped at its target",
             "start": "the unit did not start", "rise": "the room did not come up"}[cond],
            what, "" if value is None else value,
        )

    def _fm_paused(self) -> bool:
        """Learning waits: no compressor/power reading, the unit offline or defrosting."""
        stats = self.hass.data.get(DOMAIN, {}).get(f"{self.entry.entry_id}_stats")
        return (
            self.unit.online() is False
            or not self.unit.work_known()
            or (stats is not None and getattr(stats, "defrosting", False))
        )

    def follow_me_learning(self) -> dict[str, Any]:
        """What the Follow Me learning does right now, how far a check is and what it learned."""
        mode = self._active_mode if self._active_mode in (HVACMode.HEAT, HVACMode.COOL) else self._fm_mode
        key = mode.value if mode in (HVACMode.HEAT, HVACMode.COOL) else None
        info: dict[str, Any] = {"state": "off", "mode": key}
        if key is not None:
            info.update(
                offset=self._fm_offset[key], start_offset=self._fm_start[key], park=self._fm_park[key],
                stop_ok=self._fm_verified[key]["stop"], start_ok=self._fm_verified[key]["start"],
            )
        if self._regulation != REGULATION_UNIT or self._hvac_mode == HVACMode.OFF or key is None:
            return info
        if not self.unit.work_known():
            info["state"] = "no_meter"
            return info
        if not self._unit_regulates() or self._follow_me_step == "room" or self._fm_paused():
            info["state"] = "paused"
            return info
        cond = self._fm_mismatch
        outcome = self._fm_outcome(cond, key) if cond else None
        if outcome is not None:
            elapsed = max(0.0, time() - self._fm_mismatch_since)
            needed = _FM_CHECKS[cond]
            info.update(
                state=f"checking_{cond}", check=cond, outcome=outcome[0], outcome_value=outcome[1],
                elapsed_min=round(elapsed / 60, 1), needed_min=needed // 60,
                remaining_min=max(0, math.ceil((needed - elapsed) / 60)),
                progress=min(99, int(elapsed * 100 / needed)),
            )
            return info
        info["state"] = "learned" if info["stop_ok"] and info["start_ok"] else "learning"
        return info

    def stop_requested(self) -> bool:
        """The unit has been told to stop heating/cooling (Follow Me stop, neutral idle or off)."""
        if self._hvac_mode == HVACMode.OFF or self._opening_stage != OPENING_STAGE_NONE:
            return True
        if self._unit_regulates():
            return self._follow_me_step == "stop"
        return self._active_mode == HVACMode.OFF

    def _unit_regulates(self) -> bool:
        """The unit holds the room target itself, on the room temperature sent as Follow Me.

        Only while Follow Me is on and a room temperature exists – otherwise the
        unit would regulate on its own (warmer) sensor, and the thermostat with
        hysteresis and neutral idle takes over.
        """
        if self._regulation != REGULATION_UNIT or self.current_temperature is None:
            return False
        switch = self.unit.entity("follow_me")
        state = self.hass.states.get(switch) if switch else None
        return state is not None and state.state == STATE_ON

    def _desired_mode(self, temperature: float) -> HVACMode:
        if self._hvac_mode == HVACMode.OFF or self._opening_stage != OPENING_STAGE_NONE:
            return HVACMode.OFF
        if self._unit_regulates():
            # No on/off hysteresis: the unit runs in the chosen mode with the room
            # target and modulates itself. In heat_cool it changes between heat
            # and cool only when the room is clearly past the other target.
            if self._hvac_mode in (HVACMode.HEAT, HVACMode.COOL):
                return self._hvac_mode
            heat_target, cool_target = self._eff_heat(), self._eff_cool()
            if temperature >= cool_target + self._start_hysteresis:
                return HVACMode.COOL
            if temperature <= heat_target - self._start_hysteresis:
                return HVACMode.HEAT
            if self._active_mode in (HVACMode.HEAT, HVACMode.COOL):
                return self._active_mode
            return HVACMode.HEAT if temperature < (heat_target + cool_target) / 2 else HVACMode.COOL
        # Start early (small margin), stop late (normal hysteresis).
        heat_target, cool_target = self._eff_heat(), self._eff_cool()
        heat_on = temperature <= heat_target - self._start_hysteresis
        heat_off = temperature >= heat_target + self._hysteresis
        cool_on = temperature >= cool_target + self._start_hysteresis
        cool_off = temperature <= cool_target - self._hysteresis

        if self._active_mode == HVACMode.HEAT and not heat_off:
            return HVACMode.HEAT
        if self._active_mode == HVACMode.COOL and not cool_off:
            return HVACMode.COOL
        if self._hvac_mode in (HVACMode.HEAT, HVACMode.HEAT_COOL) and heat_on:
            return HVACMode.HEAT
        if self._hvac_mode in (HVACMode.COOL, HVACMode.HEAT_COOL) and cool_on:
            return HVACMode.COOL
        return HVACMode.OFF

    async def _evaluate(self, reason: str) -> None:
        if not self._started or self._eval_lock.locked():
            # A running evaluation (possibly waiting for the heat pump to
            # confirm a target) will be followed by the next periodic one.
            return
        async with self._eval_lock:
            await self._evaluate_locked(reason)

    async def _evaluate_locked(self, reason: str) -> None:
        now = time()
        stage = self._update_opening()
        opening_pause = stage != OPENING_STAGE_NONE
        if opening_pause and self._manual_until > now:
            # Openings are a safety/energy override. Resume automatic control,
            # rather than restarting an unknown remote-control mode later.
            self._manual_until = 0
            self._preset_mode = PRESET_AUTO
        if self._manual_until > now:
            self.async_write_ha_state()
            return
        if stage == OPENING_STAGE_OFF:
            await self._apply_opening_off()
            return
        if self._hvac_mode in _DIRECT_MODES:
            # Dry and fan-only are handed to the unit as they are; only an
            # opening that stays open long switches them off (above).
            pump = self.hass.states.get(self._heat_pump)
            if pump is not None and pump.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                if pump.state != self._hvac_mode.value or self._active_mode != self._hvac_mode:
                    await self._apply_mode(self._hvac_mode, reason)
                    return
            await self._restore_thermostats()
            self.async_write_ha_state()
            return
        if self._preset_mode == PRESET_MANUAL:
            self._preset_mode = PRESET_AUTO
        temperature = self.current_temperature
        if temperature is None:
            if self._sensor_unavailable_since is None:
                self._sensor_unavailable_since = now
                self.async_write_ha_state()
                return
            if now - self._sensor_unavailable_since < self._sensor_loss_grace:
                self.async_write_ha_state()
                return
            pump = self.hass.states.get(self._heat_pump)
            pump_running = pump is not None and pump.state in (
                HVACMode.HEAT.value,
                HVACMode.COOL.value,
            )
            if self._hvac_mode == HVACMode.OFF:
                await self._apply_mode(HVACMode.OFF, "temperature_sensor_unavailable")
            elif pump_running or self._active_mode != HVACMode.OFF:
                await self._apply_neutral("temperature_sensor_unavailable")
            else:
                await self._restore_thermostats()
                self.async_write_ha_state()
            return
        if self._sensor_unavailable_since is not None:
            self._sensor_unavailable_since = None
            self._sensor_stable_since = now
        desired = self._desired_mode(temperature)
        if desired == HVACMode.OFF:
            # A user start only bypasses the start guards for the start itself.
            self._user_start = False
        if desired == self._active_mode:
            if desired == HVACMode.OFF:
                if self._hvac_mode == HVACMode.OFF and self._neutral_mode is not None:
                    await self._apply_mode(HVACMode.OFF, reason)
                    return
                pump = self.hass.states.get(self._heat_pump)
                pump_off = pump is not None and pump.state == HVACMode.OFF.value
                if opening_pause and pump_off:
                    # Never switch a stopped unit on just because a door opened.
                    self._neutral_mode = None
                    await self._restore_thermostats()
                    self.async_write_ha_state()
                    return
                if self._hvac_mode != HVACMode.OFF and self._neutral_mode is None:
                    await self._apply_neutral("opening_reduce" if opening_pause else reason)
                    return
                if (
                    self._hvac_mode != HVACMode.OFF
                    and self._neutral_mode is not None
                    and now > self._command_guard_until
                    and not self._pump_matches_neutral()
                ):
                    # The unit is not where we left it (e.g. it ignored the
                    # requested target, or came back from an opening OFF).
                    await self._apply_neutral("opening_reduce" if opening_pause else "neutral_repair")
                    return
                await self._restore_thermostats()
            else:
                pump = self.hass.states.get(self._heat_pump)
                if (
                    pump is not None
                    and pump.state not in (STATE_UNAVAILABLE, STATE_UNKNOWN)
                    and pump.state != desired.value
                    and now > self._command_guard_until
                ):
                    # We believe the unit runs, but it does not (a command was
                    # lost or ignored). Send the mode again.
                    await self._apply_mode(desired, "mode_repair")
                    return
                await self._disable_thermostats()
                if desired in (HVACMode.HEAT, HVACMode.COOL) and self._unit_regulates():
                    room = self.current_temperature
                    if room is not None:
                        self.follow_me_value(room)  # advance start/hold/stop on time, too
                    self._check_follow_me(now)
                    park = self._follow_me_step == "stop" and self._fm_park[desired.value]
                    if park and not self._fm_parked:
                        # Even the largest offset does not stop this unit: hold it off by
                        # its target until the next start.
                        neutral = self._neutral_target(desired, pump)
                        await self._set_pump_target(neutral, desired, step_towards_comfort=True)
                        self._fm_parked = True
                        self._last_command_reason = "follow_me_park"
                    elif not park and (now > self._command_guard_until or reason in _TARGET_REASONS or self._fm_parked):
                        # Keep the unit's target on the room target (user, price and away
                        # changes). A no-op when it already matches.
                        self._fm_parked = False
                        await self._prepare_setpoint(desired, reason=f"target_{desired.value}")
                    self.async_write_ha_state()
                    return
                if (
                    self._compensate
                    and desired in (HVACMode.HEAT, HVACMode.COOL)
                    and now - self._last_retarget >= _RETARGET_SECONDS
                    and now > self._command_guard_until
                ):
                    # The unit regulates on its own (often much warmer) sensor
                    # and idles once that reaches its target. Keep the target
                    # compensated while running, not only at start.
                    await self._prepare_setpoint(desired, reason=f"retarget_{desired.value}")
            self.async_write_ha_state()
            return
        # A deliberate setpoint reduction must feel immediate. Holding the
        # previous heat/cool demand until minimum runtime expires can keep the
        # room heating after the user has explicitly removed that demand.
        # Minimum *off* time still protects the compressor from restarting
        # again too quickly.
        user_removed_demand = reason in ("target_change", "resume_auto", "release") and desired == HVACMode.OFF
        safety_off = (
            opening_pause
            or self._hvac_mode == HVACMode.OFF
            or user_removed_demand
        )
        elapsed = now - self._last_transition
        if self._active_mode != HVACMode.OFF and not safety_off and elapsed < self._min_run:
            return
        if desired != HVACMode.OFF:
            # When the unit regulates itself it decides when its compressor runs;
            # the start guards only protect the thermostat's own on/off cycling.
            user_start = self._user_start or self._unit_regulates()
            if now - self._sensor_stable_since < self._stable_seconds and not user_start:
                return
            if self._active_mode == HVACMode.OFF and elapsed < self._min_off and not user_start:
                # Automatic restarts respect the minimum off time. A start the
                # user asked for goes ahead; the unit has its own compressor
                # restart protection.
                self._pending_mode = desired
                self.async_write_ha_state()
                return
            self._user_start = False
            if self._active_mode != HVACMode.OFF:
                self._pending_mode = desired
                await self._apply_mode(HVACMode.OFF, "mode_change_pause")
                return
            await self._prepare_setpoint(desired)
        if desired == HVACMode.OFF and self._hvac_mode != HVACMode.OFF:
            await self._apply_neutral("opening_reduce" if opening_pause else reason)
        else:
            await self._apply_mode(desired, "opening_reduce" if opening_pause else reason)

    async def _apply_opening_off(self) -> None:
        """Hold the physical unit fully off while an opening has stayed open long."""
        pump = self.hass.states.get(self._heat_pump)
        if pump is None or pump.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            self.async_write_ha_state()
            return
        if pump.state != HVACMode.OFF.value:
            await self._apply_mode(HVACMode.OFF, "opening_off")
            notifier = self.hass.data.get(DOMAIN, {}).get(f"{self.entry.entry_id}_notifier")
            if notifier is not None:
                notifier.notify_opening_off()
            return
        if self._active_mode != HVACMode.OFF or self._neutral_mode is not None:
            self._active_mode = HVACMode.OFF
            self._neutral_mode = None
            self._last_transition = time()
        await self._restore_thermostats()
        self.async_write_ha_state()

    def _neutral_target(self, mode: HVACMode, pump: State) -> float:
        if mode == HVACMode.HEAT:
            learned = self._min_heat_accepted
            advertised = float(pump.attributes.get("min_temp", 16))
            return max(advertised, learned) if learned is not None else advertised
        learned = self._max_cool_accepted
        advertised = float(pump.attributes.get("max_temp", 32))
        return min(advertised, learned) if learned is not None else advertised

    def _setpoint_target(self, mode: HVACMode, pump: State) -> float:
        """The room setpoint itself (uncompensated), within the unit's range."""
        lo = self._neutral_target(HVACMode.HEAT, pump)
        hi = self._neutral_target(HVACMode.COOL, pump)
        target = self._eff_heat() if mode == HVACMode.HEAT else self._eff_cool()
        return _whole(max(lo, min(hi, target)))

    def _pump_matches_neutral(self) -> bool:
        pump = self.hass.states.get(self._heat_pump)
        if pump is None or self._neutral_mode is None:
            return True
        if pump.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return True
        if pump.state != self._neutral_mode.value:
            return False
        try:
            expected = (
                self._neutral_value
                if self._neutral_value is not None
                else self._neutral_target(self._neutral_mode, pump)
            )
            return abs(float(pump.attributes.get("temperature")) - expected) < 1
        except (TypeError, ValueError):
            return False

    async def _wait_for_pump_mode(self, mode: HVACMode) -> bool:
        for _ in range(_TARGET_CONFIRM_SECONDS * 2):
            pump = self.hass.states.get(self._heat_pump)
            if pump is not None and pump.state == mode.value:
                return True
            await asyncio.sleep(0.5)
        return False

    async def _wait_for_pump_target(self, target: float) -> bool:
        for _ in range(_TARGET_CONFIRM_SECONDS * 2):
            pump = self.hass.states.get(self._heat_pump)
            try:
                if pump is not None and abs(float(pump.attributes.get("temperature")) - target) < 0.5:
                    return True
            except (TypeError, ValueError):
                pass
            await asyncio.sleep(0.5)
        return False

    async def _set_pump_target(self, target: float, mode: HVACMode, step_towards_comfort: bool) -> float | None:
        """Send a target and confirm the unit actually took it.

        Some heat pumps (e.g. Qlima via Tuya) advertise a minimum they silently
        reject in heat mode. When ``step_towards_comfort`` is set, a rejected
        target is retried one degree closer to comfort and the accepted value is
        remembered for the next pause.
        """
        pump = self.hass.states.get(self._heat_pump)
        lo = float(pump.attributes.get("min_temp", 16)) if pump else 16.0
        hi = float(pump.attributes.get("max_temp", 32)) if pump else 32.0
        for attempt in range(_TARGET_CONFIRM_ATTEMPTS if step_towards_comfort else 1):
            target = float(max(lo, min(hi, target)))
            self._command_guard_until = time() + _COMMAND_ECHO_SECONDS
            await self._pump_call("set_temperature", {ATTR_TEMPERATURE: target})
            self._command_count_temperature += 1
            self._observed_pump_target = target
            self._command_guard_until = time() + _COMMAND_ECHO_SECONDS
            if await self._wait_for_pump_target(target):
                if step_towards_comfort:
                    if mode == HVACMode.HEAT:
                        self._min_heat_accepted = target
                    else:
                        self._max_cool_accepted = target
                return target
            _LOGGER.warning(
                "%s: %s did not accept target %.0f (attempt %d)",
                self.entry.title,
                self._heat_pump,
                target,
                attempt + 1,
            )
            if not step_towards_comfort:
                return None
            target = target + 1 if mode == HVACMode.HEAT else target - 1
        return None

    async def _apply_neutral(self, reason: str) -> None:
        """Stop demand without power-cycling the physical heat pump."""
        pump = self.hass.states.get(self._heat_pump)
        if pump is None:
            return
        try:
            physical_mode = HVACMode(pump.state)
        except ValueError:
            physical_mode = HVACMode.OFF
        if self._active_mode in (HVACMode.HEAT, HVACMode.COOL):
            neutral_mode = self._active_mode
        elif physical_mode in (HVACMode.HEAT, HVACMode.COOL):
            neutral_mode = physical_mode
        else:
            neutral_mode = HVACMode.COOL if self._hvac_mode == HVACMode.COOL else HVACMode.HEAT

        if (
            reason == "opening_reduce"
            and self._opening_reduce_to == OPENING_REDUCE_SETPOINT
            and not self._unit_regulates()
        ):
            neutral_target = self._setpoint_target(neutral_mode, pump)
        else:
            neutral_target = self._neutral_target(neutral_mode, pump)
        current_target = pump.attributes.get("temperature")
        try:
            target_matches = abs(float(current_target) - neutral_target) < 1
        except (TypeError, ValueError):
            target_matches = False
        self._command_guard_until = time() + _COMMAND_ECHO_SECONDS
        if physical_mode != neutral_mode:
            await self._pump_call("set_hvac_mode", {"hvac_mode": neutral_mode.value})
            self._command_count_mode += 1
            self._command_guard_until = time() + _COMMAND_ECHO_SECONDS
        if not target_matches:
            accepted = await self._set_pump_target(
                neutral_target, neutral_mode, step_towards_comfort=True
            )
            if accepted is not None:
                neutral_target = accepted
        self._neutral_value = neutral_target
        self._last_command = time()
        self._last_command_reason = reason
        self._neutral_mode = neutral_mode
        self._active_mode = HVACMode.OFF
        self._pending_mode = None
        self._last_transition = time()
        await self._restore_thermostats()
        self.async_write_ha_state()

    async def _prepare_setpoint(self, mode: HVACMode, reason: str | None = None) -> None:
        pump = self.hass.states.get(self._heat_pump)
        if pump is None:
            return
        self._neutral_mode = None
        self._last_retarget = time()
        desired = self._eff_heat() if mode == HVACMode.HEAT else self._eff_cool()
        if self._compensate and not self._unit_regulates():
            internal = pump.attributes.get("current_temperature")
            external = self.current_temperature
            try:
                desired += float(internal) - float(external)
            except (TypeError, ValueError):
                pass
        min_temp = float(pump.attributes.get("min_temp", 16))
        max_temp = float(pump.attributes.get("max_temp", 32))
        if self._min_heat_accepted is not None:
            min_temp = max(min_temp, self._min_heat_accepted)
        if self._max_cool_accepted is not None:
            max_temp = min(max_temp, self._max_cool_accepted)
        desired = _whole(max(min_temp, min(max_temp, desired)))
        current = pump.attributes.get("temperature")
        try:
            if abs(float(current) - desired) < 1:
                return
        except (TypeError, ValueError):
            pass
        self._command_guard_until = time() + _COMMAND_ECHO_SECONDS
        await self._pump_call("set_temperature", {ATTR_TEMPERATURE: desired})
        self._command_count_temperature += 1
        self._observed_pump_target = desired
        self._last_command = time()
        self._last_command_reason = reason or f"prepare_{mode.value}"

    async def _apply_mode(self, mode: HVACMode, reason: str) -> None:
        self._neutral_mode = None
        pump = self.hass.states.get(self._heat_pump)
        if pump is not None and pump.state == mode.value:
            self._active_mode = mode
            self._last_transition = time()
            if mode in (HVACMode.HEAT, HVACMode.COOL):
                await self._disable_thermostats()
            else:
                await self._restore_thermostats()
            return
        for attempt in range(_TARGET_CONFIRM_ATTEMPTS - 1):
            self._command_guard_until = time() + _COMMAND_ECHO_SECONDS
            await self._pump_call("set_hvac_mode", {"hvac_mode": mode.value})
            self._command_count_mode += 1
            self._command_guard_until = time() + _COMMAND_ECHO_SECONDS
            if await self._wait_for_pump_mode(mode):
                break
            _LOGGER.warning(
                "%s: %s did not switch to %s (attempt %d)",
                self.entry.title,
                self._heat_pump,
                mode.value,
                attempt + 1,
            )
        self._last_command = time()
        self._last_command_reason = reason
        self._active_mode = mode
        self._last_transition = time()
        if mode in (HVACMode.HEAT, HVACMode.COOL):
            await self._disable_thermostats()
        else:
            await self._restore_thermostats()
        self.async_write_ha_state()

    async def _disable_thermostats(self) -> None:
        for entity_id in self._thermostats:
            state = self.hass.states.get(entity_id)
            if state is None or state.state in (
                HVACMode.OFF,
                STATE_UNAVAILABLE,
                STATE_UNKNOWN,
            ):
                continue
            self._disabled_thermostats.setdefault(
                entity_id,
                {"hvac_mode": state.state, "temperature": state.attributes.get("temperature")},
            )
            await self.hass.services.async_call(
                "climate", "set_hvac_mode", {"entity_id": entity_id, "hvac_mode": "off"}, blocking=True
            )

    async def _restore_thermostats(self) -> None:
        saved = dict(self._disabled_thermostats)
        remaining: dict[str, dict[str, Any]] = {}
        for entity_id, values in saved.items():
            state = self.hass.states.get(entity_id)
            if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
                # Config-entry climate entities often become available later
                # than this controller during startup. Keep the snapshot and
                # retry on the next evaluation instead of losing it.
                remaining[entity_id] = values
                continue
            if state.state != HVACMode.OFF:
                continue
            await self.hass.services.async_call(
                "climate",
                "set_hvac_mode",
                {"entity_id": entity_id, "hvac_mode": values["hvac_mode"]},
                blocking=True,
            )
            if values.get("temperature") is not None:
                await self.hass.services.async_call(
                    "climate",
                    "set_temperature",
                    {"entity_id": entity_id, ATTR_TEMPERATURE: values["temperature"]},
                    blocking=True,
                )
        self._disabled_thermostats = remaining
        self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode) -> None:
        previous = self._hvac_mode
        self._hvac_mode = HVACMode(hvac_mode)
        if self._hvac_mode == HVACMode.OFF:
            self._manual_until = 0
            self._user_start = False
        else:
            self._reset_opening_episode()
            if previous == HVACMode.OFF:
                self._user_start = True
        self.async_write_ha_state()
        await self._evaluate("thermostat_mode")

    async def async_set_temperature(self, **kwargs) -> None:
        if kwargs.get("target_temp_low") is not None:
            self._heat_target = _half(kwargs["target_temp_low"])
        if kwargs.get("target_temp_high") is not None:
            self._cool_target = _half(kwargs["target_temp_high"])
        if kwargs.get(ATTR_TEMPERATURE) is not None:
            if self._hvac_mode == HVACMode.COOL:
                self._cool_target = _half(kwargs[ATTR_TEMPERATURE])
            else:
                self._heat_target = _half(kwargs[ATTR_TEMPERATURE])
        if self._hvac_mode != HVACMode.OFF:
            self._reset_opening_episode()
        self.async_write_ha_state()
        await self._evaluate("target_change")

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        self._preset_mode = preset_mode
        if preset_mode == PRESET_MANUAL:
            self._manual_until = time() + self._override_seconds
        else:
            self._manual_until = 0
            await self._evaluate("resume_auto")
        self.async_write_ha_state()

    async def async_turn_off(self) -> None:
        await self.async_set_hvac_mode(HVACMode.OFF)

    async def async_turn_on(self) -> None:
        await self.async_set_hvac_mode(HVACMode.HEAT_COOL)
