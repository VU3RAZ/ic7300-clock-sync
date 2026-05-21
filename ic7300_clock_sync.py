#!/usr/bin/env python3
"""
ic7300_clock_sync.py  —  Icom IC-7300 Clock Synchronizer via CI-V
==================================================================
Automatically detects the IC-7300 serial/USB port, queries NTP for
accurate time, and updates the radio clock via CI-V commands.
Falls back to the local system clock if NTP is unreachable.

Requirements:
    pip install pyserial ntplib

Usage:
    python ic7300_clock_sync.py                  # auto-detect port, local time
    python ic7300_clock_sync.py -p /dev/ttyUSB0  # explicit port (Linux)
    python ic7300_clock_sync.py -p COM3           # explicit port (Windows)
    python ic7300_clock_sync.py --utc             # send UTC instead of local time
    python ic7300_clock_sync.py --no-wait         # skip wait-for-:00 sec
    python ic7300_clock_sync.py --dry-run         # preview frames, nothing sent
    python ic7300_clock_sync.py --list-ports      # show all serial ports & exit
    python ic7300_clock_sync.py -v                # verbose / raw byte debug

CI-V frame format (IC-7300):
    FE FE [RADIO] [CTRL] [CMD] [SUBCMD] [DATA...] FD

Commands used:
    19 00           Read Transceiver ID  (port probing)
    1A 05 00 94     Set DATE  →  data: CC YY MM DD  (packed BCD)
    1A 05 00 95     Set TIME  →  data: HH MM        (packed BCD, 24-hour)
    FB              OK response from radio
    FA              NG (Not Good) response from radio

Port detection — 3-tier strategy:
    Tier 1  USB VID/PID scan   Silicon Labs CP210x  VID=0x10C4 / PID=0xEA60
    Tier 2  CI-V probe         broadcast "Read ID" cmd 0x19 0x00, confirm 0x94
    Tier 3  Interactive        list ports and ask user to choose

Linux permission note:
    sudo usermod -aG dialout $USER   (log out & back in to take effect)
"""

import sys
import time
import datetime
import argparse
import logging
from typing import Optional

# UTC timezone constant — avoids deprecated utcnow()
UTC = datetime.timezone.utc

# ── Dependency checks ─────────────────────────────────────────────────────────
try:
    import serial
    import serial.tools.list_ports
except ImportError:
    print("ERROR: pyserial not installed.  Run:  pip install pyserial")
    sys.exit(1)

try:
    import ntplib
    NTP_AVAILABLE = True
except ImportError:
    NTP_AVAILABLE = False


# ═════════════════════════════════════════════════════════════════════════════
#  CONFIGURATION  —  edit to match your shack
# ═════════════════════════════════════════════════════════════════════════════

SERIAL_PORT    = 'auto'     # 'auto' = detect automatically
                            # or set explicitly: '/dev/ttyUSB0' / 'COM3'

BAUD_RATE      = 115200     # match MENU > SET > Connectors > CI-V > CI-V Baud Rate

CIV_RADIO_ADDR = 0x94       # IC-7300 default CI-V address
                            # verify: MENU > SET > Connectors > CI-V > CI-V Address

CIV_CTRL_ADDR  = 0xE0       # Controller (PC) address — standard default

USE_UTC        = False      # False = send local time  (IST = UTC+5:30 for Pune) ← default
                            # True  = send UTC  (useful for DX logging software)

WAIT_FOR_ZERO  = True       # Wait until seconds == 0 before sending;
                            # CI-V cannot set seconds, so this gives
                            # precise to-the-minute accuracy.

NTP_SERVERS = [
    '0.in.pool.ntp.org',    # India NTP pool — lowest latency from Pune
    '1.in.pool.ntp.org',
    '2.in.pool.ntp.org',
    'pool.ntp.org',         # global fallback
    'time.google.com',
    'time.cloudflare.com',
    'asia.pool.ntp.org',
]
NTP_TIMEOUT = 4             # seconds to wait per NTP server before trying next

# Known USB identifiers for IC-7300 (Silicon Labs CP2102 bridge)
IC7300_USB_IDS = [
    (0x10C4, 0xEA60),       # Silicon Labs CP210x  — IC-7300 primary chip
]
# Fallback description keywords (case-insensitive) when VID/PID unavailable
IC7300_USB_KEYWORDS = [
    'cp210', 'silicon labs', 'cp2102', 'usb serial', 'ic-7300', 'icom',
]

# ═════════════════════════════════════════════════════════════════════════════


logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-7s  %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('ic7300')


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 1 — PORT DETECTION
# ─────────────────────────────────────────────────────────────────────────────

def list_all_ports() -> list:
    """Return all available serial ports sorted by device name."""
    return sorted(serial.tools.list_ports.comports(), key=lambda p: p.device)


def print_ports(ports: list) -> None:
    """Pretty-print a port list to stdout."""
    if not ports:
        print("  (no serial ports found)")
        return
    print(f"  {'PORT':<20} {'VID:PID':<14} DESCRIPTION")
    print(f"  {'-'*20} {'-'*14} {'-'*40}")
    for p in ports:
        vid_pid = f"{p.vid:04X}:{p.pid:04X}" if p.vid else "—"
        desc    = (p.description or '').strip()
        print(f"  {p.device:<20} {vid_pid:<14} {desc}")


def _score_port(port_info) -> int:
    """
    Heuristic confidence score for a port being the IC-7300.
    100 = exact VID/PID match,  10 = keyword match,  0 = no match.
    """
    score = 0
    if port_info.vid and port_info.pid:
        for vid, pid in IC7300_USB_IDS:
            if port_info.vid == vid and port_info.pid == pid:
                score += 100
                break
    desc = ' '.join(filter(None, [
        port_info.description, port_info.manufacturer, port_info.product
    ])).lower()
    if any(kw in desc for kw in IC7300_USB_KEYWORDS):
        score += 10
    return score


def _probe_civ_id(port: str, baud: int, ctrl: int,
                  timeout: float = 1.5) -> Optional[int]:
    """
    Open port, send CI-V broadcast "Read Transceiver ID" (cmd 19 00),
    and return the CI-V address reported by the radio, or None.

    TX: FE FE 00 E0 19 00 FD          (broadcast — addr 0x00)
    RX: FE FE E0 94 19 00 94 FD       (IC-7300 replies with its CI-V addr)
    """
    frame = bytes([0xFE, 0xFE, 0x00, ctrl, 0x19, 0x00, 0xFD])
    try:
        with serial.Serial(
            port=port, baudrate=baud,
            bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE, timeout=timeout
        ) as ser:
            ser.reset_input_buffer()
            ser.write(frame)
            time.sleep(0.35)

            buf      = bytearray()
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                n = ser.in_waiting
                if n:
                    buf.extend(ser.read(n))
                    if 0xFD in buf:
                        break
                else:
                    time.sleep(0.05)

        log.debug(f"  probe {port}  TX: {frame.hex(' ').upper()}")
        log.debug(f"  probe {port}  RX: {bytes(buf).hex(' ').upper()}")

        # Parse: FE FE [ctrl] [radio_addr] 19 00 [id_byte] FD
        i = 0
        while i <= len(buf) - 8:
            if (buf[i]   == 0xFE and buf[i+1] == 0xFE and
                    buf[i+2] == ctrl and buf[i+4] == 0x19 and buf[i+5] == 0x00):
                radio_id = buf[i+6]
                log.debug(f"  probe {port}  CI-V addr in response: 0x{radio_id:02X}")
                return radio_id
            i += 1

    except (serial.SerialException, OSError) as exc:
        log.debug(f"  probe {port}: {exc}")

    return None


def detect_ic7300_port(baud:         int = BAUD_RATE,
                       ctrl:         int = CIV_CTRL_ADDR,
                       expected_civ: int = CIV_RADIO_ADDR) -> Optional[str]:
    """
    Detect the IC-7300 serial port using a 3-tier strategy.

    Tier 1 — USB VID/PID scan
        Scores every port.  Single high-confidence (score >= 100) match → done.
        Multiple candidates → go to Tier 2.

    Tier 2 — CI-V probe
        Sends broadcast "Read Transceiver ID" to each candidate.
        Confirms IC-7300 by matching CI-V address 0x94 in the response.

    Tier 3 — Interactive prompt
        Prints all ports and asks the user to type one.

    Returns the device string (e.g. '/dev/ttyUSB0') or None.
    """
    all_ports = list_all_ports()

    if not all_ports:
        log.error("No serial ports found — is the IC-7300 connected and powered on?")
        return None

    log.info(f"Auto-detecting IC-7300 across {len(all_ports)} serial port(s) ...")

    # ── Tier 1: score by USB metadata ────────────────────────────────────────
    scored = sorted(
        [(p, _score_port(p)) for p in all_ports],
        key=lambda x: x[1], reverse=True
    )

    log.debug("Port scores:")
    for p, s in scored:
        vid_pid = f"0x{p.vid:04X}:0x{p.pid:04X}" if p.vid else "no VID/PID"
        log.debug(f"  score={s:3d}  {p.device:<20} {vid_pid}  {p.description}")

    high_conf = [p for p, s in scored if s >= 100]   # exact VID/PID

    if len(high_conf) == 1:
        chosen = high_conf[0]
        log.info(f"✓ Tier 1: port identified via USB VID/PID → {chosen.device}  ({chosen.description})")
        return chosen.device

    # ── Tier 2: CI-V probe ────────────────────────────────────────────────────
    candidates = [p for p, s in scored if s > 0] or [p for p, _ in scored]

    if len(high_conf) > 1:
        log.info(f"Multiple CP210x devices found — probing {len(candidates)} candidate(s) with CI-V ...")
    else:
        log.info(f"No VID/PID match — probing {len(candidates)} candidate(s) with CI-V ...")

    for port_info in candidates:
        port = port_info.device
        log.info(f"  CI-V probe → {port}  ({port_info.description or 'no description'})")
        reported_id = _probe_civ_id(port, baud, ctrl)
        if reported_id is not None:
            if reported_id == expected_civ:
                log.info(f"✓ Tier 2: IC-7300 confirmed on {port}  (CI-V ID = 0x{reported_id:02X})")
                return port
            else:
                log.info(f"  Found Icom radio on {port} (CI-V=0x{reported_id:02X}) — not IC-7300, skipping")
        else:
            log.debug(f"  No CI-V response from {port}")

    # ── Tier 3: interactive fallback ─────────────────────────────────────────
    log.warning("Auto-detection failed.  Available ports:")
    print_ports(all_ports)
    print()
    try:
        choice = input("Enter port name (or press Enter to abort): ").strip()
    except (EOFError, KeyboardInterrupt):
        choice = ''

    if choice:
        for p in all_ports:
            if p.device.lower() == choice.lower():
                log.info(f"✓ Tier 3: user selected {p.device}")
                return p.device
        log.error(f"Port '{choice}' not found in the list above")

    return None


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 2 — CI-V HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def to_bcd(value: int) -> int:
    """
    Convert integer 0-99 to packed BCD byte.
    e.g.  23 → 0x23   09 → 0x09   57 → 0x57
    All IC-7300 CI-V numeric data is in this format.
    """
    if not 0 <= value <= 99:
        raise ValueError(f"BCD out of range: {value}")
    return ((value // 10) << 4) | (value % 10)


def build_frame(radio: int, ctrl: int, payload: bytes) -> bytes:
    """Wrap payload in CI-V envelope: FE FE [radio] [ctrl] [payload] FD."""
    return bytes([0xFE, 0xFE, radio, ctrl]) + payload + bytes([0xFD])


def build_set_date(dt: datetime.datetime, radio: int, ctrl: int) -> bytes:
    """CI-V Set Date — sub-cmd 1A 05 00 94 — data CC YY MM DD (BCD)."""
    c, y = dt.year // 100, dt.year % 100
    return build_frame(radio, ctrl, bytes([
        0x1A, 0x05, 0x00, 0x94,
        to_bcd(c), to_bcd(y), to_bcd(dt.month), to_bcd(dt.day)
    ]))


def build_set_time(dt: datetime.datetime, radio: int, ctrl: int) -> bytes:
    """CI-V Set Time — sub-cmd 1A 05 00 95 — data HH MM (BCD, 24-hour)."""
    return build_frame(radio, ctrl, bytes([
        0x1A, 0x05, 0x00, 0x95,
        to_bcd(dt.hour), to_bcd(dt.minute)
    ]))


def read_until_ok(ser: serial.Serial, radio: int, ctrl: int,
                  timeout: float = 3.0) -> bytes:
    """
    Read from the serial port until an OK (FB) or NG (FA) response from the
    radio is found, or until timeout.

    The IC-7300 sends CI-V Transceive broadcasts (frequency, mode, etc.) that
    are terminated with 0xFD — exactly like a real response frame.  Stopping
    at the first 0xFD therefore often catches a broadcast instead of the OK,
    causing false "no response" failures.  This function reads past any number
    of broadcast frames and returns only when it finds the specific OK/NG reply
    addressed back to the controller, or when the timeout expires.
    """
    buf      = bytearray()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        n = ser.in_waiting
        if n:
            buf.extend(ser.read(n))
            # Scan for OK (FB) or NG (FA) addressed to the controller
            for i in range(len(buf) - 5):
                if (buf[i]   == 0xFE and buf[i+1] == 0xFE and
                        buf[i+2] == ctrl and buf[i+3] == radio and
                        buf[i+4] in (0xFB, 0xFA)):
                    return bytes(buf)          # found what we need
        else:
            time.sleep(0.02)
    return bytes(buf)                          # timeout — return whatever we got


def parse_ok(response: bytes, radio: int, ctrl: int) -> bool:
    """
    Scan buffer for OK (FB) or NG (FA) from radio addressed to controller.
    Returns True for OK, False for NG or nothing found.
    """
    i = 0
    while i <= len(response) - 6:
        if (response[i]   == 0xFE and response[i+1] == 0xFE and
                response[i+2] == ctrl  and response[i+3] == radio):
            if response[i+4] == 0xFB:
                return True
            if response[i+4] == 0xFA:
                log.error("Radio returned NG — command rejected")
                return False
        i += 1
    return False


def send_command(ser:     serial.Serial,
                 frame:   bytes,
                 radio:   int,
                 ctrl:    int,
                 label:   str,
                 retries: int = 3) -> bool:
    """Transmit one CI-V frame and verify the OK response, retrying on failure."""
    for attempt in range(1, retries + 1):
        log.debug(f"TX [{label}] attempt {attempt}: {frame.hex(' ').upper()}")
        ser.reset_input_buffer()
        ser.write(frame)
        time.sleep(0.15)
        response = read_until_ok(ser, radio, ctrl)
        log.debug(f"RX [{label}] attempt {attempt}: {response.hex(' ').upper()}")

        if parse_ok(response, radio, ctrl):
            log.info(f"  {label}  ✓" + (f" (attempt {attempt})" if attempt > 1 else ""))
            return True

        log.warning(f"  [{label}] attempt {attempt}/{retries} — no OK response")
        if attempt < retries:
            time.sleep(1.5)

    log.error(f"  {label}  — failed after {retries} attempts")
    return False


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 3 — TIME SOURCE  (NTP with system clock fallback)
# ─────────────────────────────────────────────────────────────────────────────

def now_utc() -> datetime.datetime:
    """Return current UTC time as a timezone-aware datetime (no deprecation)."""
    return datetime.datetime.now(UTC)


def now_local() -> datetime.datetime:
    """Return current local time as a timezone-aware datetime."""
    return datetime.datetime.now().astimezone()


def query_ntp() -> Optional[datetime.datetime]:
    """
    Try NTP_SERVERS in order.
    Returns a timezone-aware UTC datetime on first success, or None if all fail.
    """
    if not NTP_AVAILABLE:
        log.warning("ntplib not installed (pip install ntplib) — NTP unavailable")
        return None

    client = ntplib.NTPClient()
    for server in NTP_SERVERS:
        try:
            log.info(f"NTP  →  {server}")
            resp   = client.request(server, version=3, timeout=NTP_TIMEOUT)
            # fromtimestamp with UTC gives a timezone-aware datetime
            utc_dt = datetime.datetime.fromtimestamp(resp.tx_time, tz=UTC)
            log.info(
                f"NTP OK  offset={resp.offset * 1000:+.1f} ms  "
                f"UTC={utc_dt:%Y-%m-%d %H:%M:%S}"
            )
            return utc_dt
        except Exception as exc:
            log.debug(f"  {server}: {exc}")

    log.warning("All NTP servers unreachable")
    return None


def get_sync_time(use_utc: bool) -> tuple[datetime.datetime, str]:
    """
    Return (naive_datetime_to_send, source_label).

    The datetime is NAIVE (no tzinfo) since CI-V only cares about
    the numeric values of year/month/day/hour/minute.

    If use_utc=True  → naive UTC datetime
    If use_utc=False → naive LOCAL datetime (IST for Pune = UTC+5:30)

    Source priority: NTP → system clock fallback.
    """
    ntp_utc_aware = query_ntp()

    if ntp_utc_aware is not None:
        source       = "NTP"
        utc_aware    = ntp_utc_aware
    else:
        log.warning("Falling back to system clock")
        source       = "System"
        utc_aware    = now_utc()

    if use_utc:
        # Strip tzinfo → naive UTC for CI-V transmission
        return utc_aware.replace(tzinfo=None), source

    # Convert UTC → local timezone using system's tz rules
    local_aware = utc_aware.astimezone()          # applies system local tz (IST)
    local_naive = local_aware.replace(tzinfo=None) # strip tzinfo for CI-V
    return local_naive, source


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 4 — MAIN SYNC
# ─────────────────────────────────────────────────────────────────────────────

def sync_clock(
    serial_port:   str  = SERIAL_PORT,
    baud_rate:     int  = BAUD_RATE,
    civ_radio:     int  = CIV_RADIO_ADDR,
    civ_ctrl:      int  = CIV_CTRL_ADDR,
    use_utc:       bool = USE_UTC,
    wait_for_zero: bool = WAIT_FOR_ZERO,
    dry_run:       bool = False,
) -> bool:
    """
    Full clock sync sequence.  Returns True on success.

    Steps:
      1. Auto-detect or validate serial port
      2. Determine reference time (NTP → system fallback)
      3. Optionally wait for seconds == 0  (precise to-the-minute sync)
      4. Open serial port
      5. Send CI-V Set Date frame  →  verify OK
      6. Send CI-V Set Time frame  →  verify OK
    """

    # 1. Port resolution
    if serial_port.lower() == 'auto':
        log.info("Port set to 'auto' — starting detection ...")
        serial_port = detect_ic7300_port(baud_rate, civ_ctrl, civ_radio)
        if not serial_port:
            log.error("Port detection failed.  Use -p to specify manually.")
            return False

    tz_label = "UTC" if use_utc else "Local"

    log.info(
        f"\n{'═'*56}\n"
        f"  Icom IC-7300  CI-V Clock Sync\n"
        f"{'═'*56}\n"
        f"  Port    :  {serial_port}   @  {baud_rate} baud\n"
        f"  CI-V    :  Radio=0x{civ_radio:02X}   Controller=0x{civ_ctrl:02X}\n"
        f"  Clock   :  {tz_label + (' (UTC)' if use_utc else ' (system timezone)')}\n"
        f"  Precise :  {'Yes — wait for :00 second' if wait_for_zero else 'No — immediate send'}\n"
        f"{'═'*56}"
    )

    # 2. Reference time
    sync_dt, source = get_sync_time(use_utc)
    log.info(f"Reference [{source}]:  {sync_dt:%Y-%m-%d %H:%M:%S}  {tz_label}")

    # Dry run: show frames and exit
    if dry_run:
        df = build_set_date(sync_dt, civ_radio, civ_ctrl)
        tf = build_set_time(sync_dt, civ_radio, civ_ctrl)
        log.info(f"[DRY RUN] DATE frame: {df.hex(' ').upper()}")
        log.info(f"[DRY RUN] TIME frame: {tf.hex(' ').upper()}")
        log.info("[DRY RUN] Nothing transmitted to radio")
        return True

    # 3. Wait for minute boundary
    if wait_for_zero:
        log.info("Waiting for full-minute boundary for precise sync ...")
        while True:
            remaining = 60 - now_utc().second     # uses now_utc(), no deprecation
            if remaining >= 60:                   # second == 0
                sync_dt, source = get_sync_time(use_utc)
                log.info(f"Boundary reached — sending {sync_dt:%H:%M} {tz_label}")
                break
            time.sleep(0.1 if remaining <= 3 else 0.5)

    # 4. Open serial port
    try:
        ser = serial.Serial(
            port=serial_port, baudrate=baud_rate,
            bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE, timeout=2,
        )
        log.info(f"Serial port open: {serial_port}")
    except serial.SerialException as exc:
        log.error(f"Cannot open {serial_port}: {exc}")
        log.error("Check: cable, port name, CI-V baud rate in radio menu, dialout group (Linux)")
        return False

    # 5 & 6. Send date then time
    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        time.sleep(0.2)

        log.info("Sending CI-V commands:")
        date_ok = send_command(
            ser,
            build_set_date(sync_dt, civ_radio, civ_ctrl),
            civ_radio, civ_ctrl,
            f"SET DATE  {sync_dt:%Y-%m-%d}",
        )
        time.sleep(0.2)
        time_ok = send_command(
            ser,
            build_set_time(sync_dt, civ_radio, civ_ctrl),
            civ_radio, civ_ctrl,
            f"SET TIME  {sync_dt:%H:%M} {tz_label}",
        )

        if date_ok and time_ok:
            log.info(
                f"\n✓  IC-7300 clock synced to "
                f"{sync_dt:%Y-%m-%d %H:%M} {tz_label}  [{source}]"
            )
            return True

        log.error("\n✗  Sync incomplete — troubleshooting:")
        log.error("   1. Verify CI-V address:  MENU > SET > Connectors > CI-V > CI-V Address  (default 94h)")
        log.error("   2. Turn Echo OFF:        MENU > SET > Connectors > CI-V > CI-V Echo")
        log.error("   3. Confirm baud rate matches radio menu setting")
        log.error("   4. Run with -v for raw CI-V byte traces")
        return False

    finally:
        ser.close()
        log.debug("Serial port closed")


# ─────────────────────────────────────────────────────────────────────────────
#  SECTION 5 — CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Sync Icom IC-7300 clock via CI-V  (NTP → system clock fallback)",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        epilog=(
            "Examples:\n"
            "  %(prog)s                          auto-detect port, local time\n"
            "  %(prog)s -p /dev/ttyUSB0          explicit port (Linux)\n"
            "  %(prog)s -p COM3 --utc             UTC time, Windows\n"
            "  %(prog)s --dry-run -v              preview frames, verbose\n"
            "  %(prog)s --list-ports              show all serial ports\n"
        ),
    )
    ap.add_argument(
        '-p', '--port', default=SERIAL_PORT,
        help="Serial port.  'auto' triggers automatic detection.",
    )
    ap.add_argument(
        '-b', '--baud', type=int, default=BAUD_RATE,
        help="Baud rate.",
    )
    ap.add_argument(
        '--civ', type=lambda x: int(x, 0), default=CIV_RADIO_ADDR,
        metavar='ADDR',
        help="Radio CI-V address in hex, e.g. 0x94.",
    )
    ap.add_argument(
        '--ctrl', type=lambda x: int(x, 0), default=CIV_CTRL_ADDR,
        metavar='ADDR',
        help="Controller (PC) CI-V address in hex, e.g. 0xE0.",
    )
    ap.add_argument(
        '--utc', action='store_true',
        help="Send UTC time instead of local time.",
    )
    ap.add_argument(
        '--no-wait', action='store_true',
        help="Send immediately — do not wait for seconds == 0.",
    )
    ap.add_argument(
        '--dry-run', action='store_true',
        help="Show CI-V frames without transmitting to the radio.",
    )
    ap.add_argument(
        '--list-ports', action='store_true',
        help="Print all available serial ports and exit.",
    )
    ap.add_argument(
        '-v', '--verbose', action='store_true',
        help="Enable DEBUG output — shows raw TX/RX CI-V bytes.",
    )

    args = ap.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.list_ports:
        ports = list_all_ports()
        print(f"\nAvailable serial ports  ({len(ports)} found):")
        print_ports(ports)
        print()
        sys.exit(0)

    ok = sync_clock(
        serial_port   = args.port,
        baud_rate     = args.baud,
        civ_radio     = args.civ,
        civ_ctrl      = args.ctrl,
        use_utc       = args.utc,          # default False → local time (IST)
        wait_for_zero = not args.no_wait,
        dry_run       = args.dry_run,
    )
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
