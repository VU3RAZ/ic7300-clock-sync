#!/usr/bin/env bash
# install.sh — sets up udev + systemd auto clock-sync for the Icom IC-7300
# Run once with:  sudo bash install.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$EUID" -ne 0 ]]; then
    echo "ERROR: please run as root:  sudo bash $0"
    exit 1
fi

echo "==> Installing udev rule ..."
cp "$SCRIPT_DIR/99-ic7300-clock-sync.rules" /etc/udev/rules.d/
udevadm control --reload-rules
echo "    /etc/udev/rules.d/99-ic7300-clock-sync.rules  OK"

echo "==> Installing systemd service ..."
TARGET_USER="${SUDO_USER:-$USER}"
# Prefer the venv python if present, fall back to system python3
VENV_PYTHON="/home/$TARGET_USER/.venv/bin/python3"
if [ -x "$VENV_PYTHON" ]; then
    PYTHON="$VENV_PYTHON"
else
    PYTHON="$(su -l "$TARGET_USER" -c 'which python3')"
fi
# Always use the script from THIS repo, not any other copy
SCRIPT="$SCRIPT_DIR/ic7300_clock_sync.py"
sed -e "s|__USER__|$TARGET_USER|g" \
    -e "s|__PYTHON__|$PYTHON|g" \
    -e "s|__SCRIPT__|$SCRIPT|g" \
    "$SCRIPT_DIR/ic7300-clock-sync.service" \
    > /etc/systemd/system/ic7300-clock-sync.service
systemctl daemon-reload
echo "    /etc/systemd/system/ic7300-clock-sync.service  OK"
echo "    Script: $SCRIPT"
echo "    Python: $PYTHON"

# Make sure the user can open /dev/ttyUSB* and /dev/ic7300
if ! id -nG "$TARGET_USER" | grep -qw dialout; then
    echo "==> Adding $TARGET_USER to dialout group (will take effect at next login) ..."
    usermod -aG dialout "$TARGET_USER"
fi

# If the IC-7300 is already connected, trigger udev now so the service starts
# without needing to unplug and replug the cable.
echo "==> Checking if IC-7300 is already connected ..."
if udevadm info --query=all --subsystem=tty 2>/dev/null | grep -q "ea60"; then
    echo "    IC-7300 detected — triggering udev to apply new rules ..."
    udevadm trigger --action=add --subsystem-match=tty --attr-match=idVendor=10c4
    echo "    Done.  Service will start shortly."
else
    echo "    IC-7300 not currently connected (plug it in to trigger sync)."
fi

echo ""
echo "Done.  The clock syncs automatically on connect — no unplug/replug needed."
echo ""
echo "To check results:"
echo "  journalctl -u ic7300-clock-sync.service -n 40 -f"
