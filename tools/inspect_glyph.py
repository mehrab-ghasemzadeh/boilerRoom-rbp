"""Component analysis for specific (letter, form) pairs.

Run: python3 tools/inspect_glyph.py ج final خ final ق isolated ...
"""

import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import display_font_fa as dff


def components(columns):
    """8-connected components over a column bitmap. Returns sizes, big first."""
    h = dff.ROW_HEIGHT
    seen = set()
    sizes = []
    for c0 in range(len(columns)):
        for r0 in range(h):
            if (c0, r0) in seen:
                continue
            if not (columns[c0] >> r0) & 1:
                continue
            stack = [(c0, r0)]
            seen.add((c0, r0))
            n = 0
            while stack:
                c, r = stack.pop()
                n += 1
                for dc in (-1, 0, 1):
                    for dr in (-1, 0, 1):
                        if dc == 0 and dr == 0:
                            continue
                        cc, rr = c + dc, r + dr
                        if 0 <= cc < len(columns) and 0 <= rr < h:
                            if (cc, rr) not in seen and (columns[cc] >> rr) & 1:
                                seen.add((cc, rr))
                                stack.append((cc, rr))
            sizes.append(n)
    sizes.sort(reverse=True)
    return sizes


def show(letter, form):
    g = dff.glyph(letter, form)
    if g is None:
        print("%s %s: MISSING" % (letter, form))
        return
    columns, width, ascender, advance, lsb = g
    print("=== %s / %s ===" % (letter, form))
    print("columns=%s width=%d ascender=%d advance=%d lsb=%d" % (columns, width, ascender, advance, lsb))
    sizes = components(columns)
    print("components=%s  (body=%s, marks=%s)" % (
        sizes, sizes[0] if sizes else 0, sizes[1:]))
    if len(sizes) > 1:
        biggest_mark = sizes[1]
        verdict = "OK (mark <= 6)" if biggest_mark <= 6 else "SUSPECT (mark > 6)"
        print("verdict: %s" % verdict)
    for r in range(dff.ROW_HEIGHT - 1, -1, -1):
        print("   " + "".join("#" if (columns[c] >> r) & 1 else "." for c in range(width)))
    print()


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        args = ["ج", "final", "ح", "final", "خ", "final",
                "ق", "isolated", "ق", "final", "ع", "isolated"]
    pairs = list(zip(args[::2], args[1::2]))
    for letter, form in pairs:
        show(letter, form)
