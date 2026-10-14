"""Price steering, wake-up from the low-effort hold, turbo and the power-cycle guard."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

from homeassistant.components.climate import HVACMode
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.qlima_control.const import DOMAIN
from custom_components.qlima_control.prices import (
    LEVEL_CHEAP,
    LEVEL_EXPENSIVE,
    LEVEL_NORMAL,
    LEVEL_UNKNOWN,
    parse_prices,
    rank_now,
    to_hours,
)

from .test_openings import Clock, _setup as _setup_pump, _tick, clock  # noqa: F401
from .test_unit import _setup, _state, esp_device  # noqa: F401


def _hourly(now, cheap_now: bool = True, hours_back: int = 3, hours_ahead: int = 20):
    """Strømligning-style quarter-hour prices; the current hour is cheapest (or dearest)."""
    start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=hours_back)
    prices = []
    for h in range(hours_back + hours_ahead):
        hour = start + timedelta(hours=h)
        base = 2.0 + (h % 5) * 0.1
        if hour <= now < hour + timedelta(hours=1):
            base = 0.5 if cheap_now else 5.0
        for q in range(4):
            begin = hour + timedelta(minutes=15 * q)
            prices.append({"price": base, "start": begin.isoformat(), "end": (begin + timedelta(minutes=15)).isoformat()})
    return prices


def test_parse_all_price_formats():
    now = dt_util.utcnow()
    stromligning = parse_prices({"prices": _hourly(now)})
    assert len(stromligning) == 23 * 4
    hour = now.replace(minute=0, second=0, microsecond=0)
    nordpool = parse_prices({
        "today": [1.0, 2.0],  # plain list – ignored
        "raw_today": [{"start": hour.isoformat(), "end": (hour + timedelta(hours=1)).isoformat(), "value": 1.5}],
        "raw_tomorrow": [{"start": (hour + timedelta(hours=1)).isoformat(), "end": (hour + timedelta(hours=2)).isoformat(), "value": 1.7}],
    })
    assert [p.price for p in nordpool] == [1.5, 1.7]
    eds = parse_prices({"raw_today": [{"hour": hour.isoformat(), "price": 0.9}]})
    assert eds[0].end - eds[0].start == timedelta(hours=1)
    assert parse_prices({"forecast": [{"start": "nonsense", "price": 1}]}) == []
    assert len(to_hours(stromligning)) == 23
    assert to_hours(stromligning)[0].price == stromligning[0].price


def test_rank_now_levels():
    now = dt_util.utcnow()
    assert rank_now(parse_prices({"prices": _hourly(now, True)}), now).level == LEVEL_CHEAP
    assert rank_now(parse_prices({"prices": _hourly(now, False)}), now).level == LEVEL_EXPENSIVE
    flat = [{"price": 2.0, "start": (now + timedelta(hours=h - 3)).isoformat(), "end": (now + timedelta(hours=h - 2)).isoformat()} for h in range(24)]
    assert rank_now(parse_prices({"prices": flat}), now).level == LEVEL_NORMAL
    assert rank_now(parse_prices({"prices": _hourly(now)[:8]}), now + timedelta(hours=10)).level == LEVEL_UNKNOWN


async def test_price_steering_moves_the_target(hass, esp_device):
    now = dt_util.utcnow()
    hass.states.async_set("sensor.price", "0.5", {"prices": _hourly(now, cheap_now=True)})
    entry = await _setup(hass, esp_device, price_control=True)
    thermostat = hass.data[DOMAIN][entry.entry_id]
    assert _state(hass, "sensor", "price_level").state == "cheap"
    assert thermostat._eff_heat() == 23  # 22 + 1
    assert thermostat._eff_cool() == 24  # 25 - 1 (cool more while cheap)
    reloads = []
    orig = hass.config_entries.async_reload

    async def spy(entry_id):
        reloads.append(entry_id)
        return await orig(entry_id)

    hass.config_entries.async_reload = spy
    number = _state(hass, "number", "price_cheap_offset")
    await hass.services.async_call("number", "set_value", {"entity_id": number.entity_id, "value": 2}, blocking=True)
    await hass.async_block_till_done()
    assert thermostat._eff_heat() == 24
    switch = _state(hass, "switch", "price_control")
    assert switch.state == "on"
    await hass.services.async_call("switch", "turn_off", {"entity_id": switch.entity_id}, blocking=True)
    await hass.async_block_till_done()
    assert thermostat._eff_heat() == 22
    assert _state(hass, "sensor", "price_level").state == "off"
    assert not reloads  # live settings


async def test_expensive_lowers_heat_and_level_is_held(hass, esp_device):
    now = dt_util.utcnow()
    hass.states.async_set("sensor.price", "5", {"prices": _hourly(now, cheap_now=False)})
    entry = await _setup(hass, esp_device, price_control=True)
    thermostat = hass.data[DOMAIN][entry.entry_id]
    price = hass.data[DOMAIN][f"{entry.entry_id}_price"]
    assert price.level == "expensive" and thermostat._eff_heat() == 21
    # The forecast turns cheap at once: the level is held for 30 minutes.
    hass.states.async_set("sensor.price", "0.5", {"prices": _hourly(now, cheap_now=True)})
    price.refresh()
    assert price.level == "expensive"
    with patch("custom_components.qlima_control.smart.dt_util.utcnow", return_value=now + timedelta(minutes=31)):
        hass.states.async_set("sensor.price", "0.5", {"prices": _hourly(now + timedelta(minutes=31), cheap_now=True)})
        price.refresh()
    assert price.level == "cheap"


async def test_wake_power_cycles_an_idling_unit(hass, esp_device):
    hass.states.async_set("switch.shelly", "on")
    entry = await _setup(hass, esp_device, power_switch="switch.shelly", auto_wake=True, wake_minutes=45)
    thermostat = hass.data[DOMAIN][entry.entry_id]
    wake = hass.data[DOMAIN][f"{entry.entry_id}_wake"]
    thermostat._hvac_mode = HVACMode.HEAT
    thermostat._active_mode = HVACMode.HEAT
    thermostat._manual_until = 0
    calls = []

    async def record(call):
        calls.append((call.service, call.data["entity_id"]))

    hass.services.async_register("homeassistant", "turn_off", record)
    hass.services.async_register("homeassistant", "turn_on", record)
    # Warm enough: nothing to do.
    assert wake.check(0) == "on_target"
    hass.states.async_set("sensor.room_a", "20.0", {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.room_b", "20.0", {"unit_of_measurement": "°C"})
    await hass.async_block_till_done()
    assert wake.check(0) == "working"  # 600 W – it is trying
    hass.states.async_set("sensor.power", "90", {"unit_of_measurement": "W"})
    await hass.async_block_till_done()
    t0 = 2_000_000.0
    with patch("custom_components.qlima_control.smart.time", return_value=t0):
        wake._tick(None)
    assert wake.reason == "stalled" and wake.wake_at == t0 + 45 * 60
    assert _state(hass, "sensor", "wake_status").state == "stalled"
    with patch("custom_components.qlima_control.smart.time", return_value=t0 + 30 * 60):
        wake._tick(None)
    assert calls == []
    with patch("custom_components.qlima_control.smart.time", return_value=t0 + 46 * 60), patch(
        "custom_components.qlima_control.power.POWER_CYCLE_OFF_SECONDS", 0
    ):
        wake._tick(None)
        await hass.async_block_till_done()
    assert calls == [("turn_off", "switch.shelly"), ("turn_on", "switch.shelly")]
    assert wake.wakes_today == 1
    assert thermostat._power_guard_until > 0
    # Cooldown: idling again right after does not cycle again.
    with patch("custom_components.qlima_control.smart.time", return_value=t0 + 47 * 60):
        wake._tick(None)
    with patch("custom_components.qlima_control.smart.time", return_value=t0 + 100 * 60):
        wake._tick(None)
        await hass.async_block_till_done()
    assert len(calls) == 2


async def test_wake_is_off_by_default_and_never_when_unit_off(hass, esp_device):
    hass.states.async_set("switch.shelly", "on")
    entry = await _setup(hass, esp_device, power_switch="switch.shelly")
    wake = hass.data[DOMAIN][f"{entry.entry_id}_wake"]
    assert wake.check(0) == "off"
    hass.config_entries.async_update_entry(entry, options={**entry.options, "auto_wake": True})
    await hass.async_block_till_done()
    thermostat = hass.data[DOMAIN][entry.entry_id]
    thermostat._hvac_mode = HVACMode.OFF
    assert wake.check(0) == "not_running"


async def test_turbo_runs_thirty_minutes(hass, esp_device):
    entry = await _setup(hass, esp_device)
    presets = []

    async def set_preset(call):
        presets.append(call.data["preset_mode"])
        state = hass.states.get("climate.qlima_test_varmepumpe")
        hass.states.async_set(state.entity_id, state.state, {**state.attributes, "preset_mode": call.data["preset_mode"]})

    hass.services.async_register("climate", "set_preset_mode", set_preset)
    switch = _state(hass, "switch", "turbo")
    assert switch.state == "off"
    await hass.services.async_call("switch", "turn_on", {"entity_id": switch.entity_id}, blocking=True)
    await hass.async_block_till_done()
    assert presets == ["boost"]
    state = _state(hass, "switch", "turbo")
    assert state.state == "on" and state.attributes["until"]
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=31))
    await hass.async_block_till_done()
    assert presets == ["boost", "none"]
    assert _state(hass, "switch", "turbo").state == "off"


async def test_power_cycle_off_echo_is_not_a_remote_stop(hass, clock):
    pump, entity = await _setup_pump(hass, clock)
    assert entity.extra_state_attributes["active_mode"] == "heat"
    clock.advance(3)  # past our own command echo window
    entity.suppress_manual_detection(270)
    pump.state = "off"  # the unit lost its supply and came back off
    pump.publish()
    await hass.async_block_till_done()
    assert entity.hvac_mode == "heat"  # not mirrored as "switched off on the remote"
    await _tick(hass, entity, clock, 5)
    assert pump.state == "heat"
    assert entity.extra_state_attributes["last_command_reason"] == "mode_repair"
