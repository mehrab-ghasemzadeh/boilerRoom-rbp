"""
Generate ``src/display_font_fa.py`` from the hand-drawn binary designs in
``persian_alphabet.md``.

The NotoNaskh font that was here before rendered the same letters differently,
because it is a real typeface and these are a pixel sketch. This replaces the
glyph table only: the shaping, the baseline and the canvas path are unchanged,
so words still come out joined and right-to-left. What changes is which bitmap
each letter-form points at.

Each design in the markdown is 12 columns by 11 rows. In the font's terms that
is width=12, ascender=10 (so the 11 rows sit just above the baseline),
advance=12 (letters tile edge to edge and the shaper's ink-to-ink spacing puts
a single blank column between words), lsb=0.

Letters or forms the sketch does not have -- the lam-alef ligatures, and a
couple of hamza variants -- are omitted rather than faked, and the shaper
degrades the way it already does: it asks for isolated, then final, then
initial, then medial, and falls back to a blank piece when none exists.
"""

from __future__ import annotations

import os
import re

SRC = os.path.join(os.path.dirname(__file__), "..", "persian_alphabet.md")
OUT = os.path.join(os.path.dirname(__file__), "..", "src", "display_font_fa.py")

GLYPH_W = 12
GLYPH_H = 11
ROW_HEIGHT = 13
BASELINE = 10
ASCENDER = GLYPH_H - 1  # 10: eleven rows sitting just above the baseline
ADVANCE = GLYPH_W       # 12: tile edge to edge; the shaper spaces by ink


def parse_designs(path: str) -> dict[str, list[list[int]]]:
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()

    designs: dict[str, list[list[int]]] = {}
    label_pattern = re.compile(
        r"'([^']+)\s*-\s*(Isolated|Initial|Medial|Final)'\s*:\s*\[",
    )

    for match in label_pattern.finditer(text):
        label = f"{match.group(1).strip()} - {match.group(2)}"
        pos = match.end()
        depth = 1
        end = pos
        while end < len(text) and depth > 0:
            ch = text[end]
            if ch == "[":
                depth += 1
            elif ch == "]":
                depth -= 1
                if depth == 0:
                    break
            end += 1
        body = text[pos:end]

        rows: list[list[int]] = []
        for row_match in re.finditer(r"\[([01,\s]+)\]", body):
            row = [int(x) for x in row_match.group(1).split(",") if x.strip()]
            rows.append(row)

        if rows:
            designs[label] = rows

    return designs


def columns_from_rows(rows: list[list[int]]) -> tuple[int, ...]:
    """Turn a 2D binary grid into the font's per-column integer bitmask.

    Bit 0 is the top row of the ink, matching the canvas's convention, so the
    first row of the sketch is the highest row on the glass.
    """
    out: list[int] = []
    for col in range(GLYPH_W):
        value = 0
        for row in range(GLYPH_H):
            if row < len(rows) and col < len(rows[row]) and rows[row][col]:
                value |= 1 << row
        out.append(value)
    return tuple(out)


def main() -> None:
    designs = parse_designs(SRC)
    if not designs:
        raise SystemExit("no designs parsed from %s" % SRC)

    forms = ("isolated", "initial", "medial", "final")
    entries: list[str] = []

    for letter in sorted({label.split(" - ")[0] for label in designs}):
        entries.append(f"    {letter!r}: {{")
        for form in forms:
            key = f"{letter} - {form.capitalize()}"
            rows = designs.get(key)
            if rows is None:
                continue
            columns = columns_from_rows(rows)
            entries.append(
                f"        {form!r}: ({columns!r}, {GLYPH_W}, {ASCENDER}, "
                f"{ADVANCE}, 0),"
            )
        entries.append("    },")

    body = "\n".join(entries)

    out = f'''"""
Persian glyphs for the graphical display, generated - do not edit by hand.

Produced by ``tools/gen_persian_font_from_designs.py`` from the hand-drawn
binary designs in ``persian_alphabet.md``. Each design is 12 px wide by 11 px
tall; the four contextual forms per letter come straight out of that file.

Keyed by ``(letter, joining position)`` where the position is one of
``isolated``, ``final``, ``initial`` or ``medial``. The value is
``(columns, width, ascender, advance, lsb)``:

* ``columns`` - one int per pixel column, bit 0 at the top of the ink;
* ``width`` - how many of those columns there are;
* ``ascender`` - how many rows above the shared baseline the ink starts;
* ``advance`` - how far to step the pen;
* ``lsb`` - kept for the tuple shape the shaper expects, always 0 here.

Letters that only join on one side - alef, dal, ra, za, waw and their
hamza variants - have no initial or medial form in the sketch, and
``text_shaper`` asks for their isolated shape instead. The lam-alef
ligatures are not drawn in the sketch at all, so they are absent here and the
shaper falls back to drawing the two letters separately.
"""

from __future__ import annotations

ROW_HEIGHT = {ROW_HEIGHT}
BASELINE = {BASELINE}

GLYPHS = {{
{body}
}}


def glyph(letter: str, form: str) -> tuple[tuple[int, ...], int, int, int, int] | None:
    """The bitmap for one letter in one joining position, or None."""
    return GLYPHS.get(letter, {{}}).get(form)
'''

    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(out)

    print(f"wrote {OUT}: {len(designs)} designs, {len({label.split(' - ')[0] for label in designs})} letters")


if __name__ == "__main__":
    main()