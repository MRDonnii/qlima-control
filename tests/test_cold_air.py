"""1.11 cold-air fixes: fan lowered while the compressor stands still, and the defrost count."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from custom_components.qlima_control.fan import FAN_IDLE, FAN_RESUME, FanGuard, lowest_fan
from custom_components.qlima_control.stats import QlimaStats

MODES = ["auto", "low", "medium", "high", "SILENT"]


def _run(guard, now, **kw):
    args = dict(mode="heat", fan="auto", modes=MODES, compressor=True, power=900.0, told_to_stop=False)
    args.update(kw)
    return guard.decide(now, **args)


def test_lowest_fan_prefers_low():
    assert lowest_fan(MODES) == "low"
    assert lowest_fan(["auto", "SILENT"]) == "SILENT"
    assert lowest_fan(["auto", "high"]) is None


def test_fan_low_while_stopped_and_back_when_it_heats():
    g = FanGuard()
    assert _run(g, 0) is None
    # Compressor stops, the fan keeps blowing (14 W): not before FAN_IDLE (start-up dips are shorter).
    assert _run(g, 10, compressor=False, power=14.0) is None
    assert _run(g, 10 + FAN_IDLE - 1, compressor=False, power=14.0) is None
    action = _run(g, 10 + FAN_IDLE, compressor=False, power=14.0)
    assert action.fan_mode == "low" and g.saved == "auto" and g.lowered
    assert _run(g, 400, fan="low", compressor=False, power=14.0) is None  # nothing repeated
    # Heats again: the user's speed only once it has run FAN_RESUME.
    assert _run(g, 500, fan="low") is None
    action = _run(g, 500 + FAN_RESUME, fan="low")
    assert action.fan_mode == "auto" and g.saved is None and not g.lowered


def test_no_fan_command_in_a_real_defrost_or_when_off():
    g = FanGuard()
    _run(g, 0)
    # Defrost: the unit stops its own fan (standby power) – nothing is sent.
    assert _run(g, 10 + FAN_IDLE * 2, compressor=False, power=6.0) is None
    # Switched off: never a fan command (it could switch the unit on).
    assert _run(g, 1000, mode="off", compressor=False, power=6.0) is None
    # A requested stop lowers it whatever the power reads.
    g2 = FanGuard()
    _run(g2, 0, compressor=False, power=6.0, told_to_stop=True)
    assert _run(g2, FAN_IDLE, compressor=False, power=6.0, told_to_stop=True).fan_mode == "low"


def test_the_users_own_choice_wins():
    g = FanGuard()
    _run(g, 0, compressor=False, power=14.0)
    assert _run(g, FAN_IDLE, compressor=False, power=14.0).fan_mode == "low"
    # Changed on the remote while lowered: theirs, not lowered again until it has heated.
    assert _run(g, FAN_IDLE + 30, fan="high", compressor=False, power=14.0) is None
    assert g.saved is None and not g.lowered and g.user_hold
    assert _run(g, FAN_IDLE * 3, fan="high", compressor=False, power=14.0) is None
    # Setting off: a lowered fan comes back.
    g2 = FanGuard()
    _run(g2, 0, compressor=False, power=14.0)
    _run(g2, FAN_IDLE, compressor=False, power=14.0)
    g2.enabled = False
    assert _run(g2, FAN_IDLE + 30, fan="low", compressor=False, power=14.0).fan_mode == "auto"


def test_threshold_learned_from_standby_or_set():
    g = FanGuard()
    assert g.threshold() == 10.0
    for _ in range(50):
        g.learn_standby(2.0)
    assert g.threshold() < 7
    g.power_w = 20
    assert g.threshold() == 20


class _Clock:
    now = 1_000_000.0

    def __call__(self):
        return self.now


def _stats(step: dict) -> tuple[QlimaStats, dict]:
    reading = {"power": 900.0}
    unit = SimpleNamespace(unit_defrost=lambda: None, power=lambda: reading["power"], outdoor=lambda: 0.0)
    thermostat = SimpleNamespace(stop_requested=lambda: False, follow_me_step=lambda: step["step"])
    entry = SimpleNamespace(entry_id="e")
    hass = SimpleNamespace(data={"qlima_control": {"e": thermostat}})
    stats = QlimaStats.__new__(QlimaStats)
    stats.hass, stats.entry, stats.unit, stats.data = hass, entry, unit, {}
    stats._was_running_at_speed, stats.defrosting = 0.0, False
    stats._run_start, stats._dip_at, stats._dip_step, stats._dip_pushed = None, None, None, False
    return stats, reading


def _minutes(stats, reading, clock, power, minutes):
    reading["power"] = power
    for _ in range(int(minutes * 2)):
        clock.now += 30
        stats._detect_defrost("heat")


def test_defrost_count_skips_start_dips_and_own_stops():
    clock, step = _Clock(), {"step": "start"}
    stats, reading = _stats(step)
    with patch("custom_components.qlima_control.stats.time", clock):
        _minutes(stats, reading, clock, 900, 3)     # a start
        _minutes(stats, reading, clock, 30, 1)      # soft-start dip
        _minutes(stats, reading, clock, 900, 1)
        _minutes(stats, reading, clock, 30, 1)      # and the second one
        _minutes(stats, reading, clock, 900, 30)
        assert stats.data.get("defrosts_day", 0) == 0
        # A real defrost: drops, the unit comes back by itself.
        step["step"] = "hold"
        _minutes(stats, reading, clock, 60, 6)
        assert stats.defrosting
        _minutes(stats, reading, clock, 900, 20)
        assert stats.data.get("defrosts_day") == 1 and not stats.defrosting
        # A stop in hold that only ends because the thermostat pushed a start: not counted.
        _minutes(stats, reading, clock, 14, 5)
        step["step"] = "start"
        _minutes(stats, reading, clock, 14, 4)
        _minutes(stats, reading, clock, 900, 5)
        assert stats.data.get("defrosts_day") == 1
        # Off longer than a defrost lasts: a stop.
        step["step"] = "hold"
        _minutes(stats, reading, clock, 900, 15)
        _minutes(stats, reading, clock, 14, 20)
        _minutes(stats, reading, clock, 900, 5)
        assert stats.data.get("defrosts_day") == 1
