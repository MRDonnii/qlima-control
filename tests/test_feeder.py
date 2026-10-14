"""The room-temperature feeder must not log an error while the ESP is still connecting."""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

from homeassistant.exceptions import HomeAssistantError

from custom_components.qlima_control import feeder as q_feeder

ACTION = "qlima_test_set_room_temperature"


def _feeder(hass):
    entry = MagicMock()
    entry.data = {}
    entry.options = {}
    with patch.object(q_feeder, "esphome_action", return_value=ACTION):
        return q_feeder.RoomFeeder(hass, entry, MagicMock())


async def test_offline_esp_is_retried_without_an_error(hass, caplog):
    calls = []
    online = False

    async def service(call):
        calls.append(call.data["temperature"])
        if not online:
            raise HomeAssistantError("Authenticated connection not ready yet")

    hass.services.async_register("esphome", ACTION, service)
    feeder = _feeder(hass)

    with caplog.at_level(logging.WARNING):
        feeder._send(21.5)
        await hass.async_block_till_done()
    assert calls == [21.5]
    assert feeder._last is None  # not delivered, so the next change or tick sends it again
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    online = True
    feeder._send(21.5)
    await hass.async_block_till_done()
    assert calls == [21.5, 21.5]
    assert feeder._last == 21.5


async def test_firmware_without_the_action_sends_nothing(hass):
    feeder = _feeder(hass)
    feeder._send(21.5)
    await hass.async_block_till_done()
    assert feeder._last is None
