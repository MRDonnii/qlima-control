"""Diagnostics download: what the integration sees, for support requests."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import CONF_NOTIFY_SERVICE, DOMAIN

TO_REDACT = {CONF_NOTIFY_SERVICE}


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    unit = hass.data[DOMAIN].get(f"{entry.entry_id}_unit")
    thermostat = hass.data[DOMAIN].get(entry.entry_id)
    stats = hass.data[DOMAIN].get(f"{entry.entry_id}_stats")
    feeder = hass.data[DOMAIN].get(f"{entry.entry_id}_feeder")
    states = {}
    if unit is not None:
        for entity_id in unit.watched():
            state = hass.states.get(entity_id)
            states[entity_id] = None if state is None else {"state": state.state, "attributes": dict(state.attributes)}
    return {
        "config": async_redact_data({**entry.data, **entry.options}, TO_REDACT),
        "esp_entities": unit.esp if unit else None,
        "follow_me_action": feeder.action if feeder else None,
        "follow_me_action_available": bool(feeder and feeder.action and hass.services.has_service("esphome", feeder.action)),
        "readings": None if unit is None else {
            "room_temperature": unit.room_temperature().value,
            "room_humidity": unit.room_humidity().value,
            "power": unit.power(),
            "outdoor": unit.outdoor(),
            "unit_temperature": unit.unit_temperature(),
            "cop": unit.cop(),
            "measured_heat": unit.measured_heat(),
            "measured_cop": unit.measured_cop(),
        },
        "thermostat": None if thermostat is None else {"hvac_mode": thermostat.hvac_mode, **thermostat.extra_state_attributes},
        "stats": stats.data if stats else None,
        "linked_states": states,
    }
