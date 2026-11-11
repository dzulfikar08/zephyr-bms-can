#!/usr/bin/env python3
"""BMS (Zephyr in Renode) <-> edge controller (edge-linux-can Linux in QEMU), over host vcan0.

    . scripts/env.sh && tools/edge_demo.py [--keep 0]

1. starts the BMS firmware in Renode, bridged to vcan0
2. boots a copy of the edge-linux-can edge image in QEMU (the Zephyr SDK's
   qemu-system-aarch64) with an emulated Kvaser PCI CAN card on a QEMU CAN bus
   bridged to the same vcan0. The image's can-demo service sends heartbeats
   (0x101) on can0
3. logs in on the serial console and, from inside the Linux guest:
   - candump: the BMS frames arrive on the guest's can0
   - cansend 0x420: the edge controller closes the BMS contactor
4. on the host, checks the BMS saw the edge heartbeat (BmsStatus.EdgeAlive)
   and acted on the guest's command (BmsCommandAck, Contactor=1)

Nothing is changed in edge-linux-can: the image is booted from a read-only copy
with QEMU's snapshot mode (writes go to a temporary overlay).
"""

import argparse
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bms_test import ID, REPO, Bus, Report, Uart, fmt  # noqa: E402
from minidbc import Dbc  # noqa: E402

STORAGE = Path(os.environ.get("ZBMS_STORAGE", "/mnt/yocto/zephyr"))
SDK = Path(os.environ.get("ZEPHYR_SDK_INSTALL_DIR", STORAGE / "zephyr-sdk-0.16.8"))
QEMU = SDK / "sysroots/x86_64-pokysdk-linux/usr/bin/qemu-system-aarch64"
EDGE = STORAGE / "edge-image"
PROMPT = re.compile(rb"root@[\w-]+:[^\n#]*# ")
ANSI = re.compile(rb"\x1b\[[0-9;?]*[A-Za-z]")


class Console:
    """A tiny 'expect' over QEMU's serial console on stdin/stdout."""

    def __init__(self, proc, log):
        self.proc, self.buf, self.log = proc, b"", open(log, "wb")
        self.lock = threading.Condition()
        threading.Thread(target=self._rx, daemon=True).start()

    def _rx(self):
        while chunk := self.proc.stdout.read1(4096):
            self.log.write(chunk)
            self.log.flush()
            with self.lock:
                self.buf += chunk
                self.lock.notify_all()

    def expect(self, pattern, timeout):
        pat = re.compile(pattern) if isinstance(pattern, bytes) else pattern
        deadline = time.monotonic() + timeout
        with self.lock:
            while not (m := pat.search(ANSI.sub(b"", self.buf))):
                left = deadline - time.monotonic()
                if left <= 0 or self.proc.poll() is not None:
                    return None
                self.lock.wait(left)
            text = ANSI.sub(b"", self.buf)
            self.buf = text[m.end():]
            return text[:m.end()].decode(errors="replace")

    def send(self, s):
        self.proc.stdin.write(s.encode())
        self.proc.stdin.flush()

    def run(self, cmd, timeout=20):
        self.send(cmd + "\n")
        out = self.expect(PROMPT, timeout) or ""
        lines = out.replace("\r", "").split("\n")
        return "\n".join(lines[1:-1])  # drop the echoed command and the prompt


def start_renode(args):
    Path(args.uart_log).unlink(missing_ok=True)
    cfg = STORAGE / "renode-config/renode.config"
    script = (f'$bin=@{STORAGE}/build/zephyr/zephyr.elf; $uart_log=@{args.uart_log}; $can_if="{args.can_if}"; '
              "include @renode/bms.resc; start")
    return subprocess.Popen([os.path.join(os.environ["RENODE_DIR"], "renode"), "--disable-gui", "--plain",
                             "-P", "-1", "--config", str(cfg), "-e", script], cwd=REPO,
                            stdout=open("/tmp/zephyr-bms-edge-renode.log", "w"), stderr=subprocess.STDOUT,
                            start_new_session=True)


def start_qemu(args):
    argv = [str(QEMU), "-machine", "virt", "-cpu", "cortex-a57", "-smp", "4", "-m", "256", "-nographic",
            "-kernel", str(EDGE / "Image"),
            "-append", "root=/dev/vda rw console=ttyAMA0 swiotlb=0",
            "-drive", f"id=disk0,file={EDGE / 'edge-image-qemuarm64.rootfs.ext4'},if=none,format=raw,snapshot=on",
            "-device", "virtio-blk-pci,drive=disk0",
            "-object", "rng-random,filename=/dev/urandom,id=rng0", "-device", "virtio-rng-pci,rng=rng0",
            "-device", "i6300esb",
            # The CAN part, as in edge-linux-can's qemu-smoke.sh:
            "-object", "can-bus,id=canbus0",
            "-object", f"can-host-socketcan,id=canhost0,if={args.can_if},canbus=canbus0",
            "-device", "kvaser_pci,canbus=canbus0"]
    print("starting QEMU:", " ".join(argv[:1] + ["..."] + argv[-6:]), flush=True)
    return subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            start_new_session=True)


def main():
    ap = argparse.ArgumentParser(description="BMS in Renode <-> Yocto edge image in QEMU over vcan0")
    ap.add_argument("--can-if", default="vcan0")
    ap.add_argument("--uart-log", default="/tmp/zephyr-bms-edge-uart.log")
    ap.add_argument("--console-log", default="/tmp/zephyr-bms-edge-console.log")
    ap.add_argument("--boot-timeout", type=float, default=600,
                    help="seconds to wait for the login prompt (TCG boot is slow on a loaded host)")
    ap.add_argument("--keep", type=float, default=0, help="seconds to keep both running at the end")
    ap.add_argument("--can-watch", metavar="OUT", help="while keeping, run tools/can-watch.py and save its output here")
    args = ap.parse_args()

    bus = Bus(args.can_if, Dbc(REPO / "dbc/zephyr-bms.dbc"))
    rep = Report()
    uart = Uart(args.uart_log)
    renode = start_renode(args)
    qemu = start_qemu(args)
    con = Console(qemu, args.console_log)
    try:
        rep.check("BMS heartbeat on vcan0", bus.wait(ID["hb"], timeout=90))
        t0 = time.monotonic()
        rep.check("edge image boots to a login prompt", con.expect(rb"login: ", args.boot_timeout),
                  f"{time.monotonic() - t0:.0f} s")
        con.send("root\n")
        con.expect(PROMPT, 30)
        con.run("export TERM=dumb; stty cols 200; dmesg -n 1")  # no kernel messages on the console
        out = con.run("systemctl is-system-running --wait", 300)
        rep.check("guest systemd running", out.strip() in ("running", "degraded"), out.strip())
        out = con.run("ip -details link show can0 | head -n 3")
        rep.check("guest can0 (kvaser_pci) UP", ",UP" in out, " ".join(out.split()[:3]) if out else "")
        out = con.run("systemctl is-active can-demo@can0")
        rep.check("can-demo@can0 active (sends 0x101 heartbeats)", out.strip() == "active", out.strip())

        f = bus.wait(ID["edge"], timeout=10)
        rep.check("edge heartbeat 0x101 on host vcan0", f)
        f = bus.wait(ID["status"], lambda s: s["EdgeAlive"] == 1, timeout=10)
        rep.check("BMS sees the edge controller (BmsStatus.EdgeAlive=1)", f,
                  fmt(f[4], ["State", "Contactor", "EdgeAlive"]) if f else "")
        rep.check("BMS log: edge controller heartbeat seen", uart.wait("edge controller heartbeat seen", 5))

        out = con.run("candump -n 12 -t d can0,410:7F0", 15)
        print("       guest$ candump -n 12 -t d can0,410:7F0\n" + "\n".join("         " + ln for ln in out.splitlines()))
        rep.check("BMS frames (0x41x) arrive on the guest's can0", out.count("41") >= 12 and "410" in out)

        m = bus.mark()
        out = con.run("candump -n 1 -T 3000 can0,421:7FF & sleep 0.3; cansend can0 420#0151; wait", 15)
        print("       guest$ candump -n 1 -T 3000 can0,421:7FF & sleep 0.3; cansend can0 420#0151; wait\n"
              + "\n".join("         " + ln for ln in out.splitlines()))
        rep.check("guest receives BmsCommandAck for its ContactorClose", "421" in out and "01 51 00 02" in out,
                  "ack bytes: Cmd=01 Token=51 Result=00 (Ok) State=02 (Active)")
        f = bus.wait(ID["cmd"], timeout=1, mark=m)
        rep.check("the guest's command crossed vcan0", f, fmt(f[4], ["Cmd", "Token"]) if f else "")
        f = bus.wait(ID["status"], lambda s: s["Contactor"] == 1, timeout=2, mark=m)
        rep.check("BMS closed the contactor on the edge's command", f,
                  fmt(f[4], ["State", "Contactor", "PackCurrent", "EdgeAlive"]) if f else "")
        out = con.run("journalctl -u can-demo@can0 --no-pager -o cat | grep -E 'first frame|frames' | tail -n 12")
        print("       guest$ journalctl -u can-demo@can0 | grep -E 'first frame|frames' | tail -n 12\n" + "\n".join("         " + ln for ln in out.splitlines()))
        out = con.run("cansend can0 420#0252")  # open again
        if args.keep:
            print(f"keeping both running for {args.keep:.0f} s", flush=True)
            if args.can_watch:
                with open(args.can_watch, "w") as out:
                    subprocess.run([sys.executable, "tools/can-watch.py", args.can_if, "dbc/zephyr-bms.dbc", "dbc/can-demo.dbc",
                                    "--secs", str(int(args.keep))], cwd=REPO, stdout=out, timeout=args.keep + 30)
            else:
                time.sleep(args.keep)
        con.send("poweroff\n")
        qemu.wait(60)
        rep.check("guest powered off cleanly", qemu.returncode == 0, f"exit {qemu.returncode}")
    finally:
        for p in (qemu, renode):
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(10)
                except subprocess.TimeoutExpired:
                    p.kill()
    failed = [n for n, ok in rep.results if not ok]
    print(f"{len(rep.results) - len(failed)}/{len(rep.results)} checks passed"
          + (f"; failed: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
