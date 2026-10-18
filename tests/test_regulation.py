"""The unit regulating itself on Follow Me, the unit-preset select, and the unit's service data."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from homeassistant.components.climate import HVACMode
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.qlima_control import select as q_select
from custom_components.qlima_control.const import DOMAIN

from .test_openings import DOOR, SENSOR, Clock, _setup as _setup_pump, _tick, clock  # noqa: F401
from .test_unit import _setup, _state, esp_device  # noqa: F401

FOLLOW_ME = "switch.fake_follow_me"


async def _follow_me(hass, entity, clock, on: bool = True) -> None:
    entity.unit.esp["follow_me"] = FOLLOW_ME
    hass.states.async_set(FOLLOW_ME, "on" if on else "off")
    clock.advance(2)  # past the echo guard of the start commands
    await entity._evaluate("periodic")
    await hass.async_block_till_done()


async def _room(hass, entity, clock, value: float, minutes: float = 30) -> None:
    hass.states.async_set(SENSOR, str(value), {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    await _tick(hass, entity, clock, minutes)


async def test_unit_holds_the_target_instead_of_idling_at_the_minimum(hass, clock):
    pump, entity = await _setup_pump(hass, clock, room_temp=19.0)
    await _follow_me(hass, entity, clock)
    assert entity._unit_regulates()
    assert pump.state == "heat" and pump.target == 22  # room target, no compensation
    # Above target + hysteresis: the thermostat used to park the unit at its minimum.
    await _room(hass, entity, clock, 23.0)
    assert pump.state == "heat" and pump.target == 22
    assert entity.status() == "heating"
    assert ("temperature", 16.0) not in pump.calls


async def test_falls_back_to_the_thermostat_while_follow_me_is_off(hass, clock):
    pump, entity = await _setup_pump(hass, clock, room_temp=19.0)
    await _follow_me(hass, entity, clock, on=False)
    assert not entity._unit_regulates()
    await _room(hass, entity, clock, 23.0)
    assert pump.state == "heat" and pump.target == 17  # neutral idle at the lowest accepted


async def test_target_change_reaches_the_unit_at_once(hass, clock):
    pump, entity = await _setup_pump(hass, clock, room_temp=21.0)
    await _follow_me(hass, entity, clock)
    await entity.async_set_temperature(temperature=20)
    await hass.async_block_till_done()
    assert pump.target == 20


async def test_open_door_turns_down_to_the_minimum(hass, clock):
    pump, entity = await _setup_pump(hass, clock, room_temp=21.0, reduce_to="setpoint")
    await _follow_me(hass, entity, clock)
    assert pump.target == 22
    hass.states.async_set(DOOR, "on")
    await hass.async_block_till_done()
    await _tick(hass, entity, clock, 6)
    assert pump.state == "heat" and pump.target == 17
    hass.states.async_set(DOOR, "off")
    await hass.async_block_till_done()
    await _tick(hass, entity, clock, 11)
    assert pump.state == "heat" and pump.target == 22


async def test_unit_preset_select_offers_what_the_mode_takes(hass, esp_device):
    await _setup(hass, esp_device)
    pump = "climate.qlima_test_varmepumpe"
    attrs = dict(hass.states.get(pump).attributes)
    attrs["preset_modes"] = ["none", "boost", "eco", "sleep", "Freeze Protection"]
    hass.states.async_set(pump, "heat", attrs)
    await hass.async_block_till_done()
    select = _state(hass, "select", "unit_preset")
    entity = next(e for e in hass.data["select"].entities if e.entity_id == select.entity_id)
    assert entity.options == ["none", "boost", "sleep", "Freeze Protection"]  # no Eco in heat
    hass.states.async_set(pump, "cool", attrs)
    assert entity.options == ["none", "boost", "eco", "sleep"]
    hass.states.async_set(pump, "fan_only", attrs)
    assert entity.options == ["none"]
    hass.states.async_set(pump, "off", attrs)
    assert entity.options == ["none"]
    with pytest.raises(ServiceValidationError):
        await entity.async_select_option("boost")
    # The unit answers with its real state: no change means it did not take the preset.
    hass.states.async_set(pump, "heat", attrs)
    calls = []

    async def ignore(call):
        calls.append(call.data["preset_mode"])

    hass.services.async_register("climate", "set_preset_mode", ignore)
    with patch.object(q_select, "_CONFIRM_SECONDS", 1), pytest.raises(HomeAssistantError):
        await entity.async_select_option("boost")
    assert calls == ["boost"]
    assert "boost" not in entity.options  # refused in heat: not offered again
    hass.states.async_set(pump, "cool", attrs)
    assert "boost" in entity.options
    hass.states.async_set(pump, "heat", attrs)

    async def accept(call):
        hass.states.async_set(pump, "heat", {**attrs, "preset_mode": call.data["preset_mode"]})

    hass.services.async_register("climate", "set_preset_mode", accept)
    await entity.async_select_option("sleep")
    assert hass.states.get(pump).attributes["preset_mode"] == "sleep"


async def test_wake_uses_the_compressor_frequency(hass, esp_device):
    hass.states.async_set("switch.shelly", "on")
    entry = await _setup(hass, esp_device, power_switch="switch.shelly", auto_wake=True)
    thermostat = hass.data[DOMAIN][entry.entry_id]
    wake = hass.data[DOMAIN][f"{entry.entry_id}_wake"]
    unit = hass.data[DOMAIN][f"{entry.entry_id}_unit"]
    thermostat._hvac_mode = HVACMode.HEAT
    thermostat._active_mode = HVACMode.HEAT
    thermostat._manual_until = 0
    hass.states.async_set("sensor.room_a", "20.0", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.room_b", "20.0", {"unit_of_measurement": "°C"})
    unit.esp["compressor"] = "sensor.qlima_test_kompressor"
    hass.states.async_set("sensor.qlima_test_kompressor", "20")  # 600 W, but held at the lowest grade
    await hass.async_block_till_done()
    assert wake.check(0) == "stalled"
    hass.states.async_set("sensor.qlima_test_kompressor", "58")
    await hass.async_block_till_done()
    assert wake.check(0) == "working"
    assert unit.working("heat")
    hass.states.async_set("sensor.qlima_test_kompressor", "0")
    assert not unit.working("heat")


async def test_defrost_comes_from_the_unit_when_it_reports_it(hass, esp_device):
    entry = await _setup(hass, esp_device)
    stats = hass.data[DOMAIN][f"{entry.entry_id}_stats"]
    unit = hass.data[DOMAIN][f"{entry.entry_id}_unit"]
    unit.esp["defrost"] = "binary_sensor.qlima_test_afrimning"
    hass.states.async_set("binary_sensor.qlima_test_afrimning", "off")
    before = stats.data.get("defrosts_day", 0)
    stats._detect_defrost("heat")
    assert not stats.defrosting
    hass.states.async_set("binary_sensor.qlima_test_afrimning", "on")
    stats._detect_defrost("heat")
    stats._detect_defrost("heat")
    assert stats.defrosting and stats.data["defrosts_day"] == before + 1
    hass.states.async_set("binary_sensor.qlima_test_afrimning", "off")
    stats._detect_defrost("heat")
    assert not stats.defrosting



async def test_follow_me_is_steered_while_the_unit_regulates(hass, clock):
    """Whole-degree Follow Me steered at 0.1 K: target −1 until the room reaches the
    target, then the target (modulate), target +1 from +0.3 K (off before +0.5) until
    0.2 K below again – with minimum run and off times against short cycling."""
    pump, entity = await _setup_pump(hass, clock, room_temp=22.0)
    await _follow_me(hass, entity, clock)
    assert pump.target == 22

    def check(room, sent, step):
        assert entity.follow_me_value(room) == sent, room
        assert entity.extra_state_attributes["follow_me_step"] == step, room

    check(22.0, 22, "hold")
    check(21.85, 22, "hold")    # only 0.15 K short: no start yet
    check(21.8, 21, "start")    # 0.2 K short: start
    check(21.95, 21, "start")   # full effort until the target is reached
    check(22.0, 22, "hold")     # reached: let it modulate
    check(21.9, 22, "hold")     # dips a little: keeps modulating, no new full push
    check(22.3, 22, "hold")     # +0.3 K but it has run under 20 min: no short cycle …
    check(22.5, 23, "stop")     # … unless it reaches +0.5 K
    check(22.0, 23, "stop")     # stays off while it cools down …
    check(21.8, 23, "stop")     # … 0.2 K below, but off under 10 min: no restart yet
    clock.advance(11)
    check(21.8, 21, "start")    # rested: start again
    check(22.0, 22, "hold")
    clock.advance(21)
    check(22.3, 23, "stop")     # ran 20 min: off at +0.3 K
    check(21.5, 21, "start")    # 0.5 K below: start at once despite the off time
    assert pump.target == 22  # the unit's own target never moves
    # Cooling mirrors it.
    await entity.async_set_hvac_mode("cool")
    await _room(hass, entity, clock, 25.0, minutes=25)
    assert pump.state == "cool" and pump.target == 25
    assert entity.follow_me_value(25.2) == 26  # 0.2 K warm: start cooling
    assert entity.follow_me_value(25.05) == 26  # until the target is reached
    assert entity.follow_me_value(25.0) == 25
    assert entity.follow_me_value(24.7) == 24  # 0.3 K cold: stop


async def test_follow_me_shows_the_whole_shortfall_far_from_the_target(hass, clock):
    """Target 30 with the room at 22.7: the unit must see 22, not 29, or it barely runs."""
    pump, entity = await _setup_pump(hass, clock, room_temp=22.0)
    await _follow_me(hass, entity, clock)
    await entity.async_set_temperature(temperature=30)
    pump.target = 30
    pump.publish()
    await hass.async_block_till_done()
    assert entity.follow_me_value(22.7) == 22
    assert entity.extra_state_attributes["follow_me_step"] == "start"
    assert entity.follow_me_value(27.6) == 27   # still the true shortfall
    assert entity.follow_me_value(29.4) == 29   # within a degree: target −1 as before
    assert entity.follow_me_value(30.0) == 30   # reached: hold
    # Turned down well below the room: the unit hears the whole overshoot and stops.
    await entity.async_set_temperature(temperature=22)
    pump.target = 22
    pump.publish()
    await hass.async_block_till_done()
    assert entity.follow_me_value(23.4) == 24
    assert entity.extra_state_attributes["follow_me_step"] == "stop"
    assert entity.follow_me_value(26.2) == 27   # far above: the true overshoot


async def test_follow_me_is_the_room_when_the_thermostat_regulates(hass, clock):
    pump, entity = await _setup_pump(hass, clock, room_temp=21.0)
    await _follow_me(hass, entity, clock, on=False)
    assert entity.follow_me_value(21.6) == 21.6
    assert entity.extra_state_attributes["follow_me_step"] == "room"
