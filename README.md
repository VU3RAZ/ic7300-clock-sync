# IC-7300 Clock Sync

Automatically synchronises the Icom IC-7300 real-time clock via CI-V over USB whenever the radio is connected. Time is sourced from NTP (India pool) with a system clock fallback.

## How it works

1. **udev** detects the IC-7300's USB-serial chip (Silicon Labs CP210x, VID `10c4` / PID `ea60`) the moment the cable is plugged in.
2. udev sets the device group to `plugdev` and runs `systemctl --no-block start ic7300-clock-sync.service`.
3. The **systemd oneshot service** runs `ic7300_clock_sync.py` as your user, waits 2 s for the radio's CI-V stack to settle, then syncs date and time.

## What you need

### Hardware
- Icom IC-7300 transceiver
- USB-A to USB-B cable (the standard square-connector cable supplied with the radio)
- Linux PC — Ubuntu 20.04+ / Debian 11+ / Fedora 36+ or any distro with systemd 245+

### Software
- `systemd` and `udev` — standard on all the above distros, nothing extra to install
- `Python 3.10` or newer
- Python packages `pyserial` and `ntplib`:

```bash
pip install pyserial ntplib
```

### Radio settings

Three menu items on the IC-7300 must be set correctly before the sync will work:

| Menu path | Setting | Required value |
|-----------|---------|----------------|
| MENU > SET > Connectors > CI-V > CI-V Baud Rate | Baud rate | `115200` |
| MENU > SET > Connectors > CI-V > CI-V Address | Radio CI-V address | `94h` (factory default) |
| MENU > SET > Connectors > CI-V > CI-V Echo | Echo back | `OFF` |

### Linux user permissions

Your Linux user must be a member of either the `dialout` or `plugdev` group to open the serial port. The install script adds you to `dialout` automatically, but it only takes effect after you log out and back in.

Check your current groups:
```bash
groups $USER
```

## Files

| File | Purpose |
|------|---------|
| `ic7300_clock_sync.py` | Main sync script — can also be run manually |
| `99-ic7300-clock-sync.rules` | udev rule — triggers on USB connect |
| `ic7300-clock-sync.service` | systemd oneshot service unit (template — filled in by install script) |
| `install.sh` | One-time setup: installs the rule and service, adds user to dialout |

## Installation

### 1. Clone this repo

```bash
git clone https://github.com/VU3RAZ/ic7300-clock-sync
cd ic7300-clock-sync
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
- Substitute your username and Python path into the service file
- Copy `99-ic7300-clock-sync.rules` → `/etc/udev/rules.d/` and reload udev
- Copy the configured service → `/etc/systemd/system/` and reload systemd
- Add your user to the `dialout` group (log out and back in for this to take effect)

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

The script waits for the next full minute boundary before sending, so there can be up to 60 seconds between plug-in and the actual sync. This gives precise to-the-minute accuracy since CI-V cannot set seconds.

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

# Skip waiting for the minute boundary (send immediately)
python3 ic7300_clock_sync.py --no-wait

# Preview CI-V frames without transmitting
python3 ic7300_clock_sync.py --dry-run

# List all serial ports and exit
python3 ic7300_clock_sync.py --list-ports

# Verbose output — shows raw TX/RX CI-V bytes
python3 ic7300_clock_sync.py -v
```

## Troubleshooting

**Sync incomplete / no OK response from radio**
- Confirm CI-V baud rate in the radio menu is `115200`.
- Confirm CI-V address is `94h`.
- Turn CI-V Echo **OFF** in the radio menu.
- Run `python3 ic7300_clock_sync.py -v` to see raw CI-V byte traces.

**Port not detected**
- Run `python3 ic7300_clock_sync.py --list-ports` to list all serial devices.
- Confirm your user is in `dialout` or `plugdev`: `groups $USER`. Log out and back in after the install script adds you.

**Service never starts after plug-in**

First confirm the rule matches and the service works on its own:

```bash
# Confirm the udev rule matches the device
udevadm test /sys/class/tty/ttyUSB0 2>&1 | grep ic7300

# Start the service manually to confirm it works end-to-end
systemctl start ic7300-clock-sync.service
journalctl -u ic7300-clock-sync.service -n 20
```

If the manual start works but plug-in doesn't trigger it, re-run the install and replay the event without unplugging:

```bash
sudo bash install.sh

# Replay the plug-in event for the currently connected radio:
sudo udevadm trigger --action=add --subsystem-match=tty --attr-match=idVendor=10c4

# Check the result:
sleep 5 && journalctl -u ic7300-clock-sync.service -n 20
```
