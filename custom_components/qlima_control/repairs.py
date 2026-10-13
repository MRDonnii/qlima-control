"""Repair issues shown in Home Assistant's Settings → Repairs."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_call_later

from .const import DOMAIN

_CHECK_AFTER = 180  # give the ESP time to connect after a restart


@callback
def schedule_checks(hass: HomeAssistant, entry: ConfigEntry):
    """Check the setup once the ESP had time to connect; returns the cancel callback."""

    @callback
    def _check(_now) -> None:
        unit = hass.data[DOMAIN].get(f"{entry.entry_id}_unit")
        feeder = hass.data[DOMAIN].get(f"{entry.entry_id}_feeder")
        if unit is None:
            return
        old_fw = bool(feeder and feeder.action and not hass.services.has_service("esphome", feeder.action))
        _issue(hass, entry, "old_firmware", old_fw)
        missing = [i for i in unit.temperature_sensors if hass.states.get(i) is None]
        _issue(hass, entry, "missing_sensors", bool(missing), {"sensors": ", ".join(missing)})
        _issue(hass, entry, "no_esp", unit.heat_pump is None or hass.states.get(unit.heat_pump) is None)

    return async_call_later(hass, _CHECK_AFTER, _check)


def _issue(hass: HomeAssistant, entry: ConfigEntry, key: str, active: bool, extra: dict | None = None) -> None:
    issue_id = f"{key}_{entry.entry_id}"
    if active:
        ir.async_create_issue(
            hass, DOMAIN, issue_id, is_fixable=False, severity=ir.IssueSeverity.WARNING,
            translation_key=key, translation_placeholders={"name": entry.title, **(extra or {})},
        )
    else:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
