"""Follow Me and beeper, passed through to the ESP in the unit."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from .entity import EspProxy


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    unit = hass.data["qlima_control"][f"{entry.entry_id}_unit"]
    entities = [
        ProxySwitch(hass, entry, role, icon, category)
        for role, icon, category in (
            ("follow_me", "mdi:home-thermometer", None),
            ("beep", "mdi:volume-high", EntityCategory.CONFIG),
        )
        if unit.entity(role)
    ]
    async_add_entities(entities)


class ProxySwitch(EspProxy, SwitchEntity):
    def __init__(self, hass, entry, role, icon, category) -> None:
        super().__init__(hass, entry, role)
        self.role = role
        self._attr_icon = icon
        self._attr_entity_category = category

    @property
    def is_on(self) -> bool | None:
        state = self.hass.states.get(self.target) if self.target else None
        return None if state is None else state.state == "on"

    async def async_turn_on(self, **kwargs) -> None:
        await self.hass.services.async_call("switch", "turn_on", {"entity_id": self.target}, blocking=True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.hass.services.async_call("switch", "turn_off", {"entity_id": self.target}, blocking=True)
