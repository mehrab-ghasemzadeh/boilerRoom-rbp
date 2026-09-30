"""
Compare Naskh against the other Arabic faces for a 1bpp panel of this size.

A one-bit display cannot show weight, only presence, so a face drawn for paper
loses the thin connecting strokes that hold a letter together at eleven pixels.
This renders the letter set from each candidate and counts the two defects that
matter at that size: a dot welded to the letter it belongs to, and a letter that
has broken into pieces.

    python3 tools/compare_fonts.py
"""

from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

import gen_persian_font as gen  # noqa: E402
import preview_word as preview  # noqa: E402

# How many dots each letter carries. Two or three dots are set as one group, so
# they are one component, not two or three.
DOTS = {
    "ب": 1, "پ": 3, "ت": 2, "ث": 3, "ج": 1, "چ": 3, "خ": 1, "ذ": 1, "ژ": 3,
    "ش": 3, "ض": 1, "ظ": 1, "غ": 1, "ف": 1, "ق": 2, "ن": 1, "ی": 2, "ۀ": 2,
    "ة": 2,
}

# Kaf and gaaf carry a small stroke inside the letter, and the alefs carry a
# hamza above: both are genuinely separate pieces and are not defects.
EXPECTED_SPLIT = set("کگآأإؤئ")

CANDIDATES = [
    ("Naskh Regular", "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf"),
    ("Naskh Bold", "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Bold.ttf"),
    ("Sans Regular", "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf"),
    ("Sans Bold", "/usr/share/fonts/truetype/noto/NotoSansArabic-Bold.ttf"),
    ("Kufi Regular", "/usr/share/fonts/truetype/noto/NotoKufiArabic-Regular.ttf"),
]


def components(columns: tuple[int, ...]) -> int:
    """Count 4-connected ink blobs."""
    pixels = {
        (x, y)
        for x, column in enumerate(columns)
        for y in range(64)
        if column & (1 << y)
    }
    seen: set[tuple[int, int]] = set()
    count = 0
    for start in pixels:
        if start in seen:
            continue
        count += 1
        stack = [start]
        seen.add(start)
        while stack:
            x, y = stack.pop()
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nxt = (x + dx, y + dy)
                if nxt in pixels and nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
    return count


def score(path: str, px: int, threshold: int) -> tuple[int, int, int, list[str], list[str]]:
    """(merged, split, widest, merged_letters, split_letters) for one setting."""
    saved = (gen.RENDER_PX, gen.THRESHOLD, gen.FONT_PATH)
    gen.RENDER_PX, gen.THRESHOLD, gen.FONT_PATH = px, threshold, path
    try:
        table = preview.build_table(threshold, px, gen.SUPERSAMPLE)
    finally:
        gen.RENDER_PX, gen.THRESHOLD, gen.FONT_PATH = saved

    merged: list[str] = []
    split: list[str] = []
    widest = 0
    for letter in gen.LETTERS:
        glyph = table.get(letter, {}).get("isolated")
        if not glyph:
            continue
        columns, width, ascender, advance, lsb = glyph
        widest = max(widest, advance)
        count = components(columns)
        want = 2 if letter in DOTS else 1
        if count < want:
            merged.append(letter)
        elif count > want and letter not in EXPECTED_SPLIT:
            split.append(letter)
    return len(merged), len(split), widest, merged, split


def main() -> int:
    print(f"{'face':16s} {'px':>3s} {'th':>4s} {'dot-merged':>11s} {'split':>6s} {'wide':>5s}  letters")
    for name, path in CANDIDATES:
        if not os.path.exists(path):
            print(f"{name:16s} (not installed)")
            continue
        for px in (10, 11):
            for threshold in (50, 60, 70):
                merged, split, widest, ml, sl = score(path, px, threshold)
                print(
                    f"{name:16s} {px:3d} {threshold:4d} {merged:11d} {split:6d} {widest:5d}"
                    f"  {''.join(ml) or '-'}{' / ' + ''.join(sl) if sl else ''}"
                )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
