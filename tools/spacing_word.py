"""Where does one letter end and the next begin, on the row?

Draws a word through the real shaper and canvas and marks the column each
glyph occupies, so a gap that is one pixel too small can be seen rather than
squinted at.

Run: python3 tools/spacing_word.py نگاشت
"""

import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import display_canvas as dc
import text_shaper


def at(canvas, x, y):
    return (canvas.buffer[y * dc.BYTES_PER_ROW + (x >> 3)] & (0x80 >> (x & 7))) != 0


def draw(word: str) -> None:
    pieces = text_shaper.shape(word)
    canvas = dc.Canvas()
    x = 2
    print(f"{word}: {len(pieces)} piece(s)")
    edges = []
    for piece in pieces:
        columns, advance = piece.columns, piece.advance
        start = x
        for i, column in enumerate(columns):
            for row in range(dc.HEIGHT):
                if column & (1 << row):
                    canvas.pixel(x + i, row, on=True)
        edges.append((piece.letter, piece.form, start, start + advance, columns))
        x += advance

    print(f"{'letter':>8} {'form':>9} {'from':>5} {'to':>4} {'ink':>10}  gap-to-next")
    for i, (letter, form, start, stop, columns) in enumerate(edges):
        ink = [start + i for i, c in enumerate(columns) if c] if columns else []
        lo, hi = (min(ink), max(ink)) if ink else (start, start)
        nxt = ""
        if i + 1 < len(edges):
            next_ink = [
                edges[i + 1][2] + j for j, c in enumerate(edges[i + 1][4]) if c
            ]
            if next_ink and ink:
                gap = min(next_ink) - hi
                nxt = f"{gap}" + ("  <-- TOUCHING" if gap <= 0 else "")
        print(f"{letter:>8} {form:>9} {lo:>5} {hi:>4} {'':>10}  {nxt}")

    print()
    for y in range(13):
        print(f"{y:2} |" + "".join(
            "#" if at(canvas, x, y) else "." for x in range(0, 128)
        ))


if __name__ == "__main__":
    draw(sys.argv[1] if len(sys.argv) > 1 else "نگاشت")