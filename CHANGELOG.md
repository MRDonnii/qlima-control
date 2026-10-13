# Changelog

## 1.2.0
- Power (W) and energy (kWh) from the linked meter (e.g. the Shelly) as the unit's own sensors.
- "React to doors and windows" switch and three numbers for turn-down, switch-off and resume delays. They apply at once, without reloading.

## 1.1.1
- Heat price and cost per hour no longer use the 'measurement' state class (not allowed for monetary sensors).

## 1.1.0
- Supply switch (e.g. the Shelly feeding the unit): "Supply" switch, "Restart unit (supply)" button that cuts power for 30 s, and optional automatic restart when the unit has not answered for a set time (at most once an hour).
- Settings menu after setup: Unit, Room sensors (+ weights), Power and Shelly, Doors and windows, Control, Notifications, Advanced – change one group at a time. The ESP device can be changed too.

## 1.0.1
- A newly added unit takes over the mode the unit is running in, instead of starting as off.
- Manifest keys sorted for hassfest.

## 1.0.0
- First release: Qlima Control integration and ESP32-C3 firmware.
- Integration: room thermostat with hysteresis and neutral idle, door/window staging, release/status/countdowns (from Heat Pump Thermostat 0.4.7), multiple room sensors (mean, weighted %, min, max) pushed to the unit as Follow Me, humidity, power meter (heating/cooling, heat output, COP, running-without-power), electricity price (heat price, cost per hour), outdoor temperature, filter hours, unit responding, IR buttons (swing, louver step, display), Follow Me and beep switches, phone notifications.
- Firmware: Midea UART control, IR Follow Me/swing/display, filter hours, local web UI, WiFi setup portal, self-update from GitHub releases.
