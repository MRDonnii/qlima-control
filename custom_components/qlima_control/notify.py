"""Phone notifications: unit not responding, filter due, door/window switched it off."""

from __future__ import annotations

from datetime import timedelta
import logging
from time import time

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.event import async_track_time_interval

from .const import (
    CONF_FILTER_ALARM_HOURS,
    CONF_NOTIFY_SERVICE,
    DEFAULT_FILTER_ALARM_HOURS,
    UNIT_OFFLINE_ALERT_MINUTES,
)
from .unit import QlimaUnit

_LOGGER = logging.getLogger(__name__)


class QlimaNotifier:
    """Send each alert once per episode to the configured notify service."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, unit: QlimaUnit) -> None:
        self.hass = hass
        self.entry = entry
        self.unit = unit
        cfg = {**entry.data, **entry.options}
        service = (cfg.get(CONF_NOTIFY_SERVICE) or "").strip()
        self.service = service.removeprefix("notify.") if service else ""
        self.filter_hours = float(cfg.get(CONF_FILTER_ALARM_HOURS, DEFAULT_FILTER_ALARM_HOURS))
        self._offline_since: float | None = None
        self._sent: set[str] = set()
        self._unsub = None

    def start(self) -> None:
        if self.service:
            self._unsub = async_track_time_interval(self.hass, self._check, timedelta(minutes=1))

    def stop(self) -> None:
        if self._unsub:
            self._unsub()
            self._unsub = None

    def notify_opening_off(self) -> None:
        """Called by the thermostat when an open door/window switched the unit off."""
        self._send("opening_off", "Varmepumpen er slukket",
                   f"{self.entry.title}: en dør eller et vindue har stået åbent, så varmepumpen er slukket. Den starter igen, når der har været lukket et stykke tid.",
                   "mdi:window-open-variant", "#f59e0b", once=False)

    @callback
    def _check(self, _now) -> None:
        online = self.unit.online()
        if online is False:
            self._offline_since = self._offline_since or time()
            if time() - self._offline_since >= UNIT_OFFLINE_ALERT_MINUTES * 60:
                self._send("offline", "Varmepumpen svarer ikke",
                           f"{self.entry.title} har ikke svaret i {UNIT_OFFLINE_ALERT_MINUTES} minutter. Tjek strøm og ESP'en i anlægget.",
                           "mdi:lan-disconnect", "#ef4444")
        else:
            self._offline_since = None
            self._sent.discard("offline")
        hours = self.unit.filter_hours()
        if hours is not None and hours >= self.filter_hours:
            self._send("filter", "Rens filteret",
                       f"{self.entry.title} har kørt {hours:.0f} timer siden sidste filterrens. Rens filtrene og nulstil tælleren.",
                       "mdi:air-filter", "#38bdf8")
        elif hours is not None:
            self._sent.discard("filter")

    def _send(self, key: str, title: str, message: str, icon: str, color: str, once: bool = True) -> None:
        if not self.service or (once and key in self._sent):
            return
        self._sent.add(key)
        self.hass.async_create_task(
            self.hass.services.async_call(
                "notify",
                self.service,
                {
                    "title": title,
                    "message": message,
                    "data": {"notification_icon": icon, "color": color, "tag": f"qlima_{self.entry.entry_id}_{key}"},
                },
                blocking=False,
            )
        )
