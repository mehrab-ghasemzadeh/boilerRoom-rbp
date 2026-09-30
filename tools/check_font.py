"""
Check the generated Persian glyphs for the two faults a one-bit panel causes.

At this size a letter can be damaged in exactly two ways, and both are visible
to an operator: a dot welded onto the letter it belongs to, which is what makes
Persian look like it is missing its i's, and a letter cut in half, which makes
it unreadable. Neither is a matter of taste, so both are counted here rather
than left to be eyeballed.

    python3 tools/check_font.py
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

import display_font_fa  # noqa: E402
import gen_persian_font as gen  # noqa: E402


def blobs(columns: tuple[int, ...]) -> list[int]:
    """Sizes of the connected ink blobs, largest first."""
    pixels = {
        (x, y)
        for x, column in enumerate(columns)
        for y in range(64)
        if column & (1 << y)
    }
    seen: set[tuple[int, int]] = set()
    sizes = []
    for start in pixels:
        if start in seen:
            continue
        size = 0
        stack = [start]
        seen.add(start)
        while stack:
            x, y = stack.pop()
            size += 1
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nxt = (x + dx, y + dy)
                if nxt in pixels and nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        sizes.append(size)
    sizes.sort(reverse=True)
    return sizes


def _glyph_has_mark(letter: str, form: str) -> bool:
    """Whether the generator found a dot, hamza or inner stroke on this glyph.

    Read back out of the generated table rather than from a list of dotted
    letters, so the check and the generator agree on what a mark is: a Persian
    yeh is dotted alone and bare once it joins, and a table cannot say that.
    """
    glyph = display_font_fa.glyph(letter, form)
    if glyph is None:
        return False
    columns = glyph[0]
    # A mark is a small piece of ink that can be separated from the body at some
    # cut. The stored bitmap is already cut, so look for the same thing in the
    # font: re-rasterise and ask whether any cut frees a small piece.
    grey = gen.grey_for(letter, form)
    if grey is None:
        return False
    rows, width, height = grey
    return gen._has_mark(rows, width, height, gen.MARK_BUDGET.get(letter, gen.MAX_MARK_PIXELS))


def main() -> int:
    welded: list[str] = []
    split: list[str] = []
    checked = 0

    for letter in gen.LETTERS:
        budget = gen.MARK_BUDGET.get(letter, gen.MAX_MARK_PIXELS)
        for form in gen.FORMS:
            glyph = display_font_fa.glyph(letter, form)
            if glyph is None:
                continue
            columns = glyph[0]
            sizes = blobs(columns)
            if not sizes:
                continue
            checked += 1
            loose = sum(sizes[1:])
            name = f"{letter}/{form}"
            if _glyph_has_mark(letter, form):
                # The mark must be standing off its letter, not welded on.
                if loose == 0:
                    welded.append(name)
                elif loose > budget:
                    split.append(f"{name} ({loose}px loose)")
            elif loose:
                # Nothing should be detached from a letter that has no mark.
                split.append(f"{name} ({loose}px loose)")

    print(f"glyphs checked: {checked}")
    print(f"mark welded onto its letter: {len(welded)}  {' '.join(welded) or '-'}")
    print(f"letter cut apart:            {len(split)}")
    for name in split:
        print(f"    {name}")
    return 1 if welded or split else 0


if __name__ == "__main__":
    raise SystemExit(main())
