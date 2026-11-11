#!/usr/bin/env bash
# One-time setup of everything heavy, on the build disk. Restartable: every
# step checks whether it is already done, so just run it again after an
# interruption (laptop sleep, network drop).
#
#   scripts/setup.sh            # all steps
#   scripts/setup.sh renode     # one step: venv | zephyr | sdk | renode
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=env.sh
. "$here/env.sh"

dl="$ZBMS_STORAGE/dl"
mkdir -p "$dl" "$ZBMS_STORAGE"

step() { printf '\n=== %s  (%s)\n' "$1" "$(date +%T)"; }
fetch() {  # fetch URL FILE  (resumes partial downloads)
    [ -f "$dl/$2.ok" ] && return 0
    curl -fSL --retry 5 -C - -o "$dl/$2" "$1"
    touch "$dl/$2.ok"
}

do_venv() {
    step "Python venv with west"
    if [ ! -x "$ZBMS_VENV/bin/west" ]; then
        # Zephyr 3.7's scripts are tested on Python 3.10-3.12; the host has 3.14,
        # so let uv provide a 3.12 interpreter for the venv.
        uv venv --python 3.12 "$ZBMS_VENV"
        uv pip install --python "$ZBMS_VENV/bin/python" west
    fi
    "$ZBMS_VENV/bin/west" --version
}

do_zephyr() {
    step "west workspace: Zephyr $ZEPHYR_VERSION + only the modules an STM32 needs"
    . "$ZBMS_VENV/bin/activate"
    if [ ! -d "$ZBMS_WS/.west" ]; then
        rm -rf "$ZBMS_WS/zephyr"   # a half-done clone from an interrupted run
        west init -m https://github.com/zephyrproject-rtos/zephyr --mr "$ZEPHYR_VERSION" \
            --clone-opt=--depth=1 "$ZBMS_WS"
    fi
    cd "$ZBMS_WS"
    # Zephyr's manifest lists ~50 modules (every vendor HAL). We need two.
    west config manifest.project-filter -- '-.*,+cmsis,+hal_stm32'
    west update --narrow --fetch-opt=--depth=1
    west list
    uv pip install --python "$ZBMS_VENV/bin/python" -r "$ZEPHYR_BASE/scripts/requirements-base.txt"
}

do_sdk() {
    step "Zephyr SDK $ZEPHYR_SDK_VERSION: minimal bundle + arm toolchain + host tools"
    local base="https://github.com/zephyrproject-rtos/sdk-ng/releases/download/v$ZEPHYR_SDK_VERSION"
    local min="zephyr-sdk-${ZEPHYR_SDK_VERSION}_linux-x86_64_minimal.tar.xz"
    fetch "$base/$min" "$min"
    fetch "$base/toolchain_linux-x86_64_arm-zephyr-eabi.tar.xz" arm.tar.xz
    if [ ! -f "$ZEPHYR_SDK_INSTALL_DIR/sdk_version" ]; then
        tar -xJf "$dl/$min" -C "$ZBMS_STORAGE"
    fi
    if [ ! -x "$ZEPHYR_SDK_INSTALL_DIR/arm-zephyr-eabi/bin/arm-zephyr-eabi-gcc" ]; then
        tar -xJf "$dl/arm.tar.xz" -C "$ZEPHYR_SDK_INSTALL_DIR"
    fi
    cd "$ZEPHYR_SDK_INSTALL_DIR"
    # The SDK's own setup.sh refuses to run without wget (not installed on
    # this host), even for steps that download nothing. These are its two
    # steps, done directly; neither needs root:
    #  1. host tools (dtc, ...) from the self-extracting installer in the bundle
    if [ ! -f .hosttools.ok ]; then
        ./zephyr-sdk-x86_64-hosttools-standalone-0.9.sh -y -d .
        touch .hosttools.ok
    fi
    #  2. register the SDK as a CMake package (~/.cmake/packages/Zephyr-sdk)
    cmake -P cmake/zephyr_sdk_export.cmake
    "$ZEPHYR_SDK_INSTALL_DIR/arm-zephyr-eabi/bin/arm-zephyr-eabi-gcc" --version | head -1
}

do_renode() {
    step "Renode $RENODE_VERSION (portable, bundles its own .NET runtime)"
    local f="renode-$RENODE_VERSION.linux-portable.tar.gz"
    fetch "https://github.com/renode/renode/releases/download/v$RENODE_VERSION/$f" "$f"
    if [ ! -x "$RENODE_DIR/renode" ]; then
        mkdir -p "$ZBMS_STORAGE/renode"
        tar -xzf "$dl/$f" -C "$ZBMS_STORAGE/renode"
    fi
    "$RENODE_DIR/renode" --version
}

if [ $# -eq 0 ]; then set -- venv zephyr sdk renode; fi
for s in "$@"; do "do_$s"; done
step "done"
du -sh "$ZBMS_STORAGE"/* 2>/dev/null || true
