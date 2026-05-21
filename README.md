# IC-7300 Clock Sync

Automatically synchronises the Icom IC-7300 real-time clock via CI-V over USB — both on hot-plug and when the radio is already connected at boot. Time is sourced from NTP (India pool) with a system clock fallback.

## How it works

1. **udev** detects the IC-7300's USB-serial chip (Silicon Labs CP210x, VID `10c4` / PID `ea60`) and creates a stable symlink `/dev/ic7300`. It also registers a `SYSTEMD_WANTS` tag so systemd starts the sync service as soon as it is ready — whether the radio was connected before or after boot.
2. The **systemd oneshot service** waits 5 s for the radio's CI-V firmware to initialise, then runs `ic7300_clock_sync.py -p /dev/ic7300`.
3. The script queries NTP, waits for the next full-minute boundary, then sends CI-V Set Date and Set Time commands to the radio.

### Why `SYSTEMD_WANTS` instead of `RUN+=systemctl`

The old approach (`RUN+="/bin/systemctl --no-block start ..."`) races against systemd startup during boot — if udev fires before systemd is ready, the `systemctl` call silently fails and the radio clock never syncs. `SYSTEMD_WANTS` hands the start request to PID 1 itself, which queues it properly regardless of boot order.

### Why `/dev/ic7300`

The kernel assigns `/dev/ttyUSB0`, `/dev/ttyUSB1`, etc. based on enumeration order, which can shift if other USB-serial adapters are present. The udev rule creates a stable `/dev/ic7300` symlink tied to this specific radio's USB serial number, so the service always opens the right port.

### CI-V Transceive broadcast handling

The IC-7300 continuously broadcasts CI-V frames (frequency, mode, etc.) on the serial line, each terminated with `0xFD`. A naive "read until `0xFD`" loop stops at the first broadcast frame and never sees the actual OK reply. The script's `read_until_ok()` function reads past broadcast frames and returns only when it finds the `FB` (OK) or `FA` (NG) response addressed back to the controller, or the 3-second timeout expires. CI-V commands are retried up to 3 times on no response.

## What you need

### Hardware
- Icom IC-7300 transceiver
- USB-A to USB-B cable (the standard square-connector cable supplied with the radio)
- Linux PC — Ubuntu 20.04+ / Debian 11+ / Fedora 36+ or any distro with systemd 245+

### Software
- `systemd` and `udev` — standard on all the above distros
- Python 3.10 or newer
- Python packages:

```bash
pip install pyserial ntplib
```

### Radio settings

| Menu path | Setting | Required value |
|-----------|---------|----------------|
| MENU > SET > Connectors > CI-V > CI-V Baud Rate | Baud rate | `115200` |
| MENU > SET > Connectors > CI-V > CI-V Address | Radio CI-V address | `94h` (factory default) |
| MENU > SET > Connectors > CI-V > CI-V Echo | Echo back | `OFF` |

### Linux user permissions

Your user must be in the `dialout` or `plugdev` group to open the serial port. The install script adds you to `dialout` automatically — log out and back in for it to take effect.

```bash
groups $USER
```

## Files

| File | Purpose |
|------|---------|
| `ic7300_clock_sync.py` | Main sync script — can also be run manually |
| `99-ic7300-clock-sync.rules` | udev rule — creates `/dev/ic7300` symlink and triggers service |
| `ic7300-clock-sync.service` | systemd oneshot service unit (template filled in by `install.sh`) |
| `install.sh` | One-time setup: installs the rule and service, triggers sync for already-connected radio |

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
- Install `99-ic7300-clock-sync.rules` → `/etc/udev/rules.d/` and reload udev
- Install the configured service → `/etc/systemd/system/` and reload systemd
- Add your user to the `dialout` group
- If the radio is already connected, trigger a udev re-scan so the sync starts immediately without unplugging

### 4. Done

From this point on, connecting the radio via USB — or booting with it already connected — triggers an automatic clock sync. No unplug/replug required.

## Verifying it worked

```bash
journalctl -u ic7300-clock-sync.service -n 40
```

A successful sync looks like:

```
19:12:32  INFO     Port    :  /dev/ic7300   @  115200 baud
19:12:32  INFO     NTP OK  offset=-6.7 ms  UTC=2026-05-21 13:42:32
19:12:32  INFO     Waiting for full-minute boundary for precise sync ...
19:13:00  INFO     Boundary reached — sending 19:13 Local
19:13:00  INFO       SET DATE  2026-05-21  ✓
19:13:00  INFO       SET TIME  19:13 Local  ✓
19:13:00  INFO     ✓  IC-7300 clock synced to 2026-05-21 19:13 Local  [NTP]
```

The script waits for the next full-minute boundary before sending — up to 60 seconds after trigger — because CI-V cannot set seconds. This gives precise to-the-minute accuracy.

Follow the log live:

```bash
journalctl -u ic7300-clock-sync.service -f
```

## Manual use

```bash
python3 ic7300_clock_sync.py                    # auto-detect port, local time
python3 ic7300_clock_sync.py -p /dev/ic7300     # explicit port
python3 ic7300_clock_sync.py --utc              # send UTC instead of local time
python3 ic7300_clock_sync.py --no-wait          # send immediately, skip minute boundary
python3 ic7300_clock_sync.py --dry-run          # preview CI-V frames, nothing sent
python3 ic7300_clock_sync.py --list-ports       # list all serial ports and exit
python3 ic7300_clock_sync.py -v                 # verbose — shows raw TX/RX CI-V bytes
```

## Troubleshooting

**No OK response / sync incomplete**
- Confirm CI-V baud rate in radio menu is `115200`
- Confirm CI-V address is `94h`
- Turn CI-V Echo **OFF** in radio menu
- Run `python3 ic7300_clock_sync.py -v` to see raw CI-V byte traces

**Port not detected**
- Run `python3 ic7300_clock_sync.py --list-ports`
- Confirm `groups $USER` includes `dialout` or `plugdev`; log out and back in after install

**Service does not start after boot (radio already connected)**

Re-run the install script — it reloads udev and triggers the scan for already-present devices:

```bash
sudo bash install.sh
```

To manually trigger without reinstalling:

```bash
sudo udevadm trigger --action=add /sys/class/tty/ttyUSB0
sudo udevadm settle
journalctl -u ic7300-clock-sync.service -f
```

**`/dev/ic7300` symlink missing**

The symlink is created by the udev rule. If it is absent after install, trigger udev manually:

```bash
sudo udevadm trigger --action=add /sys/class/tty/ttyUSB0 && sudo udevadm settle
ls -la /dev/ic7300
```
