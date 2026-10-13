"""Config flow for Qlima Control: pick the ESP in the unit and link the room's sensors."""

from __future__ import annotations

from typing import Any

from homeassistant import config_entries
from homeassistant.const import CONF_NAME
from homeassistant.data_entry_flow import section
from homeassistant.helpers import device_registry as dr, selector
import voluptuous as vol

from .const import (
    AGGREGATE_MAX,
    AGGREGATE_MEAN,
    AGGREGATE_MIN,
    AGGREGATE_WEIGHTED,
    CONF_COOL_TARGET,
    CONF_DEVICE,
    CONF_ENERGY_SENSOR,
    CONF_FILTER_ALARM_HOURS,
    CONF_HEAT_PUMP,
    CONF_HEAT_TARGET,
    CONF_HUMIDITY_SENSORS,
    CONF_HYSTERESIS,
    CONF_MANUAL_OVERRIDE_MINUTES,
    CONF_MIN_OFF_MINUTES,
    CONF_MIN_RUN_MINUTES,
    CONF_MODEL,
    CONF_NOTIFY_SERVICE,
    CONF_OPENING_MIN_DELAY_MINUTES,
    CONF_OPENING_OFF_DELAY_MINUTES,
    CONF_OPENING_REDUCE_TO,
    CONF_OPENING_RESUME_DELAY_MINUTES,
    CONF_OPENING_SENSORS,
    CONF_OUTDOOR_SENSOR,
    CONF_P_MAX,
    CONF_POWER_SENSOR,
    CONF_PRICE_SENSOR,
    CONF_RUNNING_POWER_W,
    CONF_SENSOR_LOSS_GRACE_SECONDS,
    CONF_SENSOR_STABLE_SECONDS,
    CONF_SETPOINT_COMPENSATION,
    CONF_START_HYSTERESIS,
    CONF_TEMPERATURE_MODE,
    CONF_TEMPERATURE_SENSORS,
    CONF_TEMPERATURE_WEIGHTS,
    CONF_THERMOSTATS_TO_DISABLE,
    DEFAULT_COOL_TARGET,
    DEFAULT_FILTER_ALARM_HOURS,
    DEFAULT_HEAT_TARGET,
    DEFAULT_HYSTERESIS,
    DEFAULT_MANUAL_OVERRIDE_MINUTES,
    DEFAULT_MIN_OFF_MINUTES,
    DEFAULT_MIN_RUN_MINUTES,
    DEFAULT_OPENING_MIN_DELAY_MINUTES,
    DEFAULT_OPENING_OFF_DELAY_MINUTES,
    DEFAULT_OPENING_REDUCE_TO,
    DEFAULT_OPENING_RESUME_DELAY_MINUTES,
    DEFAULT_RUNNING_POWER_W,
    DEFAULT_SENSOR_LOSS_GRACE_SECONDS,
    DEFAULT_SENSOR_STABLE_SECONDS,
    DEFAULT_START_HYSTERESIS,
    DOMAIN,
    MODEL_P_MAX,
    OPENING_REDUCE_MINIMUM,
    OPENING_REDUCE_SETPOINT,
)
from .unit import esp_entities

SECTIONS = ("sensors", "openings", "control", "advanced")


def _entities(domain, device_class=None, multiple=False):
    config = selector.EntitySelectorConfig(domain=domain, multiple=multiple)
    if device_class:
        config = selector.EntitySelectorConfig(domain=domain, device_class=device_class, multiple=multiple)
    return selector.EntitySelector(config)


def _number(lo, hi, step, unit=None):
    return selector.NumberSelector(
        selector.NumberSelectorConfig(min=lo, max=hi, step=step, unit_of_measurement=unit, mode=selector.NumberSelectorMode.BOX)
    )


def _opt(key, defaults, fallback=None):
    """Optional key that keeps the current value as suggestion (so it can be cleared)."""
    value = defaults.get(key, fallback)
    return vol.Optional(key, description={"suggested_value": value}) if value not in (None, "", []) else vol.Optional(key)


def _schema(d: dict[str, Any], with_device: bool) -> vol.Schema:
    top: dict[Any, Any] = {}
    if with_device:
        top[vol.Required(CONF_NAME, default=d.get(CONF_NAME, "Varmepumpe"))] = str
        top[vol.Required(CONF_DEVICE, default=d.get(CONF_DEVICE))] = selector.DeviceSelector(
            selector.DeviceSelectorConfig(integration="esphome")
        )
    top[vol.Required(CONF_MODEL, default=d.get(CONF_MODEL, "s5225"))] = selector.SelectSelector(
        selector.SelectSelectorConfig(options=list(MODEL_P_MAX), translation_key="model")
    )
    sensors = {
        vol.Required(CONF_TEMPERATURE_SENSORS, default=d.get(CONF_TEMPERATURE_SENSORS, [])): _entities(
            ["sensor", "input_number"], "temperature", True
        ),
        vol.Required(CONF_TEMPERATURE_MODE, default=d.get(CONF_TEMPERATURE_MODE, AGGREGATE_MEAN)): selector.SelectSelector(
            selector.SelectSelectorConfig(options=[AGGREGATE_MEAN, AGGREGATE_WEIGHTED, AGGREGATE_MIN, AGGREGATE_MAX], translation_key="temperature_mode")
        ),
        _opt(CONF_HUMIDITY_SENSORS, d, []): _entities("sensor", "humidity", True),
        _opt(CONF_POWER_SENSOR, d): _entities("sensor", "power"),
        _opt(CONF_ENERGY_SENSOR, d): _entities("sensor", "energy"),
        _opt(CONF_PRICE_SENSOR, d): _entities("sensor"),
        _opt(CONF_OUTDOOR_SENSOR, d): _entities("sensor", "temperature"),
    }
    openings = {
        _opt(CONF_OPENING_SENSORS, d, []): _entities("binary_sensor", None, True),
        vol.Required(CONF_OPENING_MIN_DELAY_MINUTES, default=d.get(CONF_OPENING_MIN_DELAY_MINUTES, DEFAULT_OPENING_MIN_DELAY_MINUTES)): _number(0, 180, 1, "min"),
        vol.Required(CONF_OPENING_OFF_DELAY_MINUTES, default=d.get(CONF_OPENING_OFF_DELAY_MINUTES, DEFAULT_OPENING_OFF_DELAY_MINUTES)): _number(0, 600, 1, "min"),
        vol.Required(CONF_OPENING_RESUME_DELAY_MINUTES, default=d.get(CONF_OPENING_RESUME_DELAY_MINUTES, DEFAULT_OPENING_RESUME_DELAY_MINUTES)): _number(0, 180, 1, "min"),
        vol.Required(CONF_OPENING_REDUCE_TO, default=d.get(CONF_OPENING_REDUCE_TO, DEFAULT_OPENING_REDUCE_TO)): selector.SelectSelector(
            selector.SelectSelectorConfig(options=[OPENING_REDUCE_SETPOINT, OPENING_REDUCE_MINIMUM], translation_key="opening_reduce_to")
        ),
    }
    control = {
        vol.Required(CONF_HEAT_TARGET, default=d.get(CONF_HEAT_TARGET, DEFAULT_HEAT_TARGET)): _number(16, 30, 1, "°C"),
        vol.Required(CONF_COOL_TARGET, default=d.get(CONF_COOL_TARGET, DEFAULT_COOL_TARGET)): _number(18, 32, 1, "°C"),
        vol.Required(CONF_START_HYSTERESIS, default=d.get(CONF_START_HYSTERESIS, DEFAULT_START_HYSTERESIS)): _number(0.0, 2.0, 0.1, "°C"),
        vol.Required(CONF_HYSTERESIS, default=d.get(CONF_HYSTERESIS, DEFAULT_HYSTERESIS)): _number(0.2, 2.0, 0.1, "°C"),
        vol.Required(CONF_MIN_RUN_MINUTES, default=d.get(CONF_MIN_RUN_MINUTES, DEFAULT_MIN_RUN_MINUTES)): _number(5, 180, 1, "min"),
        vol.Required(CONF_MIN_OFF_MINUTES, default=d.get(CONF_MIN_OFF_MINUTES, DEFAULT_MIN_OFF_MINUTES)): _number(3, 60, 1, "min"),
        vol.Required(CONF_MANUAL_OVERRIDE_MINUTES, default=d.get(CONF_MANUAL_OVERRIDE_MINUTES, DEFAULT_MANUAL_OVERRIDE_MINUTES)): _number(0, 1440, 5, "min"),
        vol.Required(CONF_SETPOINT_COMPENSATION, default=d.get(CONF_SETPOINT_COMPENSATION, False)): bool,
    }
    advanced = {
        _opt(CONF_NOTIFY_SERVICE, d): str,
        vol.Required(CONF_FILTER_ALARM_HOURS, default=d.get(CONF_FILTER_ALARM_HOURS, DEFAULT_FILTER_ALARM_HOURS)): _number(50, 5000, 10, "h"),
        _opt(CONF_P_MAX, d): _number(300, 5000, 50, "W"),
        vol.Required(CONF_RUNNING_POWER_W, default=d.get(CONF_RUNNING_POWER_W, DEFAULT_RUNNING_POWER_W)): _number(20, 500, 5, "W"),
        _opt(CONF_THERMOSTATS_TO_DISABLE, d, []): _entities("climate", None, True),
        vol.Required(CONF_SENSOR_STABLE_SECONDS, default=d.get(CONF_SENSOR_STABLE_SECONDS, DEFAULT_SENSOR_STABLE_SECONDS)): _number(30, 900, 10, "s"),
        vol.Required(CONF_SENSOR_LOSS_GRACE_SECONDS, default=d.get(CONF_SENSOR_LOSS_GRACE_SECONDS, DEFAULT_SENSOR_LOSS_GRACE_SECONDS)): _number(30, 3600, 30, "s"),
    }
    top[vol.Required("sensors")] = section(vol.Schema(sensors), {"collapsed": False})
    top[vol.Required("openings")] = section(vol.Schema(openings), {"collapsed": True})
    top[vol.Required("control")] = section(vol.Schema(control), {"collapsed": True})
    top[vol.Required("advanced")] = section(vol.Schema(advanced), {"collapsed": True})
    return vol.Schema(top)


def _flatten(user_input: dict[str, Any]) -> dict[str, Any]:
    flat = {k: v for k, v in user_input.items() if k not in SECTIONS}
    for name in SECTIONS:
        flat.update(user_input.get(name) or {})
    for key in (CONF_MIN_RUN_MINUTES, CONF_MIN_OFF_MINUTES, CONF_MANUAL_OVERRIDE_MINUTES,
                CONF_OPENING_MIN_DELAY_MINUTES, CONF_OPENING_OFF_DELAY_MINUTES, CONF_OPENING_RESUME_DELAY_MINUTES,
                CONF_SENSOR_STABLE_SECONDS, CONF_SENSOR_LOSS_GRACE_SECONDS):
        if key in flat:
            flat[key] = int(flat[key])
    return flat


def _validate(hass, data: dict[str, Any], need_device: bool) -> dict[str, str]:
    errors: dict[str, str] = {}
    if need_device:
        found = esp_entities(hass, data.get(CONF_DEVICE))
        if "climate" not in found:
            errors[CONF_DEVICE] = "no_climate"
    if not data.get(CONF_TEMPERATURE_SENSORS):
        errors["sensors"] = "no_temperature"
    if int(data[CONF_OPENING_OFF_DELAY_MINUTES]) < int(data[CONF_OPENING_MIN_DELAY_MINUTES]):
        errors["openings"] = "off_before_min"
    if float(data[CONF_COOL_TARGET]) <= float(data[CONF_HEAT_TARGET]):
        errors["control"] = "cool_must_exceed_heat"
    return errors


def _weights_schema(hass, sensors: list[str], current: dict[str, float]) -> vol.Schema:
    """One percentage per room sensor (relative weights; they need not sum to 100)."""
    fields = {}
    even = round(100 / max(len(sensors), 1))
    for entity_id in sensors:
        fields[vol.Required(entity_id, default=float(current.get(entity_id, even)))] = _number(0, 100, 1, "%")
    return vol.Schema(fields)


def _weights_placeholders(hass, sensors: list[str]) -> dict[str, str]:
    names = []
    for entity_id in sensors:
        state = hass.states.get(entity_id)
        names.append(f"• {state.attributes.get('friendly_name', entity_id) if state else entity_id}: {entity_id}")
    return {"sensors": "\n".join(names)}


def _nest(flat: dict[str, Any]) -> dict[str, Any]:
    """Flat stored config → defaults dict (section schemas read flat keys)."""
    return dict(flat)


class QlimaControlConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Add one heat pump."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors: dict[str, str] = {}
        defaults: dict[str, Any] = {}
        if user_input is not None:
            data = _flatten(user_input)
            defaults = data
            errors = _validate(self.hass, data, True)
            if not errors:
                found = esp_entities(self.hass, data[CONF_DEVICE])
                data[CONF_HEAT_PUMP] = found["climate"]
                device = dr.async_get(self.hass).async_get(data[CONF_DEVICE])
                await self.async_set_unique_id(device.id if device else data[CONF_DEVICE])
                self._abort_if_unique_id_configured()
                self._data = data
                if data.get(CONF_TEMPERATURE_MODE) == AGGREGATE_WEIGHTED:
                    return await self.async_step_weights()
                return self.async_create_entry(title=data[CONF_NAME], data=data)
        return self.async_show_form(step_id="user", data_schema=_schema(_nest(defaults), True), errors=errors)

    async def async_step_weights(self, user_input=None):
        sensors = self._data[CONF_TEMPERATURE_SENSORS]
        if user_input is not None:
            self._data[CONF_TEMPERATURE_WEIGHTS] = {k: float(v) for k, v in user_input.items()}
            return self.async_create_entry(title=self._data[CONF_NAME], data=self._data)
        return self.async_show_form(
            step_id="weights",
            data_schema=_weights_schema(self.hass, sensors, self._data.get(CONF_TEMPERATURE_WEIGHTS, {})),
            description_placeholders=_weights_placeholders(self.hass, sensors),
        )

    @staticmethod
    def async_get_options_flow(config_entry):
        return QlimaControlOptionsFlow(config_entry)


class QlimaControlOptionsFlow(config_entries.OptionsFlow):
    """Change linked sensors and control settings."""

    def __init__(self, entry):
        self.entry = entry

    async def async_step_init(self, user_input=None):
        current = {**self.entry.data, **self.entry.options}
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _flatten(user_input)
            errors = _validate(self.hass, {**current, **data}, False)
            if not errors:
                self._data = data
                if data.get(CONF_TEMPERATURE_MODE) == AGGREGATE_WEIGHTED:
                    return await self.async_step_weights()
                return self.async_create_entry(title="", data=data)
            current = {**current, **data}
        return self.async_show_form(step_id="init", data_schema=_schema(_nest(current), False), errors=errors)

    async def async_step_weights(self, user_input=None):
        sensors = self._data[CONF_TEMPERATURE_SENSORS]
        if user_input is not None:
            self._data[CONF_TEMPERATURE_WEIGHTS] = {k: float(v) for k, v in user_input.items()}
            return self.async_create_entry(title="", data=self._data)
        current = {**self.entry.data, **self.entry.options}.get(CONF_TEMPERATURE_WEIGHTS, {})
        return self.async_show_form(
            step_id="weights",
            data_schema=_weights_schema(self.hass, sensors, current),
            description_placeholders=_weights_placeholders(self.hass, sensors),
        )
