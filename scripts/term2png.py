#!/usr/bin/env python3
"""Render terminal output (plain text or ANSI colours) to a PNG for the docs.

    term2png.py IN.txt OUT.png --title "kas build" [--cols 110] [--last 40]

Uses headless Chromium for the rendering (CHROMIUM=/path overrides).
"""

import argparse
import html
import os
import re
import subprocess
import tempfile

COLORS = {30: "#4b5263", 31: "#e06c75", 32: "#98c379", 33: "#e5c07b", 34: "#61afef",
          35: "#c678dd", 36: "#56b6c2", 37: "#dcdfe4", 90: "#7f848e", 91: "#e06c75",
          92: "#98c379", 93: "#e5c07b", 94: "#61afef", 95: "#c678dd", 96: "#56b6c2", 97: "#ffffff"}
SGR = re.compile(r"\x1b\[([0-9;]*)m")
OTHER_ESC = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[()][A-Z0-9]|\x1b[=>]")


def ansi_to_html(text):
    out, color, bold, pos = [], None, False, 0
    def emit(chunk):
        if not chunk:
            return
        esc = html.escape(chunk)
        style = []
        if color:
            style.append(f"color:{color}")
        if bold:
            style.append("font-weight:700")
        out.append(f'<span style="{";".join(style)}">{esc}</span>' if style else esc)
    for m in SGR.finditer(text):
        emit(text[pos:m.start()])
        pos = m.end()
        for code in [int(c) if c else 0 for c in m.group(1).split(";")]:
            if code == 0:
                color, bold = None, False
            elif code == 1:
                bold = True
            elif code in COLORS:
                color = COLORS[code]
            elif code == 39:
                color = None
    emit(text[pos:])
    return "".join(out)


def highlight(line):
    """Colour plain-text lines by content when the source had no ANSI codes."""
    if "\x1b[" in line:
        return line
    # A command line: colour only the prompt, never by what the command says.
    if line.startswith(("$ ", "root@")):
        return re.sub(r"^(\S+ ?)", lambda m: f"\x1b[1;36m{m.group(1)}\x1b[0m", line, count=1)
    # ERROR-ACTIVE is the healthy CAN bus state, not an error.
    rules = [(r"\[PASS\]|Succeeded|active$|\bUP\b|running$", "32"), (r"\[FAIL\]|ERROR(?!-ACTIVE)|failed", "31"),
             (r"WARNING", "33"), (r"^NOTE:|^\[  OK  \]", "90")]
    for rx, code in rules:
        if re.search(rx, line):
            return f"\x1b[{code}m{line}\x1b[0m"
    return line


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("out")
    ap.add_argument("--title", default="terminal")
    ap.add_argument("--cols", type=int, default=110)
    ap.add_argument("--last", type=int, default=0, help="keep only the last N lines")
    ap.add_argument("--first", type=int, default=0, help="keep only the first N lines")
    ap.add_argument("--no-highlight", action="store_true")
    args = ap.parse_args()

    text = open(args.src, encoding="utf-8", errors="replace").read().replace("\r\n", "\n")
    # Carriage returns redraw a line: keep what is visible at the end.
    lines = [l.split("\r")[-1] for l in text.split("\n")]
    lines = [OTHER_ESC.sub("", l).expandtabs(8) for l in lines]
    while lines and not lines[-1].strip():
        lines.pop()
    if args.first:
        lines = lines[: args.first]
    if args.last:
        lines = lines[-args.last:]
    clipped = []
    for l in lines:
        visible = SGR.sub("", l)
        if len(visible) > args.cols and "\x1b[" not in l:
            l = l[: args.cols - 1] + "…"
        clipped.append(l if args.no_highlight else highlight(l))
    body = "\n".join(ansi_to_html(l) for l in clipped)

    char_w, line_h = 8.4, 19
    width = int(args.cols * char_w + 48)
    height = int(len(clipped) * line_h + 84)
    page = f"""<!doctype html><meta charset="utf-8"><style>
html,body{{margin:0;background:#16181d}}
.win{{margin:12px;border-radius:10px;background:#1e2127;box-shadow:0 0 0 1px #2c313a;overflow:hidden}}
.bar{{height:30px;display:flex;align-items:center;gap:7px;padding:0 12px;background:#262a31;
     font:12px system-ui,sans-serif;color:#9da5b4}}
.dot{{width:11px;height:11px;border-radius:50%;background:#3e4451}}
.t{{margin-left:8px}}
pre{{margin:0;padding:10px 12px 12px;color:#dcdfe4;font:14px/{line_h}px "JetBrains Mono","DejaVu Sans Mono",monospace;
    white-space:pre}}
</style><div class="win"><div class="bar"><span class="dot"></span><span class="dot"></span><span class="dot"></span>
<span class="t">{html.escape(args.title)}</span></div><pre>{body}</pre></div>"""

    chromium = os.environ.get("CHROMIUM") or next(
        p for p in ["/usr/bin/chromium", "/usr/lib/chromium/chromium", "/usr/bin/google-chrome"] if os.path.exists(p))
    with tempfile.TemporaryDirectory() as tmp:
        page_path = os.path.join(tmp, "term.html")
        with open(page_path, "w") as f:
            f.write(page)
        subprocess.run([chromium, "--headless=new", "--disable-gpu", "--hide-scrollbars",
                        f"--user-data-dir={tmp}/profile", f"--screenshot={os.path.abspath(args.out)}",
                        f"--window-size={width},{height}", "--force-device-scale-factor=1",
                        f"file://{page_path}"], check=True, capture_output=True)
    print(args.out)


if __name__ == "__main__":
    main()
