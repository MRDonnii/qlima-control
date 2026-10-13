"""Shared base for entities that follow the unit's linked sensors."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_track_state_change_event

from .const import DOMAIN, SIGNAL_UPDATE
from .unit import QlimaUnit


class QlimaEntity(Entity):
    """Belongs to the thermostat's device and refreshes on linked-entity changes."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, key: str) -> None:
        self.hass = hass
        self.entry = entry
        self._attr_translation_key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = {"identifiers": {(DOMAIN, entry.entry_id)}}

    @property
    def unit(self) -> QlimaUnit:
        return self.hass.data[DOMAIN][f"{self.entry.entry_id}_unit"]

    @property
    def thermostat(self):
        return self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id)

    def watched(self) -> list[str]:
        return self.unit.watched()

    async def async_added_to_hass(self) -> None:
        @callback
        def _changed(_event: Event | None = None) -> None:
            self.async_write_ha_state()

        ids = self.watched()
        if ids:
            self.async_on_remove(async_track_state_change_event(self.hass, ids, _changed))
        self.async_on_remove(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE.format(self.entry.entry_id), _changed)
        )


class EspProxy(QlimaEntity):
    """Mirrors one ESP entity (by role) onto the thermostat's device."""

    role: str = ""

    @property
    def target(self) -> str | None:
        return self.unit.entity(self.role)

    def watched(self) -> list[str]:
        return [self.target] if self.target else []

    @property
    def available(self) -> bool:
        state = self.hass.states.get(self.target) if self.target else None
        return state is not None and state.state != "unavailable"
