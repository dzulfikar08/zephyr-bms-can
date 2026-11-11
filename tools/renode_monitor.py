#!/usr/bin/env python3
"""Run a few Renode Monitor commands over its telnet port and print the session.

    tools/renode_monitor.py [port] < commands.txt     (default port 34555)

Used to capture the Monitor for the docs while scripts/run-renode.sh runs.
"""
import re
import socket
import sys
import time

port = int(sys.argv[1]) if len(sys.argv) > 1 else 34555
s = socket.create_connection(("127.0.0.1", port), timeout=10)
buf = b""


def read_until_prompt(timeout=15):
    global buf
    end = time.time() + timeout
    while time.time() < end:
        s.settimeout(max(0.1, end - time.time()))
        try:
            chunk = s.recv(65536)
        except socket.timeout:
            break
        if not chunk:
            break
        buf += chunk
        clean = re.sub(rb"\xff[\xfb-\xfe].|\xff\xfa.*?\xff\xf0", b"", buf, flags=re.S)
        if re.search(rb"\((monitor|bms)\) ?$", clean.rstrip(b" ") + b" "):
            out, buf = clean, b""
            return out.decode(errors="replace")
    out, buf = buf, b""
    return out.decode(errors="replace")


read_until_prompt()
for line in sys.stdin:
    cmd = line.rstrip("\n")
    if not cmd or cmd.startswith("#"):
        continue
    s.sendall(cmd.encode() + b"\r\n")
    time.sleep(0.3)
    out = read_until_prompt()
    out = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", out).replace("\r", "")
    lines = out.split("\n")
    if lines and cmd in lines[0]:
        lines = lines[1:]
    print(f"(bms) {cmd}")
    print("\n".join(ln for ln in lines if not re.fullmatch(r"\((monitor|bms)\) ?", ln.strip() + " ")).rstrip())
