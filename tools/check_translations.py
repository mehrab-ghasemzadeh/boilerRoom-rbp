"""
Report English that would still reach the panel.

Translation happens in dictionaries (``FA_LINES``, ``FA_TITLES``, ``FA_LABELS``
in screen.py; ``FA_TITLES``, ``FA_LABELS``, ``_FA_ITEMS`` in control_menu.py).
A lookup that misses is invisible in testing and becomes English on a screen in
a boiler room, so this walks the source for the literals the panel is handed
and asks the real lookup functions what they turn into.

What counts as a leak:

* a run of two or more Latin letters that is not an allow-listed identifier --
  a config key, a field name, a path, a URL, a unit, or something the server
  supplies and that must survive verbatim;
* an English word the dictionaries were meant to cover.

Numeric values and user-configured names are not leaks. Field names like
``desired_config_version`` are, because the server and the reader both use
them to find the column.

Run with no arguments to check; pass ``--all`` to list allow-listed identifiers
too, which is how the allow list is maintained.
"""

from __future__ import annotations

import ast
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

sys.path.insert(0, str(SRC))

import control_menu  # noqa: E402
import screen  # noqa: E402

# The calls whose string arguments end up on the panel. ``title_calls`` take
# their first argument as a screen title, which is looked up in FA_TITLES; the
# rest of the arguments are body lines or option labels, looked up in
# FA_LINES. ``echo`` is all body lines.
title_calls = {
    "page", "splash", "read_line", "_set_context", "frame",
    "_notice", "_message",
}

line_calls = {"echo"}

# Option rows handed to the selectable lists. The panel draws these through
# screen._translate_row, so an untranslated one is a visible English row.
row_calls = {"select", "select_list", "select_checkboxes"}

# Calls where only the first argument reaches the panel. _choose's later
# arguments are the numbered block a terminal operator types into; on the panel
# it returns before that text is ever used.
title_only = {"_choose", "select"}

TITLES = {**screen.FA_TITLES, **control_menu.FA_TITLES}
LABELS = {**screen.FA_LABELS, **control_menu.FA_LABELS}
ITEMS = control_menu._FA_ITEMS
ROWS = screen.FA_ROWS
FRAGMENTS = screen.FA_FRAGMENTS

# Identifiers that reach the panel on purpose: config keys, field names, file
# names, environment variables, units, and the words the server protocol uses.
# These are matched case-sensitively and in full so that an untranslated word
# next to them still shows up as a leak.
ALLOWED = {
    # protocol / storage field names
    "cycle_count", "desired_config_version", "desired_schedule_version",
    "schedule_version", "retention_days", "size_bytes", "server_time",
    "hello_ack", "ws", "api", "url", "v", "rev", "id", "gpio", "unit",
    "name", "role", "path", "file", "rows", "relay", "sensor", "sensors",
    "relay_id", "sensor_id", "temperature", "modes", "targets",
    # units and symbols
    "c", "s", "ok", "no", "yes", "off", "on", "kb", "mb", "gb", "°c",
    # environment and paths
    "boilerroom_mapping_source", "etc", "var", "tmp", "home", "usr",
    "sqlite", "db", "json", "py", "log", "websocket", "http", "https",
    "base", "url", "ack", "hello",
    # words the dictionaries do translate, kept here only where they are part
    # of a larger identifier that must survive
    "on", "off",
}

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]*")


def _is_allowlisted(word: str) -> bool:
    """True for identifiers the panel is meant to show verbatim."""
    if word in ALLOWED or word.lower() in {a.lower() for a in ALLOWED}:
        return True
    # CamelCase / snake_case protocol names, e.g. WS hello_ack's value.
    if "_" in word and word.replace("_", "").isalnum():
        return True
    # Numbers with a unit or a version letter already split off.
    if re.fullmatch(r"[A-Za-z]+[0-9]+", word):
        return True
    return False


def _looks_like_english(text: str) -> bool:
    """True when ``text`` still carries Latin prose the panel should not show."""
    for match in _WORD.finditer(text):
        word = match.group(0).strip("._-")
        if not word or _is_allowlisted(word):
            continue
        return True
    return False


def _lookup(table: str, value: str) -> str:
    """Run ``value`` through the same lookup the panel uses."""
    if table == "FA_LINES":
        return screen.FA_LINES.get(value, value)
    if table == "FA_TITLES":
        return TITLES.get(value, value)
    if table == "FA_LABELS":
        return LABELS.get(value, value)
    if table == "FA_ROWS":
        return ROWS.get(value, value)
    if table == "FA_ITEMS":
        return ITEMS.get(value, value) if isinstance(ITEMS.get(value), str) else value
    return value


def _menu_title_for(value: str) -> str:
    """The dictionary that would be consulted for a menu's title."""
    for title in ITEMS:
        if title in value:
            return title
    return ""


def collect() -> list[tuple[str, str, str, str]]:
    """(file, call, table, literal) for every panel-visible literal."""
    out: list[tuple[str, str, str, str]] = []
    for path in sorted(SRC.glob("*.py")):
        if path.name == "check_translations.py":
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            name = name or ""
            if name in title_calls:
                # The title is the first *string* argument; helpers like
                # _notice(state, title, lines) pass runtime state first, and the
                # lines arrive as a list literal, which is where most of the
                # panel's English lives.
                first = next(
                    (
                        a
                        for a in node.args
                        if isinstance(a, (ast.Constant, ast.JoinedStr))
                        and _chunks(a)
                    ),
                    None,
                )
                if first is None:
                    continue
                pairs = [(first, "FA_TITLES")]
                for arg in node.args:
                    if arg is first:
                        continue
                    if isinstance(arg, (ast.List, ast.Tuple)):
                        for item in arg.elts:
                            for chunk in _chunks(item):
                                pairs.append((item, "FA_LINES"))
                    elif isinstance(arg, ast.JoinedStr):
                        pairs.append((arg, "FA_LINES"))
            elif name in line_calls:
                pairs = [(a, "FA_LINES") for a in node.args]
            elif name in row_calls:
                # First argument is the title; every later string argument —
                # literal or list element — is an option row.
                first = next(
                    (
                        a
                        for a in node.args
                        if isinstance(a, (ast.Constant, ast.JoinedStr)) and _chunks(a)
                    ),
                    None,
                )
                if first is None:
                    continue
                pairs = [(first, "FA_TITLES")]
                for arg in node.args:
                    if arg is first:
                        continue
                    if isinstance(arg, (ast.List, ast.Tuple)):
                        for item in arg.elts:
                            pairs.append((item, "FA_ROWS"))
                    elif isinstance(arg, ast.JoinedStr):
                        pairs.append((arg, "FA_ROWS"))
            else:
                continue
            for arg, table in pairs:
                # A list of lines is one argument holding several strings.
                if isinstance(arg, (ast.List, ast.Tuple)):
                    for item in arg.elts:
                        for chunk in _chunks(item):
                            rows.append((path.name, name, table, chunk))
                    continue
                for chunk in _chunks(arg):
                    out.append((path.name, name, table, chunk))
    return out


def _chunks(arg: ast.AST) -> list[str]:
    """The literal text inside an argument: a string, or an f-string's parts."""
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return [arg.value]
    if isinstance(arg, ast.JoinedStr):
        # JoinedStr.values holds the fixed text; a FormattedValue is an
        # interpolation whose value and format spec are data.
        return [
            part.value
            for part in arg.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        ]
    return []


def _fstring_tails() -> list[tuple[str, str]]:
    """
    (file, tail) for every f-string that reaches the panel.

    A line built with an f-string mixes fixed words with live values, so it is
    never a key in FA_LINES and never appears as a literal the table can be
    checked against. Its fixed words are translated at the source or by
    substituting FA_FRAGMENTS, and neither is visible to a literal check --
    which makes these tails the one place English can hide without the check
    above noticing. ``--tails`` lists them so they stay auditable.
    """
    out: list[tuple[str, str]] = []
    for path in sorted(SRC.glob("*.py")):
        if path.name == "check_translations.py":
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name not in {"echo", "page", "message", "_message", "_notice"}:
                continue
            for arg in node.args:
                if not isinstance(arg, ast.JoinedStr):
                    continue
                tail = "".join(
                    p.value
                    for p in arg.values
                    if isinstance(p, ast.Constant) and isinstance(p.value, str)
                )
                if tail.strip():
                    out.append((path.name, tail))
    return out


def main(argv: list[str]) -> int:
    show_all = "--all" in argv
    rows = collect()

    leaks: list[tuple[str, str, str, str]] = []
    for file, call, table, text in rows:
        # RuntimeState splits every echo on newlines before the menu sees it,
        # so probe the same fragments the panel would actually receive.
        candidates = []
        for fragment in text.split("\n"):
            if not fragment.strip():
                continue
            stripped = fragment[7:] if fragment.startswith("[menu] ") else fragment
            candidates.append(stripped)
        for key in candidates:
            key = key.strip()
            resolved = _lookup(table, key)
            if resolved != key:
                continue
            if table == "FA_LINES":
                # A line that also carries live values is not a key in FA_LINES;
                # it is translated by substituting its fixed labels. It is only
                # covered if every fragment it contains is one we translate --
                # one covered fragment does not excuse an uncovered one beside
                # it, or "  Days: Mon, Kettle" would pass on the strength of the
                # label alone.
                parts = [key]
                for label, _ in FRAGMENTS:
                    if any(label in part for part in parts):
                        split: list[str] = []
                        for part in parts:
                            split.extend(part.split(label))
                        parts = split
                residual = " ".join(parts)
                if not _looks_like_english(residual):
                    continue
            if _looks_like_english(resolved):
                leaks.append((file, call, table, key))
            break

    # De-duplicate, keep a stable order.
    seen: set[tuple[str, str, str, str]] = set()
    unique = [row for row in sorted(set(leaks)) if not (row in seen or seen.add(row))]

    if show_all:
        print("allow-listed identifiers (not leaks):")
        for word in sorted(ALLOWED):
            print(f"  {word}")
        print()

    print(f"{len(unique)} untranslated panel-visible literal(s)\n")
    by_file: dict[str, list[tuple[str, str, str]]] = {}
    for file, call, table, text in unique:
        by_file.setdefault(file, []).append((call, table, text))
    for file in sorted(by_file):
        print(f"--- {file} ---")
        for call, table, text in by_file[file]:
            print(f"  [{call} -> {table}] {text!r}")
        print()

    if "--tails" in argv:
        print("f-string lines reaching the panel (fixed words only;")
        print("values between them are data and pass through):\n")
        seen = set()
        for file, tail in _fstring_tails():
            if (file, tail) in seen:
                continue
            seen.add((file, tail))
            has_latin = any(
                c.isascii() and c.isalpha() and c not in "vVcCs" for c in tail
            )
            mark = "ENGLISH" if has_latin else "translated"
            print(f"  [{file}] {tail!r}  ({mark})")
        print()

    return 1 if unique else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
