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

# Rendering this many pixels tall for the em box. The row is 13 px, so the
# glyph box is 12 px of ink plus a spare row of leading, which is what keeps a
# descender off the row below.
RENDER_PX = 10

# Naskh has thin strokes, and at ten pixels they fall between sample points: a
# direct render loses the left arm of the beh and the result is a smudge. So
# the glyph is drawn this many times oversize and averaged back down, which is
# what a display at that size would be doing to the outlines anyway. Eight is
# the smallest factor that keeps every letter's shape intact.
SUPERSAMPLE = 8

# The threshold applied after averaging. Low on purpose: at this size a stroke
# may cover well under half a pixel, and dropping it loses a letter feature
# outright, while a little extra weight on a stem costs nothing at this scale.
THRESHOLD = 60

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


def rasterise(font_path: str, codepoint: int) -> tuple[tuple[int, ...], int, int] | None:
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

    # Average back down to panel scale, then crop to what survived the
    # threshold. Cropping after is deliberate: it bounds the glyph to the ink
    # the panel will actually show, which is what makes the widths correct.
    canvas = canvas.resize(
        (canvas.width // scale, canvas.height // scale), Image.BOX
    )

    pen = PEN_X
    bbox = canvas.point(lambda value: 255 if value > THRESHOLD else 0).getbbox()
    if bbox is None:  # space, or a codepoint this font has no glyph for
        return None

    left, top, right, bottom = bbox
    pixels = canvas.crop(bbox).load()

    columns = []
    for x in range(right - left):
        column = 0
        for y in range(bottom - top):
            if pixels[x, y] > THRESHOLD:
                column |= 1 << y
        columns.append(column)

    # How far the ink starts from the pen, before cropping removed the gap.
    # Arabic glyphs sit asymmetrically about their origin - a final beh is
    # wider than its advance and overhangs its neighbour - so without this each
    # letter draws a pixel or two left of where it belongs and a joined word
    # comes out smeared together.
    lsb = left - pen

    return tuple(columns), right - left, baseline_y // scale - top, lsb


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
            raster = rasterise(FONT_PATH, codepoint)
            if raster is not None:
                columns, width, ascender, lsb = raster
                advance = form_advance(face, hb_font, letter, form)
                table[(letter, form)] = (columns, width, ascender, advance, lsb)

    # Digits and the space never join, so one form each.
    for digit in DIGITS:
        raster = rasterise(FONT_PATH, ord(digit))
        if raster is not None:
            columns, width, ascender, lsb = raster
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