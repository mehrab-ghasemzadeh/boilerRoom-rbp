"""
Generate ``src/display_font_fa.py`` — the Persian glyph table.

This runs on a laptop, not on the panel. It needs Pillow, uharfbuzz and a Naskh
Arabic font, none of which are dependencies of the firmware: it reads the font,
works out which presentation form each letter takes in each joining position,
rasterises those forms at panel scale, and writes out a plain tuple of bitmaps
that the firmware imports like any other Python data.

Run it after changing the font or the pixel size::

    python3 tools/gen_persian_font.py

The firmware-side rules it has to respect:

* one bit per pixel, one byte per column, bit 0 at the top of the glyph box —
  the same convention ``display_font.py`` already uses for ASCII;
* every glyph in a cell shares one baseline, so a letter's descender cannot
  sink into the row beneath it;
* glyphs are variable width, because Persian letters are. A fixed-width cell
  would either clip the alef or strand a gap next to the beh.

Shaping is resolved here rather than on the device because HarfBuzz is a
compiled extension and this is a Pi Zero: shaping there would cost memory and
startup time on every boot. What the device keeps is a lookup of
(letter, joining position) -> bitmap, which is a dict read and a byte loop.
"""

from __future__ import annotations

import os
import re
import sys

import uharfbuzz as hb
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT_PATH = os.path.join(ROOT, "src", "display_font_fa.py")

FONT_PATH = "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf"

# Em size the glyphs are rasterised at. This is the one number that decides
# whether Persian is legible here, and it was wrong at ten.
#
# Naskh puts a letter's dot very close to its body — a noon is a dot over a
# bowl, with barely a hairline of daylight between them. At ten pixels that gap
# is under a pixel wide, so averaging it away leaves the dot sitting *on* the
# bowl: "خوانشها" reads as letters with the noon glued to its neighbour, which
# is the single most obvious way Persian can look wrong.
#
# Eleven is the largest em the row allows. The tallest glyph, an alef with a
# hamza, reaches ten rows above the baseline and the row is ten rows tall above
# it, so eleven is the ceiling — and at eleven the gap above a noon is a whole
# pixel, which is enough to keep the dot a dot. Going to twelve would look
# better still and does not fit: the hamza alef would be clipped.
#
# The cost is width: every word grows by about eight percent, which the
# 122 px body and 124 px title bar both have room for.
RENDER_PX = 11

# Naskh has thin strokes, and at ten pixels they fall between sample points: a
# direct render loses the left arm of the beh and the result is a smudge. So
# the glyph is drawn this many times oversize and averaged back down, which is
# what a display at that size would be doing to the outlines anyway. Eight is
# the smallest factor that keeps every letter's shape intact.
SUPERSAMPLE = 8

# The threshold applied after averaging. It is only the fallback: each glyph
# searches for a cut that suits its own strokes, and uses this when the search
# finds nothing better. Low on purpose — at this size a stroke may cover well
# under half a pixel, and dropping it loses a letter feature outright.
THRESHOLD = 60

# How much ink a glyph may carry outside its main body. A dotted letter is one
# piece of writing plus a dot, and a three-dot triangle is up to five pixels, so
# more than this is not a dot: it is a piece of the letter that has been cut off,
# or a dot that has been welded on.
MAX_MARK_PIXELS = 6

# Kaf and gaaf carry a mark of their own inside the letter — the little
# "hamza" stroke — and it is several pixels rather than one, so they get a
# larger allowance. Without this no cut can satisfy them and the search would
# give up on two of the commonest letters in the language.
MARK_BUDGET = {"ک": 11, "گ": 11}

# The range of cuts the search looks at, lowest (fattest) first.
THRESHOLD_SEARCH = tuple(range(34, 101, 2))



def _components(mask: list[list[bool]], width: int, height: int) -> list[list[tuple[int, int]]]:
    """The connected blobs of ink, four-connected, in a fixed order."""
    seen = [[False] * width for _ in range(height)]
    blobs = []
    for y in range(height):
        for x in range(width):
            if not mask[y][x] or seen[y][x]:
                continue
            blob = []
            stack = [(x, y)]
            seen[y][x] = True
            while stack:
                cx, cy = stack.pop()
                blob.append((cx, cy))
                for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                    nx, ny = cx + dx, cy + dy
                    if 0 <= nx < width and 0 <= ny < height and mask[ny][nx] and not seen[ny][nx]:
                        seen[ny][nx] = True
                        stack.append((nx, ny))
            blobs.append(blob)
    return blobs


def _has_mark(
    grey: list[list[int]], width: int, height: int, budget: int
) -> bool:
    """
    Whether this glyph carries a dot, hamza or inner stroke of its own.

    Asked of the glyph rather than of a table of letters, because a letter's
    mark is not a property of the letter: a Persian yeh carries two dots on its
    own and none once it joins, and a final heh is written bare. Whether the
    mark is there is something the font knows and this file does not, so it is
    measured — if any cut in the range leaves a small piece standing off the
    body, the mark is present in the outline and has to be cut free of it.

    Getting this wrong is not cosmetic. The noon of "خوانشها" is in its initial
    form, and a dot welded to the letter it belongs to is precisely the fault
    this whole exercise is about; a check that only looked at the isolated form
    would pass it straight through.
    """
    for cut in THRESHOLD_SEARCH:
        mask = [[grey[y][x] > cut for x in range(width)] for y in range(height)]
        blobs = _components(mask, width, height)
        if len(blobs) < 2:
            continue
        blobs.sort(key=len, reverse=True)
        loose = sum(len(blob) for blob in blobs[1:])
        if 0 < loose <= budget:
            return True
    return False


def _choose_threshold(
    grey: list[list[int]],
    width: int,
    height: int,
    mark_expected: bool,
    budget: int = MAX_MARK_PIXELS,
) -> int:
    """
    Pick the cut that keeps this letter in one piece, minus its marks.

    Naskh's strokes vary in weight within a single glyph, so no global cut
    serves every letter: the cut that keeps a noon bowl whole is also the cut
    that welds the dot above it onto the bowl, and raising it opens the gap and
    breaks the bowl instead.

    The cut is chosen per glyph instead. A letter is one piece of writing with
    its dot or hamza standing off it, so a cut is acceptable when what is left
    outside the main body is a mark's worth of pixels — and among the acceptable
    cuts the one that makes the body heaviest wins, which is the lowest, because
    more ink is a fatter stroke. That is the whole rule: never cut a letter in
    half, never weld its dot on, never cut thinner than it has to be.
    """
    best_cut = None
    best_body = -1
    for cut in THRESHOLD_SEARCH:
        mask = [[grey[y][x] > cut for x in range(width)] for y in range(height)]
        blobs = _components(mask, width, height)
        if not blobs:
            continue
        blobs.sort(key=len, reverse=True)
        body = len(blobs[0])
        leftover = sum(len(blob) for blob in blobs[1:])
        if mark_expected:
            # The mark has to end up standing clear of the letter, or the letter
            # is missing its dot and no Persian reader can tell what it says.
            if not 0 < leftover <= budget:
                continue
        elif leftover:
            # Nothing should be detached from a letter that has no mark, so
            # anything loose at all means this cut has broken the letter.
            continue
        if body > best_body:
            best_cut, best_body = cut, body
            break  # cuts ascend, so the first acceptable one is the fattest

    if best_cut is not None:
        return best_cut

    # No cut satisfies the letter. Rather than give up and use the default,
    # take the cut that leaves the most ink in the main body, which is the one
    # that has broken off the least, and prefer one that has not split the
    # letter at all. A handful of Naskh letters are too fine for this size to
    # satisfy the rule, and this keeps their damage to a single stroke.
    best = (0, 0, 0)
    for cut in THRESHOLD_SEARCH:
        mask = [[grey[y][x] > cut for x in range(width)] for y in range(height)]
        blobs = _components(mask, width, height)
        if not blobs:
            continue
        blobs.sort(key=len, reverse=True)
        body = len(blobs[0])
        leftover = sum(len(blob) for blob in blobs[1:])
        # Fewest pieces first, then heaviest body, then least loose ink.
        score = (-(len(blobs) - 1), body, -leftover)
        if score > best:
            best, best_cut = score, cut
    return THRESHOLD if best_cut is None else best_cut

# Where the pen sits while a glyph is rasterised, in panel pixels. Far from the
# edge so nothing clips, and the same for every glyph so the left side bearing
# is directly comparable across the table.
PEN_X = 200

# Baseline position within a 13 px row: 10 px above it, 3 below. Persian sits
# low in its em box — alef and the lam-alef ligature both reach the ascender
# while ج چ ژ پ گ hang three rows under the baseline.
ROW_HEIGHT = 13
BASELINE = 10

# The letters Persian actually uses, plus the Arabic letters they borrow and
# the Arabic-Indic digits. Every one of these has to survive joining: the keys
# spell "تنظیمات", "دمای آب", "پمپ".
LETTERS = (
    "اأإآءؤئبپتثجچحخدذرزژسشصضطظعغفقکگلمنوه"
    "یۀة"
)

# Lam followed by an alef is not two letters with two shapes, it is one
# ligature with its own - it is what "سلام" is spelled with, and drawing the
# two halves separately produces a lam and an alef standing next to each other
# instead of the crossed form. Unicode gives these their own presentation forms,
# so they are rasterised like any other glyph.
LIGATURES = {
    "لا": ("\uFEFB", "\uFEFC"),
    "لآ": ("\uFEF5", "\uFEF6"),
    "لأ": ("\uFEF7", "\uFEF8"),
    "لإ": ("\uFEF9", "\uFEFA"),
}

# The alefs that can form the ligature, so the shaper knows what to look for.
LIGATURE_SECOND = "اأإآ"

DIGITS = "۰۱۲۳۴۵۶۷۸۹"

# The joining positions a letter can occupy. ``final`` and ``initial`` are the
# two half-joined shapes; ``medial`` is the fully joined one. Letters that only
# join on one side (ا د ذ ر ز ژ و) have no initial or medial form and fall back
# to their isolated shape, which is what the shaping table below encodes.
FORMS = ("isolated", "final", "initial", "medial")

# A partner letter used to force a joining position. Beh is dual-joining, so it
# pulls whatever sits next to it into the form we want: before the target to
# make it final, after it to make it initial, both to make it medial.
PARTNER = "ب"

_GLYPH_NAME = re.compile(r"^uni([0-9A-F]{4,6})(?:\.[A-Za-z0-9]+)?$")


def _shape(font: hb.Font, text: str) -> list[tuple[str, float]]:
    """Shape ``text``, returning ``(glyph_name, advance)`` in visual order."""
    buffer = hb.Buffer()
    buffer.add_str(text)
    buffer.direction = "rtl"
    buffer.script = "Arab"
    buffer.language = "fa"
    hb.shape(font, buffer, {})
    return [
        (font.glyph_to_string(info.codepoint), position.x_advance)
        for info, position in zip(buffer.glyph_infos, buffer.glyph_positions)
    ]


def _name_to_codepoint(name: str) -> int | None:
    match = _GLYPH_NAME.match(name)
    return int(match.group(1), 16) if match else None


def form_codepoint(font: hb.Font, letter: str, form: str) -> int:
    """
    The codepoint HarfBuzz substitutes for ``letter`` sitting in ``form``.

    HarfBuzz is asked to shape a probe string built around a known joining
    partner and the target glyph is picked out of the result. Going through the
    shaper rather than a hand-written table is what makes the Persian-specific
    letters work: گ and ک and ی have no presentation forms of their own in
    Unicode, and only the font's own GSUB knows what they should look like.
    """
    if form == "isolated":
        probe = letter
    elif form == "final":
        probe = PARTNER + letter
    elif form == "initial":
        probe = letter + PARTNER
    else:
        probe = PARTNER + letter + PARTNER

    codepoints = [
        codepoint
        for codepoint in (
            _name_to_codepoint(name) for name, _ in _shape(font, probe)
        )
        if codepoint is not None
    ]

    if not codepoints:
        return ord(letter)

    # The buffer comes back in visual order, left to right, while the probe is
    # written in logical order. In right-to-left text the first character
    # written is the rightmost one, so the target lands at a known index:
    # alone, after a partner, before a partner, or between two of them. Taking
    # that index rather than filtering the partner out by name is what keeps
    # this correct when the letter being probed *is* the partner - filtering
    # cannot tell the two apart, and neither can a reader of the result.
    index = {
        "isolated": None,
        "final": 0,    # "ب" + letter: the letter is the leftmost, so first out
        "initial": -1,  # letter + "ب": the letter is rightmost, so last out
        "medial": 1,   # "ب" + letter + "ب": the letter is in the middle
    }[form]

    if index is None:
        return codepoints[0]
    if len(codepoints) <= abs(index):
        return ord(letter)
    return codepoints[index]


def form_advance(face: hb.Face, font: hb.Font, letter: str, form: str) -> int:
    """
    How far the pen moves past this letter, in whole panel pixels.

    This is deliberately not the width of the ink. Arabic letters overlap their
    neighbours by design - the tail of one is the shoulder of the next - so
    advancing by the cropped ink width spreads a word out and makes it read as
    unjoined letters. The font's own advance is what keeps a shaped word the
    width it should be.

    A zero-advance glyph is one the shaper has already merged into a neighbour,
    such as the alef of a lam-alef ligature; those take no space of their own.
    """
    if form == "isolated":
        probe = letter
    elif form == "final":
        probe = PARTNER + letter
    elif form == "initial":
        probe = letter + PARTNER
    else:
        probe = PARTNER + letter + PARTNER

    shaped = _shape(font, probe)
    index = {"isolated": 0, "final": 0, "initial": -1, "medial": 1}[form]
    if not shaped:
        return 0
    try:
        advance = shaped[index][1] / face.upem * RENDER_PX
    except IndexError:
        return 0
    return max(1, round(advance))


def grey_for(letter: str, form: str, hb_font=None) -> tuple[list[list[int]], int, int] | None:
    """The averaged coverage of one glyph, as (rows, width, height).

    Exposed so the checker can ask the same question of the same glyph, in the
    same way, rather than answering it a second and different way. An empty
    result means the glyph has no ink at all.
    """
    if hb_font is None:
        blob = hb.Blob.from_file_path(FONT_PATH)
        hb_font = hb.Font(hb.Face(blob))
    codepoint = form_codepoint(hb_font, letter, form)
    rows, width, height, _pen, _ascender = _render_grey(FONT_PATH, codepoint)
    return None if rows is None else (rows, width, height)


def _render_grey(
    font_path: str, codepoint: int
) -> tuple[list[list[int]] | None, int, int, int, int]:
    """
    Draw one codepoint oversize, average it down, and return the coverage.

    Returns ``(rows, width, height, pen, ascender)``. ``rows`` is the grey
    coverage of the glyph's bounding box, one value per pixel, and nothing is
    thresholded: the coverage is handed on so the cut can be chosen against it,
    which is the whole point — deciding ink from one number for the whole font
    is what welded the dots on in the first place.

    ``ascender`` is how many rows the box starts above the baseline, so the
    caller can hang every glyph off one shared baseline.
    """
    scale = SUPERSAMPLE
    font = ImageFont.truetype(font_path, RENDER_PX * scale)
    canvas = Image.new("L", (PEN_X * 2 * scale, ROW_HEIGHT * 4 * scale), 0)
    draw = ImageDraw.Draw(canvas)

    baseline_y = ROW_HEIGHT * 2 * scale
    draw.text(
        (PEN_X * scale, baseline_y),
        chr(codepoint),
        font=font,
        fill=255,
        anchor="ls",
        language="fa",
        direction="rtl",
    )

    # Average back down to panel scale. Cropping after is deliberate: it bounds
    # the glyph to the ink the panel will actually show, which is what makes the
    # widths correct.
    canvas = canvas.resize((canvas.width // scale, canvas.height // scale), Image.BOX)

    pen = PEN_X
    # Work out what survives on a loose cut first, so a faint but real stroke
    # still counts and the glyph is not cropped away to nothing.
    loose = canvas.point(lambda value: 255 if value > THRESHOLD else 0).getbbox()
    if loose is None:  # space, or a codepoint this font has no glyph for
        return None, 0, 0, pen, 0

    left, top, right, bottom = loose
    pixels = canvas.crop(loose).load()
    width, height = right - left, bottom - top
    rows = [[pixels[x, y] for x in range(width)] for y in range(height)]
    return rows, width, height, pen, baseline_y // scale - top


def rasterise(
    font_path: str,
    codepoint: int,
    *,
    mark_budget: int = MAX_MARK_PIXELS,
) -> tuple[tuple[int, ...], int, int] | None:
    """
    Draw one codepoint and return ``(columns, width, ascender)``.

    ``columns`` is one int per pixel column, bit 0 at the top of the glyph's ink.
    ``ascender`` is how many rows the ink starts above the baseline, so the
    caller can hang every glyph off one shared baseline: a letter that stops
    short of the ascender costs no rows, and a descender cannot sink into the
    row beneath it.

    The glyph is drawn on its own rather than inside a probe word: a codepoint
    that is already a presentation form is not reshaped by HarfBuzz, so
    rendering it alone gives the same shape it would take in a word.
    """
    grey, width, height, pen, baseline_row = _render_grey(font_path, codepoint)
    if grey is None:
        return None

    cut = _choose_threshold(
        grey, width, height, _has_mark(grey, width, height, mark_budget), mark_budget
    )
    mask = [[grey[y][x] > cut for x in range(width)] for y in range(height)]
    if not any(any(row) for row in mask):  # everything too faint to keep
        return None

    columns = []
    for x in range(width):
        column = 0
        for y in range(height):
            if mask[y][x]:
                column |= 1 << y
        columns.append(column)

    # How far the ink starts from the pen, before cropping removed the gap.
    # Arabic glyphs sit asymmetrically about their origin - a final beh is
    # wider than its advance and overhangs its neighbour - so without this each
    # letter draws a pixel or two left of where it belongs and a joined word
    # comes out smeared together.
    lsb = 0 - pen

    return tuple(columns), width, baseline_row, lsb


def _ink_extent(columns: tuple[int, ...]) -> tuple[int, int]:
    """The ink's extent as (rows above the baseline, rows below it)."""
    above = below = 0
    for column in columns:
        rows = [row for row in range(64) if column & (1 << row)]
        if not rows:
            continue
        above = max(above, max(rows))
        below = max(below, -min(rows) if min(rows) < 0 else 0)
    return above, below


def _check_fits(letter: str, form: str, columns: tuple[int, ...], ascender: int) -> None:
    """
    Refuse to emit a glyph that does not fit its row.

    Silently clipping the top of the hamza alef would look like a font bug on
    screen and be very hard to trace back to here, so an overflow stops the
    build instead. The generator is the only place that knows both the em size
    and the row it has to fit in, which makes it the only place this can be
    checked honestly.
    """
    above, below = _ink_extent(columns)
    depth = above - ascender + below
    if above > BASELINE or depth > ROW_HEIGHT - BASELINE:
        sys.exit(
            f"glyph {letter!r}/{form} does not fit a {ROW_HEIGHT}px row: "
            f"{above}px above the baseline, {depth}px below it "
            f"(em {RENDER_PX}px, room {BASELINE}/{ROW_HEIGHT - BASELINE})"
        )


def build() -> str:
    if not os.path.exists(FONT_PATH):
        sys.exit(f"font not found: {FONT_PATH}")

    blob = hb.Blob.from_file_path(FONT_PATH)
    face = hb.Face(blob)
    hb_font = hb.Font(face)

    table: dict[tuple[str, str], tuple[tuple[int, ...], int, int, int, int]] = {}

    for letter in LETTERS:
        for form in FORMS:
            codepoint = form_codepoint(hb_font, letter, form)
            raster = rasterise(
                FONT_PATH,
                codepoint,
                mark_budget=MARK_BUDGET.get(letter, MAX_MARK_PIXELS),
            )
            if raster is not None:
                columns, width, ascender, lsb = raster
                _check_fits(letter, form, columns, ascender)
                advance = form_advance(face, hb_font, letter, form)
                table[(letter, form)] = (columns, width, ascender, advance, lsb)

    # Digits and the space never join, so one form each.
    for digit in DIGITS:
        raster = rasterise(FONT_PATH, ord(digit))
        if raster is not None:
            columns, width, ascender, lsb = raster
            _check_fits(digit, "isolated", columns, ascender)
            table[(digit, "isolated")] = (columns, width, ascender, width, lsb)

    # The lam-alef ligature, keyed by the two letters that spell it and taking
    # only the isolated and final forms - it cannot join on its left, because
    # the alef is at that end and an alef does not connect.
    for letters, (isolated_cp, final_cp) in LIGATURES.items():
        for form, codepoint in (("isolated", isolated_cp), ("final", final_cp)):
            raster = rasterise(FONT_PATH, ord(codepoint))
            if raster is None:
                continue
            columns, width, ascender, lsb = raster
            probe = letters if form == "isolated" else PARTNER + letters
            shaped = _shape(hb_font, probe)
            advance = 0
            if shaped:
                widest = max(adv for _, adv in shaped)
                advance = max(1, round(widest / face.upem * RENDER_PX))
            table[(letters, form)] = (columns, width, ascender, advance, lsb)

    lines = [
        '"""',
        "Persian glyphs for the graphical display, generated - do not edit by hand.",
        "",
        "Produced by ``tools/gen_persian_font.py`` from",
        f"``{os.path.basename(FONT_PATH)}`` at {RENDER_PX} px.",
        "",
        "Keyed by ``(letter, joining position)`` where the position is one of",
        "``isolated``, ``final``, ``initial`` or ``medial``. The value is",
        "``(columns, width, ascender, advance, lsb)``:", "",
        "* ``columns`` - one int per pixel column, bit 0 at the top of the ink;",
        "* ``width`` - how many of those columns there are;",
        "* ``ascender`` - how many rows above the shared baseline the ink starts;",
        "* ``advance`` - how far to step the pen, which is the font's advance and",
        "  not the ink width, because Arabic letters overlap by design;",
        "* ``lsb`` - how far the ink starts from the pen, since cropping to the",
        "  ink discards the side bearing and a final beh overhangs its neighbour.",
        "",
        "Letters that only join on one side - alef, dal, ra, za, waw and their",
        "hamza variants - have no initial or medial form, and ``text_shaper``",
        "asks for their isolated shape instead.",
        '"""',
        "",
        "from __future__ import annotations",
        "",
        f"ROW_HEIGHT = {ROW_HEIGHT}",
        f"BASELINE = {BASELINE}",
        "",
        "GLYPHS = {",
    ]

    for letter in list(LETTERS) + list(DIGITS) + list(LIGATURES):
        entries = []
        for form in FORMS:
            raster = table.get((letter, form))
            if raster is None:
                continue
            columns, width, ascender, advance, lsb = raster
            entries.append(
                f"        {form!r}: ({columns!r}, {width}, {ascender}, {advance}, {lsb}),"
            )
        if not entries:
            continue
        lines.append(f"    {letter!r}: {{")
        lines.extend(entries)
        lines.append("    },")

    lines.append("}")
    lines.append("")
    lines.append("")
    lines.append("def glyph(letter: str, form: str) -> tuple[tuple[int, ...], int, int, int, int] | None:")
    lines.append('    """The bitmap for one letter in one joining position, or None."""')
    lines.append("    return GLYPHS.get(letter, {}).get(form)")
    lines.append("")

    return "\n".join(lines)


def main() -> None:
    source = build()
    with open(OUT_PATH, "w", encoding="utf-8") as handle:
        handle.write(source)
    print(f"wrote {OUT_PATH}")


if __name__ == "__main__":
    main()