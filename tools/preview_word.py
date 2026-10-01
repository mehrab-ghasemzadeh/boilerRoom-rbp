"""
Preview a Persian word as it would be drawn on the panel.

The generated font is data, so changing it means re-running the generator and
looking at the result. This renders a word straight from a glyph table so a
threshold, a size or a rasteriser change can be judged before it is written
into ``src/display_font_fa.py``.

    python3 tools/preview_word.py خوانشها
    python3 tools/preview_word.py --words تنظیمات بیشتر دمای آب
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

import gen_persian_font as gen  # noqa: E402
from text_shaper import shape  # noqa: E402

ROWS = 14

# Same vertical geometry the panel uses: a 13 px row, baseline 10, 3 below.
_DESCENDER = 3


def render(word: str, table: dict) -> list[str]:
    """Draw ``word`` onto a character grid, the way the panel would."""
    grid = [[" "] * 200 for _ in range(ROWS)]

    # The shaper asks display_font_fa for bitmaps; point it at this table so
    # the preview reflects the table under test rather than the shipped one.
    import display_font_fa

    original = display_font_fa.GLYPHS
    display_font_fa.GLYPHS = table
    try:
        cursor = 0
        for piece in shape(word):
            top = gen.BASELINE - piece.ascender
            for column_index, column in enumerate(piece.columns):
                for row_index in range(piece.ascender + _DESCENDER):
                    if column & (1 << row_index):
                        y, x = top + row_index, cursor + column_index
                        if 0 <= y < ROWS and 0 <= x < 200:
                            grid[y][x] = "#"
            cursor += piece.advance
    finally:
        display_font_fa.GLYPHS = original
    return ["".join(row).rstrip() for row in grid]


def build_table(threshold: int, render_px: int, supersample: int, gamma: float = 1.0) -> dict:
    """Rasterise every glyph with the given settings, without writing a file."""
    import uharfbuzz as hb

    blob = hb.Blob.from_file_path(gen.FONT_PATH)
    face = hb.Face(blob)
    hb_font = hb.Font(face)

    saved = (gen.THRESHOLD, gen.RENDER_PX, gen.SUPERSAMPLE)
    gen.THRESHOLD, gen.RENDER_PX, gen.SUPERSAMPLE = (
        threshold, render_px, supersample
    )
    try:
        table: dict = {}
        for letter in gen.LETTERS:
            for form in gen.FORMS:
                codepoint = gen.form_codepoint(hb_font, letter, form)
                raster = gen.rasterise(gen.FONT_PATH, codepoint)
                if raster is not None:
                    columns, width, ascender, lsb = raster
                    advance = gen.form_advance(face, hb_font, letter, form)
                    table.setdefault(letter, {})[form] = (
                        columns, width, ascender, advance, lsb
                    )
        for digit in gen.DIGITS:
            raster = gen.rasterise(gen.FONT_PATH, ord(digit))
            if raster is not None:
                columns, width, ascender, lsb = raster
                table.setdefault(digit, {})["isolated"] = (
                    columns, width, ascender, width, lsb
                )
        for letters, (iso, fin) in gen.LIGATURES.items():
            for form, codepoint in (("isolated", iso), ("final", fin)):
                raster = gen.rasterise(gen.FONT_PATH, ord(codepoint))
                if raster is None:
                    continue
                columns, width, ascender, lsb = raster
                probe = letters if form == "isolated" else gen.PARTNER + letters
                shaped = gen._shape(hb_font, probe)
                advance = 0
                if shaped:
                    widest = max(adv for _, adv in shaped)
                    advance = max(1, round(widest / face.upem * gen.RENDER_PX))
                table.setdefault(letters, {})[form] = (
                    columns, width, ascender, advance, lsb
                )
    finally:
        gen.THRESHOLD, gen.RENDER_PX, gen.SUPERSAMPLE = saved
    return table


def show(word: str, table: dict, heading: str = "") -> None:
    if heading:
        print(f"\n{heading}")
    lines = render(word, table)
    top = max(i for i, line in enumerate(lines) if line.strip())
    for index, line in enumerate(lines[: top + 1]):
        if line.strip():
            print(f"{index:2d} |{line}")


def main(argv: list[str]) -> int:
    words: list[str] = []
    threshold = gen.THRESHOLD
    render_px = gen.RENDER_PX
    supersample = gen.SUPERSAMPLE
    gamma = 1.0

    index = 0
    while index < len(argv):
        if argv[index] == "--threshold":
            threshold = int(argv[index + 1])
            index += 2
        elif argv[index] == "--px":
            render_px = int(argv[index + 1])
            index += 2
        elif argv[index] == "--gamma":
            gamma = float(argv[index + 1])
            index += 2
        elif argv[index] == "--ss":
            supersample = int(argv[index + 1])
            index += 2
        elif argv[index] == "--words":
            index += 1
            while index < len(argv) and not argv[index].startswith("--"):
                words.append(argv[index])
                index += 1
        else:
            words.append(argv[index])
            index += 1

    if not words:
        words = ["خوانش ها"]

    table = build_table(threshold, render_px, supersample, gamma)
    for word in words:
        show(
            word,
            table,
            f"--- {word}  (threshold={threshold} gamma={gamma} px={render_px} ss={supersample}) ---",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
