# Changelog

## 1.7.3 (firmware 1.3.0)
- Firmware: **TX/RX found automatically.** When the white and green wires are the other way round, the ESP gets no answer, swaps TX/RX after 20 s (GPIO matrix, no rewiring) and remembers the way that works for the next start. When neither way answers it gives up after 6 tries and keeps the saved way. New diagnostic *UART-ben*: Normal / Byttet / Søger / Intet svar.

## 1.7.2
- **Follow Me is steered instead of the unit's target** (replaces the 1.7.1 nudge): the unit's own target stays put, and the value sent as Follow Me is the target −1 from 0.2 °C below target until the target is reached (full effort, no turning down before), the target itself from there (the unit modulates), and the target +1 from +0.3 °C (off before +0.5 °C) until the room is 0.2 °C below again. Cooling mirrors it. Shown as `follow_me_step` / `follow_me_sent` on the thermostat; the value goes out at once when the step, the target or the mode changes.

## 1.7.1
- **The unit no longer idles half a degree below target.** Follow Me reaches the unit in whole degrees (the IR format has no decimals), and the unit only starts a degree past its target – so at 21.6 °C with 22 °C wanted it read "22" and stood still. While the unit regulates, its target is now nudged a degree (heat up, cool down) when the room is 0.3 K off and the unit does not run, and set back once the room is within 0.1 K. The thermostat shows the nudge as `unit_nudge`.

## 1.7.0
- **ESP32 status** on every Qlima Control device: *Connected*, *Unit not answering*, *New firmware ready* or *Not connected*, with firmware, newest firmware, ESPHome version, IP, MAC, Wi-Fi signal, uptime and whether the unit answers as attributes.
- The ESP's own diagnostics on the Qlima Control device: *ESP32 connected*, *Wi-Fi signal*, *IP address*, *uptime* and *ESPHome version*.
- Diagnostics download includes the ESP's status (IP and MAC redacted).

## 1.6.0
- **Firmware** entity on every Qlima Control device: the version the ESP runs, the newest released firmware (checked every 6 h) and *Install*, which goes through the ESP's own online update or the ESPHome dashboard.
- Firmware: the version is set once in the shared package `esphome/qlima-control.yaml`, so ESPs built from your own yaml report the real version. Remove any `project:` block from your own yaml – it overrides the package's version.

## 1.5.1
- Unit preset select: Eco is only offered in cool and auto – the Qlima S 5232 answers an Eco request in heat with Eco off (seen on the UART). A preset the unit refuses in a mode is no longer offered in that mode (until Home Assistant restarts).

## 1.5.0 (firmware 1.2.0)
- **The unit holds the target itself** (new setting *Regulation*, default): with Follow Me on, the unit gets the room target and regulates on the room temperature – no more on/off hysteresis and no idling at 17 °C when the room is warm. Only an open door/window turns it down (always to the minimum), and only a long-open one switches it off. While Follow Me is off or no room temperature exists, the thermostat with hysteresis and neutral idle takes over as before. *Regulation → The thermostat* keeps the old behaviour.
- Target, price and away changes reach the unit at once; the thermostat shows *idle* while the unit holds the target without running.
- **Unit preset select** only offers the presets the unit takes in its current mode (only "Normal" while it is off) and says so when the unit did not take the chosen one, instead of silently doing nothing.
- Firmware: commands sent close together are no longer dropped (MideaUART PR #48), **ECO** is sent in heat and auto too (upstream only allowed it in cool, so choosing Eco in heat did nothing), and the unit's **fault code** and **filter alarm** from its status frame.
- Firmware: no more power-usage query every 30 s and no capabilities query at boot – Qlima never answers them, and each held the bus for three 2-second retries, delaying commands.
- Firmware uses MRDonnii/MideaUART (branch `qlima-control`) through the extended `midea` component in `components/`.
- Optional package `esphome/qlima-control-servicedata.yaml` for other Midea units: the unit's **service data** (compressor frequency, coils, discharge temperature, fans, outdoor unit power, defrost). Qlima S 5225/5232 do not answer it (tested with three frame layouts). When a unit reports them, defrosts are counted from its own defrost flag, and wake-up and *idle* look at the compressor frequency.

## 1.4.1
- Turbo on a unit that does not heat or cool now gives a clear message instead of a server error.
- The Wake-up sensor follows its switch at once instead of after the next minute.

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
