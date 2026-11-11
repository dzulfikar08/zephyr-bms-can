#!/usr/bin/env bash
# Build the firmware:  scripts/build.sh [extra west build args]
# Output: $ZBMS_BUILD/zephyr/zephyr.elf (on the build disk)
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/env.sh"
west build -b "$BOARD" -d "$ZBMS_BUILD" "$here/../app" "$@"
ls -l "$ZBMS_BUILD/zephyr/zephyr.elf"
