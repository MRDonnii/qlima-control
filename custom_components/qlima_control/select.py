"""Pass-through of the physical unit's own preset (e.g. eco/comfort)."""

from __future__ import annotations

import asyncio

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.event import async_track_state_change_event

from .const import DOMAIN

# Unit modes in which each Midea preset is offered. Eco is cool/auto only: the Qlima
# S 5232 answers an Eco request in heat with Eco off (tested 2026-10-14). Presets not
# listed are offered in every mode except off; a preset the unit refuses in a mode is
# not offered in that mode again (until Home Assistant restarts).
_PRESET_MODES: dict[str, tuple[str, ...]] = {
    "eco": ("cool", "heat_cool"),
    "boost": ("cool", "heat"),
    "sleep": ("cool", "heat_cool", "heat"),
    "Freeze Protection": ("heat",),
}
_NONE = "none"
_CONFIRM_SECONDS = 10


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
        self._refused: set[tuple[str, str]] = set()
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

    def _all_options(self) -> list[str]:
        return list(self._attr("preset_modes") or [])

    def _mode(self) -> str | None:
        state = self.hass.states.get(self._pump)
        return state.state if state is not None else None

    @property
    def options(self) -> list[str]:
        """Only the presets the unit takes in its current mode (none while it is off)."""
        options = self._all_options()
        mode = self._mode()
        if mode in (None, "off", "unavailable", "unknown"):
            return [o for o in options if o == _NONE] or options[:1]
        return [
            o
            for o in options
            if o == _NONE or (mode in _PRESET_MODES.get(o, (mode,)) and (o, mode) not in self._refused)
        ]

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
        if option not in self.options:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="preset_not_in_mode",
                translation_placeholders={"preset": option, "mode": str(self._mode())},
            )
        await self.hass.services.async_call(
            "climate",
            "set_preset_mode",
            {"entity_id": self._pump, "preset_mode": option},
            blocking=True,
            context=self._context,
        )
        # The unit answers with its real state: tell the user when it did not take the preset.
        mode = self._mode()
        for _ in range(_CONFIRM_SECONDS * 2):
            if self._attr("preset_mode") == option:
                return
            await asyncio.sleep(0.5)
        if option != _NONE and mode:
            self._refused.add((option, mode))
            self.async_write_ha_state()
        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="preset_not_accepted",
            translation_placeholders={"preset": option},
        )
