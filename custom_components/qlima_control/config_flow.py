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
    CONF_REGULATION,
    DEFAULT_REGULATION,
    REGULATION_THERMOSTAT,
    REGULATION_UNIT,
    CONF_OPENING_RESUME_DELAY_MINUTES,
    CONF_OPENING_SENSORS,
    CONF_OUTDOOR_SENSOR,
    CONF_P_MAX,
    CONF_POWER_SENSOR,
    CONF_POWER_SWITCH,
    CONF_AUTO_POWER_CYCLE_MINUTES,
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
)
from .unit import esp_entities

SECTIONS = ("sensors", "power", "openings", "control", "notifications", "advanced")
MENU = ["device", "sensors", "power", "openings", "control", "smart", "notifications", "advanced"]


def _entities(domain, device_class=None, multiple=False):
    config = selector.EntitySelectorConfig(domain=domain, multiple=multiple)
    if device_class:
        config = selector.EntitySelectorConfig(domain=domain, device_class=device_class, multiple=multiple)
    return selector.EntitySelector(config)


PRICE_INTEGRATIONS = ("stromligning", "nordpool", "energidataservice", "tibber", "eloverblik", "entsoe", "octopus_energy")


def _price_selector():
    """Electricity price sensors: known price integrations (e.g. Strømligning) or any monetary sensor."""
    filters = [{"integration": i, "domain": "sensor"} for i in PRICE_INTEGRATIONS]
    filters.append({"domain": "sensor", "device_class": "monetary"})
    return selector.EntitySelector(selector.EntitySelectorConfig(filter=filters))


def _number(lo, hi, step, unit=None):
    return selector.NumberSelector(
        selector.NumberSelectorConfig(min=lo, max=hi, step=step, unit_of_measurement=unit, mode=selector.NumberSelectorMode.BOX)
    )


def _opt(key, defaults, fallback=None):
    """Optional key that keeps the current value as suggestion (so it can be cleared)."""
    value = defaults.get(key, fallback)
    return vol.Optional(key, description={"suggested_value": value}) if value not in (None, "", []) else vol.Optional(key)


def _fields(name: str, d: dict[str, Any]) -> dict[Any, Any]:
    """The fields of one settings group (used as a section in setup and a menu step after)."""
    if name == "device":
        return {
            vol.Required(CONF_DEVICE, default=d.get(CONF_DEVICE)): selector.DeviceSelector(
                selector.DeviceSelectorConfig(integration="esphome")
            ),
            vol.Required(CONF_MODEL, default=d.get(CONF_MODEL, "s5225")): selector.SelectSelector(
                selector.SelectSelectorConfig(options=list(MODEL_P_MAX), translation_key="model")
            ),
        }
    if name == "sensors":
        return {
            vol.Required(CONF_TEMPERATURE_SENSORS, default=d.get(CONF_TEMPERATURE_SENSORS, [])): _entities(
                ["sensor", "input_number"], "temperature", True
            ),
            vol.Required(CONF_TEMPERATURE_MODE, default=d.get(CONF_TEMPERATURE_MODE, AGGREGATE_MEAN)): selector.SelectSelector(
                selector.SelectSelectorConfig(options=[AGGREGATE_MEAN, AGGREGATE_WEIGHTED, AGGREGATE_MIN, AGGREGATE_MAX], translation_key="temperature_mode")
            ),
            _opt(CONF_HUMIDITY_SENSORS, d, []): _entities("sensor", "humidity", True),
            _opt(CONF_OUTDOOR_SENSOR, d): _entities("sensor", "temperature"),
            _opt(CONF_PRICE_SENSOR, d): _price_selector(),
        }
    if name == "power":
        return {
            _opt(CONF_POWER_SENSOR, d): _entities("sensor", "power"),
            _opt(CONF_ENERGY_SENSOR, d): _entities("sensor", "energy"),
            _opt(CONF_POWER_SWITCH, d): _entities(["switch", "input_boolean"]),
            vol.Required(CONF_AUTO_POWER_CYCLE_MINUTES, default=d.get(CONF_AUTO_POWER_CYCLE_MINUTES, 0)): _number(0, 240, 5, "min"),
            vol.Required(CONF_RUNNING_POWER_W, default=d.get(CONF_RUNNING_POWER_W, DEFAULT_RUNNING_POWER_W)): _number(20, 500, 5, "W"),
            _opt(CONF_P_MAX, d): _number(300, 5000, 50, "W"),
        }
    if name == "openings":
        return {
            _opt(CONF_OPENING_SENSORS, d, []): _entities("binary_sensor", None, True),
            vol.Required(CONF_OPENING_MIN_DELAY_MINUTES, default=d.get(CONF_OPENING_MIN_DELAY_MINUTES, DEFAULT_OPENING_MIN_DELAY_MINUTES)): _number(0, 180, 1, "min"),
            vol.Required(CONF_OPENING_OFF_DELAY_MINUTES, default=d.get(CONF_OPENING_OFF_DELAY_MINUTES, DEFAULT_OPENING_OFF_DELAY_MINUTES)): _number(0, 600, 1, "min"),
            vol.Required(CONF_OPENING_RESUME_DELAY_MINUTES, default=d.get(CONF_OPENING_RESUME_DELAY_MINUTES, DEFAULT_OPENING_RESUME_DELAY_MINUTES)): _number(0, 180, 1, "min"),
            vol.Required(CONF_OPENING_REDUCE_TO, default=d.get(CONF_OPENING_REDUCE_TO, DEFAULT_OPENING_REDUCE_TO)): selector.SelectSelector(
                selector.SelectSelectorConfig(options=[OPENING_REDUCE_SETPOINT, OPENING_REDUCE_MINIMUM], translation_key="opening_reduce_to")
            ),
        }
    if name == "control":
        return {
            vol.Required(CONF_REGULATION, default=d.get(CONF_REGULATION, DEFAULT_REGULATION)): selector.SelectSelector(
                selector.SelectSelectorConfig(options=[REGULATION_UNIT, REGULATION_THERMOSTAT], translation_key="regulation")
            ),
            vol.Required(CONF_HEAT_TARGET, default=d.get(CONF_HEAT_TARGET, DEFAULT_HEAT_TARGET)): _number(16, 30, 0.5, "°C"),
            vol.Required(CONF_COOL_TARGET, default=d.get(CONF_COOL_TARGET, DEFAULT_COOL_TARGET)): _number(18, 32, 0.5, "°C"),
            vol.Required(CONF_START_HYSTERESIS, default=d.get(CONF_START_HYSTERESIS, DEFAULT_START_HYSTERESIS)): _number(0.0, 2.0, 0.1, "°C"),
            vol.Required(CONF_HYSTERESIS, default=d.get(CONF_HYSTERESIS, DEFAULT_HYSTERESIS)): _number(0.2, 2.0, 0.1, "°C"),
            vol.Required(CONF_MIN_RUN_MINUTES, default=d.get(CONF_MIN_RUN_MINUTES, DEFAULT_MIN_RUN_MINUTES)): _number(5, 180, 1, "min"),
            vol.Required(CONF_MIN_OFF_MINUTES, default=d.get(CONF_MIN_OFF_MINUTES, DEFAULT_MIN_OFF_MINUTES)): _number(3, 60, 1, "min"),
            vol.Required(CONF_MANUAL_OVERRIDE_MINUTES, default=d.get(CONF_MANUAL_OVERRIDE_MINUTES, DEFAULT_MANUAL_OVERRIDE_MINUTES)): _number(0, 1440, 5, "min"),
            vol.Required(CONF_SETPOINT_COMPENSATION, default=d.get(CONF_SETPOINT_COMPENSATION, False)): bool,
            _opt("presence_entity", d): _entities(["person", "group", "binary_sensor", "alarm_control_panel", "input_boolean", "zone"]),
            vol.Required("away_setback", default=d.get("away_setback", 3)): _number(0, 10, 1, "°C"),
        }
    if name == "smart":
        return {
            vol.Required(CONF_PRICE_CONTROL, default=d.get(CONF_PRICE_CONTROL, False)): bool,
            vol.Required(CONF_PRICE_CHEAP_OFFSET, default=d.get(CONF_PRICE_CHEAP_OFFSET, DEFAULT_PRICE_CHEAP_OFFSET)): _number(0, 2, 1, "°C"),
            vol.Required(CONF_PRICE_EXPENSIVE_OFFSET, default=d.get(CONF_PRICE_EXPENSIVE_OFFSET, DEFAULT_PRICE_EXPENSIVE_OFFSET)): _number(0, 2, 1, "°C"),
            vol.Required(CONF_PRICE_RESOLUTION, default=d.get(CONF_PRICE_RESOLUTION, "auto")): selector.SelectSelector(
                selector.SelectSelectorConfig(options=["auto", "quarter", "hour"], translation_key="price_resolution")
            ),
            _opt(CONF_PRICE_RESOLUTION_ENTITY, d): _entities(["select", "input_select", "sensor"]),
            vol.Required(CONF_AUTO_WAKE, default=d.get(CONF_AUTO_WAKE, False)): bool,
            vol.Required(CONF_WAKE_POWER_W, default=d.get(CONF_WAKE_POWER_W, DEFAULT_WAKE_POWER_W)): _number(50, 1500, 10, "W"),
            vol.Required(CONF_WAKE_MINUTES, default=d.get(CONF_WAKE_MINUTES, DEFAULT_WAKE_MINUTES)): _number(15, 240, 5, "min"),
            vol.Required(CONF_WAKE_DEFICIT, default=d.get(CONF_WAKE_DEFICIT, DEFAULT_WAKE_DEFICIT)): _number(0.3, 3.0, 0.1, "°C"),
        }
    if name == "notifications":
        return {
            _opt(CONF_NOTIFY_SERVICE, d): str,
            vol.Required(CONF_FILTER_ALARM_HOURS, default=d.get(CONF_FILTER_ALARM_HOURS, DEFAULT_FILTER_ALARM_HOURS)): _number(50, 5000, 10, "h"),
        }
    # advanced
    return {
        _opt(CONF_THERMOSTATS_TO_DISABLE, d, []): _entities("climate", None, True),
        vol.Required(CONF_SENSOR_STABLE_SECONDS, default=d.get(CONF_SENSOR_STABLE_SECONDS, DEFAULT_SENSOR_STABLE_SECONDS)): _number(30, 900, 10, "s"),
        vol.Required(CONF_SENSOR_LOSS_GRACE_SECONDS, default=d.get(CONF_SENSOR_LOSS_GRACE_SECONDS, DEFAULT_SENSOR_LOSS_GRACE_SECONDS)): _number(30, 3600, 30, "s"),
    }


def _schema(d: dict[str, Any], with_device: bool) -> vol.Schema:
    """First setup: name + device on top, the rest in collapsible sections."""
    top: dict[Any, Any] = {vol.Required(CONF_NAME, default=d.get(CONF_NAME, "Varmepumpe")): str}
    top.update(_fields("device", d))
    for name, collapsed in (("sensors", False), ("power", False), ("openings", True), ("control", True), ("notifications", True), ("advanced", True)):
        top[vol.Required(name)] = section(vol.Schema(_fields(name, d)), {"collapsed": collapsed})
    return vol.Schema(top)


def _flatten(user_input: dict[str, Any]) -> dict[str, Any]:
    flat = {k: v for k, v in user_input.items() if k not in SECTIONS}
    for name in SECTIONS:
        flat.update(user_input.get(name) or {})
    for key in (CONF_MIN_RUN_MINUTES, CONF_MIN_OFF_MINUTES, CONF_MANUAL_OVERRIDE_MINUTES,
                CONF_OPENING_MIN_DELAY_MINUTES, CONF_OPENING_OFF_DELAY_MINUTES, CONF_OPENING_RESUME_DELAY_MINUTES,
                CONF_SENSOR_STABLE_SECONDS, CONF_SENSOR_LOSS_GRACE_SECONDS, CONF_AUTO_POWER_CYCLE_MINUTES):
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
        errors["sensors" if need_device else "base"] = "no_temperature"
    if int(data.get(CONF_OPENING_OFF_DELAY_MINUTES, 20)) < int(data.get(CONF_OPENING_MIN_DELAY_MINUTES, 5)):
        errors["openings" if need_device else "base"] = "off_before_min"
    if float(data.get(CONF_COOL_TARGET, 24)) <= float(data.get(CONF_HEAT_TARGET, 21)):
        errors["control" if need_device else "base"] = "cool_must_exceed_heat"
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
    """Settings menu: change one group at a time after setup."""

    def __init__(self, entry):
        self.entry = entry
        self._data: dict[str, Any] = {}

    @property
    def _current(self) -> dict[str, Any]:
        return {**self.entry.data, **self.entry.options, **self._data}

    async def async_step_init(self, user_input=None):
        return self.async_show_menu(step_id="init", menu_options=MENU)

    async def _group(self, name: str, user_input):
        errors: dict[str, str] = {}
        if user_input is not None:
            data = _flatten({name: user_input}) if name in SECTIONS else dict(user_input)
            merged = {**self._current, **data}
            if name == "device":
                found = esp_entities(self.hass, merged.get(CONF_DEVICE))
                if "climate" not in found:
                    errors[CONF_DEVICE] = "no_climate"
                else:
                    data[CONF_HEAT_PUMP] = found["climate"]
            else:
                errors = _validate(self.hass, merged, False)
            if not errors:
                self._data.update(data)
                if name == "sensors" and merged.get(CONF_TEMPERATURE_MODE) == AGGREGATE_WEIGHTED:
                    return await self.async_step_weights()
                return self.async_create_entry(title="", data={**self.entry.options, **self._data})
        return self.async_show_form(step_id=name, data_schema=vol.Schema(_fields(name, self._current)), errors=errors)

    async def async_step_device(self, user_input=None):
        return await self._group("device", user_input)

    async def async_step_sensors(self, user_input=None):
        return await self._group("sensors", user_input)

    async def async_step_power(self, user_input=None):
        return await self._group("power", user_input)

    async def async_step_openings(self, user_input=None):
        return await self._group("openings", user_input)

    async def async_step_control(self, user_input=None):
        return await self._group("control", user_input)

    async def async_step_notifications(self, user_input=None):
        return await self._group("notifications", user_input)

    async def async_step_smart(self, user_input=None):
        return await self._group("smart", user_input)

    async def async_step_advanced(self, user_input=None):
        return await self._group("advanced", user_input)

    async def async_step_weights(self, user_input=None):
        sensors = self._current[CONF_TEMPERATURE_SENSORS]
        if user_input is not None:
            self._data[CONF_TEMPERATURE_WEIGHTS] = {k: float(v) for k, v in user_input.items()}
            return self.async_create_entry(title="", data={**self.entry.options, **self._data})
        return self.async_show_form(
            step_id="weights",
            data_schema=_weights_schema(self.hass, sensors, self._current.get(CONF_TEMPERATURE_WEIGHTS, {})),
            description_placeholders=_weights_placeholders(self.hass, sensors),
        )
