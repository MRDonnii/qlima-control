"""Follow Me and beeper, passed through to the ESP in the unit."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from .entity import EspProxy, QlimaEntity


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
    if unit.power_switch:
        entities.append(MainsSwitch(hass, entry))
    if {**entry.data, **entry.options}.get("opening_sensors"):
        entities.append(OpeningsSwitch(hass, entry))
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


class MainsSwitch(EspProxy, SwitchEntity):
    """The relay that feeds the unit (e.g. a Shelly)."""

    _attr_icon = "mdi:power-plug"

    def __init__(self, hass, entry) -> None:
        super().__init__(hass, entry, "mains")

    @property
    def target(self) -> str | None:
        return self.unit.power_switch

    @property
    def is_on(self) -> bool | None:
        state = self.hass.states.get(self.target) if self.target else None
        return None if state is None else state.state == "on"

    async def async_turn_on(self, **kwargs) -> None:
        await self.hass.services.async_call("homeassistant", "turn_on", {"entity_id": self.target}, blocking=True)

    async def async_turn_off(self, **kwargs) -> None:
        await self.hass.services.async_call("homeassistant", "turn_off", {"entity_id": self.target}, blocking=True)


class OpeningsSwitch(QlimaEntity, SwitchEntity):
    """Whether doors/windows turn the unit down and off at all."""

    _attr_icon = "mdi:door-open"

    def __init__(self, hass, entry) -> None:
        super().__init__(hass, entry, "openings_enabled")

    def watched(self) -> list[str]:
        return []

    @property
    def is_on(self) -> bool:
        return bool({**self.entry.data, **self.entry.options}.get("openings_enabled", True))

    async def _set(self, value: bool) -> None:
        self.hass.config_entries.async_update_entry(self.entry, options={**self.entry.options, "openings_enabled": value})
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._set(False)
