"""Just enough DBC to decode and encode the DBC files of these projects.

Supports BO_/SG_ with Intel (little-endian, @1) byte order, signed and
unsigned integers, factor/offset, simple multiplexing (M / mN) and VAL_
tables. Not a general DBC parser (use cantools for that); this keeps the
tools free of dependencies.
"""

import re

BO = re.compile(r"^BO_ (\d+) (\w+): (\d+) (\w+)")
SG = re.compile(r"^\s*SG_ (\w+)\s*(M|m\d+)?\s*: (\d+)\|(\d+)@([01])([+-]) \(([^,]+),([^)]+)\)")
VAL = re.compile(r'^VAL_ (\d+) (\w+) (.*);')


class Signal:
    def __init__(self, name, mux, start, length, little, signed, factor, offset):
        self.name, self.mux, self.start, self.length = name, mux, start, length
        self.signed, self.factor, self.offset = signed, factor, offset
        if not little:
            raise ValueError(f"{name}: only Intel byte order is supported")
        self.values = {}

    def raw(self, data):
        n = int.from_bytes(bytes(data).ljust(8, b"\0"), "little")
        v = (n >> self.start) & ((1 << self.length) - 1)
        if self.signed and v >> (self.length - 1):
            v -= 1 << self.length
        return v

    def put(self, buf, physical):
        raw = round((physical - self.offset) / self.factor) & ((1 << self.length) - 1)
        n = int.from_bytes(buf, "little")
        n &= ~(((1 << self.length) - 1) << self.start)
        n |= raw << self.start
        buf[:] = n.to_bytes(len(buf), "little")


class Message:
    def __init__(self, fid, name, size):
        self.id, self.name, self.size, self.signals = fid, name, size, []

    def decode(self, data):
        """{name: physical value}, plus {name + '_label': text} where VAL_ has one."""
        switch = next((s for s in self.signals if s.mux == "M"), None)
        group = switch.raw(data) if switch else None
        out = {}
        for s in self.signals:
            if s.mux and s.mux != "M" and int(s.mux[1:]) != group:
                continue
            if s.start + s.length > 8 * len(data):
                continue  # signal not in this (shorter) frame
            r = s.raw(data)
            v = r * s.factor + s.offset
            out[s.name] = int(v) if float(v).is_integer() else round(v, 6)
            if r in s.values:
                out[s.name + "_label"] = s.values[r]
        return out

    def encode(self, **values):
        buf = bytearray(self.size)
        for s in self.signals:
            if s.name in values:
                s.put(buf, values[s.name])
        return bytes(buf)


class Dbc:
    def __init__(self, path):
        self.by_id, self.by_name = {}, {}
        msg = None
        for line in open(path, encoding="utf-8"):
            if m := BO.match(line):
                msg = Message(int(m[1]), m[2], int(m[3]))
                self.by_id[msg.id] = self.by_name[msg.name] = msg
            elif (m := SG.match(line)) and msg:
                msg.signals.append(Signal(m[1], m[2], int(m[3]), int(m[4]), m[5] == "1",
                                          m[6] == "-", float(m[7]), float(m[8])))
            elif m := VAL.match(line):
                pairs = re.findall(r'(-?\d+) "([^"]*)"', m[3])
                for s in self.by_id[int(m[1])].signals:
                    if s.name == m[2]:
                        s.values = {int(k): v for k, v in pairs}
            elif not line.strip():
                msg = None

    def decode(self, fid, data):
        m = self.by_id.get(fid)
        return (m.name, m.decode(data)) if m else (None, {})
