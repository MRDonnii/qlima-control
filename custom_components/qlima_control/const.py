"""Constants for Qlima Control."""

DOMAIN = "qlima_control"

CONF_HEAT_PUMP = "heat_pump"  # resolved from the chosen ESP device
CONF_DEVICE = "device_id"  # the ESPHome device in the unit's USB port
CONF_TEMPERATURE_SENSORS = "temperature_sensors"
CONF_TEMPERATURE_MODE = "temperature_mode"
CONF_HUMIDITY_SENSORS = "humidity_sensors"
CONF_POWER_SENSOR = "power_sensor"
CONF_ENERGY_SENSOR = "energy_sensor"
CONF_PRICE_SENSOR = "price_sensor"
CONF_OUTDOOR_SENSOR = "outdoor_sensor"
CONF_MODEL = "model"
CONF_P_MAX = "p_max"
CONF_RUNNING_POWER_W = "running_power_w"
CONF_OPENINGS_ENABLED = "openings_enabled"
# Settings changed from entities (switch/numbers) apply live, without reloading.
LIVE_KEYS = frozenset({
    "openings_enabled",
    "opening_min_delay_minutes",
    "opening_off_delay_minutes",
    "opening_resume_delay_minutes",
})
CONF_POWER_SWITCH = "power_switch"  # e.g. the Shelly relay feeding the unit
CONF_AUTO_POWER_CYCLE_MINUTES = "auto_power_cycle_minutes"  # 0 = off
POWER_CYCLE_OFF_SECONDS = 30
CONF_NOTIFY_SERVICE = "notify_service"
CONF_FILTER_ALARM_HOURS = "filter_alarm_hours"
CONF_FOLLOW_ME = "follow_me"
CONF_TEMPERATURE_SENSOR = "temperature_sensor"
CONF_THERMOSTATS_TO_DISABLE = "thermostats_to_disable"
CONF_OPENING_SENSORS = "opening_sensors"
CONF_OPENING_OFF_DELAY_SECONDS = "opening_off_delay_seconds"  # legacy (<= 0.1.x), no longer used
CONF_OPENING_MIN_DELAY_MINUTES = "opening_min_delay_minutes"
CONF_OPENING_OFF_DELAY_MINUTES = "opening_off_delay_minutes"
CONF_OPENING_RESUME_DELAY_MINUTES = "opening_resume_delay_minutes"
CONF_OPENING_REDUCE_TO = "opening_reduce_to"
CONF_HEAT_TARGET = "heat_target"
CONF_COOL_TARGET = "cool_target"
CONF_HYSTERESIS = "hysteresis"
# Margin below (heat) / above (cool) the target that starts the unit. The
# normal hysteresis is the margin past the target that stops it.
CONF_START_HYSTERESIS = "start_hysteresis"
CONF_MIN_RUN_MINUTES = "min_run_minutes"
CONF_MIN_OFF_MINUTES = "min_off_minutes"
CONF_SENSOR_STABLE_SECONDS = "sensor_stable_seconds"
CONF_SENSOR_LOSS_GRACE_SECONDS = "sensor_loss_grace_seconds"
CONF_MANUAL_OVERRIDE_MINUTES = "manual_override_minutes"
CONF_SETPOINT_COMPENSATION = "setpoint_compensation"

AGGREGATE_MEAN = "mean"
AGGREGATE_MIN = "min"
AGGREGATE_MAX = "max"
AGGREGATE_WEIGHTED = "weighted"
CONF_TEMPERATURE_WEIGHTS = "temperature_weights"
MODEL_P_MAX = {"s5225": 1200, "s5232": 1600, "custom": 1200}
DEFAULT_RUNNING_POWER_W = 120
DEFAULT_FILTER_ALARM_HOURS = 500
UNIT_OFFLINE_ALERT_MINUTES = 10

DEFAULT_HEAT_TARGET = 21.0
DEFAULT_COOL_TARGET = 24.0
DEFAULT_HYSTERESIS = 0.5
DEFAULT_START_HYSTERESIS = 0.1
DEFAULT_MIN_RUN_MINUTES = 20
DEFAULT_MIN_OFF_MINUTES = 10
DEFAULT_SENSOR_STABLE_SECONDS = 120
DEFAULT_SENSOR_LOSS_GRACE_SECONDS = 300
DEFAULT_MANUAL_OVERRIDE_MINUTES = 120
DEFAULT_OPENING_OFF_DELAY_SECONDS = 120  # legacy
DEFAULT_OPENING_MIN_DELAY_MINUTES = 5
DEFAULT_OPENING_OFF_DELAY_MINUTES = 20
DEFAULT_OPENING_RESUME_DELAY_MINUTES = 10
# What the unit is turned down to while a door/window is open (before switch-off).
OPENING_REDUCE_SETPOINT = "setpoint"
OPENING_REDUCE_MINIMUM = "minimum"
DEFAULT_OPENING_REDUCE_TO = OPENING_REDUCE_SETPOINT

PRESET_AUTO = "auto"
PRESET_MANUAL = "manual"

OPENING_STAGE_NONE = "none"
OPENING_STAGE_MIN = "min"
OPENING_STAGE_OFF = "off"

PLATFORMS = ["climate", "select", "sensor", "binary_sensor", "button", "switch", "number"]

# Dispatcher signal sent whenever a thermostat writes its state.
SIGNAL_UPDATE = "qlima_control_update_{}"

# Countdowns exposed as timestamp sensors, in display order.
COUNTDOWN_MANUAL_OVERRIDE = "manual_override"
COUNTDOWN_OPENING_REDUCE = "opening_reduce"
COUNTDOWN_OPENING_OFF = "opening_off"
COUNTDOWN_OPENING_RESUME = "opening_resume"
COUNTDOWN_START_ALLOWED = "start_allowed"
COUNTDOWN_MIN_RUN = "min_run"
COUNTDOWN_SENSOR_LOSS = "sensor_loss"
COUNTDOWNS = (
    COUNTDOWN_MANUAL_OVERRIDE,
    COUNTDOWN_OPENING_REDUCE,
    COUNTDOWN_OPENING_OFF,
    COUNTDOWN_OPENING_RESUME,
    COUNTDOWN_START_ALLOWED,
    COUNTDOWN_MIN_RUN,
    COUNTDOWN_SENSOR_LOSS,
)

# What the thermostat is doing right now (enum sensor).
STATUSES = (
    "off",
    "manual",
    "opening_off",
    "opening_reduced",
    "opening_resume_wait",
    "opening_reduce_wait",
    "sensor_lost",
    "start_wait",
    "heating",
    "cooling",
    "drying",
    "fan_only",
    "idle",
)
