"""The ESP32 in the unit's USB port: connection, firmware and network."""

from __future__ import annotations

import re
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import CONF_DEVICE, DOMAIN
from .unit import QlimaUnit

LATEST_FIRMWARE = "firmware_latest"  # hass.data[DOMAIN] key, set by the Firmware entity
# The prebuilt image (web installer / online update) names itself qlima-control-<mac>.
# Any other node name is a build from the user's own yaml, e.g. adopted in ESPHome.
_FACTORY_NAME = re.compile(r"^qlima-control-[0-9a-f]{6}$")

ESP_STATUSES = ["online", "unit_silent", "update", "offline"]


def firmware_version(sw_version: str | None) -> str | None:
    """The project version ESPHome reports as "1.2.0 (ESPHome 2026.9.1)"."""
    if not sw_version or sw_version.startswith("ESPHome"):
        return None
    head = sw_version.split(" ", 1)[0].lstrip("v")
    return head if head and all(p.isdigit() for p in head.split(".")) else None


def version_key(version: str) -> tuple[int, ...]:
    return tuple(int(p) for p in version.split("."))


def newer(latest: str | None, installed: str | None) -> bool:
    if not latest or not installed:
        return False
    try:
        return version_key(latest) > version_key(installed)
    except ValueError:
        return False


def esphome_node_name(hass: HomeAssistant, device_id: str | None) -> str | None:
    """The ESPHome node name of the device (e.g. 'qlima-control-393058')."""
    device = dr.async_get(hass).async_get(device_id) if device_id else None
    if device is None:
        return None
    # HA 2026.10+ has one config entry per device (config_entry_id); older versions a set.
    entry_id = getattr(device, "config_entry_id", None)
    for entry_id in [entry_id] if entry_id else list(device.config_entries):
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is not None and entry.domain == "esphome":
            return entry.data.get("device_name")
    return None


def runs_prebuilt_image(hass: HomeAssistant, device_id: str | None) -> bool:
    """The ESP runs the prebuilt image, whose Wi-Fi is saved on the ESP itself.

    Only then is the image's online update safe. A build from the user's own yaml
    has its Wi-Fi compiled in; the prebuilt image would not know it.
    """
    name = esphome_node_name(hass, device_id)
    return bool(name and _FACTORY_NAME.match(name))


def esp_device(hass: HomeAssistant, unit: QlimaUnit) -> dr.DeviceEntry | None:
    device_id = unit.config.get(CONF_DEVICE)
    return dr.async_get(hass).async_get(device_id) if device_id else None


def connected(hass: HomeAssistant, unit: QlimaUnit) -> bool:
    """The ESP is connected when its climate entity is available."""
    climate = unit.entity("climate")
    state = hass.states.get(climate) if climate else None
    return state is not None and state.state != "unavailable"


def _state(hass: HomeAssistant, unit: QlimaUnit, role: str) -> str | None:
    entity_id = unit.entity(role)
    state = hass.states.get(entity_id) if entity_id else None
    if state is None or state.state in ("unavailable", "unknown", ""):
        return None
    return state.state


def _number(value: str | None) -> float | None:
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def esp_info(hass: HomeAssistant, unit: QlimaUnit) -> dict[str, Any]:
    """Everything known about the ESP, for the status sensor and the card."""
    device = esp_device(hass, unit)
    installed = firmware_version(device.sw_version if device else None)
    latest = hass.data.get(DOMAIN, {}).get(LATEST_FIRMWARE)
    esphome = _state(hass, unit, "esphome")
    if not esphome and device and device.sw_version and "ESPHome " in device.sw_version:
        esphome = device.sw_version.split("ESPHome ", 1)[1].rstrip(")")
    mac = next((c[1] for c in device.connections if c[0] == dr.CONNECTION_NETWORK_MAC), None) if device else None
    wifi = _number(_state(hass, unit, "wifi"))
    uptime = _number(_state(hass, unit, "uptime"))
    return {
        "name": (device.name_by_user or device.name) if device else None,
        "model": device.model if device else None,
        "firmware": installed,
        "newest_firmware": latest or installed,
        "firmware_up_to_date": None if not installed else not newer(latest, installed),
        "esphome": esphome,
        "ip": _state(hass, unit, "ip"),
        "mac": mac.upper() if mac else None,
        "wifi_signal": round(wifi) if wifi is not None else None,
        "uptime_hours": round(uptime / 3600, 1) if uptime is not None else None,
        "unit_responding": unit.online(),
    }


def esp_status(hass: HomeAssistant, unit: QlimaUnit) -> str:
    if not connected(hass, unit):
        return "offline"
    if unit.online() is False:
        return "unit_silent"
    info = esp_info(hass, unit)
    if info["firmware_up_to_date"] is False:
        return "update"
    return "online"
