"""Door/window delays as live-adjustable numbers."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant

from .const import (
    CONF_OPENING_MIN_DELAY_MINUTES,
    CONF_OPENING_OFF_DELAY_MINUTES,
    CONF_OPENING_RESUME_DELAY_MINUTES,
    DEFAULT_OPENING_MIN_DELAY_MINUTES,
    DEFAULT_OPENING_OFF_DELAY_MINUTES,
    DEFAULT_OPENING_RESUME_DELAY_MINUTES,
)
from .entity import QlimaEntity

_NUMBERS = (
    ("opening_reduce_after", CONF_OPENING_MIN_DELAY_MINUTES, DEFAULT_OPENING_MIN_DELAY_MINUTES, 0, 180, "mdi:thermometer-chevron-down"),
    ("opening_off_after", CONF_OPENING_OFF_DELAY_MINUTES, DEFAULT_OPENING_OFF_DELAY_MINUTES, 0, 600, "mdi:power-off"),
    ("opening_resume_after", CONF_OPENING_RESUME_DELAY_MINUTES, DEFAULT_OPENING_RESUME_DELAY_MINUTES, 0, 180, "mdi:play-circle-outline"),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    if not {**entry.data, **entry.options}.get("opening_sensors"):
        return
    async_add_entities([OpeningDelay(hass, entry, *spec) for spec in _NUMBERS])


class OpeningDelay(QlimaEntity, NumberEntity):
    """Minutes before the unit is turned down / switched off / resumed."""

    _attr_mode = NumberMode.BOX
    _attr_native_step = 1
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hass, entry, key, conf, default, lo, hi, icon) -> None:
        super().__init__(hass, entry, key)
        self._conf = conf
        self._default = default
        self._attr_native_min_value = lo
        self._attr_native_max_value = hi
        self._attr_icon = icon

    def watched(self) -> list[str]:
        return []

    @property
    def native_value(self) -> float:
        return float({**self.entry.data, **self.entry.options}.get(self._conf, self._default))

    async def async_set_native_value(self, value: float) -> None:
        cfg = {**self.entry.data, **self.entry.options}
        new = {self._conf: int(value)}
        # Keep the order: turn down ≤ switch off.
        if self._conf == CONF_OPENING_MIN_DELAY_MINUTES and value > float(cfg.get(CONF_OPENING_OFF_DELAY_MINUTES, 20)):
            new[CONF_OPENING_OFF_DELAY_MINUTES] = int(value)
        if self._conf == CONF_OPENING_OFF_DELAY_MINUTES and value < float(cfg.get(CONF_OPENING_MIN_DELAY_MINUTES, 5)):
            new[CONF_OPENING_MIN_DELAY_MINUTES] = int(value)
        self.hass.config_entries.async_update_entry(self.entry, options={**self.entry.options, **new})
        self.async_write_ha_state()
