"""Hours of simulated operation: the real integration against a Midea-like unit and a room.

The unit model is what we have seen on Qlima S 5225/5232:
  - it acts on the Follow Me value it receives and reports it back as its indoor temperature;
  - it stops when that is `stop_at` past its target and starts when `start_at` short (the
    garage S 5225: stops at +2, starts at −1); some units may also shift the value by a hidden
    compensation (`comp`); in between a running compressor modulates, a stopped one stays off;
  - soft start (the compressor ramps up over minutes), at least 3 minutes off;
  - the indoor fan draws a little power even with the compressor stopped.
The controller does not know any of this; it learns the unit's stop and start levels from
the power it measures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import random
from unittest.mock import patch

import pytest

from homeassistant.components.climate import HVACMode

from custom_components.qlima_control import climate as q_climate
from custom_components.qlima_control.const import DOMAIN

from .test_openings import Clock
from .test_unit import _setup, esp_device  # noqa: F401

PUMP = "climate.qlima_test_varmepumpe"
ROOMS = ("sensor.room_a", "sensor.room_b")
POWER = "sensor.power"


@dataclass
class MideaSim:
    mode: str = "heat"
    target: float = 22.0
    comp: int = 0              # hidden: the unit perceives Follow Me − comp (heat) / + comp (cool)
    stop_at: float = 1.0       # stops when it perceives the room this far past its target
    start_at: float = 1.0      # starts when it perceives the room this far short
    defrost_every: int = 0     # minutes of heating between defrosts (0: never)
    own_min_run: int = 0       # minutes it keeps running after a start whatever it is told
    echo_shift: int = 0        # what it adds to the Follow Me value it reports
    fm: float | None = None
    running: bool = False
    freq: float = 0.0          # 0..1 compressor
    off_minutes: float = 99.0
    run_minutes: int = 0
    defrost_left: int = 0
    log: list = field(default_factory=list)

    def perceived(self, room: float) -> float:
        if self.fm is None:
            return room + 2.0  # its own sensor under the ceiling
        return self.fm - self.comp if self.mode == "heat" else self.fm + self.comp

    def step(self, room: float) -> None:
        if self.mode not in ("heat", "cool"):
            self.running, self.freq, self.defrost_left = False, 0.0, 0
            self.off_minutes += 1
            return
        if self.defrost_left:
            self.defrost_left -= 1
            if not self.defrost_left:
                self.freq = 0.1
            return
        seen = self.perceived(room)
        demand = (self.target - seen) if self.mode == "heat" else (seen - self.target)
        if not self.running:
            self.off_minutes += 1
            if demand >= self.start_at and self.off_minutes >= 3:
                self.running, self.freq, self.run_minutes = True, 0.1, 0
        elif demand <= -self.stop_at and self.run_minutes >= self.own_min_run:
            self.running, self.freq, self.off_minutes = False, 0.0, 0
        if self.running:
            want = max(0.15, min(1.0, 0.35 + 0.2 * demand))
            self.freq += max(-0.15, min(0.08, want - self.freq))
            self.run_minutes += 1
            if self.mode == "heat" and self.defrost_every and self.run_minutes >= self.defrost_every:
                self.defrost_left, self.run_minutes = 8, 0

    def reported(self, room: float) -> float:
        """Its indoor temperature as it reports it: the Follow Me value it acts on, when on."""
        if self.mode in ("heat", "cool") and self.fm is not None:
            return self.fm + self.echo_shift
        return round(room + 2.0)

    @property
    def power(self) -> float:
        if self.mode not in ("heat", "cool"):
            return 1.0
        if self.defrost_left:
            return 60.0  # the compressor reverses, the indoor fan stands still
        return 25.0 + (200.0 + 1200.0 * self.freq if self.running else 0.0)

    @property
    def heat_w(self) -> float:
        if self.defrost_left:
            return -400.0
        out = (self.power - 25.0) * 3.2 if self.running else 0.0
        return out if self.mode == "heat" else -out


class Room:
    def __init__(self, temp: float, outdoor: float, loss_w_per_k: float = 110.0, capacity: float = 5.0e6):
        self.temp, self.outdoor, self.h, self.c = temp, outdoor, loss_w_per_k, capacity

    def step(self, heat_w: float, seconds: float = 60.0) -> None:
        self.temp += (heat_w - self.h * (self.temp - self.outdoor)) * seconds / self.c


@pytest.fixture
def sim_clock():
    clock = Clock()
    with patch.object(q_climate, "time", clock), patch.object(q_climate, "_TARGET_CONFIRM_SECONDS", 1), patch(
        "custom_components.qlima_control.stats.time", clock
    ), patch("custom_components.qlima_control.smart.time", clock), patch(
        "custom_components.qlima_control.followme.time", clock
    ):
        yield clock


class Plant:
    """Wires the simulated unit and room to Home Assistant and runs minute by minute."""

    def __init__(self, hass, entity, unit: MideaSim, room: Room, clock: Clock, noise: float = 0.0, lag: float = 0.0):
        self.hass, self.entity, self.unit, self.room, self.clock = hass, entity, unit, room, clock
        self.feeder = hass.data[DOMAIN][f"{entity.entry.entry_id}_feeder"]
        self.stats = hass.data[DOMAIN][f"{entity.entry.entry_id}_stats"]
        self.trace: list[tuple[float, float, bool, float, str]] = []
        self.noise, self.random = noise, random.Random(7)
        self.power_ok = True
        self.ir_ok = True
        self.lag, self.delivered = lag, 0.0  # minutes the heat takes to reach the room sensor
        self.report_ok = True  # the unit's indoor temperature entity is there

    def register(self) -> None:
        async def set_temperature(call):
            self.unit.target = float(call.data["temperature"])
            self.publish(call.context)

        async def set_hvac_mode(call):
            self.unit.mode = call.data["hvac_mode"]
            self.publish(call.context)

        async def set_room(call):
            if self.ir_ok:
                self.unit.fm = float(round(call.data["temperature"]))

        self.hass.services.async_register("climate", "set_temperature", set_temperature)
        self.hass.services.async_register("climate", "set_hvac_mode", set_hvac_mode)
        self.hass.services.async_register("esphome", "qlima_test_set_room_temperature", set_room)

    def publish(self, context=None) -> None:
        old = self.hass.states.get(PUMP)
        attrs = dict(old.attributes) if old else {}
        attrs.update({"temperature": self.unit.target, "current_temperature": round(self.room.temp + 2, 1),
                      "min_temp": 17, "max_temp": 30})
        self.hass.states.async_set(PUMP, self.unit.mode, attrs, context=context)

    async def run(self, minutes: int) -> None:
        for _ in range(minutes):
            self.clock.advance(1)
            self.unit.step(self.room.temp)
            if self.lag:
                self.delivered += (self.unit.heat_w - self.delivered) / self.lag
            else:
                self.delivered = self.unit.heat_w
            self.room.step(self.delivered)
            for sensor in ROOMS:
                # Real room sensors report tenths, some with a little noise.
                reading = round(self.room.temp + self.random.uniform(-self.noise, self.noise), 1)
                self.hass.states.async_set(sensor, f"{reading:.1f}", {"unit_of_measurement": "°C", "device_class": "temperature"})
            power = f"{self.unit.power:.0f}" if self.power_ok else "unavailable"
            self.hass.states.async_set("sensor.qlima_test_inde", f"{self.unit.reported(self.room.temp):.1f}" if self.report_ok else "unavailable")
            self.hass.states.async_set(POWER, power, {"unit_of_measurement": "W"})
            self.publish()
            await self.hass.async_block_till_done()
            self.feeder._tick(None)
            self.stats._tick(None)
            self.entity._periodic_update(None)
            await self.hass.async_block_till_done()
            self.trace.append((self.clock.now, round(self.room.temp, 2), self.unit.running, self.unit.power, self.entity._fm.step))

    def window(self, minutes: int):
        return self.trace[-minutes:]

    def band(self, minutes: int) -> tuple[float, float]:
        temps = [t for _, t, *_ in self.window(minutes)]
        return min(temps), max(temps)

    def starts(self, minutes: int) -> int:
        w = self.window(minutes)
        return sum(1 for a, b in zip(w, w[1:]) if b[2] and not a[2])


async def _plant(hass, esp_device, clock, *, room: float, outdoor: float, comp: int = 0, target: float = 22.0,
                 mode: str = "heat", cool_target: float = 25.0, unit: dict | None = None, noise: float = 0.0,
                 lag: float = 0.0, **extra) -> Plant:
    hass.states.async_set("sensor.qlima_test_ude", str(outdoor))
    for sensor in ROOMS:
        hass.states.async_set(sensor, f"{room:.1f}", {"unit_of_measurement": "°C", "device_class": "temperature"})
    sim = MideaSim(mode=mode, target=target if mode in ("heat", "heat_cool") else cool_target, comp=comp, **(unit or {}))
    if mode == "heat_cool":
        sim.mode = "heat"
    extra.setdefault("opening_sensors", [])
    entry = await _setup(hass, esp_device, heat_target=target, cool_target=cool_target, regulation="unit", **extra)
    entity = hass.data[DOMAIN][entry.entry_id]
    plant = Plant(hass, entity, sim, Room(room, outdoor), clock, noise=noise, lag=lag)
    plant.register()
    plant.publish()
    await hass.async_block_till_done()  # seen inside the start-up grace, not as a remote change
    clock.advance(3)
    if entity.hvac_mode != mode:
        await entity.async_set_hvac_mode(mode)
    await hass.async_block_till_done()
    return plant


def _levels(plant, mode: str = "heat") -> dict:
    return plant.entity._fm.levels[mode]


def _peaks(plant, minutes: int) -> list[float]:
    """The highest room temperature of each off period in the window."""
    peaks, current = [], None
    for _, temp, running, _, _ in plant.window(minutes):
        if not running:
            current = temp if current is None else max(current, temp)
        elif current is not None:
            peaks.append(current)
            current = None
    return peaks


class Notices:
    """Records the persistent notifications the integration creates and dismisses."""

    def __init__(self, monkeypatch):
        from custom_components.qlima_control import learning_notice

        self.shown: dict[str, tuple[str, str]] = {}
        self.log: list[tuple[str, str, str]] = []

        def create(hass, message, title=None, notification_id=None):
            self.shown[notification_id] = (title, message)
            self.log.append(("create", notification_id, title))

        def dismiss(hass, notification_id):
            self.shown.pop(notification_id, None)
            self.log.append(("dismiss", notification_id, ""))

        monkeypatch.setattr(learning_notice.persistent_notification, "async_create", create)
        monkeypatch.setattr(learning_notice.persistent_notification, "async_dismiss", dismiss)

    def created(self, kind: str) -> list[str]:
        return [title for action, nid, title in self.log if action == "create" and nid.startswith(f"qlima_control_{kind}_")]


# ---------------------------------------------------------------- holding the target


@pytest.mark.parametrize("comp", [0, 1, 2])
async def test_heats_up_and_holds_the_target(hass, esp_device, sim_clock, comp):
    """From 18 °C to 22 °C, then hold for hours – whatever the unit adds to Follow Me itself."""
    plant = await _plant(hass, esp_device, sim_clock, room=18.0, outdoor=5.0, comp=comp)
    await plant.run(4 * 60)
    assert plant.room.temp >= 21.7, plant.trace[-1]
    assert _levels(plant)["stop"] == 1 + comp and _levels(plant)["start"] == 1  # learned exactly
    await plant.run(8 * 60)
    low, high = plant.band(6 * 60)
    assert low >= 21.6 and high <= 22.6, (low, high)
    assert plant.starts(6 * 60) <= 10  # no short cycling
    assert any(not running for _, _, running, _, _ in plant.window(6 * 60))  # it really stops


async def test_a_unit_like_the_garage(hass, esp_device, sim_clock):
    """Stops only when told +2, starts at −1 (seen on the garage S 5225 on 2026-10-18)."""
    plant = await _plant(hass, esp_device, sim_clock, room=20.0, outdoor=5.0, unit={"stop_at": 2.0})
    await plant.run(10 * 60)
    assert _levels(plant)["stop"] == 2 and _levels(plant)["start"] == 1
    assert not plant.entity._fm.history[1:], plant.entity._fm.history  # one lesson, nothing more
    low, high = plant.band(6 * 60)
    assert low >= 21.6 and high <= 22.6, (low, high)
    plant.room.outdoor = 12.0  # mild: it has to stop and start
    await plant.run(8 * 60)
    low, high = plant.band(6 * 60)
    assert low >= 21.6 and high <= 22.6, (low, high)
    assert any(not running for _, _, running, _, _ in plant.window(6 * 60))
    assert plant.starts(6 * 60) <= 10


async def test_a_unit_that_starts_late(hass, esp_device, sim_clock):
    """Starts only when told −2: the start level is learned once, then it starts at −0.2 °C."""
    plant = await _plant(hass, esp_device, sim_clock, room=22.0, outdoor=8.0, unit={"start_at": 2.0})
    await plant.run(10 * 60)
    assert _levels(plant)["start"] == 2 and _levels(plant)["stop"] == 1
    low, high = plant.band(5 * 60)
    assert low >= 21.6 and high <= 22.6, (low, high)


async def test_far_below_the_target_the_unit_sees_the_whole_shortfall(hass, esp_device, sim_clock):
    """With learned levels the start is never weaker than the room: 19.3 °C below 22 °C sends 19."""
    plant = await _plant(hass, esp_device, sim_clock, room=19.3, outdoor=0.0, unit={"stop_at": 2.0})
    _levels(plant)["stop"] = _levels(plant)["floor"] = 2
    await plant.run(2)
    assert plant.entity._fm.step == "start" and plant.unit.fm == 19.0


async def test_turned_down_below_the_room_stops_the_compressor(hass, esp_device, sim_clock):
    """2026-10-18: heating hard to 30 °C, then the target set to 22 °C at 23 °C."""
    plant = await _plant(hass, esp_device, sim_clock, room=22.7, outdoor=8.0, comp=1, target=30.0)
    await plant.run(30)
    assert plant.unit.running and plant.unit.power > 900
    await plant.entity.async_set_temperature(temperature=22)
    await plant.hass.async_block_till_done()
    await plant.run(20)
    # Far above the target the room itself is sent: the unit stops at once.
    assert not plant.unit.running, plant.window(20)
    assert next(i for i, row in enumerate(plant.window(20)) if not row[2]) <= 12
    await plant.run(4 * 60)
    assert _levels(plant)["stop"] == 2  # near the target its compensation shows and is learned
    low, high = plant.band(90)
    assert 21.6 <= low and high <= 22.6, (low, high)


async def test_a_unit_that_ignores_follow_me_is_parked_off(hass, esp_device, sim_clock):
    """Beyond anything Follow Me can say: its own target is parked during stops."""
    plant = await _plant(hass, esp_device, sim_clock, room=21.0, outdoor=5.0, comp=7)
    await plant.run(12 * 60)
    lv = _levels(plant)
    assert lv["stop"] == 5 and lv["park"]
    low, high = plant.band(6 * 60)
    assert low >= 21.5 and high <= 22.8, (low, high)
    assert any(not running for _, _, running, _, _ in plant.window(6 * 60))
    targets = set()
    for _ in range(6 * 60):
        await plant.run(1)
        targets.add(plant.unit.target)
    assert targets == {17.0, 22.0}  # parked only while stopped


async def test_half_degree_target(hass, esp_device, sim_clock):
    plant = await _plant(hass, esp_device, sim_clock, room=19.0, outdoor=5.0)
    await plant.entity.async_set_temperature(temperature=21.5)
    await plant.hass.async_block_till_done()
    assert plant.entity.target_temperature == 21.5
    await plant.run(10 * 60)
    low, high = plant.band(6 * 60)
    assert low >= 21.1 and high <= 22.0, (low, high)
    assert plant.unit.target == 22.0  # the unit itself only takes whole degrees


async def test_cooling_mirrors_it(hass, esp_device, sim_clock):
    plant = await _plant(hass, esp_device, sim_clock, room=27.0, outdoor=30.0, comp=1, mode="cool", cool_target=24.0)
    await plant.run(12 * 60)
    assert _levels(plant, "cool")["stop"] == 2 and _levels(plant)["stop"] == 1
    low, high = plant.band(6 * 60)
    assert low >= 23.4 and high <= 24.4, (low, high)


async def test_heat_cool_follows_the_weather(hass, esp_device, sim_clock):
    plant = await _plant(hass, esp_device, sim_clock, room=20.0, outdoor=5.0, mode="heat_cool",
                         target=21.0, cool_target=24.0)
    await plant.run(6 * 60)
    assert plant.unit.mode == "heat"
    low, high = plant.band(3 * 60)
    assert low >= 20.6 and high <= 21.6, (low, high)
    plant.room.outdoor = 34.0
    await plant.run(10 * 60)
    assert plant.unit.mode == "cool"
    low, high = plant.band(3 * 60)
    assert low >= 23.4 and high <= 24.4, (low, high)


async def test_a_unit_that_stops_at_its_target_does_not_short_cycle(hass, esp_device, sim_clock):
    """It stops as soon as it is told the room is on target: the minimum off time still applies."""
    plant = await _plant(hass, esp_device, sim_clock, room=21.0, outdoor=8.0, unit={"stop_at": 0.0})
    await plant.run(12 * 60)
    assert _levels(plant)["stop"] == 1
    starts = [t for (t, *_), (_, _, on, *_) in zip(plant.window(6 * 60), plant.window(6 * 60)[1:]) if on]
    assert plant.starts(6 * 60) <= 14
    # Each restart comes at least the minimum off time after the compressor stopped.
    trace = plant.window(6 * 60)
    off_at = None
    for (t0, _, on0, *_), (t1, _, on1, *_) in zip(trace, trace[1:]):
        if on0 and not on1:
            off_at = t1
        elif on1 and not on0 and off_at is not None:
            assert t1 - off_at >= 10 * 60 - 60, (t1 - off_at) / 60
    low, high = plant.band(6 * 60)
    assert low >= 21.4 and high <= 22.4, (low, high)
    assert starts is not None


# ---------------------------------------------------------------- what it must not learn wrong


async def test_noisy_room_sensor_does_not_short_cycle(hass, esp_device, sim_clock):
    plant = await _plant(hass, esp_device, sim_clock, room=21.0, outdoor=8.0, comp=1, noise=0.15)
    await plant.run(12 * 60)
    assert _levels(plant)["stop"] == 2
    assert plant.starts(6 * 60) <= 8
    low, high = plant.band(6 * 60)
    assert low >= 21.5 and high <= 22.7, (low, high)


async def test_defrosts_do_not_teach_a_wrong_level(hass, esp_device, sim_clock):
    plant = await _plant(hass, esp_device, sim_clock, room=21.0, outdoor=0.0, unit={"defrost_every": 50})
    await plant.run(12 * 60)
    stats = plant.hass.data[DOMAIN][f"{plant.entity.entry.entry_id}_stats"]
    assert stats.data.get("defrosts_day", 0) >= 5
    assert _levels(plant) == {"stop": 1, "start": 1, "floor": 1, "strikes": 0, "park": False}
    low, high = plant.band(6 * 60)
    assert low >= 21.3 and high <= 22.6, (low, high)


async def test_requested_stops_are_no_defrosts(hass, esp_device, sim_clock):
    plant = await _plant(hass, esp_device, sim_clock, room=19.0, outdoor=6.0)
    await plant.run(10 * 60)
    stats = plant.hass.data[DOMAIN][f"{plant.entity.entry.entry_id}_stats"]
    assert stats.data.get("defrosts_day", 0) == 0
    assert plant.starts(10 * 60) >= 2  # it did cycle in that time


async def test_a_cold_day_beyond_its_capacity_learns_nothing_wrong(hass, esp_device, sim_clock):
    """At −25 °C the unit cannot keep up and runs flat out: no level may drift."""
    plant = await _plant(hass, esp_device, sim_clock, room=21.0, outdoor=-25.0, comp=1)
    await plant.run(10 * 60)
    assert plant.unit.running and plant.unit.freq > 0.9
    assert _levels(plant)["stop"] <= 2 and _levels(plant)["start"] == 1
    plant.room.outdoor = 5.0
    await plant.run(10 * 60)
    assert _levels(plant)["stop"] == 2 and _levels(plant)["start"] == 1
    low, high = plant.band(4 * 60)
    assert low >= 21.6 and high <= 22.6, (low, high)


async def test_the_units_own_minimum_run_is_no_failed_stop(hass, esp_device, sim_clock):
    """A small room overshoots fast; the unit runs its own 8 minutes regardless."""
    plant = await _plant(hass, esp_device, sim_clock, room=21.0, outdoor=8.0, unit={"own_min_run": 8})
    plant.room.c = 1.2e6
    await plant.run(8 * 60)
    assert _levels(plant)["stop"] == 1, plant.entity._fm.history


async def test_without_a_power_sensor_nothing_is_learned(hass, esp_device, sim_clock):
    plant = await _plant(hass, esp_device, sim_clock, room=20.0, outdoor=5.0, power_sensor="")
    await plant.run(10 * 60)
    assert _levels(plant)["stop"] == 1 and plant.entity.follow_me_learning()["state"] == "no_meter"
    low, high = plant.band(6 * 60)
    assert low >= 21.6 and high <= 22.6, (low, high)


async def test_a_lost_power_sensor_keeps_what_was_learned(hass, esp_device, sim_clock):
    plant = await _plant(hass, esp_device, sim_clock, room=21.0, outdoor=5.0, comp=2)
    await plant.run(8 * 60)
    assert _levels(plant)["stop"] == 3
    plant.power_ok = False
    await plant.run(6 * 60)
    assert _levels(plant)["stop"] == 3
    low, high = plant.band(4 * 60)
    assert low >= 21.6 and high <= 22.6, (low, high)


async def test_a_fault_code_pauses_the_learning(hass, esp_device, sim_clock):
    plant = await _plant(hass, esp_device, sim_clock, room=21.0, outdoor=5.0, comp=2)
    hass.states.async_set("sensor.qlima_test_fejlkode", "5")
    await plant.run(4 * 60)
    info = plant.entity.follow_me_learning()
    assert info["state"] == "paused" and info["reason"] == "fault"
    assert _levels(plant)["stop"] == 1
    hass.states.async_set("sensor.qlima_test_fejlkode", "0")
    await plant.run(8 * 60)
    assert _levels(plant)["stop"] == 3


async def test_follow_me_that_does_not_arrive_pauses_the_learning(hass, esp_device, sim_clock, monkeypatch):
    """IR blocked: the unit falls back on its own sensor. Seen within minutes, nothing learned wrong."""
    notices = Notices(monkeypatch)
    plant = await _plant(hass, esp_device, sim_clock, room=21.0, outdoor=5.0, comp=1)
    entity = plant.entity
    await plant.run(6 * 60)
    assert entity._fm.echo and _levels(plant)["stop"] == 2
    plant.ir_ok, plant.unit.fm = False, None
    await plant.run(5)
    info = entity.follow_me_learning()
    assert info["state"] == "paused" and info["reason"] in ("follow_me_unconfirmed", "defrost")  # stops at once
    for _ in range(3 * 60):
        await plant.run(1)
        if entity.follow_me_learning()["state"] == "no_follow_me":
            break
    assert entity.follow_me_learning()["state"] == "no_follow_me"  # said once the reports clearly do not follow
    title, message = notices.shown[f"qlima_control_no_echo_{entity.entry.entry_id}"]
    assert "når ikke frem" in title and "IR" in message
    await plant.run(2 * 60)
    assert _levels(plant)["stop"] == 2 and not _levels(plant)["park"]
    plant.ir_ok = True
    await plant.run(5)
    assert entity.follow_me_learning()["state"] != "no_follow_me"
    assert f"qlima_control_no_echo_{entity.entry.entry_id}" not in notices.shown


async def test_an_open_door_pauses_and_it_comes_back(hass, esp_device, sim_clock):
    hass.states.async_set("binary_sensor.door", "off")
    plant = await _plant(hass, esp_device, sim_clock, room=21.5, outdoor=5.0, comp=1,
                         opening_sensors=["binary_sensor.door"])
    await plant.run(5 * 60)
    hass.states.async_set("binary_sensor.door", "on")
    plant.room.h = 400.0  # the cold comes in
    await plant.run(40)
    assert not plant.unit.running
    assert plant.entity.follow_me_learning()["state"] == "paused"
    hass.states.async_set("binary_sensor.door", "off")
    plant.room.h = 110.0
    await plant.run(30)
    assert plant.unit.running and plant.unit.power > 1000  # back up at full power from ~19 °C
    await plant.run(6 * 60)
    assert _levels(plant)["stop"] == 2
    low, high = plant.band(2 * 60)
    assert low >= 21.6 and high <= 22.6, (low, high)


async def test_a_price_offset_moves_the_band(hass, esp_device, sim_clock):
    plant = await _plant(hass, esp_device, sim_clock, room=21.5, outdoor=5.0, comp=1)
    await plant.run(5 * 60)
    price = hass.data[DOMAIN][f"{plant.entity.entry.entry_id}_price"]
    with patch.object(type(price), "offset", lambda self, cool=False: 1):
        await plant.entity.async_price_changed()
        await plant.run(5 * 60)
        low, high = plant.band(3 * 60)
        assert low >= 22.6 and high <= 23.6, (low, high)
    with patch.object(type(price), "offset", lambda self, cool=False: -1):
        await plant.entity.async_price_changed()
        await plant.run(5 * 60)
        low, high = plant.band(3 * 60)
        assert low >= 20.6 and high <= 21.6, (low, high)
    assert _levels(plant)["stop"] == 2


async def test_raising_the_target_during_a_stop_starts_at_once(hass, esp_device, sim_clock):
    plant = await _plant(hass, esp_device, sim_clock, room=21.5, outdoor=5.0)
    await plant.run(4 * 60)
    while plant.entity._fm.step != "stop":
        await plant.run(1)
    await plant.entity.async_set_temperature(temperature=23)
    await hass.async_block_till_done()
    await plant.run(5)
    assert plant.unit.running  # 0.5 °C short or more: no waiting for the minimum off time
    await plant.run(4 * 60)
    low, high = plant.band(2 * 60)
    assert low >= 22.6 and high <= 23.6, (low, high)


async def test_a_remote_control_target_is_held_then_released(hass, esp_device, sim_clock):
    """Someone sets 25 °C on the remote: the unit holds that on the true room for the
    override time, then the thermostat takes its 22 °C back."""
    plant = await _plant(hass, esp_device, sim_clock, room=21.5, outdoor=5.0, comp=1)
    await plant.run(5 * 60)
    plant.unit.target = 25.0
    plant.publish()
    await hass.async_block_till_done()
    assert plant.entity.status() == "manual"
    await plant.run(30)
    assert plant.entity._fm.step == "room" and plant.unit.fm == round(plant.room.temp)
    await plant.run(80)
    assert plant.room.temp > 23.0  # it heats towards the remote's 25 °C
    await plant.run(2 * 60)
    assert plant.unit.target == 22.0 and plant.entity.status() != "manual"
    await plant.run(5 * 60)
    low, high = plant.band(2 * 60)
    assert low >= 21.6 and high <= 22.6, (low, high)
    assert _levels(plant)["stop"] == 2


# ---------------------------------------------------------------- correcting and keeping


async def test_a_too_high_stop_level_comes_down(hass, esp_device, sim_clock):
    """Restored stop level 4 (never needed for a stop) on a unit that needs 1: it stops in hold → down."""
    plant = await _plant(hass, esp_device, sim_clock, room=21.0, outdoor=8.0)
    _levels(plant).update(stop=4, floor=1)
    await plant.run(10 * 60)
    assert _levels(plant)["stop"] == 1
    low, high = plant.band(4 * 60)
    assert low >= 21.6 and high <= 22.6, (low, high)


async def test_a_level_a_stop_needed_takes_two_odd_stops_to_lower(hass, esp_device, sim_clock):
    """Stop level 2 that a stop needed (floor 2) on a unit that needs 1: lowered on the second
    stop in hold – never raised again in between (no flip-flopping)."""
    plant = await _plant(hass, esp_device, sim_clock, room=21.0, outdoor=8.0)
    _levels(plant).update(stop=2, floor=2)
    await plant.run(12 * 60)
    assert _levels(plant)["stop"] == 1
    changes = [(h["check"], h["change"], h["value"]) for h in plant.entity._fm.history]
    assert changes == [("hold", "stop_level", 1)], changes
    low, high = plant.band(4 * 60)
    assert low >= 21.6 and high <= 22.6, (low, high)


async def test_the_learning_is_kept_in_the_state_and_over_a_restart(hass, esp_device, sim_clock):
    from homeassistant.core import State
    from pytest_homeassistant_custom_component.common import mock_restore_cache

    levels = {"heat": {"stop": 2, "start": 1, "floor": 2, "strikes": 0, "park": False},
              "cool": {"stop": 1, "start": 1, "floor": 1, "strikes": 0, "park": False}}
    mock_restore_cache(hass, [State("climate.test", "heat", {
        "follow_me_levels": levels, "follow_me_echo": True,
        "follow_me_history": [{"at": "2026-10-18T12:58:00+00:00", "mode": "heat", "check": "stop",
                               "change": "stop_level", "value": 2, "from": 1}],
        "heat_target_saved": 22, "cool_target_saved": 25,
    })])
    plant = await _plant(hass, esp_device, sim_clock, room=21.0, outdoor=5.0, unit={"stop_at": 2.0})
    assert plant.entity.entity_id == "climate.test"
    assert _levels(plant) == levels["heat"] and plant.entity._fm.echo
    await plant.run(6 * 60)
    assert _levels(plant)["stop"] == 2 and len(plant.entity._fm.history) == 1  # nothing relearned
    attrs = plant.hass.states.get(plant.entity.entity_id).attributes
    assert attrs["follow_me_levels"]["heat"]["stop"] == 2
    low, high = plant.band(4 * 60)
    assert low >= 21.6 and high <= 22.6, (low, high)


async def test_what_1_8_learned_is_carried_over(hass, esp_device, sim_clock):
    """1.8.x stored offset 1, its minimum 1 and start push 1 (the garage): that is stop 2, start 1."""
    from homeassistant.core import State
    from pytest_homeassistant_custom_component.common import mock_restore_cache

    mock_restore_cache(hass, [State("climate.test", "heat", {
        "follow_me_offset_heat": 1, "follow_me_offset_min_heat": 1, "follow_me_start_offset_heat": 1,
        "follow_me_park_heat": False, "follow_me_verified": {"heat": {"stop": True, "start": True}},
        "heat_target_saved": 22, "cool_target_saved": 25,
    })])
    plant = await _plant(hass, esp_device, sim_clock, room=21.0, outdoor=5.0, unit={"stop_at": 2.0})
    assert _levels(plant) == {"stop": 2, "start": 1, "floor": 2, "strikes": 0, "park": False}
    assert _levels(plant, "cool")["stop"] == 1
    assert plant.entity._fm.verified["heat"] == {"stop": True, "start": True}
    await plant.run(6 * 60)
    assert plant.entity._fm.history == []


async def test_the_learning_can_be_reset(hass, esp_device, sim_clock, monkeypatch):
    Notices(monkeypatch)
    plant = await _plant(hass, esp_device, sim_clock, room=21.0, outdoor=5.0, comp=1)
    await plant.run(6 * 60)
    assert _levels(plant)["stop"] == 2 and plant.entity._fm.history
    button = next(s for s in hass.states.async_all("button") if s.entity_id.endswith("reset_follow_me_learning"))
    await hass.services.async_call("button", "press", {"entity_id": button.entity_id}, blocking=True)
    assert _levels(plant)["stop"] == 1 and plant.entity._fm.history == [] and plant.entity._fm.weather["heat"] == {}
    await plant.run(6 * 60)
    assert _levels(plant)["stop"] == 2  # and learned again


# ---------------------------------------------------------------- telling what it does


async def test_the_learning_is_reported(hass, esp_device, sim_clock):
    plant = await _plant(hass, esp_device, sim_clock, room=21.0, outdoor=5.0, comp=1)
    entity = plant.entity
    seen = None
    for _ in range(8 * 60):
        await plant.run(1)
        info = entity.follow_me_learning()
        if seen is None and info["state"] == "checking_stop" and info["elapsed_min"] >= 3:
            seen = info
    assert seen["outcome"] == "stop_level" and seen["outcome_value"] == 2 and seen["stop_level"] == 1
    assert seen["needed_min"] == 6 and 40 <= seen["progress"] <= 70 and seen["remaining_min"] <= 3
    sensor = next(s for s in hass.states.async_all("sensor") if s.entity_id.endswith("follow_me_learning"))
    assert sensor.state == "learned"
    a = sensor.attributes
    assert a["stop_level"] == 2 and a["start_level"] == 1 and a["stop_ok"] and a["start_ok"] and a["follow_me_ok"]
    assert [(h["check"], h["change"], h["value"], h["from"]) for h in a["history"]] == [("stop", "stop_level", 2, 1)]
    assert a["outdoor_band"] == "5..10" and a["start_at"] == 0.2 and a["stop_at"] == 0.3
    await entity.async_set_hvac_mode("off")
    assert entity.follow_me_learning()["state"] == "off"


async def test_a_check_is_announced_and_explained(hass, esp_device, sim_clock, monkeypatch):
    notices = Notices(monkeypatch)
    plant = await _plant(hass, esp_device, sim_clock, room=21.0, outdoor=5.0, comp=1)
    entry_id = plant.entity.entry.entry_id
    while _levels(plant)["stop"] == 1:
        await plant.run(1)
        if plant.entity.follow_me_learning()["state"] == "checking_stop":
            title, message = notices.shown[f"qlima_control_learning_{entry_id}"]
            assert "lærer anlægget at kende" in title
            assert "Hvorfor" in message and "Hvad sker der" in message and "ikke en fejl" in message
            assert "1° over dets mål" in message and "2° over målet" in message
        assert plant.clock.now < 1_000_000 + 8 * 3600
    title, message = notices.shown[f"qlima_control_learning_{entry_id}"]
    assert title.endswith("lært") and "var 1° over" in message and "er 2° over" in message and "Gemt" in message
    assert len(notices.created("learning")) == 2  # one check, one result – nothing else


async def test_everyday_cycles_stay_quiet(hass, esp_device, sim_clock, monkeypatch):
    """A unit that does as told: normal start/stop delays are no learning and give no message."""
    notices = Notices(monkeypatch)
    plant = await _plant(hass, esp_device, sim_clock, room=21.0, outdoor=12.0)
    states = set()
    for _ in range(10 * 60):
        await plant.run(1)
        states.add(plant.entity.follow_me_learning()["state"])
    assert notices.log == []
    assert not any(s.startswith("checking_") for s in states), states
    assert plant.starts(10 * 60) >= 3
    assert plant.entity.follow_me_learning()["state"] == "learned"


# ---------------------------------------------------------------- the weather


async def test_overshoot_is_learned_for_the_weather(hass, esp_device, sim_clock):
    """A slow room (the heat reaches the sensor late): the band at this outdoor temperature narrows."""
    plant = await _plant(hass, esp_device, sim_clock, room=21.0, outdoor=3.0, lag=25)
    plant.room.c = 2.0e6
    await plant.run(5 * 60)
    first_low, first_high = plant.band(4 * 60)
    await plant.run(14 * 60)
    fm = plant.entity._fm
    stats = fm.weather["heat"]["0"]
    assert stats["n_over"] >= 2 and stats["n_under"] >= 2, stats
    start_at, stop_at = fm.thresholds("heat")
    assert start_at < 0.2, (start_at, stop_at)  # it keeps falling after a start here: start sooner
    low, high = plant.band(6 * 60)
    assert low > first_low + 0.15 and high < first_high - 0.15, ((first_low, first_high), (low, high))
    assert low >= 21.55 and high <= 22.3, (low, high)
    info = plant.entity.follow_me_learning()
    assert info["outdoor_band"] == "0..5" and info["stop_at"] == stop_at and info["bands_learned"] == 1


async def test_each_kind_of_weather_is_learned_on_its_own(hass, esp_device, sim_clock):
    """From a mild day into frost: the frost band is learned separately and the target still holds."""
    plant = await _plant(hass, esp_device, sim_clock, room=21.0, outdoor=11.0, lag=10)
    await plant.run(10 * 60)
    plant.room.outdoor = -6.0
    hass.states.async_set("sensor.qlima_test_ude", "-6")
    await plant.run(12 * 60)
    weather = plant.entity._fm.weather["heat"]
    assert "10" in weather and "-10" in weather, weather
    low, high = plant.band(5 * 60)
    assert low >= 21.4 and high <= 22.4, (low, high)


# ---------------------------------------------------------------- review findings (2026-10-18)


async def test_follow_me_that_never_arrives_teaches_nothing(hass, esp_device, sim_clock, monkeypatch):
    """IR blocked from the very start (fresh install): nothing is learned, and it says so."""
    notices = Notices(monkeypatch)
    plant = await _plant(hass, esp_device, sim_clock, room=20.0, outdoor=5.0, comp=1)
    plant.ir_ok = False
    await plant.run(8 * 60)
    assert _levels(plant) == {"stop": 1, "start": 1, "floor": 1, "strikes": 0, "park": False}
    assert plant.entity._fm.history == []
    assert plant.entity.follow_me_learning()["state"] == "no_follow_me"
    assert f"qlima_control_no_echo_{plant.entity.entry.entry_id}" in notices.shown


async def test_follow_me_lost_while_the_unit_keeps_the_last_value_teaches_nothing(hass, esp_device, sim_clock):
    """IR lost mid-run, the unit acting on the last value it got: no level may move."""
    plant = await _plant(hass, esp_device, sim_clock, room=21.0, outdoor=5.0, comp=1)
    await plant.run(6 * 60)
    before = dict(_levels(plant))
    plant.ir_ok = False  # the unit keeps its last Follow Me value
    await plant.run(4 * 60)
    assert _levels(plant) == before, plant.entity._fm.history


async def test_a_power_glitch_does_not_restart_the_minimum_run(hass, esp_device, sim_clock):
    plant = await _plant(hass, esp_device, sim_clock, room=20.0, outdoor=5.0)
    await plant.run(30)
    fm = plant.entity._fm
    assert fm._unit_on and fm._on_since is not None
    on_since = fm._on_since
    plant.power_ok = False
    await plant.run(1)
    plant.power_ok = True
    await plant.run(2)
    assert fm._on_since == on_since


async def test_a_short_unit_dropout_keeps_the_step_and_its_timers(hass, esp_device, sim_clock):
    """The unit unavailable for a minute (an ESPHome reconnect) during a stop: still stop, no early start."""
    plant = await _plant(hass, esp_device, sim_clock, room=21.0, outdoor=8.0)
    await plant.run(3 * 60)
    while plant.entity._fm.step != "stop":
        await plant.run(1)
    await plant.run(2)
    hass.states.async_set(PUMP, "unavailable", {})
    plant.entity._periodic_update(None)
    await hass.async_block_till_done()
    plant.publish()
    plant.entity._periodic_update(None)
    await hass.async_block_till_done()
    assert plant.entity._fm.step == "stop"


async def test_a_shown_check_is_dismissed_when_the_step_moves_on(hass, esp_device, sim_clock, monkeypatch):
    notices = Notices(monkeypatch)
    plant = await _plant(hass, esp_device, sim_clock, room=22.0, outdoor=8.0, unit={"start_at": 2.0})
    nid = f"qlima_control_learning_{plant.entity.entry.entry_id}"
    shown = False
    for _ in range(6 * 60):
        await plant.run(1)
        if nid in notices.shown and "lærer anlægget" in notices.shown[nid][0]:
            shown = True
            plant.room.temp = 22.6  # the sun comes in: no start needed any more
    assert shown
    assert nid not in notices.shown or "lært" in notices.shown[nid][0]


async def test_odd_stops_in_hold_must_come_in_a_row(hass, esp_device, sim_clock):
    """A strike from one odd stop in hold is forgotten after a hold that goes normally."""
    plant = await _plant(hass, esp_device, sim_clock, room=21.0, outdoor=5.0, comp=1)
    await plant.run(6 * 60)
    lv = _levels(plant)
    assert lv["stop"] == 2 and lv["floor"] == 2
    lv["strikes"] = 1
    await plant.run(4 * 60)
    assert lv["strikes"] == 0 and lv["stop"] == 2


async def test_a_held_back_start_shows_as_start_wait(hass, esp_device, sim_clock):
    """The room needs heat but the compressor stopped minutes ago: status and countdown say so."""
    from custom_components.qlima_control.const import COUNTDOWN_START_ALLOWED

    plant = await _plant(hass, esp_device, sim_clock, room=21.0, outdoor=8.0)
    await plant.run(3 * 60)
    while plant.entity._fm.step != "stop" or plant.unit.running:
        await plant.run(1)
    plant.room.temp = 21.75  # needs a start, under FORCE, minutes after the compressor stopped
    await plant.run(1)
    assert plant.entity._fm.step == "stop" and not plant.unit.running
    assert plant.entity.status() == "start_wait"
    assert plant.entity.countdowns()[COUNTDOWN_START_ALLOWED] is not None
    await plant.run(12)
    assert plant.entity._fm.step == "start" and plant.entity.status() != "start_wait"


async def test_without_the_esp_action_follow_me_is_not_steered(hass, esp_device, sim_clock):
    """Firmware without the room-temperature action reads the sensor itself: the room is sent as it is."""
    plant = await _plant(hass, esp_device, sim_clock, room=21.5, outdoor=5.0)
    hass.services.async_remove("esphome", "qlima_test_set_room_temperature")
    await plant.run(5)
    assert plant.entity._fm.step == "room"
    assert plant.entity.follow_me_value(21.46) == 21.46


async def test_the_learning_is_in_its_own_store(hass, esp_device, sim_clock, hass_storage):
    """Kept per config entry, not only in the thermostat's state: a reload brings it back."""
    plant = await _plant(hass, esp_device, sim_clock, room=21.0, outdoor=5.0, comp=1)
    await plant.run(6 * 60)
    assert _levels(plant)["stop"] == 2
    await plant.entity._fm._store.async_save({"levels": plant.entity._fm.levels, "history": plant.entity._fm.history,
                                               "verified": plant.entity._fm.verified, "weather": {}, "echo": True})
    entry_id = plant.entity.entry.entry_id
    stored = hass_storage[f"qlima_control.followme.{entry_id}"]["data"]
    assert stored["levels"]["heat"]["stop"] == 2 and stored["echo"] is True
    assert await hass.config_entries.async_reload(entry_id)
    await hass.async_block_till_done()
    entity = hass.data[DOMAIN][entry_id]
    assert entity._fm.levels["heat"]["stop"] == 2 and entity._fm.echo


def test_cool_texts_say_maximum_and_degrees_follow_the_value_sent():
    from custom_components.qlima_control import learning_notice as n

    cool = {"cool": True, "stop": 5, "told": 5, "old": None, "value": None, "left": 3, "start_at": 0.2, "stop_at": 0.3}
    assert "maksimum" in n._result_text("stop", "park", cool) and "minimum" not in n._result_text("stop", "park", cool)
    ctx = {"cool": False, "room": 20.3, "target": 22.0, "short": 1.7, "sent": 20.0, "told": 2, "start": 1,
           "value": 2, "left": 6, "power": 20}
    text = n._check_text("start", "start_level", ctx)
    assert "rummet er 2° under dets mål (20 °C)" in text


def _runs(plant, minutes: int) -> list[int]:
    """Lengths (min) of the complete compressor runs in the window."""
    runs, start = [], None
    trace = plant.window(minutes)
    for i in range(1, len(trace)):
        was, on = trace[i - 1][2], trace[i][2]
        if on and not was:
            start = i
        elif was and not on and start is not None:
            runs.append(i - start)
            start = None
    return runs


async def test_a_lost_power_meter_keeps_the_minimum_times(hass, esp_device, sim_clock):
    """Hours without a power reading: the steps time the runs, no short cycling."""
    plant = await _plant(hass, esp_device, sim_clock, room=21.0, outdoor=12.0)
    plant.room.c = 2.5e6
    await plant.run(3 * 60)
    plant.power_ok = False
    await plant.run(6 * 60)
    runs = _runs(plant, 6 * 60)
    assert runs and min(runs) >= 15, runs
    assert plant.starts(6 * 60) <= 12


async def test_a_stop_in_hold_taken_for_a_defrost_still_rests(hass, esp_device, sim_clock):
    """At 3 °C a unit that stops at its target looks like it defrosts: its restart still waits the off time."""
    plant = await _plant(hass, esp_device, sim_clock, room=21.0, outdoor=3.0, unit={"stop_at": 0.0})
    plant.room.h = 180.0
    await plant.run(10 * 60)
    trace = plant.window(6 * 60)
    off_at = None
    for (t0, _, on0, *_), (t1, _, on1, *_) in zip(trace, trace[1:]):
        if on0 and not on1:
            off_at = t1
        elif on1 and not on0 and off_at is not None:
            assert t1 - off_at >= 10 * 60 - 60, (t1 - off_at) / 60
    assert _levels(plant)["stop"] == 1


async def test_a_unit_that_reports_follow_me_shifted_still_learns(hass, esp_device, sim_clock):
    """It reports the value it got minus one: that offset is learned from three values, then it learns."""
    plant = await _plant(hass, esp_device, sim_clock, room=20.0, outdoor=5.0, comp=1, unit={"echo_shift": -1})
    await plant.run(10 * 60)
    fm = plant.entity._fm
    assert fm.echo_offset == -1 and fm.echo and not fm.no_echo
    assert _levels(plant)["stop"] == 2
    low, high = plant.band(4 * 60)
    assert low >= 21.6 and high <= 22.6, (low, high)


async def test_without_the_units_indoor_temperature_nothing_is_learned(hass, esp_device, sim_clock):
    """No echo to confirm Follow Me (the entity is gone): it steers, but learns nothing."""
    plant = await _plant(hass, esp_device, sim_clock, room=20.0, outdoor=5.0, comp=1)
    plant.report_ok = False
    await plant.run(6 * 60)
    assert _levels(plant)["stop"] == 1 and plant.entity._fm.history == []
    info = plant.entity.follow_me_learning()
    assert info["state"] == "paused" and info["reason"] == "follow_me_unconfirmed"


async def test_a_unit_dropout_is_no_evidence(hass, esp_device, sim_clock):
    """While the unit is unavailable (steering kept from the cache) nothing is confirmed or checked."""
    plant = await _plant(hass, esp_device, sim_clock, room=21.0, outdoor=8.0)
    await plant.run(3 * 60)
    while plant.entity._fm.step != "stop" or not plant.unit.running:
        await plant.run(1)
        if plant.entity._fm.step == "stop" and not plant.unit.running:
            plant.entity._fm.verified["heat"]["stop"] = False  # wait for a stop seen from the start
    fm = plant.entity._fm
    fm.verified["heat"]["stop"] = False
    hass.states.async_set(PUMP, "unavailable", {})
    await hass.async_block_till_done()
    plant.entity._periodic_update(None)
    await hass.async_block_till_done()
    assert fm.verified["heat"]["stop"] is False and fm._check is None


def test_a_pause_forgets_a_half_seen_echo_problem():
    from types import SimpleNamespace

    from custom_components.qlima_control.followme import FollowMe

    fm = FollowMe.__new__(FollowMe)
    fm._check, fm._track, fm._notice = "stop", {"x": 1}, None
    fm._bad_since, fm._pairs = 123.0, {21: 2}
    fm.hass, fm.entry = None, SimpleNamespace(entry_id="x")
    fm.idle()
    assert fm._bad_since is None and fm._pairs == {} and fm._check is None
