"""The Firmware update entity: the ESP's project version against the newest release."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from homeassistant.exceptions import HomeAssistantError

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
    hass.config_entries.async_update_entry(source, data={"device_name": "qlima-control-a1b2c3"})  # prebuilt image
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


async def test_esp_status_and_diagnostics(hass, esp_device):
    source = next(iter(hass.config_entries.async_entries("esphome")))
    registry = er.async_get(hass)
    for domain, name, object_id in (
        ("sensor", "WiFi signal", "qlima_test_wifi"),
        ("sensor", "Oppetid", "qlima_test_oppetid"),
        ("text_sensor", "IP-adresse", "qlima_test_ip"),
        ("text_sensor", "ESPHome version", "qlima_test_esphome"),
    ):
        platform = "sensor"
        registry.async_get_or_create(
            platform, "esphome", f"{MAC}/0/{domain}/{name}", suggested_object_id=object_id,
            device_id=esp_device.id, config_entry=source,
        )
    hass.states.async_set("sensor.qlima_test_wifi", "-58")
    hass.states.async_set("sensor.qlima_test_oppetid", "7200")
    hass.states.async_set("sensor.qlima_test_ip", "192.0.2.10")
    hass.states.async_set("sensor.qlima_test_esphome", "2026.9.1")
    dr.async_get(hass).async_update_device(esp_device.id, sw_version="1.2.0 (ESPHome 2026.9.1)")
    with patch.object(q_update.FirmwareUpdate, "_check", return_value=None):
        await _setup(hass, esp_device)

    status = _state(hass, "sensor", "esp_status")
    assert status.state == "online"
    assert status.attributes["firmware"] == "1.2.0"
    assert status.attributes["esphome"] == "2026.9.1"
    assert status.attributes["ip"] == "192.0.2.10"
    assert status.attributes["wifi_signal"] == -58
    assert status.attributes["uptime_hours"] == 2.0
    assert status.attributes["mac"] == MAC
    assert _state(hass, "binary_sensor", "esp_connected").state == "on"
    assert _state(hass, "sensor", "esp_wifi_signal").state == "-58.0"
    assert _state(hass, "sensor", "esp_ip_address").state == "192.0.2.10"
    assert float(_state(hass, "sensor", "esp_uptime").state) == 2.0  # shown in hours

    # A newer release: the status says so.
    entity = await _entity(hass)
    entity._latest = "1.3.0"
    hass.data["qlima_control"]["firmware_latest"] = "1.3.0"
    hass.states.async_set("sensor.qlima_test_wifi", "-60")
    await hass.async_block_till_done()
    assert _state(hass, "sensor", "esp_status").state == "update"

    # The unit stops answering, then the ESP drops off.
    hass.states.async_set("binary_sensor.qlima_test_svarer", "off")
    await hass.async_block_till_done()
    assert _state(hass, "sensor", "esp_status").state == "unit_silent"
    climate = hass.states.get("climate.qlima_test_varmepumpe")
    hass.states.async_set("climate.qlima_test_varmepumpe", "unavailable", climate.attributes)
    await hass.async_block_till_done()
    assert _state(hass, "sensor", "esp_status").state == "offline"
    assert _state(hass, "binary_sensor", "esp_connected").state == "off"


async def test_own_yaml_never_gets_the_prebuilt_online_update(hass, esp_device):
    """Adopted in ESPHome: the online update would install an image without its Wi-Fi."""
    source = next(iter(hass.config_entries.async_entries("esphome")))
    hass.config_entries.async_update_entry(source, data={"device_name": "qlima-control-koekken"})
    registry = er.async_get(hass)
    registry.async_get_or_create(
        "update", "esphome", f"{MAC}/0/update/Firmware", suggested_object_id="qlima_test_firmware",
        device_id=esp_device.id, config_entry=source,
    )
    with patch.object(q_update.FirmwareUpdate, "_check", return_value=None):
        await _setup(hass, esp_device)
    entity = await _entity(hass)
    assert not entity.supported_features
    with pytest.raises(HomeAssistantError):
        await entity.async_install(None, False)
    # With the ESPHome dashboard's update entity it installs through that one.
    registry.async_get_or_create(
        "update", "esphome", f"{MAC}-dashboard", suggested_object_id="qlima_test_dashboard",
        device_id=esp_device.id, config_entry=source,
    )
    calls = []

    async def install(call):
        calls.append(call.data["entity_id"])

    hass.services.async_register("update", "install", install)
    await entity.async_install(None, False)
    assert calls == ["update.qlima_test_dashboard"]
