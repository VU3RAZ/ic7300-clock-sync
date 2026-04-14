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
PYTHON="$(su -l "$TARGET_USER" -c 'which python3')"
SCRIPT="$SCRIPT_DIR/ic7300_clock_sync.py"
sed -e "s|__USER__|$TARGET_USER|g" \
    -e "s|__PYTHON__|$PYTHON|g" \
    -e "s|__SCRIPT__|$SCRIPT|g" \
    "$SCRIPT_DIR/ic7300-clock-sync.service" \
    > /etc/systemd/system/ic7300-clock-sync.service
systemctl daemon-reload
echo "    /etc/systemd/system/ic7300-clock-sync.service  OK"

# Make sure the user can open /dev/ttyUSB* — plugdev covers it via the udev
# rule above, but dialout is the traditional group; add both to be safe.
if ! id -nG "$TARGET_USER" | grep -qw dialout; then
    echo "==> Adding $TARGET_USER to dialout group (will take effect at next login) ..."
    usermod -aG dialout "$TARGET_USER"
fi

echo ""
echo "Done.  Plug in the IC-7300 and the clock will sync automatically."
echo ""
echo "To check results after connecting:"
echo "  journalctl -u ic7300-clock-sync.service -n 40 -f"
