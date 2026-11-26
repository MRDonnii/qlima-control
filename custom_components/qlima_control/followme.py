"""Follow Me steering: the value the unit regulates on, and what the unit has taught us.

Follow Me reaches a Midea unit in whole degrees over IR, and the unit only starts or stops
once that value is far enough from its own target. So the room sensor (0.1 K) decides and
the value sent is steered in three steps (heat; cool is mirrored):

  start – the room is `start_at` below the target: the unit is told the room is at least
          its *start level* below its target (the room itself when that is further off),
          so it works – until the room is `hold_early` short of the target (it still rises
          a little after the push ends);
  hold  – at the target: at most `hold_offset` (default 1) degree past the unit's own
          target, so a running inverter turns down to its minimum and keeps running.
          It no longer depends on the stop level (1.10 sent stop level − 1, which was 2–3
          past the target on the garage and stopped the compressor anyway);
          With Follow Me in half degrees (firmware 1.5+) hold follows the room in halves:
          +0 below the target, hold − 0.5 at it, the full hold value above (HOLD_BAND), and a
          start close to the target with the compressor running asks for only half a degree
          (SOFT_START), so it does not run flat out past the target;
  stop  – the room is `stop_at` above the target: the unit is told its *stop level* above
          (the room when further), so the compressor stops – until the room is `start_at`
          below the target again.

The unit's own target is the room target rounded. The compressor's minimum run and off
times are measured on the compressor itself (followed with every thermostat update, also
while not steering) and give way only when the room is FORCE off target. Without a fresh
compressor reading the steps are timed instead; unknown counts as done.

The unit reports the Follow Me value it acts on as its indoor temperature (some with a
constant offset, which is learned from three values). Nothing is learned unless that echo
confirms the value sent right now, nor while the unit is offline, shows a fault code, has no
power reading or defrosts. Its levels are learned from what it does (compressor, else power):
  - still working CHECKS["stop"] after stop (beyond its own minimum run) → stop level +1;
  - stopped in hold, where it should run, for CHECKS["hold"] (twice as long in defrost
    weather, where such a stop looks like a defrost), the second time in a row → the hold
    value comes half a degree closer to the target – e.g. +1, then +0.5, then +0. With firmware
    1.5+ ("Follow Me i halve grader") the ESP sends Follow Me in °F, which the unit rounds to
    half degrees, so the half is sent as it is (except the few halves no whole °F reaches,
    see fahrenheit_echo). Otherwise Follow Me is whole degrees, and a half is the two
    neighbouring degrees in turn (the lower one while the room is below the target, the
    higher one once it is above);
  - idle CHECKS["start"] in start → start level +1;
  - working CHECKS["rise"] in start without the room coming up while RISE_SHORT short →
    this start only is pushed a degree further;
  - even STOP_MAX does not stop it → its own target is parked at the neutral value while
    stopped and restored at the next start.
Per 5 °C outdoor band it measures how far the room still rises once the start step ends and
still falls after a start, and moves stop_at and start_at towards the target accordingly.
Checks that take longer than a unit needs to react, and what is learned, are announced in
Home Assistant. The learning is kept in its own store (per config entry).
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
import math
from time import time
from typing import Any, Callable

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from . import learning_notice
from .const import DOMAIN
from .unit import QlimaUnit

_LOGGER = logging.getLogger(__name__)

START = 0.2           # K below the target where the start step begins
STOP = 0.3            # K above the target where the stop step begins
FORCE = 0.5           # K off the target that overrides the minimum run/off time
MIN_START = 0.1       # the weather learning moves start_at no closer than this
MIN_STOP = 0.15       # … and stop_at no closer than this
STOP_MAX = 5          # stop level beyond which the unit's target is parked during stops
START_MAX = 4
BOOST_MAX = 3
UNIT_MIN_RUN = 7 * 60  # a stop sooner after a start may hit the unit's own minimum run
CHECKS = {"stop": 6 * 60, "hold": 6 * 60, "start": 10 * 60, "rise": 15 * 60}
SHOW = {"stop": 2 * 60, "hold": 2 * 60, "start": 4 * 60}  # announced once longer than a normal reaction
DEFROST_OUTDOOR = 6.0  # at or below: a stop in hold may be a defrost – check it twice as long
RISE_SHORT = 0.5
PROGRESS = 0.2
ECHO_SETTLE = 90       # s the unit may take to report a new value
ECHO_LATE = 10 * 60    # s of a mismatch that does not follow the values sent = Follow Me does not arrive
FRESH = 3 * 60         # s after which a compressor reading no longer counts
UNCONFIRMED_NOTICE = 60 * 60  # s without a confirmed Follow Me before Home Assistant is told once
BAND = 5
TRACK_MAX = 45 * 60
TRACK_TURN = 0.2
HISTORY = 10
LEAD_MIN = 6.0        # min: the unit takes about this long to turn down – the start step ends that much rise early
LEAD_MAX = 1.0        # K: … but never more than this short of the target
SLOPE_SPAN = 8 * 60   # s of room readings the rise rate is taken over (at least 3 min)
RISING = 0.02         # K/min: the room counts as rising (hold stays on its full value, no soft start)
SOFT_START = 0.5      # K: with half degrees, a start this close to the target (compressor running) asks for only half a degree
HOLD_BAND = 0.1       # K: with half degrees, hold sends +0 this far below the target, the full hold value this far above
DITHER = 0.1           # K: a half-degree hold uses the lower degree below target − this, the higher above target + this
HOLD_OFFSET = 1        # degrees past the unit's target sent in hold (setting)
HOLD_EARLY = 0.2       # K short of the target where the start step hands over to hold (setting)
HOLD_CUT_MAX = 6
MODES = ("heat", "cool")
STORE_VERSION = 1


def _default_levels() -> dict[str, Any]:
    # stop/start: degrees past/short of the unit's target it must be told to stop/start;
    # floor: lowest stop level a stop has needed (kept for older stores); strikes: stops in
    # hold in a row; hold_cut: half degrees the hold value was brought closer to the target.
    return {"stop": 1, "start": 1, "floor": 1, "strikes": 0, "park": False, "hold_cut": 0}



def _half(value: float) -> float:
    return math.floor(value * 2 + 0.5) / 2


def fahrenheit_echo(celsius: float) -> float:
    """The half degree a Qlima S 52xx acts on for Follow Me sent the way firmware 1.5 does.

    The ESP sends the whole °F whose °C value rounds to the wanted half degree (nearest one
    on a tie); the unit rounds that to the nearest 0.5 °C (tested 2026-11-26: 71 °F → 21.5,
    72 → 22.0, 73 → 23.0, 74 → 23.5). A few halves, e.g. 22.5 and 27.5, no whole °F reaches.
    Mirror of the lambda in esphome/qlima-control.yaml (send_follow_me).
    """
    want = _half(celsius)
    first = round(celsius * 1.8 + 32)
    best, err = first, 99.0
    for f in range(first - 2, first + 3):
        fc = (f - 32) / 1.8
        e = abs(_half(fc) - want) * 10 + abs(fc - celsius)
        if e < err:
            best, err = f, e
    return _half((best - 32) / 1.8)

@dataclass
class Steering:
    """What the thermostat regulates to right now."""

    mode: str            # "heat" / "cool"
    target: float        # room target (away and price included)
    unit_target: float   # the unit's own target (whole degrees)


class FollowMe:
    """Steers the Follow Me value and learns the unit's start and stop levels.

    The state is written by value() when the step or the value changes, and by the thermostat
    after every evaluate(); nothing inside the evaluation writes it, because a write lets the
    feeder call value() again in the middle of a change.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, unit: QlimaUnit, write: Callable[[], None]) -> None:
        self.hass = hass
        self.entry = entry
        self.unit = unit
        self._write = write
        self._store = Store(hass, STORE_VERSION, f"qlima_control.followme.{entry.entry_id}")
        self.min_run = 20 * 60
        self.min_off = 10 * 60
        # Settings (set by the thermostat from the options).
        self.hold_offset = HOLD_OFFSET
        self.hold_early = HOLD_EARLY
        self.stop_margin = STOP
        self._dither_low = False
        self._hold_band = 0  # half-degree hold: -1 room below target, 0 at it, 1 above
        self._rooms: list[tuple[float, float]] = []  # (time, room) for the rise rate
        self._rise: float | None = None  # K/min towards the target side (heat: rising > 0)
        # Learned (kept in the store).
        self.levels = {m: _default_levels() for m in MODES}
        self.verified = {m: {"stop": False, "start": False} for m in MODES}
        self.weather: dict[str, dict[str, dict[str, float]]] = {m: {} for m in MODES}
        self.history: list[dict[str, Any]] = []
        self.echo = False
        self.echo_offset = 0   # what the unit adds to the value it reports
        # Runtime.
        self.step = "room"
        self.sent: float | None = None
        self.parked = False
        self.no_echo = False
        self._mode: str | None = None
        self._target: float | None = None
        self._unit_target: float | None = None
        self._room: float | None = None
        self._short = 0.0
        self._boost = 0
        self._sent_at = 0.0
        self._step_since = time()
        self._run_since: float | None = None   # start step began (no power reading)
        self._unit_on: bool | None = None       # the compressor, last known (defrosts bridged)
        self._raw_on: bool | None = None        # … as read, defrosts included
        self._seen_at: float | None = None      # when it was last read
        self._drop_at: float | None = None      # when it last went off as read
        self._in_defrost = False
        self._on_since: float | None = None
        self._off_since: float | None = None
        self._seen: set[bool] = set()           # compressor states seen in this step
        self._hold_stopped = False
        self._check: str | None = None
        self._check_since = 0.0
        self._check_room = 0.0
        self._track: dict[str, Any] | None = None
        self._notice: str | None = None
        self._echo_values: set[int] = set()
        self._bad_since: float | None = None
        self._pairs: dict[int, set[int]] = {}   # value sent → what the unit reported minus it, while they differ
        self._unconfirmed_since: float | None = None
        self._unconfirmed_told = False
        self.echo_state = "ok"

    # ------------------------------------------------------------------ storage
    async def async_load(self) -> bool:
        """Load what was learned; False when nothing is stored yet."""
        data = await self._store.async_load()
        if not isinstance(data, dict):
            return False
        self._apply_saved(data)
        return True

    def _data(self) -> dict[str, Any]:
        return copy.deepcopy({"levels": self.levels, "verified": self.verified, "weather": self.weather,
                              "history": self.history, "echo": self.echo, "echo_offset": self.echo_offset})

    def _save(self) -> None:
        self._store.async_delay_save(self._data, 5)

    async def async_flush(self) -> None:
        """Write now (the thermostat is removed, e.g. on a reload): nothing waits in a timer."""
        await self._store.async_save(self._data())

    def _apply_saved(self, data: dict[str, Any]) -> None:
        levels, verified, weather = data.get("levels") or {}, data.get("verified") or {}, data.get("weather") or {}
        for mode in MODES:
            lv = _default_levels()
            saved = levels.get(mode)
            if isinstance(saved, dict):
                for key in ("stop", "start", "floor", "strikes", "hold_cut"):
                    try:
                        lv[key] = int(saved.get(key, lv[key]))
                    except (TypeError, ValueError):
                        pass
                lv["park"] = bool(saved.get("park", False))
            lv["stop"] = max(1, min(STOP_MAX, lv["stop"]))
            lv["start"] = max(1, min(START_MAX, lv["start"]))
            lv["floor"] = max(1, min(lv["stop"], lv["floor"]))
            lv["strikes"] = max(0, min(1, lv["strikes"]))
            lv["hold_cut"] = max(0, min(HOLD_CUT_MAX, lv["hold_cut"]))
            self.levels[mode] = lv
            v = verified.get(mode) if isinstance(verified.get(mode), dict) else {}
            self.verified[mode] = {k: bool(v.get(k, False)) for k in ("stop", "start")}
            bands = weather.get(mode)
            if isinstance(bands, dict):
                self.weather[mode] = {
                    str(band): {k: float(x) for k, x in stats.items() if isinstance(x, (int, float))}
                    for band, stats in bands.items() if isinstance(stats, dict)
                }
        history = data.get("history")
        if isinstance(history, list):
            self.history = [dict(h) for h in history if isinstance(h, dict) and "from" in h][-HISTORY:]
        self.echo = bool(data.get("echo", False))
        try:
            self.echo_offset = max(-3, min(3, int(data.get("echo_offset", 0))))
        except (TypeError, ValueError):
            self.echo_offset = 0

    def migrate(self, attrs: dict[str, Any]) -> None:
        """Take over what an earlier version kept in the thermostat's state (no store yet)."""
        levels = dict(attrs.get("follow_me_levels")) if isinstance(attrs.get("follow_me_levels"), dict) else {}

        def num(key: str) -> int:
            try:
                return int(attrs.get(key) or 0)
            except (TypeError, ValueError):
                return 0

        for mode in MODES:
            if mode not in levels and f"follow_me_offset_{mode}" in attrs:
                # 1.8.x shifted all values by an offset, with its proven minimum and a start push.
                offset = num(f"follow_me_offset_{mode}")
                levels[mode] = {
                    "stop": 1 + offset, "floor": 1 + num(f"follow_me_offset_min_{mode}"),
                    "start": 1 - offset + num(f"follow_me_start_offset_{mode}"),
                    "park": bool(attrs.get(f"follow_me_park_{mode}", False)), "strikes": 0,
                }
        history = []
        for h in attrs.get("follow_me_history") or []:
            if not isinstance(h, dict):
                continue
            if "from" in h:
                history.append(h)
            elif h.get("change") in ("offset_up", "offset_down") and isinstance(h.get("value"), int):
                # 1.8.x: an offset step is the stop level one higher.
                up = h["change"] == "offset_up"
                history.append({**h, "change": "stop_level", "value": 1 + h["value"],
                                "from": h["value"] if up else 2 + h["value"]})
        self._apply_saved({"levels": levels, "verified": attrs.get("follow_me_verified") or {},
                           "weather": attrs.get("follow_me_weather") or {}, "history": history,
                           "echo": attrs.get("follow_me_echo", False)})
        if any(self.levels[m] != _default_levels() for m in MODES) or self.history or self.echo:
            self._save()

    # ------------------------------------------------------------------ steering
    def thresholds(self, mode: str) -> tuple[float, float]:
        """(start_at, stop_at): how far from the target the start/stop steps begin here."""
        base = self.stop_margin
        start_at, stop_at = START, base
        stats = self.weather[mode].get(self.band() or "", {})
        if stats.get("n_under", 0) >= 2:
            start_at = max(MIN_START, START - max(0.0, stats.get("under", 0.0) - 0.1))
        if stats.get("n_over", 0) >= 2:
            stop_at = max(min(MIN_STOP, base), base - max(0.0, stats.get("over", 0.0) - 0.3))
        return round(start_at, 2), round(stop_at, 2)

    def early(self, mode: str) -> float:
        """K short of the target where the start push hands over to hold: the setting, but no
        more than the room was measured to rise after the push in this outdoor band."""
        early = max(0.0, min(0.5, float(self.hold_early)))
        stats = self.weather[mode].get(self.band() or "", {})
        if stats.get("n_over", 0) >= 2:
            early = min(early, max(0.0, float(stats.get("over", 0.0))))
        return round(early, 2)

    def stop_level(self, mode: str) -> int:
        """Degrees past the unit's target sent in stop: the learned level, and always more than hold."""
        return max(int(self.levels[mode]["stop"]), (self.hold_half(mode) + 1) // 2 + 1)

    def hold_half(self, mode: str) -> int:
        """The hold value in half degrees past the unit's target (setting minus what was learned)."""
        return max(0, int(round(2 * float(self.hold_offset))) - int(self.levels[mode].get("hold_cut", 0)))

    def _hold_degrees(self, mode: str, short: float, ts: float = 0.0, sign: int = 1) -> float:
        half = self.hold_half(mode)
        if self.unit.half_degrees():
            return self._hold_halves(half, short, ts, sign) / 2
        if half % 2 == 0:
            return half // 2
        # Whole degrees only: take the two neighbours in turn, by the room.
        if short >= DITHER:
            self._dither_low = True
        elif short <= -DITHER:
            self._dither_low = False
        return (half - 1) // 2 if self._dither_low else (half + 1) // 2

    def _hold_halves(self, half: int, short: float, ts: float, sign: int) -> int:
        """Hold in half degrees past the unit's target, by the room (Follow Me in °F).

        At the target the unit is told the room is a half degree less warm than the hold value
        (e.g. +0.5); HOLD_BAND below the target it gets +0 (a little more heat), above it the
        full hold value (e.g. +1). Each band holds until the room is back at the target, so it
        does not flicker. The inverter turns up and down instead of stopping and starting.
        """
        if self._rise is not None and self._rise > RISING:
            self._hold_band = 1  # still rising: the full hold value, so it turns down in time
        elif short >= HOLD_BAND:
            self._hold_band = -1
        elif short <= -HOLD_BAND:
            self._hold_band = 1
        elif (self._hold_band < 0 and short <= 0) or (self._hold_band > 0 and short >= 0):
            self._hold_band = 0
        h = max(0, half - 1 + self._hold_band) if half > 0 else 0
        # A half no whole °F reaches (22.5, 27.5): one more half past the target – less effort.
        while h < half and fahrenheit_echo(ts + sign * h / 2) != ts + sign * h / 2:
            h += 1
        return h

    def _slope(self, now: float, room: float) -> float | None:
        """How fast the room moves, K/min (heat: rising > 0), over the last SLOPE_SPAN."""
        self._rooms = [(t, r) for t, r in self._rooms if now - t <= SLOPE_SPAN] + [(now, room)]
        t0, r0 = self._rooms[0]
        if now - t0 < 3 * 60:
            return None
        return (room - r0) / ((now - t0) / 60)

    @property
    def last_mode(self) -> str | None:
        """The mode it steered last (heat/cool)."""
        return self._mode

    def band(self) -> str | None:
        outdoor = self.unit.outdoor()
        return None if outdoor is None else str(int(math.floor(outdoor / BAND) * BAND))

    def _cold(self) -> bool:
        outdoor = self.unit.outdoor()
        return outdoor is not None and outdoor <= DEFROST_OUTDOOR

    def value(self, room: float, steering: Steering | None) -> float:
        """The Follow Me value to send now (the room itself when not steering)."""
        now = time()
        self._room = room
        step, value = "room", float(room)
        if steering is not None:
            mode = steering.mode
            if mode != self._mode:
                self._mode, self.step, self._track = mode, "room", None  # heat and cool each start afresh
            sign = 1 if mode == "heat" else -1
            self._target, self._unit_target = steering.target, steering.unit_target
            short = round((steering.target - room) * sign, 2)  # > 0: the room still needs heat (cool)
            self._short = short
            slope = self._slope(now, room)
            self._rise = None if slope is None else slope * sign
            step = self._next_step(short, now, mode)
            lv = self.levels[mode]
            ts = steering.unit_target
            boost = self._boost if step == self.step else 0
            if step == "start":
                # Never closer to the target than the room is: far off, the unit must see the
                # whole shortfall – and at least its start level.
                level = ts - sign * (lv["start"] + boost)
                base = min(math.floor(room), level) if sign > 0 else max(math.ceil(room), level)
                soft = ts - sign * 0.5
                if (self.unit.half_degrees() and not boost and short < SOFT_START
                        and (self._rise is None or self._rise <= RISING)
                        and self._compressor(now) and fahrenheit_echo(soft) == soft):
                    # Close to the target with the compressor running (back from hold): ask
                    # for half a degree, so it does not run flat out up to the target and past it.
                    base = soft
            elif step == "stop":
                level = ts + sign * self.stop_level(mode)
                base = max(math.ceil(room), level) if sign > 0 else min(math.floor(room), level)
            else:
                base = ts + sign * self._hold_degrees(mode, short, ts, sign)
            value = float(base)
        if step != self.step:
            self._enter(step, room, now)
        if (step, value) != (self.step, self.sent):
            if value != self.sent:
                self._sent_at = now
            self.step, self.sent = step, value
            self._write()
        return value

    def _next_step(self, short: float, now: float, mode: str) -> str:
        start_at, stop_at = self.thresholds(mode)
        last = self.step
        early = self.early(mode)
        if last == "start":
            if self.unit.half_degrees() and self._rise is not None and self._rise > RISING:
                # Rising fast: the unit needs LEAD_MIN to turn down, so hand over that much rise early.
                early = max(early, min(LEAD_MAX, self._rise * LEAD_MIN))
            return "start" if short > early else "hold"
        if last == "hold" and short > 0 and self.unit.half_degrees() and self._rise is not None and self._rise > RISING:
            return "hold"  # handed over early while the room rises: let it coast up to the target
        if last == "hold" and early > 0:
            # Handed over early: the room may sit there – push again only once it falls further.
            start_at = max(start_at, round(early + 0.1, 2))
        if short >= start_at:
            if short >= FORCE or self._rested(now):
                return "start"
            return "stop" if last == "stop" else "hold"
        if last == "stop":
            return "stop"
        if short <= -stop_at and (self._compressor(now) is False or short <= -FORCE or self._ran(now)):
            return "stop"
        return "hold"

    def _compressor(self, now: float) -> bool | None:
        """The compressor as last known – None when that reading is older than FRESH."""
        if self._seen_at is None or now - self._seen_at > FRESH:
            return None
        return self._unit_on

    def _ran(self, now: float) -> bool:
        """The compressor has run its minimum time (unknown counts as done)."""
        since = self._on_since if self._compressor(now) is not None else self._run_since
        return since is None or now - since >= self.min_run

    def _rested(self, now: float) -> bool:
        """The compressor has been off its minimum time – or is running anyway."""
        if self._in_defrost and self._raw_on is False and self._drop_at is not None and self._compressor(now) is not None:
            # Off during what may be a defrost (or a stop taken for one): count from the drop.
            return now - self._drop_at >= self.min_off
        state = self._compressor(now)
        if state:
            return True
        if state is None:
            since = self._step_since if self.step == "stop" else None
        else:
            since = self._off_since
        return since is None or now - since >= self.min_off

    def _enter(self, step: str, room: float, now: float) -> None:
        left = self.step
        if (left == "hold" and step == "start" and self._check == "hold" and self._mode in MODES
                and now - self._check_since >= CHECKS["hold"]):
            # It stood still in hold until the room needed a push: longer than a defrost (in
            # frost the full check would outlast the minimum off time) – a stop in hold.
            self._apply("hold", self._mode, self.outcome("hold", self._mode))
        if left == "hold" and not self._hold_stopped and self._mode in MODES and self.levels[self._mode]["strikes"]:
            self.levels[self._mode]["strikes"] = 0  # a hold that went normally: odd stops do not add up
            self._save()
        self._step_since = now
        if step == "start":
            self._run_since = now
        self._dismiss_check()
        self._check = None
        state = self._compressor(now)
        self._seen = {state} if state is not None else set()  # how the step found the compressor
        self._hold_stopped = False
        self._boost = 0
        # Weather learning: how far the room still goes the old way after a start / the start step.
        kind = None
        if step == "start" and left in ("hold", "stop") and self._short < FORCE and self._compressor(now) is False:
            kind = "under"  # a real compressor start
        elif step == "hold" and left == "start":
            kind = "over"
        track = self._track
        if track is not None and self._mode in MODES and (
            (track["kind"] == "under" and step == "hold") or (track["kind"] == "over" and step == "start")
        ):
            # The step moved on while still measuring: what was seen so far is the amount.
            self._record(track, self._mode)
            self._track = None
        if step in ("room", "start"):
            self._track = None
        if kind and self._mode in MODES and self.band() is not None:
            signed = room * (1 if self._mode == "heat" else -1)
            self._track = {"kind": kind, "mode": self._mode, "band": self.band(), "since": now,
                           "ref": signed, "extreme": signed}

    # ------------------------------------------------------------------ evaluation
    def track_compressor(self) -> None:
        """With every thermostat update: when the compressor started and stopped (also while
        not steering).

        Only known→known changes are stamped, and a defrost does not end a run (the minimum
        times do not restart); if the compressor stays off after a "defrost", it was a stop
        and counts from the drop. A missing reading keeps the last state, which is no longer
        trusted after FRESH.
        """
        on = self.unit.compressor_on()
        if on is None:
            return
        now = time()
        self._seen_at = now
        if not on and self._raw_on is not False:
            self._drop_at = now
        self._raw_on = on
        if self._defrosting():
            self._in_defrost = True
            return
        if self._in_defrost:
            self._in_defrost = False
            if not on and self._unit_on:
                self._unit_on, self._off_since = False, self._drop_at or now  # it was a stop
                return
        if self._unit_on is not None and on != self._unit_on:
            if on:
                self._on_since = now
            else:
                self._off_since = self._drop_at or now
        self._unit_on = on

    def _defrosting(self) -> bool:
        stats = self.hass.data.get(DOMAIN, {}).get(f"{self.entry.entry_id}_stats")
        return stats is not None and bool(getattr(stats, "defrosting", False))

    def evaluate(self, room: float | None, steering: Steering | None) -> None:
        """Once per thermostat evaluation: check the echo and learn."""
        now = time()
        self.track_compressor()
        if steering is None or room is None or self.step == "room":
            self.idle()
            return
        mode = steering.mode
        echo = self._check_echo(now)
        self.echo_state = echo
        if echo in ("bad", "unknown") and not self.no_echo:
            if self._unconfirmed_since is None:
                self._unconfirmed_since = now
            elif not self._unconfirmed_told and now - self._unconfirmed_since >= UNCONFIRMED_NOTICE:
                self._unconfirmed_told = True
                learning_notice.notify_unconfirmed(self.hass, self.entry.entry_id, self.entry.title, echo == "unknown")
        elif echo == "ok":
            if self._unconfirmed_told:
                learning_notice.dismiss_no_echo(self.hass, self.entry.entry_id)
            self._unconfirmed_since, self._unconfirmed_told = None, False
        reason = self.pause_reason()
        if self.unit.pump_mode() != mode:
            # The unit is not (or not yet seen) in the mode – e.g. briefly unavailable: no evidence.
            self._check, self._track = None, None
            self._dismiss_check()
            return
        working = self.unit.working(mode) if self.unit.work_known() else None
        if echo == "settling":
            return  # a value just sent: judge again once the unit has reported it
        # In defrost weather a stop in hold looks like a defrost the power reading made up:
        # that check runs on (twice as long). A defrost the unit reports itself is one.
        hold_in_frost = (reason == "defrost" and self.step == "hold" and self._cold()
                         and self.unit.unit_defrost() is None)
        if working is None or echo != "ok" or (reason is not None and not hold_in_frost):
            self._check, self._track = None, None
            self._dismiss_check()
            return
        if reason is not None:
            self._track = None
        self._learn(now, mode, room, working)
        if reason is None:
            self._follow_weather(now, mode, room, working)

    def idle(self) -> None:
        """Not steering: nothing is checked or tracked."""
        self._check, self._track = None, None
        self._bad_since, self._pairs = None, {}
        self._unconfirmed_since = None
        self._dismiss_check()

    def pause_reason(self) -> str | None:
        if self.unit.online() is False:
            return "offline"
        if not self.unit.work_known():
            return "no_meter"
        if self.no_echo:
            return "no_follow_me"
        if self.unit.fault_code():
            return "fault"
        if self._defrosting():
            return "defrost"
        return None

    def _check_echo(self, now: float) -> str:
        """Whether the unit confirms the value sent now: "ok", "settling" (just sent, not reported
        yet), "bad", or "unknown" (it reports no indoor temperature).

        It reports the Follow Me value it acts on as its indoor temperature – some plus a
        constant offset, learned once three values sent show the same difference. Reports that
        do not follow the values sent for ECHO_LATE mean Follow Me does not arrive: learning
        stops and Home Assistant says so, until the values match again.
        """
        sent, reported = self.sent, self.unit.unit_temperature()
        if sent is None:
            return "settling"
        if reported is None:
            self._bad_since, self._pairs = None, {}
            return "unknown"
        # What the unit should act on: whole degrees (the ESP rounds like lroundf), or with
        # Follow Me in °F the half degree the unit makes of the whole °F the ESP picks.
        expected = fahrenheit_echo(sent) if self.unit.half_degrees() else math.floor(sent + 0.5)
        if abs(reported - expected - self.echo_offset) < 0.5:
            self._bad_since, self._pairs = None, {}
            if not self.echo:
                self._echo_values.add(int(expected))
                if len(self._echo_values) >= 3:
                    self.echo = True
                    self._save()
            if self.no_echo:
                self.no_echo = False
                learning_notice.dismiss_no_echo(self.hass, self.entry.entry_id)
                _LOGGER.info("%s: the unit follows Follow Me again", self.entry.title)
            return "ok"
        if now - self._sent_at < ECHO_SETTLE:
            return "settling"
        if self._bad_since is None:
            self._bad_since = now
        self._pairs.setdefault(int(expected), set()).add(int(round(reported - expected)))
        diffs = set().union(*self._pairs.values())
        if len(self._pairs) >= 3 and len(diffs) == 1 and abs(next(iter(diffs))) <= 3:
            # It follows every value sent, shifted by the same amount: that is its echo.
            self.echo_offset = next(iter(diffs))
            self.echo, self._echo_values = True, set(self._pairs)
            self._bad_since, self._pairs = None, {}
            _LOGGER.info("%s: the unit reports Follow Me with an offset of %+d °C", self.entry.title, self.echo_offset)
            if self.no_echo:
                self.no_echo = False
                learning_notice.dismiss_no_echo(self.hass, self.entry.entry_id)
            self._save()
            return "ok"
        late = now - self._bad_since
        # Reports that do not follow the values sent – or, once the echo was confirmed, a steady
        # other value for twice as long – mean Follow Me does not arrive.
        if not self.no_echo and ((len(diffs) > 1 and late >= ECHO_LATE) or (self.echo and late >= 2 * ECHO_LATE)):
            self.no_echo = True
            _LOGGER.warning("%s: the unit reports %.0f °C but was sent Follow Me %.0f °C – it does not arrive",
                            self.entry.title, reported, sent)
            learning_notice.notify_no_echo(self.hass, self.entry.entry_id, self.entry.title, reported, sent,
                                           int((late + ECHO_SETTLE) / 60))
        return "bad"

    def _learn(self, now: float, mode: str, room: float, working: bool) -> None:
        sign = 1 if mode == "heat" else -1
        step = self.step
        verified = self.verified[mode]
        # Confirmed only on evidence: seen running then stopping in stop, idle then running in start.
        if step == "stop" and not working and True in self._seen and not verified["stop"]:
            verified["stop"] = True
            self._save()
        elif step == "start" and working and False in self._seen and not verified["start"]:
            verified["start"] = True
            self._save()
        self._seen.add(working)
        cond = None
        if step == "stop":
            if working and not self.parked and (self._on_since is None or now - self._on_since >= UNIT_MIN_RUN):
                cond = "stop"
        elif step == "hold":
            if not working and True in self._seen:
                cond = "hold"
        elif step == "start":
            if not working:
                cond = "start" if self._short >= self.thresholds(mode)[0] else None
            elif self._short >= RISE_SHORT:
                cond = "rise"
        if cond != self._check:
            self._check, self._check_since, self._check_room = cond, now, room
            self._dismiss_check()  # the unit did as told: nothing to learn
            return
        if cond is None:
            return
        if cond == "rise" and (room - self._check_room) * sign >= PROGRESS:
            self._check_since, self._check_room = now, room  # the room comes up
            return
        needed = self._needed(cond)
        elapsed = now - self._check_since
        outcome = self.outcome(cond, mode)
        if elapsed < needed:
            if outcome and cond in SHOW and elapsed >= SHOW[cond] and self._notice != "check":
                left = max(1, math.ceil((needed - elapsed) / 60))
                learning_notice.notify_check(self.hass, self.entry.entry_id, self.entry.title, cond,
                                             outcome[0], self._notice_context(mode, outcome[1], left))
                self._notice = "check"
            return
        self._check_since, self._check_room = now, room
        self._apply(cond, mode, outcome)

    def _needed(self, cond: str) -> int:
        return CHECKS[cond] * 2 if cond == "hold" and self._cold() else CHECKS[cond]

    def outcome(self, cond: str | None, mode: str) -> tuple[str, int | None] | None:
        """What a check that runs out changes: (what, new value); None when nothing can."""
        lv = self.levels[mode]
        if cond == "stop":
            if self.stop_level(mode) < STOP_MAX:
                return "stop_level", self.stop_level(mode) + 1
            return None if lv["park"] else ("park", None)
        if cond == "hold":
            half = self.hold_half(mode)
            if half > 0 and lv["strikes"] > 0:
                return "hold_level", half - 1  # the second stop in hold in a row
            return None
        if cond == "start":
            return ("start_level", lv["start"] + 1) if lv["start"] < START_MAX else None
        if cond == "rise":
            return ("boost", self._boost + 1) if self._boost < BOOST_MAX else None
        return None

    def _apply(self, cond: str, mode: str, outcome: tuple[str, int | None] | None) -> None:
        lv, verified = self.levels[mode], self.verified[mode]
        if cond == "hold":
            self._hold_stopped = True
            self._seen.discard(True)  # once per stop in hold
            if outcome is None and self.hold_half(mode) > 0:
                lv["strikes"] = 1  # one odd stop: lower the hold only if it happens again
                self._save()
        if outcome is None:
            self._dismiss_check()
            return
        what, value = outcome
        if what == "boost":
            self._boost = value
            _LOGGER.info("%s: the room does not come up – this Follow Me start pushed %d° further", self.entry.title, value)
            return
        old = (self.stop_level(mode) if what == "stop_level" else lv["start"] if what == "start_level"
               else self.hold_half(mode) if what == "hold_level" else None)
        if what == "park":
            lv["park"] = True
            verified["stop"] = False
        elif what == "start_level":
            lv["start"] = value
            verified["start"] = False
        elif cond == "stop":
            lv["stop"] = lv["floor"] = value
            lv["strikes"] = 0
            verified["stop"] = False
        else:  # a stop in hold: the hold value half a degree closer to the target
            lv["hold_cut"] = min(HOLD_CUT_MAX, lv.get("hold_cut", 0) + 1)
            lv["strikes"] = 0
        self.history = [*self.history, {
            "at": datetime.fromtimestamp(time(), timezone.utc).isoformat(timespec="seconds"),
            "mode": mode, "check": cond, "change": what, "value": value, "from": old,
        }][-HISTORY:]
        _LOGGER.warning("%s: Follow Me %s – %s → %s %s", self.entry.title, mode,
                        {"stop": "kept working after stop", "hold": "stopped in hold", "start": "did not start"}[cond],
                        what, "" if value is None else value)
        learning_notice.notify_result(self.hass, self.entry.entry_id, self.entry.title, cond, what,
                                      self._notice_context(mode, value, 0, old))
        self._notice = "result"
        self._save()

    def _dismiss_check(self) -> None:
        if self._notice == "check":
            learning_notice.dismiss_learning(self.hass, self.entry.entry_id)
            self._notice = None

    def _notice_context(self, mode: str, value: int | None, left: int, old: int | None = None) -> dict[str, Any]:
        start_at, stop_at = self.thresholds(mode)
        told = None
        if self.sent is not None and self._unit_target is not None:
            told = int(round(abs(self.sent - self._unit_target)))  # what the value sent says, in degrees
        return {"room": self._room, "target": self._target, "short": abs(self._short), "sent": self.sent,
                "told": told, "power": self.unit.power(), "stop": self.stop_level(mode),
                "start": self.levels[mode]["start"], "value": value, "old": old, "left": left,
                "hold": self.hold_half(mode),
                "cool": mode == "cool", "start_at": start_at, "stop_at": stop_at}

    def _follow_weather(self, now: float, mode: str, room: float, working: bool) -> None:
        """Follow the room after a start / after the start step until it turns; learn the band."""
        track = self._track
        if track is None or track["mode"] != mode:
            return
        signed = room * (1 if mode == "heat" else -1)
        elapsed = now - track["since"]
        if track["kind"] == "over":
            track["extreme"] = max(track["extreme"], signed)
            turned = track["extreme"] - signed >= TRACK_TURN
            amount = track["extreme"] - track["ref"]
        else:
            if not working and elapsed > 5 * 60:
                self._track = None  # it did not start: not a normal start
                return
            track["extreme"] = min(track["extreme"], signed)
            turned = signed - track["extreme"] >= TRACK_TURN
            amount = track["ref"] - track["extreme"]
        if not turned and elapsed < TRACK_MAX:
            return
        self._track = None
        self._record(track, mode)

    def _record(self, track: dict[str, Any], mode: str) -> None:
        """Add a finished over/undershoot measurement to its outdoor band."""
        if track.get("mode") != mode:
            return
        amount = track["extreme"] - track["ref"] if track["kind"] == "over" else track["ref"] - track["extreme"]
        stats = self.weather[mode].setdefault(track["band"], {})
        field = track["kind"]
        n = int(stats.get(f"n_{field}", 0))
        amount = max(0.0, min(1.5, round(amount, 2)))
        old = stats.get(field)
        stats[field] = amount if old is None else round(old + (amount - old) * (0.5 if n < 3 else 0.3), 3)
        stats[f"n_{field}"] = n + 1
        self._save()

    # ------------------------------------------------------------------ reporting
    def countdowns(self) -> tuple[float | None, float | None]:
        """(minimum run ends, a needed start is allowed) as epoch seconds, while they hold the unit."""
        now = time()
        run_end = start_end = None
        state = self._compressor(now)
        if self._in_defrost and self._raw_on is False and self._drop_at is not None and state is not None:
            # Off in what may be a defrost: a needed start waits from the drop.
            if self.step in ("hold", "stop") and self._mode and self._short >= self.thresholds(self._mode)[0]:
                end = self._drop_at + self.min_off
                start_end = end if end > now else None
            return None, start_end
        if self.step in ("start", "hold") and state and self._on_since is not None:
            end = self._on_since + self.min_run
            run_end = end if end > now else None
        if self.step in ("hold", "stop") and state is False and self._off_since is not None and self._mode:
            if self._short >= self.thresholds(self._mode)[0]:
                end = self._off_since + self.min_off
                start_end = end if end > now else None
        return run_end, start_end

    def info(self, *, active: bool, steering: bool, mode: str | None) -> dict[str, Any]:
        """What the learning does now, how far a check is, and what it has learned."""
        result: dict[str, Any] = {"state": "off", "mode": mode, "follow_me_ok": self.echo}
        if mode in MODES:
            lv = self.levels[mode]
            start_at, stop_at = self.thresholds(mode)
            band = self.band()
            stats = self.weather[mode].get(band or "", {})
            result.update(
                stop_level=self.stop_level(mode), start_level=lv["start"], park=lv["park"],
                hold_offset=self.hold_half(mode) / 2, hold_early=self.early(mode),
                stop_ok=self.verified[mode]["stop"], start_ok=self.verified[mode]["start"],
                outdoor_band=None if band is None else f"{band}..{int(band) + BAND}",
                start_at=start_at, stop_at=stop_at, overshoot=stats.get("over"), undershoot=stats.get("under"),
                bands_learned=sum(1 for s in self.weather[mode].values() if s.get("n_over", 0) + s.get("n_under", 0)),
            )
        if not active or mode not in MODES:
            return result
        reason = self.pause_reason()
        if reason in ("no_meter", "no_follow_me"):
            result["state"] = reason
            return result
        cond = self._check if self._check in SHOW else None
        if reason is not None and not (reason == "defrost" and cond == "hold"):
            result.update(state="paused", reason=reason)
            return result
        if not steering or self.step == "room":
            result.update(state="paused", reason="steering_off")
            return result
        if self.echo_state in ("bad", "unknown"):
            result.update(state="paused", reason="follow_me_unconfirmed")
            return result
        outcome = self.outcome(cond, mode) if cond else None
        elapsed = max(0.0, time() - self._check_since) if cond else 0.0
        if outcome is not None and elapsed >= SHOW[cond]:
            needed = self._needed(cond)
            result.update(
                state=f"checking_{cond}", check=cond, outcome=outcome[0], outcome_value=outcome[1],
                elapsed_min=round(elapsed / 60, 1), needed_min=needed // 60,
                remaining_min=max(0, math.ceil((needed - elapsed) / 60)), progress=min(99, int(elapsed * 100 / needed)),
            )
            return result
        result["state"] = "learned" if result["stop_ok"] and result["start_ok"] else "learning"
        return result

    def attributes(self) -> dict[str, Any]:
        return {
            "follow_me_step": self.step,
            "follow_me_sent": self.sent,
            "follow_me_parked": self.parked,
            "follow_me_levels": copy.deepcopy(self.levels),
            "follow_me_verified": copy.deepcopy(self.verified),
            "follow_me_weather": copy.deepcopy(self.weather),
            "follow_me_history": copy.deepcopy(self.history),
            "follow_me_echo": self.echo,
            "follow_me_echo_offset": self.echo_offset,
        }

    def reset(self) -> None:
        """Forget what was learned (e.g. after a unit swap) and start over."""
        self.levels = {m: _default_levels() for m in MODES}
        self.verified = {m: {"stop": False, "start": False} for m in MODES}
        self.weather = {m: {} for m in MODES}
        self.history = []
        self.echo, self.echo_offset, self._echo_values, self.no_echo = False, 0, set(), False
        self._bad_since, self._pairs = None, {}
        self._check, self._track, self._boost = None, None, 0
        learning_notice.dismiss_learning(self.hass, self.entry.entry_id)
        learning_notice.dismiss_no_echo(self.hass, self.entry.entry_id)
        self._notice = None
        _LOGGER.warning("%s: Follow Me learning reset", self.entry.title)
        self._save()
        self._write()
