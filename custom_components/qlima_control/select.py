"""Pass-through of the physical unit's own preset (e.g. eco/comfort)."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.event import async_track_state_change_event

from .const import DOMAIN


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    """Add the unit-preset select when the physical unit has presets."""
    async_add_entities([UnitPresetSelect(hass, entry)])


class UnitPresetSelect(SelectEntity):
    """Choose the heat pump's own preset from the thermostat's device."""

    _attr_has_entity_name = True
    _attr_translation_key = "unit_preset"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self._attr_unique_id = f"{entry.entry_id}_unit_preset"
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.entry_id)}}

    @property
    def _pump(self) -> str:
        return self.hass.data[DOMAIN][f"{self.entry.entry_id}_unit"].heat_pump

    def _attr(self, key: str):
        state = self.hass.states.get(self._pump)
        return state.attributes.get(key) if state is not None else None

    @property
    def available(self) -> bool:
        state = self.hass.states.get(self._pump)
        return (
            state is not None
            and state.state not in ("unavailable", "unknown")
            and bool(self._attr("preset_modes"))
        )

    @property
    def options(self) -> list[str]:
        return list(self._attr("preset_modes") or [])

    @property
    def current_option(self) -> str | None:
        value = self._attr("preset_mode")
        return value if value in self.options else None

    async def async_added_to_hass(self) -> None:
        @callback
        def _changed(_event: Event) -> None:
            self.async_write_ha_state()

        self.async_on_remove(async_track_state_change_event(self.hass, [self._pump], _changed))

    async def async_select_option(self, option: str) -> None:
        await self.hass.services.async_call(
            "climate",
            "set_preset_mode",
            {"entity_id": self._pump, "preset_mode": option},
            blocking=True,
            context=self._context,
        )
