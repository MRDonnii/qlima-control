"""Finds out by itself which way TX/RX are wired to the unit and remembers it."""

import esphome.codegen as cg
from esphome.components import uart
import esphome.config_validation as cv
from esphome.const import CONF_ID, CONF_UART_ID

CODEOWNERS = ["@MRDonnii"]
DEPENDENCIES = ["uart", "esp32"]

CONF_TX_PIN = "tx_pin"
CONF_RX_PIN = "rx_pin"
CONF_ANSWERED = "answered"
CONF_PROBE_INTERVAL = "probe_interval"
CONF_MAX_SWITCHES = "max_switches"

qlima_uart_ns = cg.esphome_ns.namespace("qlima_uart")
UartAutoSwap = qlima_uart_ns.class_("UartAutoSwap", cg.Component)

CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.declare_id(UartAutoSwap),
        cv.Required(CONF_UART_ID): cv.use_id(uart.UARTComponent),
        # The pins as wired by the book; the same numbers as the uart's tx_pin/rx_pin.
        cv.Required(CONF_TX_PIN): cv.int_range(min=0, max=48),
        cv.Required(CONF_RX_PIN): cv.int_range(min=0, max=48),
        # True once the unit has answered (a valid frame came in).
        cv.Required(CONF_ANSWERED): cv.returning_lambda,
        cv.Optional(CONF_PROBE_INTERVAL, default="20s"): cv.positive_time_period_milliseconds,
        cv.Optional(CONF_MAX_SWITCHES, default=6): cv.int_range(min=1, max=50),
    }
).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    parent = await cg.get_variable(config[CONF_UART_ID])
    cg.add(var.set_uart(parent))
    cg.add(var.set_pins(config[CONF_TX_PIN], config[CONF_RX_PIN]))
    answered = await cg.process_lambda(config[CONF_ANSWERED], [], return_type=cg.bool_)
    cg.add(var.set_answered(answered))
    cg.add(var.set_probe_interval(config[CONF_PROBE_INTERVAL]))
    cg.add(var.set_max_switches(config[CONF_MAX_SWITCHES]))
