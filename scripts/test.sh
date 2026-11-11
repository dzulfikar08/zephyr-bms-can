#!/usr/bin/env bash
# End-to-end test: Renode (headless) runs the firmware, the test talks to it
# over host vcan0.  scripts/test.sh [--hold 60] [--skip-watchdog] ...
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/env.sh"
ip link show "${CAN_IF:-vcan0}" >/dev/null || {
    echo "no ${CAN_IF:-vcan0}: sudo modprobe vcan && sudo ip link add dev vcan0 type vcan && sudo ip link set up vcan0" >&2
    exit 2
}
[ -f "$ZBMS_BUILD/zephyr/zephyr.elf" ] || "$here/build.sh"
exec python3 "$here/../tools/bms_test.py" "$@"
