# Changelog

## 1.4.0
- **Price steering**: the target goes a whole degree up in cheap periods and down in expensive ones (cooling the other way), ranked against the forecast from 6 h back to 24 h ahead and held at least 30 minutes. Reads Strømligning, Nord Pool, Energi Data Service, Tibber-style and generic price forecasts; quarter or hour periods, or follow a select.
- **Wake-up**: optional power-cycle when the unit idles on low power while the room stays below target (Midea units drop to a low-effort hold that only a power cycle clears). At most 3 a day, 2 hours apart, never during defrost or when the unit should not run.
- **Turbo 30 min** switch: the unit's boost preset for half an hour.
- After any power-cycle (button, auto-restart or wake-up) the unit coming back in another mode is no longer taken for a remote-control stop; the mode is repaired.
- New entities: Price level, Wake-up, Price steering, Wake when idling, Turbo 30 min, Price: up when cheap / down when expensive. New settings page *Price and wake-up*.

## 1.3.1
- Finds the ESPHome device through `config_entry_id` on Home Assistant 2026.10+ (removes the deprecation warning about `DeviceEntry.config_entries`).

## 1.3.0 (firmware 1.1.0)
- Energy and cost today and this month, runtime today, starts today and defrosts today – saved, so they survive restarts.
- Defrost detection ("Afrimer") from the power pattern in the cold.
- Optional DS18B20 supply/return sensors on the ESP (GPIO5/GPIO4): measured heat output and measured COP.
- Away setback: link a person, group or alarm; the heat target drops (cool target rises) while nobody is home.
- Electricity price picker that lists Strømligning, Nord Pool, Energi Data Service, Tibber and other monetary sensors.
- Diagnostics download and repair issues (old firmware, missing room sensors, ESP not found).
- Firmware: "Opdatér automatisk" switch – installs new releases by itself; firmware is only rebuilt when it changes.
- Wiring diagram in the README and on the installer page.

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
