"""ESP lookup, linked sensors, derived values, proxies and the config flow."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant import config_entries
from homeassistant.helpers import device_registry as dr, entity_registry as er

from custom_components.qlima_control import climate as q_climate
from custom_components.qlima_control.const import DOMAIN
from custom_components.qlima_control.unit import cop_model

MAC = "AA:BB:CC:DD:EE:FF"
ESP = {
    "climate": ("climate", "Varmepumpe", "climate.qlima_test_varmepumpe"),
    "swing_toggle": ("button", "Vip auto til⁄fra", "button.qlima_test_vip"),
    "swing_step": ("button", "Lamel ét trin", "button.qlima_test_lamel"),
    "display": ("button", "Display", "button.qlima_test_display"),
    "filter_reset": ("button", "Nulstil filtertæller", "button.qlima_test_filter_reset"),
    "follow_me": ("switch", "Follow Me", "switch.qlima_test_follow_me"),
    "beep": ("switch", "Bip", "switch.qlima_test_bip"),
    "outdoor": ("sensor", "Udetemperatur", "sensor.qlima_test_ude"),
    "unit_temp": ("sensor", "Indetemperatur anlæg", "sensor.qlima_test_inde"),
    "filter_hours": ("sensor", "Driftstimer siden filterrens", "sensor.qlima_test_filter"),
    "online": ("binary_sensor", "Anlæg svarer", "binary_sensor.qlima_test_svarer"),
}


@pytest.fixture
def esp_device(hass):
    """An ESPHome device with the Qlima firmware's entities in the registries."""
    source = MockConfigEntry(domain="esphome", title="qlima-test")
    source.add_to_hass(hass)
    device = dr.async_get(hass).async_get_or_create(
        config_entry_id=source.entry_id, connections={(dr.CONNECTION_NETWORK_MAC, MAC)}, name="Qlima Test"
    )
    registry = er.async_get(hass)
    for domain, name, entity_id in ESP.values():
        registry.async_get_or_create(
            domain, "esphome", f"{MAC}/0/{domain}/{name}", suggested_object_id=entity_id.split(".", 1)[1],
            device_id=device.id, config_entry=source,
        )
    climate_attrs = {"min_temp": 17, "max_temp": 30, "temperature": 22, "current_temperature": 22,
                     "hvac_modes": ["off", "heat", "cool", "dry", "fan_only", "heat_cool"],
                     "preset_modes": ["none", "eco", "boost"], "preset_mode": "none"}
    hass.states.async_set("climate.qlima_test_varmepumpe", "heat", climate_attrs)
    for _, _, entity_id in ESP.values():
        if entity_id.startswith(("button.",)):
            hass.states.async_set(entity_id, "unknown")
    hass.states.async_set("switch.qlima_test_follow_me", "on")
    hass.states.async_set("switch.qlima_test_bip", "off")
    hass.states.async_set("sensor.qlima_test_ude", "7")
    hass.states.async_set("sensor.qlima_test_inde", "26")
    hass.states.async_set("sensor.qlima_test_filter", "120")
    hass.states.async_set("binary_sensor.qlima_test_svarer", "on")
    hass.states.async_set("sensor.room_a", "21.0", {"unit_of_measurement": "°C", "device_class": "temperature"})
    hass.states.async_set("sensor.room_b", "22.0", {"unit_of_measurement": "°C", "device_class": "temperature"})
    hass.states.async_set("sensor.hum", "48", {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.power", "600", {"unit_of_measurement": "W"})
    hass.states.async_set("sensor.price", "2.40")
    hass.states.async_set("binary_sensor.door", "off")
    return device


async def _setup(hass, device, **extra):
    data = {
        "name": "Test", "device_id": device.id, "heat_pump": "climate.qlima_test_varmepumpe", "model": "s5225",
        "temperature_sensors": ["sensor.room_a", "sensor.room_b"], "temperature_mode": "mean",
        "humidity_sensors": ["sensor.hum"], "power_sensor": "sensor.power", "price_sensor": "sensor.price",
        "opening_sensors": ["binary_sensor.door"], "heat_target": 22, "cool_target": 25,
        **extra,
    }
    entry = MockConfigEntry(domain=DOMAIN, title="Test", data=data)
    entry.add_to_hass(hass)
    with patch.object(q_climate, "_TARGET_CONFIRM_SECONDS", 1):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    return entry


def _state(hass, domain: str, key: str):
    registry = er.async_get(hass)
    for entry in registry.entities.values():
        if entry.platform == DOMAIN and entry.domain == domain and entry.unique_id.endswith("_" + key):
            return hass.states.get(entry.entity_id)
    raise AssertionError(f"no {domain} {key}")


def test_cop_model_shape():
    mild = cop_model(10, 22, 21, 400, 1200)
    cold = cop_model(-10, 22, 21, 400, 1200)
    full = cop_model(10, 22, 21, 1200, 1200)
    assert 3.5 < mild < 5.5
    assert cold < mild and full < mild
    assert cop_model(None, 22, 21, 400, 1200) is None
    # The unit's ceiling sensor is capped at room + 3 K.
    assert cop_model(7, 30, 21, 400, 1200) == cop_model(7, 24, 21, 400, 1200)


async def test_esp_lookup_and_measurements(hass, esp_device):
    entry = await _setup(hass, esp_device)
    unit = hass.data[DOMAIN][f"{entry.entry_id}_unit"]
    assert unit.heat_pump == "climate.qlima_test_varmepumpe"
    assert unit.entity("follow_me") == "switch.qlima_test_follow_me"
    assert _state(hass, "sensor", "room_temperature").state == "21.5"
    assert _state(hass, "sensor", "room_humidity").state == "48.0"
    assert _state(hass, "sensor", "outdoor_temperature").state == "7.0"
    assert float(_state(hass, "sensor", "cop").state) > 2
    heat = float(_state(hass, "sensor", "heat_output").state)
    assert 1500 < heat < 3500
    assert float(_state(hass, "sensor", "heat_price").state) == pytest.approx(2.40 / unit.cop(), abs=0.002)
    assert _state(hass, "binary_sensor", "heating").state == "on"
    assert _state(hass, "binary_sensor", "unit_online").state == "on"
    thermostat = hass.data[DOMAIN][entry.entry_id]
    assert thermostat.current_temperature == 21.5
    assert thermostat.current_humidity == 48


async def test_room_mode_min_and_sensor_change(hass, esp_device):
    entry = await _setup(hass, esp_device, temperature_mode="min")
    assert _state(hass, "sensor", "room_temperature").state == "21.0"
    hass.states.async_set("sensor.room_a", "unavailable")
    await hass.async_block_till_done()
    assert _state(hass, "sensor", "room_temperature").state == "22.0"


async def test_heating_off_without_power(hass, esp_device):
    await _setup(hass, esp_device)
    hass.states.async_set("sensor.power", "15")
    with patch("custom_components.qlima_control.binary_sensor.time", return_value=10**10):
        hass.states.async_set("sensor.power", "14")
        await hass.async_block_till_done()
    assert _state(hass, "binary_sensor", "heating").state == "off"


async def test_proxies_call_the_esp(hass, esp_device):
    await _setup(hass, esp_device)
    calls = []

    async def record(call):
        calls.append((call.domain, call.service, call.data["entity_id"]))

    proxies = [hass.data["button"].get_entity(_state(hass, "button", k).entity_id)
               for k in ("swing_toggle", "swing_step", "display", "filter_reset")]
    follow = hass.data["switch"].get_entity(_state(hass, "switch", "follow_me").entity_id)
    hass.services.async_register("button", "press", record)
    hass.services.async_register("switch", "turn_off", record)
    for proxy in proxies:
        await proxy.async_press()
    await follow.async_turn_off()
    targets = [c[2] for c in calls]
    for expected in ("button.qlima_test_vip", "button.qlima_test_lamel", "button.qlima_test_display",
                     "button.qlima_test_filter_reset", "switch.qlima_test_follow_me"):
        assert expected in targets, (expected, targets)
    assert _state(hass, "switch", "follow_me").state == "on"


async def test_config_flow_creates_entry(hass, esp_device):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] == "form"
    user_input = {
        "name": "Garagen", "device_id": esp_device.id, "model": "s5225",
        "sensors": {"temperature_sensors": ["sensor.room_a"], "temperature_mode": "mean", "power_sensor": "sensor.power"},
        "openings": {"opening_sensors": ["binary_sensor.door"], "opening_min_delay_minutes": 5,
                     "opening_off_delay_minutes": 20, "opening_resume_delay_minutes": 10, "opening_reduce_to": "setpoint"},
        "control": {"heat_target": 22, "cool_target": 25, "start_hysteresis": 0.1, "hysteresis": 0.5,
                    "min_run_minutes": 20, "min_off_minutes": 10, "manual_override_minutes": 120, "setpoint_compensation": False},
        "advanced": {"filter_alarm_hours": 500, "running_power_w": 120, "sensor_stable_seconds": 120, "sensor_loss_grace_seconds": 300},
    }
    with patch("custom_components.qlima_control.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)
    assert result["type"] == "create_entry", result
    data = result["data"]
    assert data["heat_pump"] == "climate.qlima_test_varmepumpe"
    assert data["temperature_sensors"] == ["sensor.room_a"]
    assert data["min_run_minutes"] == 20 and isinstance(data["min_run_minutes"], int)


async def test_config_flow_rejects_device_without_climate(hass, esp_device):
    other = dr.async_get(hass).async_get_or_create(
        config_entry_id=next(iter(hass.config_entries.async_entries("esphome"))).entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, "11:22:33:44:55:66")}, name="Other",
    )
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        "name": "X", "device_id": other.id, "model": "s5225",
        "sensors": {"temperature_sensors": ["sensor.room_a"], "temperature_mode": "mean"},
        "openings": {"opening_min_delay_minutes": 5, "opening_off_delay_minutes": 20, "opening_resume_delay_minutes": 10, "opening_reduce_to": "setpoint"},
        "control": {"heat_target": 22, "cool_target": 25, "start_hysteresis": 0.1, "hysteresis": 0.5, "min_run_minutes": 20, "min_off_minutes": 10, "manual_override_minutes": 120, "setpoint_compensation": False},
        "advanced": {"filter_alarm_hours": 500, "running_power_w": 120, "sensor_stable_seconds": 120, "sensor_loss_grace_seconds": 300},
    })
    assert result["type"] == "form" and result["errors"] == {"device_id": "no_climate"}


async def test_room_temperature_is_pushed_to_the_esp(hass, esp_device):
    entry_esphome = next(iter(hass.config_entries.async_entries("esphome")))
    hass.config_entries.async_update_entry(entry_esphome, data={"device_name": "qlima-test"})
    sent = []

    async def action(call):
        sent.append(call.data["temperature"])

    hass.services.async_register("esphome", "qlima_test_set_room_temperature", action)
    await _setup(hass, esp_device)
    hass.states.async_set("sensor.room_a", "20.0", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    assert sent and sent[-1] == 21.0


async def test_weighted_room_temperature(hass, esp_device):
    await _setup(hass, esp_device, temperature_mode="weighted",
                 temperature_weights={"sensor.room_a": 75, "sensor.room_b": 25})
    assert _state(hass, "sensor", "room_temperature").state == "21.25"
    hass.states.async_set("sensor.room_a", "unavailable")
    await hass.async_block_till_done()
    assert _state(hass, "sensor", "room_temperature").state == "22.0"


async def test_config_flow_weights_step(hass, esp_device):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {
        "name": "Stuen", "device_id": esp_device.id, "model": "s5232",
        "sensors": {"temperature_sensors": ["sensor.room_a", "sensor.room_b"], "temperature_mode": "weighted"},
        "openings": {"opening_min_delay_minutes": 5, "opening_off_delay_minutes": 20, "opening_resume_delay_minutes": 10, "opening_reduce_to": "setpoint"},
        "control": {"heat_target": 22, "cool_target": 25, "start_hysteresis": 0.1, "hysteresis": 0.5, "min_run_minutes": 20, "min_off_minutes": 10, "manual_override_minutes": 120, "setpoint_compensation": False},
        "advanced": {"filter_alarm_hours": 500, "running_power_w": 120, "sensor_stable_seconds": 120, "sensor_loss_grace_seconds": 300},
    })
    assert result["type"] == "form" and result["step_id"] == "weights"
    with patch("custom_components.qlima_control.async_setup_entry", return_value=True):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"sensor.room_a": 60, "sensor.room_b": 40})
    assert result["type"] == "create_entry"
    assert result["data"]["temperature_weights"] == {"sensor.room_a": 60.0, "sensor.room_b": 40.0}


async def test_new_thermostat_takes_over_running_mode(hass, esp_device):
    entry = await _setup(hass, esp_device)
    assert hass.data[DOMAIN][entry.entry_id].hvac_mode == "heat"
