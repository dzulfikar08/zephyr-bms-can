#!/usr/bin/env python3
"""Send one BmsCommand (0x420) on a SocketCAN interface and print the ack.

    tools/bms-cmd.py ContactorClose          # or a number: tools/bms-cmd.py 0x01
    tools/bms-cmd.py SimOverTemp --if vcan0

Commands: ContactorClose ContactorOpen BalancingOn BalancingOff FaultReset
          SimNormal SimOverTemp SimOverVoltage SimHang
"""
import argparse
import random
import socket
import struct
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from minidbc import Dbc  # noqa: E402

dbc = Dbc(Path(__file__).resolve().parent.parent / "dbc/zephyr-bms.dbc")
names = {v: k for k, v in dbc.by_name["BmsCommand"].signals[0].values.items()}

ap = argparse.ArgumentParser(description="Send a BmsCommand and wait for BmsCommandAck")
ap.add_argument("cmd", help=" | ".join(names))
ap.add_argument("--if", dest="ifname", default="vcan0")
a = ap.parse_args()
cmd = names[a.cmd] if a.cmd in names else int(a.cmd, 0)
token = random.randrange(256)

s = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
s.bind((a.ifname,))
s.setsockopt(socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER, struct.pack("=II", 0x421, 0x7FF))
s.settimeout(2.0)
s.send(struct.pack("=IB3x8s", 0x420, 2, bytes([cmd, token]).ljust(8, b"\0")))
t = time.monotonic()
print(f"sent 0x420 [2] {cmd:02X} {token:02X}  BmsCommand Cmd={a.cmd} Token={token}")
try:
    while True:
        fid, dlc, data = struct.unpack("=IB3x8s", s.recv(16))
        _, sig = dbc.decode(0x421, data[:dlc])
        if sig["Token"] == token:
            print(f"ack  0x421 after {(time.monotonic() - t) * 1000:.0f} ms: Result={sig['Result_label']} "
                  f"State={sig['State_label']}")
            sys.exit(0 if sig["Result"] == 0 else 1)
except socket.timeout:
    print("no BmsCommandAck within 2 s (is the BMS running?)")
    sys.exit(2)
