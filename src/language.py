"""
Which language the panel speaks, and remembering the operator's choice.

The source strings are English throughout, and the Persian panel is produced by
looking them up in the tables in :mod:`screen`. So "English" is the no-op case:
every lookup falls through and the original text is drawn. Keeping the source
language as the identity means a line nobody translated, or a value that came
from the device, still comes out readable rather than blank.

Held in a module global rather than threaded through every drawing call because
the alternative is a parameter on about twenty functions that every one of them
would immediately have to pass along again.
"""

import json
import logging
from pathlib import Path

from json_store import read_json, write_json
from load_env import env_path

_log = logging.getLogger("edge.language")

PERSIAN = "fa"
ENGLISH = "en"

# What the panel shows when nothing has been chosen and nothing has been saved:
# Persian. The panel is built for it and its fonts are generated for it, so the
# safer mistake is a Persian panel the operator can switch away from.
DEFAULT = PERSIAN

LANGUAGES = (PERSIAN, ENGLISH)

LANGUAGE_PATH = env_path("BOILERROOM_LANGUAGE", "language.json")

_current = DEFAULT


def normalise(value: object) -> str | None:
    """The canonical code for ``value``, or None if it is not one we speak."""
    if not isinstance(value, str):
        return None
    code = value.strip().lower()
    if code in LANGUAGES:
        return code
    # A locale like ``fa_IR`` or ``en-GB`` from an operator's hand-typed config.
    # Only the primary subtag matters here; the panel translates into a language,
    # not into a region's spelling of it.
    primary = code.replace("-", "_").split("_", 1)[0]
    return primary if primary in LANGUAGES else None


def get() -> str:
    """The language currently drawn, as one of :data:`LANGUAGES`."""
    return _current


def set_language(code: object) -> str:
    """
    Switch language. Returns the code actually set.

    An unusable value leaves the language alone and is reported, rather than
    silently falling back: a typo in a config file should be visible.
    """
    global _current
    resolved = normalise(code)
    if resolved is None:
        _log.warning("Ignoring unknown language %r", code)
        return _current
    _current = resolved
    return _current


def is_persian() -> bool:
    return _current == PERSIAN


def name(code: str) -> str:
    """
    A language's name *in that language*, for the picker.

    Naming a language in its own script is the convention: an operator who
    cannot read the current language still finds their own, and neither row is
    written in a language the other reader has to have.
    """
    return {"fa": "فارسی", "en": "English"}.get(code, code)


async def load(path: Path | None = None) -> str:
    """
    Apply the saved choice, if there is a usable one.

    A missing or unreadable file is not an error: the panel has a default, and
    refusing to draw because a preferences file is corrupt would leave somebody
    unable to read the temperature of their boiler.
    """
    payload = await read_json(path or LANGUAGE_PATH)
    if payload is None:
        _log.info("No saved language — using %s", DEFAULT)
        return _current
    saved = payload.get("language")
    resolved = normalise(saved)
    if resolved is None:
        _log.warning("Saved language %r is not one we speak — using %s", saved, DEFAULT)
        return _current
    return set_language(resolved)


async def save(code: str | None = None, path: Path | None = None) -> str:
    """Remember the choice so the panel comes up in it next time."""
    resolved = set_language(code)
    await write_json(path or LANGUAGE_PATH, {"language": resolved})
    return resolved
