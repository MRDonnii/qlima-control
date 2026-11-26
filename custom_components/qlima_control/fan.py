"""The indoor fan while the compressor stands still: no cold draught.

A Midea unit in heat keeps its indoor fan running when its compressor stops (its own stop,
a stop the thermostat asked for, or the minutes before a restart). On "auto" that blows
unheated air into the room. While the compressor has been off FAN_IDLE seconds and the unit
draws fan-only power (more than standby), the fan is set to its lowest speed; the user's own
choice is kept and set back once the compressor has run again for FAN_RESUME seconds.

During a real defrost the unit stops the fan itself (standby power): nothing is sent then.
A fan speed somebody else chooses while it is lowered (remote, app, card) is theirs: it is
left alone until the compressor has run again.

1.12: with the outlet sensor (DS18B20 "Udblæsningstemperatur") fitted, air that leaves the
unit less than COLD_K warmer than the room for FAN_IDLE seconds counts too – also without a
power meter. "Stille om natten" keeps the quietest speed (Silent when the unit has it) in a
chosen time window while the unit heats or cools; a speed chosen in that window is kept until
it ends.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

FAN_IDLE = 120          # s the compressor must be off before the fan is lowered (start-up dips are shorter)
FAN_RESUME = 60         # s the compressor must run before the user's fan speed comes back
STANDBY_DEFAULT_W = 6.0  # what a switched-off unit draws until it has been seen
FAN_MARGIN_W = 4.0      # fan-only power is this much above standby
LOW_NAMES = ("low", "silent", "quiet")
QUIET_NAMES = ("silent", "quiet", "low")
COLD_K = 3.0            # K: outlet air less than this above the room is not heating


def lowest_fan(modes: list[str] | None) -> str | None:
    """The unit's lowest fan speed by name ('low' first: every Midea unit has it)."""
    if not modes:
        return None
    by_lower = {str(m).lower(): m for m in modes}
    for name in LOW_NAMES:
        if name in by_lower:
            return by_lower[name]
    return None


def quietest_fan(modes: list[str] | None) -> str | None:
    """The quietest speed (Silent first)."""
    if not modes:
        return None
    by_lower = {str(m).lower(): m for m in modes}
    for name in QUIET_NAMES:
        if name in by_lower:
            return by_lower[name]
    return None


def cold_air(mode: str | None, outlet: float | None, room: float | None) -> bool | None:
    """In heat: the air leaving the unit is not warm (None without both readings)."""
    if mode != "heat" or outlet is None or room is None:
        return None
    return outlet < room + COLD_K


@dataclass
class FanAction:
    fan_mode: str
    why: str  # "lower" / "night" / "restore"


class FanGuard:
    """Decides when the fan is lowered and set back; the thermostat sends the command."""

    def __init__(self) -> None:
        self.enabled = True
        self.night_enabled = False
        self.power_w = 0.0            # fan-only threshold (W); 0 = standby + FAN_MARGIN_W
        self.saved: str | None = None  # the user's fan speed to set back
        self.lowered_to: str | None = None  # the speed it was set to
        self.user_hold = False        # somebody chose a speed while it stood still
        self.night_hold = False       # … or in the night window
        self.standby_w: float | None = None
        self._off_since: float | None = None
        self._on_since: float | None = None
        self._cold_since: float | None = None

    @property
    def lowered(self) -> bool:
        return self.lowered_to is not None

    # ------------------------------------------------------------ inputs
    def threshold(self) -> float:
        if self.power_w and self.power_w > 0:
            return float(self.power_w)
        return (self.standby_w if self.standby_w is not None else STANDBY_DEFAULT_W) + FAN_MARGIN_W

    def learn_standby(self, power: float | None) -> None:
        """While the unit is switched off: what it draws in standby (slowly averaged)."""
        if power is None or power < 0 or power > 30:
            return
        self.standby_w = power if self.standby_w is None else round(self.standby_w + (power - self.standby_w) * 0.1, 2)

    def remember(self, fan: str | None, low: str | None = None) -> None:
        """Keep the user's fan speed (e.g. before the unit is switched off for an open door)."""
        if fan and not self.lowered and self.saved is None:
            self.saved = fan

    def user_choice(self, fan: str, night: bool = False) -> None:
        """Somebody chose a speed through the thermostat: that is the speed from now on."""
        self.saved = None
        if night and self.night_enabled:
            self.night_hold = True
        elif self.lowered or self._off_since is not None:
            self.user_hold = True
        self.lowered_to = None

    def cold_air_active(self, now: float) -> bool:
        return self._cold_since is not None and now - self._cold_since >= FAN_IDLE

    # ------------------------------------------------------------ decision
    def decide(self, now: float, *, mode: str | None, fan: str | None, modes: list[str] | None,
               compressor: bool | None, power: float | None, told_to_stop: bool,
               night: bool = False, cold: bool | None = None) -> FanAction | None:
        if compressor is True:
            self._off_since = None
            if self._on_since is None:
                self._on_since = now
        elif compressor is False:
            self._on_since = None
            if self._off_since is None:
                self._off_since = now
        if cold:
            if self._cold_since is None:
                self._cold_since = now
        else:
            self._cold_since = None
        if not night:
            self.night_hold = False
        if mode not in ("heat", "cool"):
            # Off (or dry/fan only): never send a fan command, it could switch the unit on.
            self._off_since = self._on_since = self._cold_since = None
            return None
        if self.lowered and fan is not None and fan != self.lowered_to:
            # Somebody else chose a speed while it was lowered: theirs.
            self.lowered_to, self.saved = None, None
            if night and self.night_enabled:
                self.night_hold = True
            else:
                self.user_hold = True
            return None
        if compressor is None:
            # No compressor reading: the outlet air tells (warm again = heating); without it a
            # kept speed comes back at once and nothing is lowered.
            running = cold is not True
        else:
            running = self._on_since is not None and now - self._on_since >= FAN_RESUME
        if running:
            self.user_hold = False
        low = lowest_fan(modes)
        want, why = None, "lower"
        if self.night_enabled and night and not self.night_hold:
            want, why = quietest_fan(modes), "night"
        if want is None and self.enabled and mode == "heat" and low is not None and not self.user_hold:
            idle = self._off_since is not None and now - self._off_since >= FAN_IDLE
            blowing = told_to_stop or (power is not None and power >= self.threshold())
            cold_long = self.cold_air_active(now) and compressor is not True
            if (idle and blowing) or cold_long or (self.lowered_to == low and not running):
                want = low
        if want is not None:
            if fan is None or fan == want:
                return None
            if self.saved is None and not self.lowered:
                self.saved = fan
            self.lowered_to = want
            return FanAction(want, why)
        self.lowered_to = None
        if self.saved is not None:
            saved, self.saved = self.saved, None
            return FanAction(saved, "restore") if fan != saved else None
        return None

    # ------------------------------------------------------------ state
    def attributes(self) -> dict[str, Any]:
        return {"fan_lowered": self.lowered, "fan_lowered_to": self.lowered_to, "fan_user_choice": self.saved,
                "fan_standby_w": self.standby_w, "fan_idle_threshold_w": round(self.threshold(), 1)}

    def restore(self, attrs: dict[str, Any]) -> None:
        lowered_to = attrs.get("fan_lowered_to") or ("low" if attrs.get("fan_lowered") else None)
        self.lowered_to = str(lowered_to) if lowered_to else None
        saved = attrs.get("fan_user_choice")
        self.saved = str(saved) if saved else None
        try:
            standby = attrs.get("fan_standby_w")
            self.standby_w = float(standby) if standby is not None else None
        except (TypeError, ValueError):
            self.standby_w = None
