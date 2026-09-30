"""
Persian shaping and right-to-left reordering, in pure Python.

Persian is a cursive script and a right-to-left one, and a bitmap display needs
both of those handled before it can draw anything. Drawing a string here means
two things happened first:

1. **Shaping** — every letter was swapped for the form it takes given its
   neighbours. A beh between two vowels is a different shape from a beh at the
   end of a word, from the same codepoint. Without this the text renders as a
   row of isolated letters, which is how it looks if you just map codepoints to
   glyphs.

2. **Reordering** — the shaped letters were put in the order they are read,
   right to left, while the canvas draws left to right.

Neither is optional, and neither is hard. The joining rules are three
classifications and one pass over the string; the reordering is a simplified
bidi over :func:`unicodedata.bidirectional`.

The one thing this deliberately does not do is handle the full Unicode Bidi
Algorithm. Full UBA is what you need for a document or a web page. This is a
menu of short labels, so what is here is the part that actually shows up:
Persian runs reverse, numbers inside them do not. ``68`` must read as 68, and a
panel full of temperatures and cut-out thresholds is precisely where getting
that wrong would be dangerous.

No dependency on the canvas or the hardware, so it can be checked on a laptop:

    >>> [piece.letter for piece in shape("سلام")]
    ['س', 'ل', 'ا', 'م']
"""

from __future__ import annotations

import unicodedata

from display_font import CELL_WIDTH as _ASCII_CELL
from display_font import GLYPH_HEIGHT as _ASCII_HEIGHT
from display_font import glyph as _ascii_glyph
from display_font_fa import glyph as _glyph

__all__ = ["Piece", "shape", "text_width", "has_rtl"]


# Joining behaviour, from Unicode's ArabicShaping.txt. This is the whole
# classification: how a letter connects to the letters around it.
#
#   D - dual: connects on both sides (ب پ ت ث ج چ ح خ س ش ص ض ط ظ ع غ ف ق
#       ک گ ل م ن ه ی ئ)
#   R - right-joining: connects only to the letter before it (ا د ذ ر ز ژ و
#       and their hamza variants, ۀ ة)
#   U - non-joining: stands alone, and breaks any join across it (ء)
#
# Digits and everything outside Arabic are non-joining too, and are simply not
# listed: lookups that miss fall through to "does not join", which is the
# right answer for them.
_JOINING = {
    "ب": "D", "پ": "D", "ت": "D", "ث": "D",
    "ج": "D", "چ": "D", "ح": "D", "خ": "D",
    "د": "R", "ذ": "R", "ر": "R", "ز": "R", "ژ": "R",
    "س": "D", "ش": "D", "ص": "D", "ض": "D",
    "ط": "D", "ظ": "D", "ع": "D", "غ": "D",
    "ف": "D", "ق": "D", "ک": "D", "گ": "D",
    "ل": "D", "م": "D", "ن": "D", "ه": "D", "ی": "D", "ئ": "D",
    "ا": "R", "أ": "R", "إ": "R", "آ": "R", "ؤ": "R",
    "و": "R", "ۀ": "R", "ة": "R",
    "ء": "U",
}

# A gap between letters, so a word does not read as one long word.
_GAP = 1

# Lam plus an alef is one glyph, not two. The glyph table is keyed by the two
# letters that spell it.
_LIGATURE_SECOND = "اأإآ"

# Codepoints that exist only to change how the text around them joins, and take
# up no room of their own. ZWNJ is the one that matters: it is how Persian
# writes a half-space inside a word, as in "خوانش‌ها", where the letters either
# side must *not* join. Rendering it as a glyph would draw a box where nothing
# is, and letting it into the run would break joining that should hold.
_INVISIBLE = frozenset("‌‍‎‏⁦⁧⁨⁩")

# Anything at or above this is Persian script or a related block. The canvas
# uses this to decide whether a string needs the slow path at all, and the
# overwhelming majority of what this device draws is ASCII.
_RTL_FROM = 0x0590


class Piece:
    """
    One drawable glyph: where it came from, and what to draw for it.

    ``letter`` and ``form`` are kept because they are what a caller inspecting
    output wants to see, and what the tests assert on. The canvas uses the
    bitmap fields and ignores them.
    """

    __slots__ = ("letter", "form", "columns", "width", "ascender", "advance", "rtl")

    def __init__(
        self,
        letter: str,
        form: str,
        columns: tuple[int, ...],
        width: int,
        ascender: int,
        advance: int,
        rtl: bool,
    ) -> None:
        self.letter = letter
        self.form = form
        self.columns = columns
        self.width = width
        self.ascender = ascender
        self.advance = advance
        self.rtl = rtl

    def __repr__(self) -> str:
        return f"Piece({self.letter!r}, {self.form!r}, w={self.width})"


def has_rtl(text: str) -> bool:
    """True when ``text`` holds anything this module has to reorder."""
    return any(ord(character) >= _RTL_FROM for character in text)


def _joins_forward(letter: str) -> bool:
    """Can ``letter`` connect to the one after it?"""
    return _JOINING.get(letter, "U") in ("D", "R")


def _joins_backward(letter: str) -> bool:
    """Can the one before ``letter`` connect to it?"""
    return _JOINING.get(letter, "U") == "D"


def _neighbours(letters: list[str], index: int) -> tuple[str | None, str | None]:
    """
    The nearest letters either side, skipping anything non-joining.

    Persian text here carries no diacritics — the panel has no room for them and
    the UI does not use them — so a single skip is enough. Skipping spaces
    matters: "ب ت" must not join across the space.
    """
    before = letters[index - 1] if index > 0 else None
    after = letters[index + 1] if index + 1 < len(letters) else None
    return before, after


def _form_for(letters: list[str], index: int) -> str:
    """Which of the four shapes this letter takes, given its neighbours."""
    letter = letters[index]
    before, after = _neighbours(letters, index)

    joined_left = (
        before is not None
        and _joins_forward(before)
        and _joins_backward(letter)
    )
    joined_right = (
        after is not None
        and _joins_forward(letter)
        and _joins_backward(after)
    )

    if joined_left and joined_right:
        return "medial"
    if joined_left:
        return "final"
    if joined_right:
        return "initial"
    return "isolated"


def _ascii_piece(character: str) -> Piece:
    """
    Wrap a character from the ASCII font as a piece.

    Persian labels carry numbers and punctuation, so a Persian string is
    rarely pure Persian. Those characters are drawn from the existing 5x7
    font at the same advance it uses everywhere else, which keeps a line that
    mixes scripts on one baseline.
    """
    columns = _ascii_glyph(character)
    return Piece(
        character,
        "isolated",
        tuple(columns),
        _ASCII_CELL - 1,
        _ASCII_HEIGHT,
        _ASCII_CELL,
        rtl=False,
    )


def _piece(letter: str, form: str, rtl: bool) -> Piece | None:
    """
    Build a piece from the glyph table, degrading rather than failing.

    A letter that cannot take the form asked for — alef has no initial form,
    because it only joins on one side — falls back to isolated and then to
    final, which is what the shaping rules say happens anyway.
    """
    for candidate in (form, "isolated", "final", "initial", "medial"):
        raster = _glyph(letter, candidate)
        if raster is not None:
            columns, width, ascender, advance, _lsb = raster
            return Piece(letter, candidate, columns, width, ascender, advance, rtl)

    if not rtl:
        return _ascii_piece(letter)

    return None


def _ligature_at(letters: list[str], index: int) -> tuple[str, int] | None:
    """
    The lam-alef ligature starting at ``index``, if there is one.

    Lam followed by an alef is written as a single crossed glyph, not as two
    letters. "سلام" is spelled with one, and drawing the halves separately gives
    a lam and an alef standing shoulder to shoulder instead - which reads as a
    spelling mistake rather than as the word.

    Returns the ligature and how many letters it swallowed.
    """
    if letters[index] != "ل":
        return None
    if index + 1 >= len(letters) or letters[index + 1] not in _LIGATURE_SECOND:
        return None
    pair = letters[index] + letters[index + 1]
    return (pair, 2) if _glyph(pair, "isolated") is not None else None


def _shape_rtl_run(run: str) -> list[Piece]:
    """
    Shape one right-to-left run and return it in *drawing* order.

    Shaping happens in logical order — a letter's shape depends on the
    neighbours as they are written, not as they are drawn — and the result is
    reversed on the way out so the canvas can walk it left to right.
    """
    letters = list(run)
    shaped = []

    index = 0
    while index < len(letters):
        ligature = _ligature_at(letters, index)
        if ligature is not None:
            pair, size = ligature
            before = letters[index - 1] if index > 0 else None
            # The ligature joins to the letter before it and never to the one
            # after, because the alef sits at that end.
            form = "final" if before is not None and _joins_forward(before) else "isolated"
            piece = _piece(pair, form, rtl=True)
            if piece is not None:
                shaped.append(piece)
            index += size
            continue

        form = _form_for(letters, index)
        piece = _piece(letters[index], form, rtl=True)
        if piece is None:
            # Nothing to draw for this codepoint. A space is the expected case;
            # anything else would be a letter missing from the glyph table.
            shaped.append(Piece(letters[index], form, (), 0, 0, _GAP, rtl=True))
        else:
            shaped.append(piece)
        index += 1

    shaped.reverse()
    return shaped


def _runs(text: str) -> list[tuple[bool, str]]:
    """Split ``text`` into (is_rtl, text) runs, keeping order."""
    runs: list[tuple[bool, str]] = []
    current: list[str] = []
    current_rtl: bool | None = None

    for character in text:
        if character == " ":
            # A space is neutral. It ends the current run and stands alone,
            # so it is not reordered against whichever side it sits on.
            if current:
                runs.append((current_rtl, "".join(current)))
                current = []
            current_rtl = None
            runs.append((False, " "))
            continue

        if character in _INVISIBLE:
            # ZWNJ and friends. They are not a gap and not a letter: they tell
            # the shaper not to join across a position. Drawn as nothing, and
            # dropped from the run so they cannot affect joining either — the
            # space that follows them in "خوانش‌ها" is what ends the run.
            continue

        rtl = ord(character) >= _RTL_FROM
        if current_rtl is None or rtl == current_rtl:
            current.append(character)
            current_rtl = rtl
        else:
            runs.append((current_rtl, "".join(current)))
            current = [character]
            current_rtl = rtl

    if current:
        runs.append((current_rtl, "".join(current)))

    return runs


def _base_direction(text: str) -> bool:
    """
    Is this line right-to-left? True when the first strong character is.

    This is the same rule the Unicode bidi algorithm uses to pick a paragraph
    direction, and it is the one that matters here: a Persian label is
    right-to-left, while a line of numbers and ASCII - "68", "12:30" - is not,
    and treating those as Persian would reverse digits that must not reverse.

    Digits and punctuation are weak and are skipped; a line of nothing but
    weak characters defaults to left-to-right, which is the safe direction for
    the numeric labels this device is full of.
    """
    for character in text:
        category = unicodedata.bidirectional(character)
        if category in ("R", "AL"):  # strong right-to-left, or an Arabic letter
            return True
        if category == "L":  # strong left-to-right
            return False
    return False


def shape(text: str) -> list[Piece]:
    """
    Shape and reorder ``text`` for drawing left to right.

    Returns the pieces in the order the canvas should draw them, each carrying
    the pixels to draw and how wide to step.

    Two reversals happen, and they are not the same one. Within a Persian run
    the letters reverse, because that is the order they are read in. Across the
    whole line the *runs* reverse in a right-to-left paragraph, so a label like
    ``"دمای آب: 68"`` puts its number at the left-hand end where a Persian
    reader expects the end of the line to be. Numbers themselves never reverse:
    68 is 68, not 86, and this panel is mostly temperatures.
    """
    if not text:
        return []

    runs = _runs(text)
    if not runs:
        return []

    rtl_paragraph = _base_direction(text)

    pieces: list[Piece] = []
    for is_rtl, run in runs:
        if is_rtl:
            pieces.extend(_shape_rtl_run(run))
        elif run == " ":
            pieces.append(Piece(" ", "isolated", (), 0, 0, 3, rtl=False))
        else:
            # ASCII and digits, drawn left to right exactly as typed.
            for character in run:
                piece = _piece(character, "isolated", rtl=False)
                if piece is None:
                    piece = Piece(character, "isolated", (), 0, 0, 5, rtl=False)
                pieces.append(piece)

    if rtl_paragraph:
        # Reorder the runs, not the pieces: a number keeps its digits in order
        # while moving to the other side of the line.
        grouped: list[list[Piece]] = [[]]
        for piece in pieces:
            if piece.rtl and grouped[-1]:
                grouped.append([])
            grouped[-1].append(piece)
        pieces = [piece for group in reversed(grouped) for piece in group]

    return pieces


def text_width(text: str) -> int:
    """Pixels ``text`` will occupy once shaped."""
    return sum(piece.advance for piece in shape(text))