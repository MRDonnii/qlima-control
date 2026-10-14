"""Status, countdowns and measurements (room, outdoor, COP, heat, price, filter)."""

from __future__ import annotations

from datetime import datetime, timezone

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, UnitOfPower, UnitOfTemperature, UnitOfTime
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import COUNTDOWNS, DOMAIN, SIGNAL_UPDATE, STATUSES
from .entity import QlimaEntity


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    """Add the status, next-change and one countdown sensor per timer."""
    entities: list[SensorEntity] = [StatusSensor(hass, entry), NextChangeSensor(hass, entry)]
    entities += [CountdownSensor(hass, entry, key) for key in COUNTDOWNS]
    unit = hass.data[DOMAIN][f"{entry.entry_id}_unit"]
    for key, fn, kind, needs in _MEASUREMENTS:
        if needs(unit):
            entities.append(Measurement(hass, entry, key, fn, kind))
    if unit.price_sensor:
        entities.append(PriceLevelSensor(hass, entry))
    if unit.power_switch:
        entities.append(WakeSensor(hass, entry))
    stats = hass.data[DOMAIN][f"{entry.entry_id}_stats"]
    for key, field, kind, needs in _STATS:
        if needs(unit):
            entities.append(Measurement(hass, entry, key, lambda u, f=field: stats.value(f), kind))
    async_add_entities(entities)


_TEMP = (SensorDeviceClass.TEMPERATURE, UnitOfTemperature.CELSIUS, 1, None)
_KINDS = {
    "temp": _TEMP,
    "humidity": (SensorDeviceClass.HUMIDITY, PERCENTAGE, 0, None),
    "power": (SensorDeviceClass.POWER, UnitOfPower.WATT, 0, None),
    "cop": (None, None, 2, "mdi:heat-pump"),
    "price": (SensorDeviceClass.MONETARY, "DKK/kWh", 3, "mdi:cash"),
    "cost": (SensorDeviceClass.MONETARY, "DKK/h", 2, "mdi:cash-clock"),
    "hours": (SensorDeviceClass.DURATION, UnitOfTime.HOURS, 0, "mdi:air-filter"),
    "energy": (SensorDeviceClass.ENERGY, "kWh", 2, None),
    "money": (SensorDeviceClass.MONETARY, "DKK", 2, "mdi:cash"),
    "runtime": (SensorDeviceClass.DURATION, UnitOfTime.HOURS, 1, "mdi:timer-outline"),
    "count": (None, None, 0, "mdi:counter"),
}
_MEASUREMENTS = (
    ("room_temperature", lambda u: u.room_temperature().value, "temp", lambda u: bool(u.temperature_sensors)),
    ("room_humidity", lambda u: u.room_humidity().value, "humidity", lambda u: bool(u.humidity_sensors)),
    ("outdoor_temperature", lambda u: u.outdoor(), "temp", lambda u: bool(u.outdoor_sensor or u.entity("outdoor"))),
    ("unit_temperature", lambda u: u.unit_temperature(), "temp", lambda u: bool(u.entity("unit_temp"))),
    ("cop", lambda u: u.cop(), "cop", lambda u: True),
    ("power", lambda u: u.power(), "power", lambda u: bool(u.power_sensor)),
    ("energy", lambda u: u._float(u.energy_sensor), "energy", lambda u: bool(u.energy_sensor)),
    ("heat_output", lambda u: u.heat_output(), "power", lambda u: bool(u.power_sensor)),
    ("heat_price", lambda u: u.heat_price(), "price", lambda u: bool(u.price_sensor)),
    ("cost_per_hour", lambda u: u.cost_per_hour(), "cost", lambda u: bool(u.price_sensor and u.power_sensor)),
    ("filter_hours", lambda u: u.filter_hours(), "hours", lambda u: bool(u.entity("filter_hours"))),
    ("supply_temperature", lambda u: u.supply_temperature(), "temp", lambda u: bool(u.entity("supply_temp"))),
    ("return_temperature", lambda u: u.return_temperature(), "temp", lambda u: bool(u.entity("return_temp"))),
    ("measured_heat_output", lambda u: u.measured_heat(), "power", lambda u: bool(u.entity("supply_temp"))),
    ("measured_cop", lambda u: u.measured_cop(), "cop", lambda u: bool(u.entity("supply_temp") and u.power_sensor)),
)
# From the persisted statistics.
_STATS = (
    ("energy_today", "energy_day", "energy", lambda u: bool(u.energy_sensor or u.power_sensor)),
    ("energy_month", "energy_month", "energy", lambda u: bool(u.energy_sensor or u.power_sensor)),
    ("cost_today", "cost_day", "money", lambda u: bool((u.energy_sensor or u.power_sensor) and u.price_sensor)),
    ("cost_month", "cost_month", "money", lambda u: bool((u.energy_sensor or u.power_sensor) and u.price_sensor)),
    ("runtime_today", "runtime_day", "runtime", lambda u: True),
    ("starts_today", "starts_day", "count", lambda u: True),
    ("defrosts_today", "defrosts_day", "count", lambda u: bool(u.power_sensor)),
)


class Measurement(QlimaEntity, SensorEntity):
    """A value derived from the unit's linked sensors."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, hass, entry, key, fn, kind) -> None:
        super().__init__(hass, entry, key)
        self._fn = fn
        device_class, unit, precision, icon = _KINDS[kind]
        self._attr_device_class = device_class
        self._attr_native_unit_of_measurement = unit
        self._attr_suggested_display_precision = precision
        if icon:
            self._attr_icon = icon
        if key in ("filter_hours", "energy"):
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        if key in ("energy_today", "energy_month", "runtime_today", "starts_today", "defrosts_today"):
            self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        if device_class == SensorDeviceClass.MONETARY:
            # HA allows no 'measurement' on money; running totals are 'total'.
            self._attr_state_class = SensorStateClass.TOTAL if kind == "money" else None

    @property
    def native_value(self):
        return self._fn(self.unit)

    @property
    def extra_state_attributes(self):
        if self._attr_translation_key == "room_temperature":
            reading = self.unit.room_temperature()
            return {"sources": reading.sources, "mode": self.unit.temperature_mode}
        if self._attr_translation_key == "cop":
            return {"model": "Carnot × 0,40 med belastning (el-effekt / p_max), udedel og indsugning", "p_max": self.unit.p_max}
        return None


def _as_datetime(value: float | None) -> datetime | None:
    return datetime.fromtimestamp(value, timezone.utc) if value is not None else None


class _ThermostatSensor(SensorEntity):
    """Follows the thermostat entity of the same config entry."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, key: str) -> None:
        self.hass = hass
        self.entry = entry
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.entry_id)}}

    @property
    def _thermostat(self):
        return self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id)

    @property
    def available(self) -> bool:
        return self._thermostat is not None

    async def async_added_to_hass(self) -> None:
        @callback
        def _updated() -> None:
            self.async_write_ha_state()

        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE.format(self.entry.entry_id), _updated)
        )


class StatusSensor(_ThermostatSensor):
    """What the thermostat is doing: heating, door pause, manual override, ..."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(STATUSES)

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, "status")

    @property
    def native_value(self) -> str | None:
        thermostat = self._thermostat
        return thermostat.status() if thermostat is not None else None


class NextChangeSensor(_ThermostatSensor):
    """When the first running timer ends, and which one it is."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, "next_change")

    @property
    def native_value(self) -> datetime | None:
        thermostat = self._thermostat
        return _as_datetime(thermostat.next_change()[1]) if thermostat is not None else None

    @property
    def extra_state_attributes(self):
        thermostat = self._thermostat
        return {"event": thermostat.next_change()[0] if thermostat is not None else None}


class CountdownSensor(_ThermostatSensor):
    """End time of one timer; unknown while that timer is not running."""

    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, key: str) -> None:
        super().__init__(hass, entry, f"countdown_{key}")
        self._key = key

    @property
    def native_value(self) -> datetime | None:
        thermostat = self._thermostat
        return _as_datetime(thermostat.countdowns()[self._key]) if thermostat is not None else None


class PriceLevelSensor(_ThermostatSensor):
    """Cheap / normal / expensive right now, and what that does to the target."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["off", "unknown", "cheap", "normal", "expensive"]
    _attr_icon = "mdi:cash-sync"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, "price_level")

    @property
    def _price(self):
        return self.hass.data.get(DOMAIN, {}).get(f"{self.entry.entry_id}_price")

    @property
    def native_value(self) -> str | None:
        price = self._price
        return price.level if price is not None else None

    @property
    def extra_state_attributes(self):
        price = self._price
        return price.attributes() if price is not None else None


class WakeSensor(_ThermostatSensor):
    """Whether the unit is idling while the room is too cold, and when it is woken."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["off", "busy", "not_running", "unit_not_on", "defrosting", "no_data", "on_target", "working", "stalled"]
    _attr_icon = "mdi:alarm-light-outline"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, "wake_status")

    @property
    def _wake(self):
        return self.hass.data.get(DOMAIN, {}).get(f"{self.entry.entry_id}_wake")

    @property
    def native_value(self) -> str | None:
        wake = self._wake
        return wake.reason if wake is not None else None

    @property
    def extra_state_attributes(self):
        wake = self._wake
        if wake is None:
            return None
        return {**wake.attributes(), "wakes_today": wake.wakes_today}
