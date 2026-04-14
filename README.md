# IC-7300 Clock Sync

Automatically synchronises the Icom IC-7300 real-time clock via CI-V over USB whenever the radio is connected. Time is sourced from NTP (India pool) with a system clock fallback.

## How it works

1. **udev** detects the IC-7300's USB-serial chip (Silicon Labs CP210x, VID `10c4` / PID `ea60`) the moment the cable is plugged in.
2. udev sets the device group to `plugdev` and runs `systemctl --no-block start ic7300-clock-sync.service`.
3. The **systemd oneshot service** runs `ic7300_clock_sync.py` as your user, waits 2 s for the radio's CI-V stack to settle, then syncs date and time.

## Requirements

- Linux with systemd and udev (standard on Ubuntu/Debian/Fedora)
- Python 3.10+
- Python packages: `pyserial`, `ntplib`

```bash
pip install pyserial ntplib
```

## Files

| File | Purpose |
|------|---------|
| `ic7300_clock_sync.py` | Main sync script — can also be run manually |
| `99-ic7300-clock-sync.rules` | udev rule — triggers on USB connect |
| `ic7300-clock-sync.service` | systemd oneshot service unit |
| `install.sh` | Installs the udev rule and service (run once as root) |

## Installation

### 1. Clone or download this folder

```bash
git clone <repo-url> ~/code/icomremote
cd ~/code/icomremote
```

### 2. Install Python dependencies

```bash
pip install pyserial ntplib
```

### 3. Run the install script

```bash
sudo bash install.sh
```

This will:
- Copy `99-ic7300-clock-sync.rules` to `/etc/udev/rules.d/` and reload udev
- Copy `ic7300-clock-sync.service` to `/etc/systemd/system/` and reload systemd
- Add your user to the `dialout` group (takes effect at next login)

### 4. Plug in the IC-7300

That's it. From this point on, connecting the radio via USB triggers an automatic clock sync. No reboot required.

## Verifying it worked

After plugging in the radio, check the service log:

```bash
journalctl -u ic7300-clock-sync.service -n 40
```

A successful sync looks like:

```
09:39:24  INFO     ✓ Tier 1: port identified via USB VID/PID → /dev/ttyUSB0
09:39:32  INFO     NTP OK  offset=+24.4 ms  UTC=2026-04-14 04:09:32
09:39:32  INFO     Waiting for full-minute boundary for precise sync ...
09:40:00  INFO     Boundary reached — sending 09:40 Local
09:40:00  INFO       SET DATE  2026-04-14  ✓
09:40:00  INFO       SET TIME  09:40 Local  ✓
09:40:00  INFO     ✓  IC-7300 clock synced to 2026-04-14 09:40 Local  [NTP]
```

To follow the log live while plugging in:

```bash
journalctl -u ic7300-clock-sync.service -f
```

## Manual use

The script can be run directly at any time without the udev/systemd automation:

```bash
# Auto-detect port, local time (default)
python3 ic7300_clock_sync.py

# Explicit port
python3 ic7300_clock_sync.py -p /dev/ttyUSB0

# Send UTC instead of local time
python3 ic7300_clock_sync.py --utc

# Preview CI-V frames without transmitting
python3 ic7300_clock_sync.py --dry-run

# List all serial ports and exit
python3 ic7300_clock_sync.py --list-ports

# Verbose output — shows raw TX/RX CI-V bytes
python3 ic7300_clock_sync.py -v
```

## Radio configuration

Verify these settings on the IC-7300 before first use:

| Menu path | Setting | Value |
|-----------|---------|-------|
| MENU > SET > Connectors > CI-V > CI-V Baud Rate | Baud rate | `115200` (must match `BAUD_RATE` in script) |
| MENU > SET > Connectors > CI-V > CI-V Address | Radio address | `94h` (default, matches `CIV_RADIO_ADDR`) |
| MENU > SET > Connectors > CI-V > CI-V Echo | Echo | `OFF` |

## Troubleshooting

**Sync incomplete / no OK response**
- Confirm CI-V baud rate in the radio menu matches `BAUD_RATE` in the script (default 115200).
- Confirm CI-V address is `94h`.
- Turn CI-V Echo **OFF** in the radio menu.
- Run with `-v` to see raw CI-V byte traces.

**Port not detected**
- Run `python3 ic7300_clock_sync.py --list-ports` to see all connected serial devices.
- On Linux, confirm your user is in the `dialout` or `plugdev` group: `groups $USER`. Log out and back in after the install script adds you.

**Service never starts after plug-in**

First verify the rule is matching and the service starts correctly on its own:

```bash
# Confirm the rule matches the device
udevadm test /sys/class/tty/ttyUSB0 2>&1 | grep ic7300

# Start the service manually to confirm it works
systemctl start ic7300-clock-sync.service
journalctl -u ic7300-clock-sync.service -n 20
```

If the manual start works but plug-in doesn't trigger it, reload and replay:

```bash
sudo bash install.sh
sudo systemctl daemon-reload
sudo udevadm control --reload-rules

# Replay the plug-in event without unplugging:
sudo udevadm trigger --action=add --subsystem-match=tty --attr-match=idVendor=10c4

# Check the result:
sleep 5 && journalctl -u ic7300-clock-sync.service -n 20
```
