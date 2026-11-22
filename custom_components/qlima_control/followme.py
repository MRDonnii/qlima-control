"""Follow Me steering: the value the unit regulates on, and what the unit has taught us.

Follow Me reaches a Midea unit in whole degrees over IR, and the unit only starts or stops
once that value is far enough from its own target. So the room sensor (0.1 K) decides and
the value sent is steered in three steps (heat; cool is mirrored):

  start – the room is `start_at` below the target: the unit is told the room is at least
          its *start level* below its target (the room itself when that is further off),
          so it works – until the room reaches the target;
  hold  – at the target: one degree short of the *stop level*, so a running unit turns
          down and keeps running, and a stopped one stays off;
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
    weather, where such a stop looks like a defrost) → stop level −1 – only on the second
    time in a row when the level was needed for a stop, so one odd stop does not undo it;
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
MODES = ("heat", "cool")
STORE_VERSION = 1


def _default_levels() -> dict[str, Any]:
    # stop/start: degrees past/short of the unit's target it must be told to stop/start;
    # floor: lowest stop level a stop has needed; strikes: odd stops in hold at the floor.
    return {"stop": 1, "start": 1, "floor": 1, "strikes": 0, "park": False}


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
                for key in ("stop", "start", "floor", "strikes"):
                    try:
                        lv[key] = int(saved.get(key, lv[key]))
                    except (TypeError, ValueError):
                        pass
                lv["park"] = bool(saved.get("park", False))
            lv["stop"] = max(1, min(STOP_MAX, lv["stop"]))
            lv["start"] = max(1, min(START_MAX, lv["start"]))
            lv["floor"] = max(1, min(lv["stop"], lv["floor"]))
            lv["strikes"] = max(0, min(1, lv["strikes"]))
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
        start_at, stop_at = START, STOP
        stats = self.weather[mode].get(self.band() or "", {})
        if stats.get("n_under", 0) >= 2:
            start_at = max(MIN_START, START - max(0.0, stats.get("under", 0.0) - 0.1))
        if stats.get("n_over", 0) >= 2:
            stop_at = max(MIN_STOP, STOP - max(0.0, stats.get("over", 0.0) - 0.3))
        return round(start_at, 2), round(stop_at, 2)

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
            step = self._next_step(short, now, mode)
            lv = self.levels[mode]
            ts = steering.unit_target
            boost = self._boost if step == self.step else 0
            if step == "start":
                # Never closer to the target than the room is: far off, the unit must see the
                # whole shortfall – and at least its start level.
                level = ts - sign * (lv["start"] + boost)
                base = min(math.floor(room), level) if sign > 0 else max(math.ceil(room), level)
            elif step == "stop":
                level = ts + sign * lv["stop"]
                base = max(math.ceil(room), level) if sign > 0 else min(math.floor(room), level)
            else:
                base = ts + sign * (lv["stop"] - 1)
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
        if last == "start":
            return "start" if short > 0 else "hold"
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
        expected = math.floor(sent + 0.5)  # the ESP rounds like lroundf
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
            if lv["stop"] < STOP_MAX:
                return "stop_level", lv["stop"] + 1
            return None if lv["park"] else ("park", None)
        if cond == "hold":
            if lv["stop"] > 1 and (lv["stop"] > lv["floor"] or lv["strikes"] > 0):
                return "stop_level", lv["stop"] - 1
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
            if outcome is None and lv["stop"] > 1:
                lv["strikes"] += 1  # the level was needed for a stop: lower only if it happens again
                self._save()
        if outcome is None:
            self._dismiss_check()
            return
        what, value = outcome
        if what == "boost":
            self._boost = value
            _LOGGER.info("%s: the room does not come up – this Follow Me start pushed %d° further", self.entry.title, value)
            return
        old = lv["stop"] if what == "stop_level" else lv["start"] if what == "start_level" else None
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
        else:  # a stop in hold: one level lower
            lv["stop"] = value
            lv["floor"] = min(lv["floor"], value)
            lv["strikes"] = 0
            lv["park"] = False
            verified["stop"] = False
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
                "told": told, "power": self.unit.power(), "stop": self.levels[mode]["stop"],
                "start": self.levels[mode]["start"], "value": value, "old": old, "left": left,
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
                stop_level=lv["stop"], start_level=lv["start"], park=lv["park"],
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
