"""Follow Me and beeper, passed through to the ESP in the unit."""

from __future__ import annotations

from datetime import datetime, timezone

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant

from homeassistant.exceptions import ServiceValidationError

from .const import DOMAIN
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
    if unit.price_sensor:
        entities.append(OptionSwitch(hass, entry, "price_control", False, "mdi:cash-sync"))
    if unit.power_switch:
        entities.append(OptionSwitch(hass, entry, "auto_wake", False, "mdi:alarm-light-outline"))
    entities.append(TurboSwitch(hass, entry))
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


class OptionSwitch(QlimaEntity, SwitchEntity):
    """A setting stored in the entry options, applied live without a reload."""

    def __init__(self, hass, entry, key: str, default: bool, icon: str) -> None:
        super().__init__(hass, entry, key)
        self._key = key
        self._default = default
        self._attr_icon = icon

    def watched(self) -> list[str]:
        return []

    @property
    def is_on(self) -> bool:
        return bool({**self.entry.data, **self.entry.options}.get(self._key, self._default))

    async def _set(self, value: bool) -> None:
        self.hass.config_entries.async_update_entry(self.entry, options={**self.entry.options, self._key: value})
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs) -> None:
        await self._set(False)


class OpeningsSwitch(OptionSwitch):
    """Whether doors/windows turn the unit down and off at all."""

    def __init__(self, hass, entry) -> None:
        super().__init__(hass, entry, "openings_enabled", True, "mdi:door-open")


class TurboSwitch(QlimaEntity, SwitchEntity):
    """The unit's boost preset for 30 minutes; off ends it early."""

    _attr_icon = "mdi:rocket-launch-outline"

    def __init__(self, hass, entry) -> None:
        super().__init__(hass, entry, "turbo")

    @property
    def _turbo(self):
        return self.hass.data.get(DOMAIN, {}).get(f"{self.entry.entry_id}_turbo")

    def watched(self) -> list[str]:
        return [self.unit.heat_pump] if self.unit.heat_pump else []

    @property
    def available(self) -> bool:
        turbo = self._turbo
        return turbo is not None and turbo.available()

    @property
    def is_on(self) -> bool:
        turbo = self._turbo
        return bool(turbo and turbo.active)

    @property
    def extra_state_attributes(self):
        turbo = self._turbo
        if turbo is None or not turbo.active:
            return {"until": None}
        return {"until": datetime.fromtimestamp(turbo.until, timezone.utc).isoformat()}

    async def async_turn_on(self, **kwargs) -> None:
        turbo = self._turbo
        if turbo is None:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="turbo_no_boost")
        await turbo.async_start()
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs) -> None:
        turbo = self._turbo
        if turbo is not None:
            await turbo.async_stop()
        self.async_write_ha_state()
