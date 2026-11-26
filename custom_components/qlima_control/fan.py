"""The indoor fan while the compressor stands still: no cold draught.

A Midea unit in heat keeps its indoor fan running when its compressor stops (its own stop,
a stop the thermostat asked for, or the minutes before a restart). On "auto" that blows
unheated air into the room. While the compressor has been off FAN_IDLE seconds and the unit
draws fan-only power (more than standby), the fan is set to its lowest speed; the user's own
choice is kept and set back once the compressor has run again for FAN_RESUME seconds.

During a real defrost the unit stops the fan itself (standby power): nothing is sent then.
A fan speed somebody else chooses while it is lowered (remote, app, card) is theirs: it is
left alone until the compressor has run again.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

FAN_IDLE = 120          # s the compressor must be off before the fan is lowered (start-up dips are shorter)
FAN_RESUME = 60         # s the compressor must run before the user's fan speed comes back
STANDBY_DEFAULT_W = 6.0  # what a switched-off unit draws until it has been seen
FAN_MARGIN_W = 4.0      # fan-only power is this much above standby
LOW_NAMES = ("low", "silent", "quiet")


def lowest_fan(modes: list[str] | None) -> str | None:
    """The unit's lowest fan speed by name ('low' first: every Midea unit has it)."""
    if not modes:
        return None
    by_lower = {str(m).lower(): m for m in modes}
    for name in LOW_NAMES:
        if name in by_lower:
            return by_lower[name]
    return None


@dataclass
class FanAction:
    fan_mode: str
    why: str  # "lower" / "restore"


class FanGuard:
    """Decides when the fan is lowered and set back; the thermostat sends the command."""

    def __init__(self) -> None:
        self.enabled = True
        self.power_w = 0.0            # fan-only threshold (W); 0 = standby + FAN_MARGIN_W
        self.saved: str | None = None  # the user's fan speed to set back
        self.lowered = False
        self.user_hold = False        # somebody chose a speed while it stood still
        self.standby_w: float | None = None
        self._off_since: float | None = None
        self._on_since: float | None = None

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

    def remember(self, fan: str | None, low: str | None) -> None:
        """Keep the user's fan speed (e.g. before the unit is switched off for an open door)."""
        if fan and not self.lowered and fan != low and self.saved is None:
            self.saved = fan

    def user_choice(self, fan: str) -> None:
        """Somebody chose a speed through the thermostat: that is the speed from now on."""
        self.saved = None
        if self.lowered or (self._off_since is not None):
            self.user_hold = True
        self.lowered = False

    # ------------------------------------------------------------ decision
    def decide(self, now: float, *, mode: str | None, fan: str | None, modes: list[str] | None,
               compressor: bool | None, power: float | None, told_to_stop: bool) -> FanAction | None:
        low = lowest_fan(modes)
        if compressor is True:
            self._off_since = None
            if self._on_since is None:
                self._on_since = now
        elif compressor is False:
            self._on_since = None
            if self._off_since is None:
                self._off_since = now
        if mode not in ("heat", "cool"):
            # Off (or dry/fan only): never send a fan command, it could switch the unit on.
            self._off_since = self._on_since = None
            return None
        if self.lowered and fan is not None and fan != low:
            # Somebody else chose a speed while it was lowered: theirs.
            self.lowered, self.saved, self.user_hold = False, None, True
            return None
        # Without a compressor or power reading it cannot be lowered, and a kept speed comes back at once.
        running = compressor is None or (self._on_since is not None and now - self._on_since >= FAN_RESUME)
        if running:
            self.user_hold = False
        if self.saved is not None and (running or mode != "heat" or not self.enabled):
            saved, self.saved, self.lowered = self.saved, None, False
            return FanAction(saved, "restore") if fan != saved else None
        if self.lowered or not self.enabled or mode != "heat" or low is None or self.user_hold:
            return None
        idle = self._off_since is not None and now - self._off_since >= FAN_IDLE
        blowing = told_to_stop or (power is not None and power >= self.threshold())
        if idle and blowing and fan is not None and fan != low:
            self.saved, self.lowered = fan, True
            return FanAction(low, "lower")
        return None

    # ------------------------------------------------------------ state
    def attributes(self) -> dict[str, Any]:
        return {"fan_lowered": self.lowered, "fan_user_choice": self.saved,
                "fan_standby_w": self.standby_w, "fan_idle_threshold_w": round(self.threshold(), 1)}

    def restore(self, attrs: dict[str, Any]) -> None:
        self.lowered = bool(attrs.get("fan_lowered", False))
        saved = attrs.get("fan_user_choice")
        self.saved = str(saved) if saved else None
        try:
            standby = attrs.get("fan_standby_w")
            self.standby_w = float(standby) if standby is not None else None
        except (TypeError, ValueError):
            self.standby_w = None
