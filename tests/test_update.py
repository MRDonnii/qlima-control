"""The Firmware update entity: the ESP's project version against the newest release."""

from __future__ import annotations

from unittest.mock import patch

from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.qlima_control import update as q_update
from custom_components.qlima_control.update import firmware_version

from .test_unit import MAC, _setup, _state, esp_device  # noqa: F401


def test_firmware_version_from_sw_version():
    assert firmware_version("1.2.0 (ESPHome 2026.9.1)") == "1.2.0"
    assert firmware_version("ESPHome 2026.9.1") is None  # no project version
    assert firmware_version(None) is None


async def _entity(hass):
    return next(e for e in hass.data["update"].entities if e.unique_id.endswith("_firmware"))


async def test_firmware_shows_installed_and_newest(hass, esp_device):
    dr.async_get(hass).async_update_device(esp_device.id, sw_version="1.1.0 (ESPHome 2026.9.1)")
    with patch.object(q_update.FirmwareUpdate, "_check", return_value=None):
        await _setup(hass, esp_device)
    entity = await _entity(hass)
    state = _state(hass, "update", "firmware")
    assert state.attributes["installed_version"] == "1.1.0"
    assert state.state == "off"  # nothing known about releases yet
    assert not state.attributes["supported_features"]  # no ESP update entity to install with

    entity._latest = "1.2.0"
    entity.async_write_ha_state()
    state = _state(hass, "update", "firmware")
    assert state.state == "on" and state.attributes["latest_version"] == "1.2.0"

    # The ESP reconnects with the new firmware: up to date.
    dr.async_get(hass).async_update_device(esp_device.id, sw_version="1.2.0 (ESPHome 2026.9.1)")
    await hass.async_block_till_done()
    state = _state(hass, "update", "firmware")
    assert state.state == "off" and state.attributes["installed_version"] == "1.2.0"

    # A local build newer than the release is not an update.
    dr.async_get(hass).async_update_device(esp_device.id, sw_version="1.3.0 (ESPHome 2026.9.1)")
    await hass.async_block_till_done()
    assert _state(hass, "update", "firmware").state == "off"


async def test_firmware_installs_through_the_esp(hass, esp_device):
    source = next(iter(hass.config_entries.async_entries("esphome")))
    er.async_get(hass).async_get_or_create(
        "update", "esphome", f"{MAC}/0/update/Firmware", suggested_object_id="qlima_test_firmware",
        device_id=esp_device.id, config_entry=source,
    )
    hass.states.async_set("update.qlima_test_firmware", "on", {"in_progress": False})
    dr.async_get(hass).async_update_device(esp_device.id, sw_version="1.1.0 (ESPHome 2026.9.1)")
    calls = []

    async def install(call):
        calls.append(call.data["entity_id"])

    with patch.object(q_update.FirmwareUpdate, "_check", return_value=None):
        await _setup(hass, esp_device)
    hass.services.async_register("update", "install", install)  # stands in for the ESP's own
    entity = await _entity(hass)
    assert entity.supported_features
    await entity.async_install(None, False)
    assert calls == ["update.qlima_test_firmware"]
