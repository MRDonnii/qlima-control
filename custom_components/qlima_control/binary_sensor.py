"""Heating/cooling (from mode + measured power), openings, unit responding, power problem."""

from __future__ import annotations

from time import time

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .entity import QlimaEntity

# Keep "heating" on through short power dips (defrost, compressor ramps).
_RUN_HOLD_SECONDS = 90
# Running without measurable power for this long is reported as a problem.
_NO_POWER_SECONDS = 600


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    unit = hass.data[DOMAIN][f"{entry.entry_id}_unit"]
    entities = [Running(hass, entry, "heating", "heat"), Running(hass, entry, "cooling", "cool"), Online(hass, entry)]
    if {**entry.data, **entry.options}.get("opening_sensors"):
        entities.append(OpeningOpen(hass, entry))
    if unit.power_sensor:
        entities.append(NoPower(hass, entry))
    async_add_entities(entities)


class Running(QlimaEntity, BinarySensorEntity):
    """On while the unit is in this mode and actually draws power."""

    _attr_device_class = BinarySensorDeviceClass.RUNNING

    def __init__(self, hass, entry, key: str, mode: str) -> None:
        super().__init__(hass, entry, key)
        self._mode = mode
        self._last_on = 0.0
        self._attr_icon = "mdi:fire" if mode == "heat" else "mdi:snowflake"

    @property
    def is_on(self) -> bool:
        if self.unit.running(self._mode):
            self._last_on = time()
            return True
        return self.unit.pump_mode() == self._mode and time() - self._last_on < _RUN_HOLD_SECONDS


class Online(QlimaEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass, entry) -> None:
        super().__init__(hass, entry, "unit_online")

    @property
    def is_on(self) -> bool | None:
        return self.unit.online()


class OpeningOpen(QlimaEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.OPENING

    def __init__(self, hass, entry) -> None:
        super().__init__(hass, entry, "opening")

    def watched(self) -> list[str]:
        cfg = {**self.entry.data, **self.entry.options}
        return list(cfg.get("opening_sensors", []))

    @property
    def is_on(self) -> bool:
        return any((s := self.hass.states.get(i)) is not None and s.state == "on" for i in self.watched())


class NoPower(QlimaEntity, BinarySensorEntity):
    """The unit is on (heat/cool) but the power meter shows nothing for a long time."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, hass, entry) -> None:
        super().__init__(hass, entry, "no_power")
        self._since: float | None = None

    @property
    def is_on(self) -> bool:
        mode = self.unit.pump_mode()
        power = self.unit.power()
        idle = mode in ("heat", "cool") and power is not None and power < 5
        if not idle:
            self._since = None
            return False
        self._since = self._since or time()
        return time() - self._since >= _NO_POWER_SECONDS
