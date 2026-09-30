"""Report which generated (letter, form) pairs are actually reached by app text.

Run: python3 tools/which_glyphs.py
"""

import re
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import display_font_fa as dff
import text_shaper

REAL = dff.glyph
USED = set()

SEEN_TEXT = set()


def traced(letter, form):
    USED.add((letter, form))
    return REAL(letter, form)


def collect_text():
    """Best-effort: every Persian string literal in the app source."""
    for path in (ROOT / "src").glob("*.py"):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if '"' in line or "'" in line:
                SEEN_TEXT.add(line)
    return SEEN_TEXT


def main():
    dff.glyph = traced
    text_shaper._glyph = traced

    # Shape maximal runs of letters/digits/spaces, so joining actually happens.
    run_re = re.compile(r"[\u0600-\u06FF\u200c]+")
    runs = set()
    for line in collect_text():
        for run in run_re.findall(line):
            runs.add(run)

    for run in runs:
        try:
            text_shaper.shape(run)
        except Exception:
            pass

    generated = {
        (letter, form)
        for letter, forms in dff.GLYPHS.items()
        for form in forms
    }

    hit = sorted(generated & USED)
    miss = sorted(generated - USED)

    print("reached: %d / %d" % (len(hit), len(generated)))
    print("\nSPLIT GLYPHS THAT ARE REACHED")
    split = {("ج", "final"), ("ح", "final"), ("خ", "final"),
             ("ق", "isolated"), ("ق", "final")}
    for item in sorted(split & set(hit)):
        print("  %s %s   <-- reached" % item)
    reached_split = split & set(hit)
    if not reached_split:
        print("  (none)")

    print("\nREACHED (%d)" % len(hit))
    for letter, form in hit:
        print("  %s %s" % (letter, form))
    print("\nNOT REACHED (per-glyph scan judged, never used by app text)")
    for letter, form in miss:
        print("  %s %s" % (letter, form))


if __name__ == "__main__":
    main()
