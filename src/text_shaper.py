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

# Characters that look Persian by codepoint but must still be drawn left to
# right, because they are numbers and a number reads the same either way round
# only if you do not reverse it. Persian and Arabic-Indic digits live inside the
# Arabic block, so a plain codepoint test would swallow them into a reversed
# run and turn 135 into 531.
#
# The test is the Unicode bidi class rather than a codepoint list: EN is a
# European or Arabic-Indic digit, AN an Arabic-Indic number. Both are numbers,
# and both keep their order. Doing this by class means a digit nobody thought
# about is still a digit.
_NUMBER_CLASSES = ("EN", "AN")


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


def _is_rtl_letter(character: str) -> bool:
    """
    Is this a Persian letter that needs reversing?

    A codepoint test is not enough. The Arabic block contains the Arabic-Indic
    and Persian digits alongside the letters, and a digit is a number: it reads
    135, not 531, whichever script it is written in. So the number classes are
    excluded here, and a number keeps its order whether or not the line around
    it is Persian.

    Diacritics, which are combining and take their shape from the letter they
    sit on, count as letters so they stay inside the run they belong to.
    """
    if character.isdigit():
        return False
    if unicodedata.bidirectional(character) in _NUMBER_CLASSES:
        return False
    return ord(character) >= _RTL_FROM


def _connects_to_next(letter: str) -> bool:
    """
    Can ``letter`` connect to the one that follows it?

    Only a dual-joining letter can. A right-joining letter — alef, dal, ra, za,
    waw and the rest — draws its tail down and away, so nothing joins onto its
    left-hand side. That is the whole reason ر ends a word in its final form
    rather than carrying a letter after it.
    """
    return _JOINING.get(letter, "U") in ("D", "L")


def _is_ligature(token: str) -> bool:
    """Is this token a lam-alef pair rather than a single letter?"""
    return len(token) == 2


def _connects_to_previous(letter: str) -> bool:
    """
    Can the letter before ``letter`` connect onto it?

    Both dual- and right-joining letters accept a join on this side: a
    right-joining letter is precisely one that connects to the letter on its
    right and to nothing else. So ر at the end of a word takes a final form
    because it is joined from the letter before it.

    A lam-alef ligature accepts one too. That is why the letter in front of a
    "لا" takes an initial form: the ligature's lam reaches back to it.
    """
    if _is_ligature(letter):
        return True
    return _JOINING.get(letter, "U") in ("D", "R")


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


def _tokens(letters: list[str]) -> list[str]:
    """
    Collapse lam-alef into single tokens.

    The ligature is one joined shape, and it accepts a join from the letter
    before it exactly as a letter does. Shaping each letter against this list
    rather than against the raw characters is what makes the letter in front of
    a lam-alef take its initial form: without it, "بلا" gives a beh that was
    told it had nothing to join to and comes out isolated and detached.
    """
    tokens: list[str] = []
    index = 0
    while index < len(letters):
        ligature = _ligature_at(letters, index)
        if ligature is not None:
            pair, size = ligature
            tokens.append(pair)
            index += size
            continue
        tokens.append(letters[index])
        index += 1
    return tokens


def _shape_rtl_run(run: str) -> list[Piece]:
    """
    Shape one right-to-left run and return it in *drawing* order.

    Shaping happens in logical order — a letter's shape depends on the
    neighbours as they are written, not as they are drawn — and the result is
    reversed on the way out so the canvas can walk it left to right.
    """
    letters = list(run)
    tokens = _tokens(letters)
    shaped = []

    for position, token in enumerate(tokens):
        before = tokens[position - 1] if position > 0 else None
        after = tokens[position + 1] if position + 1 < len(tokens) else None

        if _is_ligature(token):
            # The ligature joins to the letter before it and never to the one
            # after, because the alef sits at that end and an alef does not
            # connect forwards.
            form = (
                "final"
                if before is not None and _connects_to_next(before)
                else "isolated"
            )
        else:
            # A join exists only when both letters are willing: the one joining
            # has to reach out, and the one being joined has to accept. The
            # token before this letter may be a ligature, which accepts a join
            # the same way a letter does.
            joined_before = (
                before is not None
                and _connects_to_next(before)
                and _connects_to_previous(token)
            )
            joined_after = (
                after is not None
                and _connects_to_next(token)
                and _connects_to_previous(after)
            )

            if joined_before and joined_after:
                form = "medial"
            elif joined_before:
                form = "final"
            elif joined_after:
                form = "initial"
            else:
                form = "isolated"

        piece = _piece(token, form, rtl=True)
        if piece is None:
            # Nothing to draw for this codepoint. A space is the expected case;
            # anything else would be a letter missing from the glyph table.
            shaped.append(Piece(token, form, (), 0, 0, _GAP, rtl=True))
        else:
            shaped.append(piece)

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

        rtl = _is_rtl_letter(character)
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
    groups: list[list[Piece]] = []

    for is_rtl, run in runs:
        if is_rtl:
            # Already in visual order: the first letter of the word is the
            # rightmost one, because that is where reading starts.
            group = _shape_rtl_run(run)
        elif run == " ":
            group = [Piece(" ", "isolated", (), 0, 0, 3, rtl=False)]
        else:
            # ASCII and digits, drawn left to right exactly as typed.
            group = []
            for character in run:
                piece = _piece(character, "isolated", rtl=False)
                if piece is None:
                    piece = Piece(character, "isolated", (), 0, 0, 5, rtl=False)
                group.append(piece)

        groups.append(group)
        pieces.extend(group)

    if rtl_paragraph and len(groups) > 1:
        # Reverse the order of the runs, and only the runs. Each run's letters
        # are already in the right order for drawing, so reversing them again
        # here would undo the shaping and lay the word out backwards. What does
        # need reversing is which run comes first: in a right-to-left line the
        # first run is the rightmost one, so "دمای آب: 68" puts its number at
        # the left-hand end where the end of the line is.
        pieces = [piece for group in reversed(groups) for piece in group]

    return pieces


def text_width(text: str) -> int:
    """Pixels ``text`` will occupy once shaped."""
    return sum(piece.advance for piece in shape(text))