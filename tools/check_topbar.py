"""Pixel-level check that top-bar title, clock and counter never overlap.

Reads the raw framebuffer because Canvas has no get(x, y).

Run: python3 tools/check_topbar.py
"""

import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import display_canvas as dc
import screen


def pixel(canvas, x, y):
    index = y * dc.BYTES_PER_ROW + (x >> 3)
    return (canvas.buffer[index] >> (x & 7)) & 1


def ink_columns(canvas, y0, y1):
    """Columns carrying glyph ink.

    The title bar is filled and the text is drawn inverted, so ink is 0.
    """
    cols = set()
    for y in range(y0, y1 + 1):
        for x in range(dc.WIDTH):
            if not pixel(canvas, x, y):
                cols.add(x)
    return cols


def runs(cols):
    out = []
    for x in sorted(cols):
        if out and x == out[-1][1] + 1:
            out[-1][1] = x
        else:
            out.append([x, x])
    return [(a, b) for a, b in out]


def check(title, connected=True):
    canvas = dc.Canvas()
    v = screen.Screen.__new__(screen.Screen)
    v.canvas = canvas
    v.link = lambda: connected
    v.frame(title)
    bar = ink_columns(canvas, 0, 12)
    spans = runs(bar)
    overlap = False
    for i in range(len(spans) - 1):
        if spans[i][1] + 1 >= spans[i + 1][0]:
            overlap = True
    flag = "OVERLAP" if overlap else "ok"
    print("%-46s link=%-11s spans=%s  %s" % (
        title[:44], repr(connected), spans, flag))
    return overlap


def main():
    titles = list(screen.FA_TITLES)[:40]
    titles.append("A very long latin title that will not possibly fit here")
    bad = 0
    for connected in (True, False):
        for title in titles:
            if check(title, connected):
                bad += 1
    print("\n%d overlapping case(s)" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
