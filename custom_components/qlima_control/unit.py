"""One Qlima unit: its ESP entities, the linked sensors and derived values."""

from __future__ import annotations

from dataclasses import dataclass
import math
from statistics import mean
from typing import Any

from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import (
    AGGREGATE_MAX,
    AGGREGATE_MIN,
    AGGREGATE_WEIGHTED,
    CONF_DEVICE,
    CONF_ENERGY_SENSOR,
    CONF_HEAT_PUMP,
    CONF_HUMIDITY_SENSORS,
    CONF_MODEL,
    CONF_OUTDOOR_SENSOR,
    CONF_P_MAX,
    CONF_POWER_SENSOR,
    CONF_POWER_SWITCH,
    CONF_PRICE_SENSOR,
    CONF_RUNNING_POWER_W,
    CONF_TEMPERATURE_MODE,
    CONF_TEMPERATURE_SENSOR,
    CONF_TEMPERATURE_SENSORS,
    CONF_TEMPERATURE_WEIGHTS,
    DEFAULT_RUNNING_POWER_W,
    MODEL_AIRFLOW,
    MODEL_P_MAX,
)

# Roles of the ESP firmware's entities, found by domain and entity name.
# The ESPHome unique id is "<MAC>/<n>/<domain>/<name>".
ESP_ROLES: dict[str, tuple[str, tuple[str, ...]]] = {
    "climate": ("climate", ("Varmepumpe",)),
    "swing_toggle": ("button", ("Vip auto til⁄fra", "Vip auto til/fra")),
    "swing_step": ("button", ("Lamel ét trin",)),
    "display": ("button", ("Display",)),
    "filter_reset": ("button", ("Nulstil filtertæller",)),
    "follow_me": ("switch", ("Follow Me",)),
    "beep": ("switch", ("Bip",)),
    "outdoor": ("sensor", ("Udetemperatur",)),
    "unit_temp": ("sensor", ("Indetemperatur anlæg",)),
    "filter_hours": ("sensor", ("Driftstimer siden filterrens",)),
    "online": ("binary_sensor", ("Anlæg svarer",)),
    "supply_temp": ("sensor", ("Udblæsningstemperatur",)),
    "return_temp": ("sensor", ("Indsugningstemperatur",)),
}

_INVALID = (STATE_UNAVAILABLE, STATE_UNKNOWN, "", None)


def esp_entities(hass: HomeAssistant, device_id: str | None) -> dict[str, str]:
    """Map each ESP role to the entity id on the chosen device."""
    if not device_id:
        return {}
    registry = er.async_get(hass)
    found: dict[str, str] = {}
    for entry in er.async_entries_for_device(registry, device_id, include_disabled_entities=False):
        parts = str(entry.unique_id).split("/")
        if len(parts) < 4:
            continue
        domain, name = parts[-2], parts[-1]
        for role, (role_domain, names) in ESP_ROLES.items():
            if role not in found and domain == role_domain and name in names:
                found[role] = entry.entity_id
    return found


def cop_model(
    outdoor: float | None,
    intake: float | None,
    room: float | None,
    power_w: float | None,
    p_max: float,
) -> float | None:
    """Physical COP estimate (Carnot with load-dependent approach temperatures).

    x = electrical power / p_max. Evaporator = outdoor − (3 + 5x), condenser =
    intake + (5 + 10x), where intake is the unit's own sensor but at most the
    room + 3 K. COP = 0.40 · T_cond[K] / (T_cond − T_evap), minus up to 7 %
    for defrosting around −1 °C. Calibrated to ≈4.4 at 7 °C out / 20 °C in /
    nominal load (Qlima SCOP 4.0–4.1).
    """
    if outdoor is None:
        return None
    t_in = intake
    if t_in is None:
        t_in = room + 2.0 if room is not None else 21.0
    elif room is not None:
        t_in = min(t_in, room + 3.0)
    p = power_w if power_w is not None and power_w >= 80 else 0.35 * p_max
    x = max(0.05, min(p / p_max, 1.2))
    t_evap = outdoor - (3.0 + 5.0 * x)
    t_cond = t_in + (5.0 + 10.0 * x)
    cop = 0.40 * (t_cond + 273.15) / max(t_cond - t_evap, 5.0)
    bump = max(0.0, 1.0 - abs(outdoor + 1.0) / 6.0)
    cop *= 1.0 - 0.07 * bump
    return round(max(1.0, min(cop, 7.0)), 2)


@dataclass
class Reading:
    """A numeric reading from one or more entities."""

    value: float | None
    sources: list[str]


class QlimaUnit:
    """Linked entities and derived values for one heat pump."""

    def __init__(self, hass: HomeAssistant, config: dict[str, Any]) -> None:
        self.hass = hass
        self.update(config)

    def update(self, config: dict[str, Any]) -> None:
        self.config = config
        self.esp = esp_entities(self.hass, config.get(CONF_DEVICE))
        sensors = config.get(CONF_TEMPERATURE_SENSORS) or []
        if not sensors and config.get(CONF_TEMPERATURE_SENSOR):
            sensors = [config[CONF_TEMPERATURE_SENSOR]]
        self.temperature_sensors: list[str] = list(sensors)
        self.temperature_mode: str = config.get(CONF_TEMPERATURE_MODE, "mean")
        self.temperature_weights: dict[str, float] = {
            k: float(v) for k, v in (config.get(CONF_TEMPERATURE_WEIGHTS) or {}).items()
        }
        self.humidity_sensors: list[str] = list(config.get(CONF_HUMIDITY_SENSORS) or [])
        self.power_sensor: str | None = config.get(CONF_POWER_SENSOR) or None
        self.power_switch: str | None = config.get(CONF_POWER_SWITCH) or None
        self.energy_sensor: str | None = config.get(CONF_ENERGY_SENSOR) or None
        self.price_sensor: str | None = config.get(CONF_PRICE_SENSOR) or None
        self.outdoor_sensor: str | None = config.get(CONF_OUTDOOR_SENSOR) or None
        model = config.get(CONF_MODEL, "s5225")
        self.airflow = MODEL_AIRFLOW.get(model, MODEL_AIRFLOW["custom"])
        self.p_max = float(config.get(CONF_P_MAX) or MODEL_P_MAX.get(model, 1200))
        self.running_power_w = float(config.get(CONF_RUNNING_POWER_W, DEFAULT_RUNNING_POWER_W))

    # --- entity ids -------------------------------------------------------
    @property
    def heat_pump(self) -> str | None:
        return self.esp.get("climate") or self.config.get(CONF_HEAT_PUMP)

    def entity(self, role: str) -> str | None:
        return self.esp.get(role)

    def watched(self) -> list[str]:
        ids = [
            *self.temperature_sensors,
            *self.humidity_sensors,
            self.power_sensor,
            self.power_switch,
            self.energy_sensor,
            self.price_sensor,
            self.outdoor_sensor,
            *self.esp.values(),
        ]
        return [i for i in dict.fromkeys(ids) if i]

    # --- readings ---------------------------------------------------------
    def _float(self, entity_id: str | None) -> float | None:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in _INVALID:
            return None
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) else None

    def _combine(self, ids: list[str], mode: str = "mean", weights: dict[str, float] | None = None) -> Reading:
        values = [(i, v) for i in ids if (v := self._float(i)) is not None]
        if not values:
            return Reading(None, [])
        numbers = [v for _, v in values]
        if mode == AGGREGATE_WEIGHTED:
            # Weights are relative: a missing sensor's share goes to the others.
            w = [(v, max(0.0, (weights or {}).get(i, 0.0))) for i, v in values]
            total = sum(x for _, x in w)
            value = sum(v * x for v, x in w) / total if total > 0 else mean(numbers)
            return Reading(round(value, 2), [i for i, _ in values])
        if mode == AGGREGATE_MIN:
            value = min(numbers)
        elif mode == AGGREGATE_MAX:
            value = max(numbers)
        else:
            value = mean(numbers)
        return Reading(round(value, 2), [i for i, _ in values])

    def room_temperature(self) -> Reading:
        return self._combine(self.temperature_sensors, self.temperature_mode, self.temperature_weights)

    def room_humidity(self) -> Reading:
        return self._combine(self.humidity_sensors)

    def power(self) -> float | None:
        return self._float(self.power_sensor)

    def outdoor(self) -> float | None:
        own = self._float(self.outdoor_sensor)
        return own if own is not None else self._float(self.entity("outdoor"))

    def unit_temperature(self) -> float | None:
        return self._float(self.entity("unit_temp"))

    def price(self) -> float | None:
        return self._float(self.price_sensor)

    def filter_hours(self) -> float | None:
        return self._float(self.entity("filter_hours"))

    def pump_mode(self) -> str | None:
        state = self.hass.states.get(self.heat_pump) if self.heat_pump else None
        return None if state is None or state.state in _INVALID else state.state

    def online(self) -> bool | None:
        entity_id = self.entity("online")
        state = self.hass.states.get(entity_id) if entity_id else None
        if state is None or state.state in _INVALID:
            return None if entity_id else (self.pump_mode() is not None)
        return state.state == "on"

    def running(self, mode: str) -> bool:
        """The unit is in `mode` and actually draws power."""
        power = self.power()
        if self.pump_mode() != mode:
            return False
        if self.power_sensor is None:
            return True
        return power is not None and power > self.running_power_w

    def cop(self) -> float | None:
        return cop_model(
            self.outdoor(),
            self.unit_temperature(),
            self.room_temperature().value,
            self.power(),
            self.p_max,
        )

    def heat_output(self) -> float | None:
        """Estimated heat delivered (W) while heating, 0 otherwise."""
        if not self.running("heat"):
            return 0.0
        power, cop = self.power(), self.cop()
        if power is None or cop is None:
            return None
        return round(power * cop)

    def heat_price(self) -> float | None:
        """Price of one kWh of heat (electricity price ÷ COP)."""
        price, cop = self.price(), self.cop()
        if price is None or not cop:
            return None
        return round(price / cop, 3)

    def cost_per_hour(self) -> float | None:
        price, power = self.price(), self.power()
        if price is None or power is None:
            return None
        return round(price * power / 1000, 3)

    # --- measured heat from the optional DS18B20 sensors -------------------
    def supply_temperature(self) -> float | None:
        return self._float(self.entity("supply_temp"))

    def return_temperature(self) -> float | None:
        return self._float(self.entity("return_temp"))

    def airflow_now(self) -> float:
        """Indoor-unit airflow (m³/h) for the current fan speed."""
        state = self.hass.states.get(self.heat_pump) if self.heat_pump else None
        fan = str(state.attributes.get("fan_mode", "")).lower() if state else ""
        low, mid, high = self.airflow
        return {"low": low, "medium": mid, "mid": mid, "high": high, "silent": low * 0.8, "quiet": low * 0.8, "turbo": high * 1.1}.get(fan, mid)

    def measured_heat(self) -> float | None:
        """Heat delivered (W) = airflow × 0.335 W/(m³/h·K) × (supply − return)."""
        supply, ret = self.supply_temperature(), self.return_temperature()
        if supply is None or ret is None:
            return None
        if self.pump_mode() not in ("heat", "cool"):
            return 0.0
        return round(abs(supply - ret) * self.airflow_now() * 0.335)

    def measured_cop(self) -> float | None:
        heat, power = self.measured_heat(), self.power()
        if heat is None or power is None or power < 80 or heat <= 0:
            return None
        return round(heat / power, 2)
