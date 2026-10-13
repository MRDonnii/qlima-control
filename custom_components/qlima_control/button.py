"""Release (thermostat) and the unit's IR/UART buttons."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .entity import EspProxy, QlimaEntity

_PROXIES = (
    ("swing_toggle", "mdi:arrow-up-down", None),
    ("swing_step", "mdi:arrow-up-down-bold-outline", None),
    ("display", "mdi:monitor", None),
    ("filter_reset", "mdi:air-filter", EntityCategory.CONFIG),
)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities):
    unit = hass.data[DOMAIN][f"{entry.entry_id}_unit"]
    entities: list[ButtonEntity] = [ReleaseButton(hass, entry)]
    entities += [ProxyButton(hass, entry, role, icon, cat) for role, icon, cat in _PROXIES if unit.entity(role)]
    if unit.power_switch:
        entities.append(PowerCycleButton(hass, entry))
    async_add_entities(entities)


class ReleaseButton(QlimaEntity, ButtonEntity):
    """Clear manual override, door/window pause and start waits."""

    _attr_icon = "mdi:play-circle-outline"

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, "release")

    def watched(self) -> list[str]:
        return []

    async def async_press(self) -> None:
        if self.thermostat is not None:
            await self.thermostat.async_release()


class ProxyButton(EspProxy, ButtonEntity):
    def __init__(self, hass, entry, role, icon, category) -> None:
        super().__init__(hass, entry, role)
        self.role = role
        self._attr_icon = icon
        self._attr_entity_category = category

    async def async_press(self) -> None:
        await self.hass.services.async_call("button", "press", {"entity_id": self.target}, blocking=True)


class PowerCycleButton(QlimaEntity, ButtonEntity):
    """Cut the unit's supply for 30 s and switch it back on."""

    _attr_icon = "mdi:restart-alert"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, entry, "power_cycle")

    def watched(self) -> list[str]:
        return []

    async def async_press(self) -> None:
        power = self.hass.data[DOMAIN].get(f"{self.entry.entry_id}_power")
        if power is not None:
            self.hass.async_create_task(power.async_power_cycle("button"))
