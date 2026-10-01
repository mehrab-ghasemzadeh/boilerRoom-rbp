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
            for dx, dy in (
                    (1, 0), (-1, 0), (0, 1), (0, -1),
                    (1, 1), (1, -1), (-1, 1), (-1, -1),
                ):
                nxt = (x + dx, y + dy)
                if nxt in pixels and nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        sizes.append(size)
    sizes.sort(reverse=True)
    return sizes


HAMZA_ALOFS = ("أ", "إ", "آ", "ؤ", "ئ", "ء")
# A kaf's stroke and an alef's hamza are part of the letter's drawing, not a
# dot standing off it, so they are allowed to be attached. The dots below are
# not: those must stand clear or the letter is misread.
ATTACHED_MARKS = HAMZA_ALOFS + ("ک", "گ")


def _expected_marks(letter: str, form: str) -> int:
    """
    How many dots this glyph should have, or 0 when it should have none.

    Taken from the table the generator published rather than re-rasterising the
    source font and guessing. Re-deriving it here meant the check and the
    generator could disagree, and when they did the checker reported dotless
    letters as having their dots welded on.
    """
    if letter in ATTACHED_MARKS:
        return 0
    if letter == "ی":
        return 0 if form in display_font_fa.YEH_BARE_FORMS else 2
    entry = display_font_fa.DOTTED.get(letter)
    return entry[0] if entry else 0


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
            expected = _expected_marks(letter, form)
            if expected:
                # Every dot must be its own pixel standing clear of the letter:
                # not welded on, and not fused side by side into one bar.
                if loose == 0:
                    welded.append(name)
                elif len(sizes) - 1 != expected or loose > budget:
                    split.append(
                        f"{name} ({len(sizes) - 1} dot cluster(s), "
                        f"want {expected})"
                    )
            elif loose:
                # A hamza or a kaf stroke is allowed to stand clear of its
                # letter; nothing else should.
                if letter in ATTACHED_MARKS or letter == "ۀ":
                    if loose > budget:
                        split.append(f"{name} ({loose}px loose)")
                else:
                    split.append(f"{name} ({loose}px loose)")

    print(f"glyphs checked: {checked}")
    print(f"mark welded onto its letter: {len(welded)}  {' '.join(welded) or '-'}")
    print(f"letter cut apart:            {len(split)}")
    for name in split:
        print(f"    {name}")
    return 1 if welded or split else 0


if __name__ == "__main__":
    raise SystemExit(main())
