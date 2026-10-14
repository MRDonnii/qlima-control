"""Send the room temperature to the ESP, which passes it to the unit as Follow Me."""

from __future__ import annotations

from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.util import slugify

from .const import CONF_DEVICE, DOMAIN, SIGNAL_UPDATE
from .unit import QlimaUnit

_LOGGER = logging.getLogger(__name__)
ACTION = "set_room_temperature"
_RESEND = timedelta(seconds=60)


def esphome_action(hass: HomeAssistant, device_id: str | None) -> str | None:
    """The ESPHome action name of the device, e.g. 'qlima_garagen_set_room_temperature'."""
    if not device_id:
        return None
    device = dr.async_get(hass).async_get(device_id)
    if device is None:
        return None
    # HA 2026.10+ has one config entry per device (config_entry_id); older versions a set.
    entry_id = getattr(device, "config_entry_id", None)
    entry_ids = [entry_id] if entry_id else list(device.config_entries)
    for entry_id in entry_ids:
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is not None and entry.domain == "esphome":
            name = entry.data.get("device_name") or entry.title
            return f"{slugify(name)}_{ACTION}"
    return None


class RoomFeeder:
    """Pushes the room temperature on change and every minute.

    While the unit regulates on it, the thermostat steers the value (see
    follow_me_value): it then also goes out when the target or mode changes.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, unit: QlimaUnit) -> None:
        self.hass = hass
        self.entry = entry
        self.unit = unit
        self.action = esphome_action(hass, {**entry.data, **entry.options}.get(CONF_DEVICE))
        self._unsubs: list = []
        self._last: float | None = None

    def start(self) -> None:
        if not self.action:
            return
        self._unsubs.append(
            async_track_state_change_event(self.hass, self.unit.temperature_sensors, self._changed)
        )
        self._unsubs.append(async_track_time_interval(self.hass, self._tick, _RESEND))
        self._unsubs.append(
            async_dispatcher_connect(self.hass, SIGNAL_UPDATE.format(self.entry.entry_id), self._changed)
        )

    def stop(self) -> None:
        while self._unsubs:
            self._unsubs.pop()()

    def _value(self) -> float | None:
        room = self.unit.room_temperature().value
        if room is None:
            return None
        thermostat = self.hass.data.get(DOMAIN, {}).get(self.entry.entry_id)
        return thermostat.follow_me_value(room) if thermostat is not None else room

    @callback
    def _changed(self, _event: Event | None = None) -> None:
        value = self._value()
        if value is not None and value != self._last:
            self._send(value)

    @callback
    def _tick(self, _now) -> None:
        value = self._value()
        if value is not None:
            self._send(value)

    def _send(self, value: float) -> None:
        if not self.hass.services.has_service("esphome", self.action):
            return  # firmware without the action (older) – it reads the sensor itself
        self.hass.async_create_task(self._push(value))

    async def _push(self, value: float) -> None:
        try:
            await self.hass.services.async_call(
                "esphome", self.action, {"temperature": value}, blocking=True
            )
        except HomeAssistantError as err:
            # The ESP is offline or still connecting (typical right after a HA restart); the
            # next change or the one-minute resend delivers the value.
            _LOGGER.debug("Room temperature not sent to %s: %s", self.action, err)
            return
        self._last = value
