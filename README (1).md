# FastLifeBG — Live TDI ECU Display

A real-time digital gauge cluster for Volkswagen Audi Group (VAG) 1.9 TDI diesel engines, built on a Raspberry Pi. It speaks the KW1281 diagnostic protocol directly to a Bosch EDC15P+ engine control unit over the K-Line bus, reads live engine data, and renders it on a compact TFT display mounted in the dashboard.

![Status](https://img.shields.io/badge/status-working-success) ![Platform](https://img.shields.io/badge/platform-Raspberry%20Pi-c51a4a) ![Language](https://img.shields.io/badge/python-3.x-blue) ![License](https://img.shields.io/badge/license-MIT-green)

---

## What it does

The factory dashboard on these cars shows almost nothing — a coolant gauge and a fuel gauge. This project taps into the same diagnostic port a mechanic uses (the one VCDS/VAG-COM connects to) and pulls real engine telemetry the factory cluster never displays: actual vs. requested boost pressure, per-cylinder injector balance, intake air temperature, fuel temperature, injection quantity, VNT turbo vane position, calculated road speed, fuel consumption, battery voltage, and stored fault codes.

It then presents all of this on a dash-mounted screen with two complete visual themes, configurable colour schemes, audible warnings for dangerous conditions, and built-in performance timers (0–100, 100–200 km/h).

---

## Key features

- **Direct ECU communication** — implements the KW1281 serial protocol from scratch in pure Python, including the 5-baud slow init, block counter handshaking, and the turn-token ping-pong that the protocol requires
- **Live engine telemetry** — boost, injection, temperatures, turbo vane position, load, and more, polled roughly every 250 ms
- **Per-cylinder injector diagnostics** — reads the ECU's fuel balancing values and flags a worn or stuck injector before it causes engine damage
- **Boost deviation monitoring** — compares actual boost to the ECU's target and triggers a visual + audible warning on sustained overboost
- **Two visual themes** — an icon-based layout and a bold "block" layout inspired by modern digital clusters, switchable on the fly
- **10 configurable accent colours** — match the display to the car's existing dashboard lighting; warning colours automatically shift to stay readable
- **Performance timers** — 0–100, 0–200, and rolling 100–200 km/h with speedometer-error correction
- **Fault code reader** — decodes stored DTCs to the searchable VAG number plus a best-effort OBD-II P-code
- **Two physical buttons** — navigate pages and adjust settings without a touchscreen, designed for use while driving
- **Auto-start on boot** — runs as a systemd service, comes alive with the ignition
- **Settings persist** — colour, brightness, and theme are saved to disk and restored on reboot

---

## Hardware

| Component | Detail |
|-----------|--------|
| Computer | Raspberry Pi 3B (development), Pi Zero WH (production target) |
| Display | ILI9341 240×320 TFT over SPI |
| Diagnostic cable | CH340-based KKL USB → K-Line adapter (`/dev/ttyUSB0`) |
| Buzzer | 3-pin active buzzer module |
| Buttons | 2× momentary push buttons (navigate + confirm) |
| Target ECU | Bosch EDC15P+ — VAG part 038906019DQ, 1.9 TDI ASZ 131 PS |
| Protocol | KW1281 over K-Line, 9600 baud, ECU address 0x01 |

### Wiring (Raspberry Pi GPIO, BCM numbering)

| Function | GPIO | Notes |
|----------|------|-------|
| Display SCK | GPIO11 | SPI clock |
| Display MOSI | GPIO10 | SPI data |
| Display CS | GPIO7 | SPI CE1 |
| Display DC | GPIO24 | data/command |
| Display RST | GPIO25 | reset |
| Display backlight | GPIO23 | PWM brightness control |
| Display VCC | 3.3 V | **not 5 V** |
| Buzzer signal | GPIO18 | active buzzer |
| Button 1 (navigate) | GPIO17 | to GND, internal pull-up |
| Button 2 (confirm) | GPIO27 | to GND, internal pull-up |
| KKL cable | USB | appears as `/dev/ttyUSB0` |

Switched 12 V power is taken from a fuse that is only live with the engine running, stepped down to 5 V by a DC-DC buck converter, so the unit powers up and shuts down with the car.

---

## Software

| File | Description |
|------|-------------|
| `edc15_driver.py` | KW1281 protocol driver — connection, block handshaking, group reads, fault-code reads, value decoding |
| `edc15_display.py` | Display application — two themes, five pages each, button handling, settings, alarms, performance timers |

### Dependencies

```bash
pip3 install luma.lcd Pillow pyserial --break-system-packages
```

SPI must be enabled (`sudo raspi-config` → Interface Options → SPI). The CH340 USB driver is built into modern Linux kernels.

---

## Running

```bash
# Simulator mode — no hardware needed, generates fake data
python3 edc15_display.py --sim --page 1

# Live, connected to the car
python3 edc15_display.py --port /dev/ttyUSB0
```

Theme 1 lives on pages 1–5, Theme 2 on pages 6–10. The two physical buttons cycle pages and drive the settings menu.

### Run on boot (systemd)

```ini
[Unit]
Description=FastLifeBG ECU Display
After=multi-user.target

[Service]
Type=simple
User=admin
WorkingDirectory=/home/admin/fastlifebg
ExecStart=/usr/bin/python3 /home/admin/fastlifebg/edc15_display.py --port /dev/ttyUSB0
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

---

## How the protocol works

KW1281 is an old, quirky, half-duplex serial protocol. The two devices take turns, and every byte sent must be acknowledged by the other side echoing back its complement. The connection is opened by transmitting the ECU address one bit at a time at five bits per second (the "5-baud init"). After that, the ECU streams identification blocks and then expects a continuous back-and-forth of acknowledgement blocks to keep the session alive — go quiet for too long and it hangs up.

The trickiest part, and the bulk of the debugging in this project, was the turn-taking: after certain blocks the ECU hands control back with a token, and the driver must *take* the turn by staying silent rather than replying — replying creates an infinite ping-pong loop that locks the session. Getting that handshake right is what made the connection stable enough to use while driving.

The data itself comes in "measurement groups", each containing a few values encoded as a type byte plus two data bytes, decoded with a per-type formula (e.g. one type means `0.01 × a × b` for boost pressure).

---

## Notes

- This project communicates with a real engine ECU. It only ever reads data and reads stored fault codes; it does not write to the ECU or modify any engine parameters.
- Built and tested on one specific car (a remapped 131 PS ASZ). Measurement group layouts vary between ECU software versions, so the group mapping may need adjustment for other cars.
- Diagnostic group numbers and decoding formulas were confirmed by live scanning and cross-referenced against open KW1281 implementations.

---

## License

MIT — see `LICENSE`.
