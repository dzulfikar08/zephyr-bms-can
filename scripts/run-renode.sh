#!/usr/bin/env bash
# Run the BMS in Renode until Ctrl-C (or for $SECS seconds).
#   scripts/run-renode.sh            headless; firmware console in $UART_LOG
#   GUI=1 scripts/run-renode.sh      with Renode's Monitor and UART windows
# The Renode Monitor is also reachable with: telnet localhost $MONITOR_PORT
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
. "$here/env.sh"
: "${UART_LOG:=/tmp/zephyr-bms-uart.log}" "${MONITOR_PORT:=34555}" "${CAN_IF:=vcan0}"
[ -f "$ZBMS_BUILD/zephyr/zephyr.elf" ] || "$here/build.sh"
mkdir -p "$ZBMS_STORAGE/renode-config"
opts=(--plain --config "$ZBMS_STORAGE/renode-config/renode.config" -P "$MONITOR_PORT")
[ "${GUI:-0}" = 1 ] || opts+=(--disable-gui)
cd "$here/.."   # bms.resc finds the .repl relative to the repo root
echo "Renode: firmware console -> $UART_LOG, monitor on telnet localhost:$MONITOR_PORT, CAN on $CAN_IF"
cmd=(renode "${opts[@]}" -e "\$bin=@$ZBMS_BUILD/zephyr/zephyr.elf; \$uart_log=@$UART_LOG; \$can_if=\"$CAN_IF\"; include @renode/bms.resc; start")
if [ -n "${SECS:-}" ]; then exec timeout "$SECS" "${cmd[@]}"; else exec "${cmd[@]}"; fi
