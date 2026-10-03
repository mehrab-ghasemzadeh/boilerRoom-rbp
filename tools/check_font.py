"""
Check the generated Persian glyphs for the faults a one-bit panel causes.

At this size a letter can go wrong in a handful of ways, and every one of them
is visible to an operator, so they are counted here rather than left to be
eyeballed:

* a letter-form that is missing entirely, or drawn blank;
* a letter cut in half, which makes it unreadable;
* a dot welded onto the letter it belongs to, which is what makes Persian look
  like it is missing its i's;
* a dot that is supposed to be there and is not;
* a joining line that is broken, or two forms of one letter that came out
  identical, so a letter cannot show which neighbours it has.

The last of those is the one worth automating. ``ص`` once had all four of its
forms drawn identically and nothing complained, which is why nothing looked
wrong until a word was set in it.

What each letter should look like is taken from ``text_shaper``'s own joining
table rather than restated here, so this checker and the shaper cannot disagree
about which forms a letter has.

    python3 tools/check_font.py
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import display_font_fa as font  # noqa: E402
import text_shaper  # noqa: E402

FORMS = ("isolated", "initial", "medial", "final")

# The joining line, as a row index into a design. Everything on this row is the
# stroke that has to reach the neighbouring letter.
JOIN_ROW = 6

# How a letter joins, from Unicode's ArabicShaping.txt. Only used to work out
# which forms a letter is supposed to have.
DUAL = "D"
RIGHT = "R"

# Dots, as (above the letter, below it), by hand from the alphabet rather than
# measured out of the designs. Measuring would only prove the drawings agree
# with themselves; the point here is to catch a dot that was never drawn.
DOTS = {
    "ب": (0, 1), "پ": (0, 3), "ت": (2, 0), "ث": (3, 0),
    "ج": (0, 1), "چ": (0, 3), "خ": (1, 0), "ذ": (1, 0),
    "ز": (1, 0), "ژ": (3, 0), "ش": (3, 0), "ض": (1, 0),
    "ظ": (1, 0), "غ": (1, 0), "ف": (1, 0), "ق": (2, 0),
    "ن": (1, 0),
    # Persian yeh is dotless isolated and final, and takes two below otherwise.
    "ی": ("initial-medial", 0),
}

# A mark that is drawn as part of the letter rather than as a dot standing off
# it: the madda and hamza over the alef variants, and the yeh hamza. They are
# allowed to touch, or to stand clear, without that being a fault.
ATTACHED_MARKS = ("ا", "آ", "أ", "إ", "ؤ", "ئ", "ک", "گ", "ۀ")

# A two-letter key is one design standing for a lam-alef pair.
LIGATURES = text_shaper._LIGATURE_SECOND


def pixels(columns: tuple[int, ...], height: int) -> set[tuple[int, int]]:
    return {
        (x, row)
        for x, column in enumerate(columns)
        for row in range(height)
        if column & (1 << row)
    }


def blobs(dots: set[tuple[int, int]]) -> list[int]:
    """Sizes of the connected ink blobs, largest first, 8-connected."""
    seen: set[tuple[int, int]] = set()
    sizes: list[int] = []
    for start in dots:
        if start in seen:
            continue
        size = 0
        stack = [start]
        seen.add(start)
        while stack:
            x, y = stack.pop()
            size += 1
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    nxt = (x + dx, y + dy)
                    if nxt in dots and nxt not in seen:
                        seen.add(nxt)
                        stack.append(nxt)
        sizes.append(size)
    sizes.sort(reverse=True)
    return sizes


def join_span(glyph) -> tuple[int, int] | None:
    """Leftmost and rightmost column carrying ink on the joining line."""
    columns = glyph[0]
    marked = [
        x for x in range(len(columns))
        if columns[x] & (1 << JOIN_ROW)
    ]
    return (marked[0], marked[-1]) if marked else None





def expected_forms(letter: str) -> tuple[str, ...]:
    if len(letter) == 2:
        return ("isolated", "final")
    joining = text_shaper._JOINING.get(letter, "U")
    if joining == DUAL:
        return FORMS
    if joining == RIGHT:
        return ("isolated", "final")
    return ("isolated",)


def expected_dots(letter: str, form: str) -> int:
    entry = DOTS.get(letter)
    if not entry:
        return 0
    above, below = entry
    if above == "initial-medial":
        above = 2 if form in ("initial", "medial") else 0
    return above + below


def main() -> int:
    missing: list[str] = []
    blank: list[str] = []
    cut: list[str] = []
    welded: list[str] = []
    absent: list[str] = []
    loose: list[str] = []
    broken: list[str] = []
    identical: list[str] = []
    checked = 0

    letters = sorted(font.GLYPHS)

    for letter in letters:
        raster = font.GLYPHS[letter]
        height = font.GLYPH_HEIGHT if hasattr(font, "GLYPH_HEIGHT") else 11
        forms = raster

        for form in expected_forms(letter):
            if form not in forms:
                missing.append(f"{letter}/{form}")
                continue
            glyph = forms[form]
            columns, _width, _asc, _adv, _lsb = glyph
            checked += 1

            ink = pixels(columns, 64)
            if not ink:
                blank.append(f"{letter}/{form}")
                continue

            checked_letter = len(letter) == 1

            if checked_letter:
                # A ligature is two strokes crossing on purpose, so the shape of
                # its blobs says nothing about whether it is damaged. Every other
                # check below is about one letter sitting in one cell.
                sizes = blobs(ink)
                total = sum(sizes)
                loose_px = total - sizes[0]

                if sizes[0] < total / 2:
                    cut.append(f"{letter}/{form} "
                               f"(largest blob {sizes[0]} of {total})")

                want = expected_dots(letter, form)
                if want:
                    if loose_px == 0:
                        welded.append(f"{letter}/{form} (wants {want} dot(s))")
                    elif loose_px < want:
                        absent.append(f"{letter}/{form} "
                                      f"({loose_px} dot px, want {want})")
                elif loose_px and letter not in ATTACHED_MARKS:
                    loose.append(f"{letter}/{form} ({loose_px}px off the letter)")

        # The joining line has to be one unbroken stroke, and a dual-joining
        # letter has to actually differ between its forms.
        #
        # Note what is deliberately *not* asserted here: that an initial reaches
        # further left than an isolated. It does not have to. A ب is drawn with
        # its bowl sweeping left, so the isolated and final forms run further
        # left than the initial, and treating that as a fault would condemn
        # every bowl-tailed letter. What does have to hold is that an initial
        # and a medial reach forward by the same amount, since both are drawing
        # the same connector towards the letter that follows.
        if expected_forms(letter) == FORMS:
            spans = {f: join_span(forms[f]) for f in FORMS if f in forms}
            missing_line = [f for f, s in spans.items() if s is None]
            if missing_line:
                broken.append(f"{letter}: no ink on the joining line in "
                              f"{', '.join(missing_line)}")
            else:
                for a, b in zip(FORMS, FORMS[1:]):
                    if forms[a] == forms[b]:
                        identical.append(f"{letter}: {a} is drawn exactly as {b}")
                if spans["initial"][0] != spans["medial"][0]:
                    broken.append(
                        f"{letter}: medial reaches {spans['medial'][0]} but "
                        f"initial reaches {spans['initial'][0]}; both draw the "
                        f"forward connector"
                    )
                if spans["medial"][1] < spans["final"][1]:
                    broken.append(
                        f"{letter}: medial reaches {spans['medial'][1]} but "
                        f"final reaches {spans['final'][1]}"
                    )

    def report(title: str, items: list[str]) -> None:
        print(f"{title:<38} {len(items)}")
        for item in items:
            print(f"    {item}")

    print(f"glyphs checked: {checked}   letters: {len(letters)}")
    report("form missing:", missing)
    report("glyph blank:", blank)
    report("letter cut apart:", cut)
    report("identical forms:", identical)
    report("joining line wrong:", broken)
    report("dot welded onto its letter:", welded)
    report("dot missing:", absent)
    report("unexpected loose ink:", loose)

    fatal = missing or blank or cut or identical or broken
    return 1 if fatal else 0


if __name__ == "__main__":
    raise SystemExit(main())
