#!/usr/bin/env python3
"""Render captured ANSI terminal output to a PNG that looks like a terminal
window. Understands the small subset of cursor controls that mac-cleaner's TUI
uses (CUP `[H`, EL `[K`, ED `[J`, `\\r`, `\\n`) plus SGR colors, so it works for
both the linear commands (scan/suggest) and a final TUI frame.

Only dependency is Pillow. Usage:
    ansi_to_png.py input.ansi output.png ["Window Title"]
"""
import sys

from PIL import Image, ImageDraw, ImageFont

FONT_PATH = "/System/Library/Fonts/Menlo.ttc"   # index 0 Regular, 1 Bold
FONT_SIZE = 26

# Menlo has no emoji glyphs; swap the few we use for monospace-safe symbols so
# the screenshot stays crisp instead of showing tofu boxes.
SUBST = {
    "️": "",      # variation selector
    "📁": "▸", "🌐": "●", "🔨": "●", "📦": "●", "🗂": "●",
    "⏳": "…", "⚠": "!", "✓": "✓", "❯": "▸",
}

PALETTE = {
    31: (233, 110, 110), 32: (150, 196, 120), 33: (230, 192, 120),
    34: (97, 175, 239), 35: (198, 120, 221), 36: (86, 182, 194),
    37: (210, 210, 210),
}
DEFAULT_FG = (212, 212, 212)
BG = (24, 24, 33)


def _dim(c):
    return tuple(int(x * 0.55) for x in c)


class Cell:
    __slots__ = ("ch", "fg", "bold", "dim", "rev")

    def __init__(self, ch=" ", fg=None, bold=False, dim=False, rev=False):
        self.ch, self.fg, self.bold, self.dim, self.rev = ch, fg, bold, dim, rev


def emulate(text: str):
    """Apply the terminal control codes and return the final screen as rows."""
    for k, v in SUBST.items():
        text = text.replace(k, v)
    rows: list[list[Cell]] = [[]]
    r = c = 0
    fg = None
    bold = dim = rev = False

    def ensure(rr, cc):
        while len(rows) <= rr:
            rows.append([])
        row = rows[rr]
        while len(row) <= cc:
            row.append(Cell())

    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "\x1b" and i + 1 < n and text[i + 1] == "[":
            j = i + 2
            while j < n and not (0x40 <= ord(text[j]) <= 0x7e):
                j += 1
            if j >= n:
                break
            params, final = text[i + 2:j], text[j]
            i = j + 1
            if params.startswith("?"):
                continue                      # private modes (?25l, ?1049h) → ignore
            if final == "m":
                codes = params.split(";") if params else ["0"]
                for code in codes:
                    if code in ("", "0"):
                        fg, bold, dim, rev = None, False, False, False
                    elif code == "1":
                        bold = True
                    elif code == "2":
                        dim = True
                    elif code == "7":
                        rev = True
                    elif code == "22":
                        bold = dim = False
                    elif code == "27":
                        rev = False
                    elif code == "39":
                        fg = None
                    elif code.isdigit():
                        v = int(code)
                        if 30 <= v <= 37:
                            fg = PALETTE.get(v, DEFAULT_FG)
                        elif 90 <= v <= 97:
                            fg = PALETTE.get(v - 60, DEFAULT_FG)
            elif final in ("H", "f"):
                ps = params.split(";")
                r = (int(ps[0]) - 1) if ps and ps[0] else 0
                c = (int(ps[1]) - 1) if len(ps) > 1 and ps[1] else 0
            elif final == "J":
                if params in ("", "0"):
                    ensure(r, c)
                    rows[r] = rows[r][:c]
                    del rows[r + 1:]
                elif params == "2":
                    rows[:] = [[]]
                    r = c = 0
            elif final == "K":
                ensure(r, c)
                if params in ("", "0"):
                    rows[r] = rows[r][:c]
                elif params == "2":
                    rows[r] = []
            continue
        if ch == "\r":
            c = 0
        elif ch == "\n":
            r += 1
            c = 0
        elif ch == "\x1b":
            pass
        else:
            ensure(r, c)
            rows[r][c] = Cell(ch, fg, bold, dim, rev)
            c += 1
        i += 1
    return rows


def render(rows, out_path, title="mac-cleaner"):
    reg = ImageFont.truetype(FONT_PATH, FONT_SIZE)
    try:
        bold = ImageFont.truetype(FONT_PATH, FONT_SIZE, index=1)
    except Exception:
        bold = reg
    cw = int(round(reg.getlength("M")))
    asc, desc = reg.getmetrics()
    chh = asc + desc + 6

    while rows and not any(cell.ch.strip() or cell.rev for cell in rows[-1]):
        rows.pop()
    if not rows:
        rows = [[Cell(" ")]]
    ncols = max(20, max(len(r) for r in rows))
    pad, topbar = 22, 46
    W = pad * 2 + ncols * cw
    H = topbar + pad + len(rows) * chh

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, W, topbar], fill=(40, 40, 54))
    for k, col in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        cx = pad + k * 24
        d.ellipse([cx, topbar // 2 - 7, cx + 14, topbar // 2 + 7], fill=col)
    if title:
        tw = reg.getlength(title)
        d.text(((W - tw) / 2, (topbar - FONT_SIZE) / 2 - 1), title,
               font=reg, fill=(170, 170, 182))

    y = topbar + pad // 2
    for row in rows:
        x = pad
        for cell in row:
            fg = cell.fg or DEFAULT_FG
            if cell.dim:
                fg = _dim(fg)
            f = bold if cell.bold else reg
            if cell.rev:
                d.rectangle([x, y, x + cw, y + chh], fill=fg)
                d.text((x, y), cell.ch, font=f, fill=BG)
            elif cell.ch != " ":
                d.text((x, y), cell.ch, font=f, fill=fg)
            x += cw
        y += chh
    img.save(out_path)
    print(f"wrote {out_path} ({W}x{H})")


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 1
    with open(argv[0], "r", encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    title = argv[2] if len(argv) > 2 else "mac-cleaner"
    render(emulate(text), argv[1], title)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
