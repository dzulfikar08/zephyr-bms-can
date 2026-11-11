# Source this file:  . scripts/env.sh
# Paths of the heavy things (all on the build disk, not in this repo).

: "${ZBMS_STORAGE:=/mnt/yocto/zephyr}"
export ZBMS_STORAGE
export ZBMS_WS="$ZBMS_STORAGE/ws"                       # west workspace: zephyr/ + modules/
export ZBMS_VENV="$ZBMS_STORAGE/venv"                   # Python venv with west and Zephyr's requirements
export ZEPHYR_SDK_INSTALL_DIR="$ZBMS_STORAGE/zephyr-sdk-0.16.8"
export ZEPHYR_TOOLCHAIN_VARIANT=zephyr
export ZEPHYR_BASE="$ZBMS_WS/zephyr"
: "${ZBMS_BUILD:=$ZBMS_STORAGE/build}"                   # build output (west build -d)
export ZBMS_BUILD
export RENODE_DIR="$ZBMS_STORAGE/renode/renode_1.17.0-portable"

export ZEPHYR_VERSION=v3.7.2
export ZEPHYR_SDK_VERSION=0.16.8
export RENODE_VERSION=1.17.0
export BOARD=nucleo_h743zi

if [ -d "$ZBMS_VENV" ]; then
    # shellcheck disable=SC1091
    . "$ZBMS_VENV/bin/activate"
fi
# dtc and friends from the SDK's host tools (Arch has no dtc installed)
if [ -d "$ZEPHYR_SDK_INSTALL_DIR/sysroots/x86_64-pokysdk-linux/usr/bin" ]; then
    PATH="$ZEPHYR_SDK_INSTALL_DIR/sysroots/x86_64-pokysdk-linux/usr/bin:$PATH"
fi
PATH="$RENODE_DIR:$PATH"
export PATH
