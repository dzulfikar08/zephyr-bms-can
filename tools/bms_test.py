#!/usr/bin/env python3
"""End-to-end test: Zephyr BMS firmware in Renode <-> host vcan0.

Starts Renode headless (unless --no-renode: then a BMS must already be
running on the bus), listens on the host SocketCAN interface, decodes frames
with dbc/zephyr-bms.dbc, sends BmsCommand frames like an edge controller
would, and checks the answers and the state changes. Exit code 0 = all passed.

    . scripts/env.sh && tools/bms_test.py            # or scripts/test.sh
    tools/bms_test.py --no-renode --skip-watchdog    # against a running BMS

Uses only the Python standard library (AF_CAN raw sockets, Linux only).
"""

import argparse
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from minidbc import Dbc  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
ID = dict(status=0x410, cells=0x411, temps=0x412, volts=0x413, hb=0x41F,
          cmd=0x420, ack=0x421, fault=0x430, edge=0x101)
CMD = dict(ContactorClose=1, ContactorOpen=2, BalancingOn=3, BalancingOff=4, FaultReset=5,
           SimNormal=0x10, SimOverTemp=0x11, SimOverVoltage=0x12, SimHang=0x7E)
CAN_FMT = "=IB3x8s"


class Bus:
    """Receives every frame on the interface in a thread; lets the test wait for some."""

    def __init__(self, ifname, dbc):
        self.dbc = dbc
        self.sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        self.sock.bind((ifname,))
        self.frames = []  # (host time, id, data, message name, signals)
        self.lock = threading.Lock()
        self.cv = threading.Condition(self.lock)
        threading.Thread(target=self._rx, daemon=True).start()

    def _rx(self):
        while True:
            raw = self.sock.recv(16)
            fid, dlc, data = struct.unpack(CAN_FMT, raw)
            fid &= socket.CAN_EFF_MASK
            data = data[:dlc]
            name, sig = self.dbc.decode(fid, data)
            with self.cv:
                self.frames.append((time.monotonic(), fid, data, name, sig))
                self.cv.notify_all()

    def mark(self):
        with self.lock:
            return len(self.frames)

    def since(self, mark, fid=None):
        with self.lock:
            return [f for f in self.frames[mark:] if fid is None or f[1] == fid]

    def wait(self, fid, pred=lambda s: True, timeout=5.0, mark=None):
        """First frame with this id after `mark` whose decoded signals satisfy pred."""
        start = self.mark() if mark is None else mark
        deadline = time.monotonic() + timeout
        with self.cv:
            i = start
            while True:
                while i < len(self.frames):
                    f = self.frames[i]
                    i += 1
                    if f[1] == fid and pred(f[4]):
                        return f
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self.cv.wait(left)

    def send(self, fid, data):
        self.sock.send(struct.pack(CAN_FMT, fid, len(data), data.ljust(8, b"\0")))


class Uart:
    """The firmware console, from the file Renode copies usart3 to."""

    def __init__(self, path):
        self.path = Path(path)

    def text(self):
        try:
            return self.path.read_text(errors="replace")
        except FileNotFoundError:
            return ""

    def wait(self, needle, timeout, count=1):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.text().count(needle) >= count:
                return True
            time.sleep(0.1)
        return False


class Report:
    def __init__(self):
        self.results = []
        self.t0 = time.monotonic()

    def check(self, name, ok, detail=""):
        self.results.append((name, bool(ok)))
        tag = "\x1b[32m[PASS]\x1b[0m" if ok else "\x1b[31m[FAIL]\x1b[0m"
        print(f"{tag} {time.monotonic() - self.t0:6.1f}s  {name}" + (f"  ({detail})" if detail else ""),
              flush=True)
        return ok


def fmt(sig, keys):
    return " ".join(f"{k}={sig.get(k + '_label', sig.get(k))}" for k in keys)


def command(bus, rep, name, token, expect_result, expect_state=None):
    m = bus.mark()
    bus.send(ID["cmd"], bus.dbc.by_name["BmsCommand"].encode(Cmd=CMD[name], Token=token))
    t = time.monotonic()
    ack = bus.wait(ID["ack"], lambda s: s["Token"] == token, timeout=2.0, mark=m)
    if not ack:
        return rep.check(f"{name}: BmsCommandAck received", False, "no ack within 2 s")
    s = ack[4]
    ok = s.get("Result_label") == expect_result and (expect_state is None or s.get("State_label") == expect_state)
    return rep.check(f"{name} -> {expect_result}" + (f", {expect_state}" if expect_state else ""), ok,
                     f"ack after {(ack[0] - t) * 1000:.0f} ms: {fmt(s, ['Cmd', 'Result', 'State'])}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--can-if", default=os.environ.get("CAN_IF", "vcan0"))
    ap.add_argument("--elf", default=os.path.join(os.environ.get("ZBMS_BUILD", "/mnt/yocto/zephyr/build"),
                                                  "zephyr/zephyr.elf"))
    ap.add_argument("--uart-log", default="/tmp/zephyr-bms-test-uart.log")
    ap.add_argument("--renode-log", default="/tmp/zephyr-bms-test-renode.log")
    ap.add_argument("--no-renode", action="store_true", help="test a BMS that is already running")
    ap.add_argument("--skip-watchdog", action="store_true")
    ap.add_argument("--hold", type=float, default=0, help="keep Renode running this many seconds after the checks")
    args = ap.parse_args()

    dbc = Dbc(REPO / "dbc/zephyr-bms.dbc")
    bus = Bus(args.can_if, dbc)  # listen before the BMS starts, to time the boot
    uart = Uart(args.uart_log)
    rep = Report()
    renode = None

    if not args.no_renode:
        Path(args.uart_log).unlink(missing_ok=True)
        renode_bin = Path(os.environ.get("RENODE_DIR", "")) / "renode"
        cfg = Path(os.environ.get("ZBMS_STORAGE", "/tmp")) / "renode-config/renode.config"
        cfg.parent.mkdir(parents=True, exist_ok=True)
        script = (f'$bin=@{args.elf}; $uart_log=@{args.uart_log}; $can_if="{args.can_if}"; '
                  "include @renode/bms.resc; start")
        print(f"starting Renode: {renode_bin} ... -e '{script}'", flush=True)
        renode = subprocess.Popen([str(renode_bin), "--disable-gui", "--plain", "-P", "-1", "--config", str(cfg),
                                   "-e", script], cwd=REPO, stdout=open(args.renode_log, "w"),
                                  stderr=subprocess.STDOUT, start_new_session=True)
    try:
        run_checks(args, bus, uart, rep, booting=renode is not None)
        if args.hold:
            print(f"holding {args.hold:.0f} s (Renode keeps running)", flush=True)
            time.sleep(args.hold)
    finally:
        if renode:
            renode.terminate()
            try:
                renode.wait(10)
            except subprocess.TimeoutExpired:
                renode.kill()

    counts = {}
    for f in bus.since(0):
        counts[f[1]] = counts.get(f[1], 0) + 1
    print("frames seen on", args.can_if + ":", " ".join(f"{k:03X}={v}" for k, v in sorted(counts.items())))
    failed = [n for n, ok in rep.results if not ok]
    print(f"{len(rep.results) - len(failed)}/{len(rep.results)} checks passed"
          + (f"; failed: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


def run_checks(args, bus, uart, rep, booting):
    t_start = time.monotonic()
    if booting:
        rep.check("firmware boots (UART: Booting Zephyr)", uart.wait("Booting Zephyr", 90))
        rep.check("CAN controller started (UART)", uart.wait("started", 10))
    hb = bus.wait(ID["hb"], timeout=90 if booting else 3)
    if not rep.check("first BmsHeartbeat on " + args.can_if, hb,
                     f"{hb[0] - t_start:.1f} s after the Renode start" if hb and booting else ""):
        return

    # Renode runs slower than the wall clock for the first seconds (its JIT
    # translates the firmware), then catches up. Measure rates after that.
    if booting:
        warm = bus.wait(ID["hb"], lambda s: s["Uptime"] >= 10, timeout=60)
        rep.check("firmware uptime reached 10 s", warm,
                  f"{warm[0] - hb[0]:.1f} s of wall clock after the first heartbeat" if warm else "")
    m = bus.mark()
    time.sleep(10.0)
    st, hbs = bus.since(m, ID["status"]), bus.since(m, ID["hb"])
    rep.check("BmsStatus every 100 ms", 90 <= len(st) <= 110, f"{len(st)} frames in 10 s")
    rep.check("BmsHeartbeat every 1 s", 9 <= len(hbs) <= 11, f"{len(hbs)} frames in 10 s")
    nc, nt = len(bus.since(m, ID["cells"])), len(bus.since(m, ID["temps"]))
    rep.check("BmsCellSummary and BmsTemperatures every 500 ms", 18 <= nc <= 22 and 18 <= nt <= 22,
              f"{nc} and {nt} frames in 10 s")
    if len(hbs) >= 2:
        virt = hbs[-1][4]["Uptime"] - hbs[0][4]["Uptime"]
        print(f"       info: virtual/host time over the window: {virt} s (firmware uptime) "
              f"in {hbs[-1][0] - hbs[0][0]:.2f} s (host)")
    groups = sorted({f[4]["Group"] for f in bus.since(m, ID["volts"])})
    rep.check("BmsCellVoltages: all 3 multiplexed groups", groups == [0, 1, 2], f"groups {groups}")
    seqs = [f[4]["Seq"] for f in hbs]
    rep.check("heartbeat Seq counts up by 1", all(b - a == 1 for a, b in zip(seqs, seqs[1:])), f"{seqs}")
    s = st[-1][4] if st else {}
    rep.check("initial state Standby, contactor open", s.get("State_label") == "Standby" and s.get("Contactor") == 0,
              fmt(s, ["PackVoltage", "PackCurrent", "Soc", "State", "Contactor"]))
    v_before = s.get("PackVoltage", 0)

    # Edge controller (edge-linux-can in QEMU) on the bus? Informational only.
    edge = bus.since(0, ID["edge"])
    print(f"       info: {len(edge)} edge heartbeats (0x101) seen so far; BmsStatus.EdgeAlive={s.get('EdgeAlive')}")

    # Contactor
    command(bus, rep, "ContactorClose", 0x11, "Ok", "Active")
    f = bus.wait(ID["status"], lambda s: s["Contactor"] == 1, timeout=1.0)
    rep.check("status shows contactor closed and discharge current", f and f[4]["PackCurrent"] > 10,
              fmt(f[4], ["State", "Contactor", "PackCurrent", "PackVoltage", "Soc"]) if f else "no frame")
    time.sleep(5.0)
    f = bus.wait(ID["status"], timeout=1.0)
    rep.check("pack voltage falls while discharging", f and f[4]["PackVoltage"] < v_before,
              f"{v_before} V -> {f[4]['PackVoltage']} V (incl. I*R drop)" if f else "")

    # Balancing
    command(bus, rep, "BalancingOn", 0x12, "Ok")
    f = bus.wait(ID["cells"], lambda s: s["BalancingMask"] != 0, timeout=2.0)
    rep.check("BmsCellSummary.BalancingMask set", f, f"mask=0x{f[4]['BalancingMask']:02X}" if f else "")

    m = bus.mark()
    bus.send(ID["cmd"], bytes([0x55, 0x13]))
    ack = bus.wait(ID["ack"], lambda s: s["Token"] == 0x13, timeout=2.0, mark=m)
    rep.check("unknown command 0x55 -> Unknown", ack and ack[4].get("Result_label") == "Unknown")

    # Over-temperature
    m = bus.mark()
    t = time.monotonic()
    command(bus, rep, "SimOverTemp", 0x20, "Ok")
    f = bus.wait(ID["fault"], lambda s: s["Trigger"] == 4, timeout=30.0, mark=m)
    rep.check("BmsFault OverTemp raised", f,
              f"after {f[0] - t:.1f} s: {fmt(f[4], ['FaultFlags', 'Trigger', 'Index'])} Value={f[4]['Value'] / 10} degC"
              if f else "none within 30 s")
    f = bus.wait(ID["status"], timeout=1.0)
    rep.check("state Fault, contactor opened, FaultFlags bit 2",
              f and f[4]["State_label"] == "Fault" and f[4]["Contactor"] == 0 and f[4]["FaultFlags"] & 4,
              fmt(f[4], ["State", "Contactor", "FaultFlags", "PackCurrent"]) if f else "")
    rep.check("UART logs the fault", uart.wait("FAULT 0x04", 2) if args.uart_log and not args.no_renode else True)
    command(bus, rep, "ContactorClose", 0x21, "Rejected", "Fault")
    command(bus, rep, "FaultReset", 0x22, "Rejected", "Fault")
    command(bus, rep, "SimNormal", 0x23, "Ok")
    f = bus.wait(ID["temps"], lambda s: s["TempMax"] < 50, timeout=40.0)
    rep.check("temperature back below 50 degC", f, f"TempMax={f[4]['TempMax']}" if f else "")
    command(bus, rep, "FaultReset", 0x24, "Ok", "Standby")

    # Over-voltage
    m = bus.mark()
    t = time.monotonic()
    command(bus, rep, "SimOverVoltage", 0x30, "Ok")
    f = bus.wait(ID["fault"], lambda s: s["Trigger"] == 1, timeout=40.0, mark=m)
    rep.check("BmsFault OverVoltage raised", f,
              f"after {f[0] - t:.1f} s: cell {f[4]['Index']} at {f[4]['Value']} mV" if f else "none within 40 s")
    command(bus, rep, "SimNormal", 0x31, "Ok")
    f = bus.wait(ID["cells"], lambda s: s["CellMax"] < 4200, timeout=20.0)
    rep.check("cell voltage back below 4.2 V", f, f"CellMax={f[4]['CellMax']} mV" if f else "")
    command(bus, rep, "FaultReset", 0x32, "Ok", "Standby")
    command(bus, rep, "BalancingOff", 0x33, "Ok")

    if args.skip_watchdog:
        return
    # Watchdog: stall the BMS thread; the task watchdog must reboot the MCU.
    last_seq = bus.wait(ID["hb"], timeout=2.0)[4]["Seq"]
    t = time.monotonic()
    m = bus.mark()
    command(bus, rep, "SimHang", 0x7E, "Ok")
    if not args.no_renode:
        rep.check("UART: task watchdog fired", uart.wait("task watchdog", 5),
                  next((ln.strip() for ln in uart.text().splitlines() if "task watchdog:" in ln), ""))
    f = bus.wait(ID["hb"], lambda s: s["Seq"] < last_seq, timeout=20.0, mark=m)
    rep.check("BMS rebooted (heartbeat Seq restarted)", f,
              f"Seq {last_seq} -> {f[4]['Seq']} after {f[0] - t:.1f} s" if f else "no restart within 20 s")
    if not args.no_renode:
        rep.check("UART: second boot", uart.wait("Booting Zephyr", 5, count=2))
    f = bus.wait(ID["status"], timeout=2.0)
    rep.check("after reboot: Standby, contactor open", f and f[4]["State_label"] == "Standby" and f[4]["Contactor"] == 0)


if __name__ == "__main__":
    sys.exit(main())
