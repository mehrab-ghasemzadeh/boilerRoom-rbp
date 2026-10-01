"""
Interactive control menu — runs concurrently with the sensor loop.

Blocking terminal input runs in a worker thread via asyncio.to_thread so the
event loop keeps polling sensors and posting telemetry.

Answers come from an input device rather than from ``input()`` directly: the
keyboard on the bench, the GPIO matrix keypad on the installed hardware, chosen
by USE_MOCK_HARDWARE the same way the relays and sensor readers are. Both hand
back a whole line, so there is one menu rather than a keyboard menu and a
keypad menu drifting apart.

Every prompt therefore has to be answerable from sixteen keys. The menus were
already numeric; what needed work was the free text around them, and the
parsers now take a digit form of each — days as 1-7, a time as HHMM, a date as
YYYYMMDD — alongside the words. See ``keypad_layout`` for the key table.

The graphical display
---------------------
Where one is fitted, the same menu is shown on it instead: one highlighted row
at a time, moved with ``2`` and ``8``, opened with ``#`` and left with ``*``,
with a legend along the bottom saying so. The functions below did not have to
change for it. Two things carry them across:

  * the option tables are the menu, and both faces are built from them — the
    numbered text for a terminal, the selectable list for the panel, so the two
    cannot drift apart
  * everything written with ``state.echo`` is captured rather than printed, and
    shown a screenful at a time the moment something asks for input

Which means a screen with six rows and a screen with fifty show the same
things, and there is one implementation of what those things are.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import sys
from pathlib import Path

from auth import (
    API_BASE_URL,
    WS_BASE_URL,
    CredentialError,
    credentials_present,
    device_username,
    set_credentials,
    token_manager,
)
from config import GAS_SENSORS, RELAYS, TEMPERATURE_SENSORS, UNITS
from config_editor import (
    LIMIT_FIELDS,
    ConfigEditError,
    blank_document as blank_config_document,
    describe_limits,
    parse_temperature_text,
    set_limit,
)
from data_logger import reading_store
from setpoint_store import (
    MAX_SETPOINT_C,
    MIN_SETPOINT_C,
    SetpointError,
    validate as validate_setpoint,
)
from device_config import ConfigError, config_store, describe as describe_config
from display_font import DEGREE
from display_canvas import text_width, truncate, wrap
from keypad_layout import CANCEL, ENTER, NEXT, cap_for
from logging_setup import get_logger
import language
from mapping_provider import DEFAULT_MAPPING_PATH
from limits_guard import limit_guard
from runtime_state import RuntimeState
from screen import BODY_COLUMNS, SCROLL_KEYS, Screen
from schedule_editor import (
    ScheduleEditError,
    add_exception,
    add_weekly_rule,
    blank_document,
    parse_date_text,
    parse_days,
    parse_state_text,
    parse_time_text,
    remove_exception,
    remove_weekly_rule,
)
from schedule_runner import (
    TARGET_ROLE,
    ScheduleError,
    Target,
    WEEKDAYS,
    relay_for_target,
    schedule_runner,
)

# relay role -> schedule target type, the reverse of schedule_runner's map
ROLE_TARGET = {role: kind for kind, role in TARGET_ROLE.items()}

_log = get_logger("menu")

# Day index -> name for display
REVERSE_DAYS = {index: name for name, index in WEEKDAYS.items()}

# Weekday names as the panel shows them. The schedule stores full English names
# and the rows are built from the first three of them, which is "mon" rather
# than anything an operator can read on a Persian panel, so the abbreviation is
# translated here rather than left to a string substitution further downstream
# that cannot see inside an f-string.
FA_WEEKDAYS = {
    "mon": "دوشنبه",
    "tue": "سه‌شنبه",
    "wed": "چهارشنبه",
    "thu": "پنج‌شنبه",
    "fri": "جمعه",
    "sat": "شنبه",
    "sun": "یکشنبه",
}


def _weekday(name: str) -> str:
    """The Persian weekday name for a stored English one."""
    return FA_WEEKDAYS.get(name[:3].lower(), name[:3])


def _weekdays(names) -> str:
    """A comma-separated run of Persian weekday names."""
    return ", ".join(_weekday(str(n)) for n in names)


def _t(english: str, persian: str) -> str:
    """
    Whichever of the two the panel is currently speaking.

    For prose that is built at runtime and so is not a key in any table — a
    status line naming a target, a word inside an f-string. The English is the
    source and the Persian is written beside it, so the two cannot drift apart
    the way a lookup table and its callers do.
    """
    return persian if language.is_persian() else english


def _on_off(state: bool) -> str:
    """The Persian word for a relay or rule state."""
    return _t("on", "روشن") if state else _t("off", "خاموش")

MENU = """
--- Control Menu ---
  1) Last sensor readings
  2) Relay status / control
  3) Set unit mode (automatic/manual)
  4) Change temperatures
  5) Change schedule
  6) Show active schedule
  7) Show app configuration
  8) Show device mapping
  9) Show status
 10) Change language
  0) Quit
> """

TEMPERATURE_MENU = """
  1) Set a boiler's temperature
  2) Clear a boiler's temperature (use the device-wide limit)
  3) Device-wide safety limits
  0) Back
> """

LIMITS_MENU = """
  1) Max water temperature
  2) Min water temperature
  3) Max ambient temperature
  4) Discard local edits (back to the published config)
  0) Back
> """

# Each language named in its own script, so the row is findable by an operator
# who cannot read the one currently on the panel. Which row is *current* is
# shown by the panel's own selection, not by a tick, for the same reason.
LANGUAGE_MENU = """
  1) English
  2) فارسی
  0) Back
> """

# The same options as the blocks above, as (answer, short label). The terminal
# reads the block; the display builds a selectable list from these. Labels are
# written to fit twenty columns, which is what the panel has.
MAIN_ITEMS = (
    ("1", "Sensor readings"),
    ("2", "Relay control"),
    ("3", "Unit modes"),
    ("4", "Temperatures"),
    ("5", "Change schedule"),
    ("6", "Active schedule"),
    ("7", "App configuration"),
    ("8", "Device mapping"),
    ("9", "Status"),
    ("10", "Change language"),
    ("0", "Quit"),
)

# Identical to MAIN_ITEMS on purpose: a language's name is written in that
# language, so translating the rows would only ever change one of the two.
LANGUAGE_ITEMS = (
    ("1", language.name(language.ENGLISH)),
    ("2", language.name(language.PERSIAN)),
    ("0", "Back"),
)

TEMPERATURE_ITEMS = (
    ("1", "Set boiler temp"),
    ("2", "Clear boiler temp"),
    ("3", "Safety limits"),
    ("0", "Back"),
)

LIMITS_ITEMS = (
    ("1", "Max water temp"),
    ("2", "Min water temp"),
    ("3", "Max ambient temp"),
    ("4", "Discard local edits"),
    ("0", "Back"),
)

SCHEDULE_V2_ITEMS = (
    ("1", "Add weekly rule"),
    ("2", "Remove weekly rule"),
    ("3", "Add date exception"),
    ("4", "Remove exception"),
    ("0", "Back"),
)

# Returned when the operator pressed the back key rather than choosing
# anything. Distinct from "0" because on the main menu "0" is Quit, and a back
# key that stops the heating agent is not a back key.
BACK = "\x00back"


# Persian for the panel.
#
# The menu above stays in English on purpose: it is also the terminal menu, and
# the terminal is where you debug. What the panel draws goes through this table
# instead, so the two can differ without either being wrong.
#
# Labels are short because the panel is 122 px wide at 13 px a row and there are
# three rows of it. A word that does not fit is truncated with a "~", and a
# label whose end is cut off is a label nobody can act on — so these are written
# to fit rather than translated word for word.
FA_MAIN_ITEMS = (
    ("1", "خوانش‌ها"),
    ("2", "راه‌اندازی رله"),
    ("3", "حالت‌ها"),
    ("4", "دماها"),
    ("5", "تغییر زمان‌بندی"),
    ("6", "زمان‌بندی فعال"),
    ("7", "پیکربندی"),
    ("8", "نگاشت دستگاه"),
    ("9", "وضعیت"),
    ("10", "زبان"),
    ("0", "خروج"),
)

FA_TEMPERATURE_ITEMS = (
    ("1", "تنظیم دمای دیگ"),
    ("2", "حذف دمای دیگ"),
    ("3", "حدود ایمنی"),
    ("0", "بازگشت"),
)

FA_LIMITS_ITEMS = (
    ("1", "بیشینه دمای آب"),
    ("2", "کمینه دمای آب"),
    ("3", "بیشینه دمای محیط"),
    ("4", "حذف ویرایش‌ها"),
    ("0", "بازگشت"),
)

FA_SCHEDULE_V2_ITEMS = (
    ("1", "افزودن قانون هفتگی"),
    ("2", "حذف قانون هفتگی"),
    ("3", "افزودن استثنا"),
    ("4", "حذف استثنا"),
    ("0", "بازگشت"),
)

# Screen titles. Keyed by the English title passed to _choose(), so a handler
# does not have to know which language it is being drawn in.
FA_TITLES = {
    "Menu": "منو",
    "Language": "زبان",
    "Schedule": "زمان‌بندی",
    "Temperatures": "دماها",
    "Safety limits": "حدود ایمنی",
}

FA_LABELS = {
    "OK": "تأیید",
    "Back": "بازگشت",
}


def _fa_label(label: str) -> str:
    """The Persian form of an English label, or the label itself if untranslated."""
    return FA_LABELS.get(label, label)


# Which Persian item list goes with which English menu title.
_FA_ITEMS = {
    "Schedule": FA_SCHEDULE_V2_ITEMS,
    "Temperatures": FA_TEMPERATURE_ITEMS,
    "Safety limits": FA_LIMITS_ITEMS,
    "Menu": FA_MAIN_ITEMS,
    "Language": LANGUAGE_ITEMS,
}


# The device answers are read from. A module-level handle, like the schedule
# and config stores, so the twenty-odd call sites of _prompt keep their
# signature instead of threading it through every menu function.
_input_device = None

# The panel those answers are shown on, or None where there is no display and
# the menu is a terminal one. Held here for the same reason.
_screen: Screen | None = None

# The state, for the handful of helpers below that need it and are called from
# places whose signature is fixed by twenty existing call sites.
_state: RuntimeState | None = None

# What the screen currently being drawn is about, used to title the pages that
# menu output is shown on. Set from the option label as each one is opened.
_context = "Menu"


def set_input_device(device) -> None:
    global _input_device
    _input_device = device


def input_device():
    """The active input device, defaulting to the keyboard."""
    if _input_device is None:
        from mock_keypad import MockKeypad

        set_input_device(MockKeypad())
    return _input_device


def set_screen(screen: Screen | None) -> None:
    global _screen
    _screen = screen


def screen() -> Screen | None:
    """The display's screens, or None when the menu is a terminal one."""
    return _screen


def _set_context(title: str) -> None:
    global _context
    _context = title


def _label_for(items: tuple[tuple[str, str], ...], answer: str, default: str) -> str:
    for value, label in items:
        if value == answer:
            return label
    return default


def _strip_tag(line: str) -> str:
    """Drop the ``[menu]`` prefix — on twenty columns it is four wasted words."""
    return line[7:] if line.startswith("[menu] ") else line


async def _flush_page(state: RuntimeState) -> None:
    """
    Show whatever the menu has written since the last screen, then clear it.

    Called before anything asks for input, which is what turns output written
    for a scrolling terminal into pages on a panel that does not scroll:
    everything a function echoed is on screen, in order, and paged through at
    the operator's speed rather than gone by the time they look up.
    """
    view = screen()
    if view is None:
        return

    lines = [_strip_tag(line) for line in state.take_echo()]

    # A page waits for a keypress. On the way out there is nobody to press one,
    # and the buffer would hold the agent open on a screen saying it is closing.
    if state.shutdown.is_set() or not any(line.strip() for line in lines):
        return

    await view.page(_context, lines)


async def _prompt(text: str, *, mask: bool = False) -> str:
    """
    Ask a question and read the answer.

    ``mask`` hides what is typed, which only the device password wants. It has
    no effect on a terminal, where the keyboard's own echo is doing the showing
    and there is no panel to hide it on.
    """
    view = screen()
    if view is None:
        return (await input_device().read_line(text)).strip()

    if _state is not None:
        await _flush_page(_state)
    return (await view.read_line(text, title=_context, mask=mask)).strip()


async def _notice(state: RuntimeState, title: str, lines: list[str]) -> None:
    """
    Put something on the panel and carry straight on.

    For the screens nobody should have to acknowledge — "checking with the
    server" while a login runs. ``_flush_page`` would stop and wait for a key,
    which is right for output an operator asked for and wrong for a thing that
    is about to replace itself.
    """
    view = screen()
    if view is None:
        for line in lines:
            await state.echo(line)
        return

    _set_context(title)
    state.take_echo()  # these are on the panel now, not queued behind it
    await view.splash(title, lines)


async def _message(state: RuntimeState, title: str, lines: list[str]) -> None:
    """Show something the operator has to read, and wait for them to move on."""
    view = screen()
    if view is None:
        for line in lines:
            await state.echo(line)
        return

    _set_context(title)
    state.take_echo()
    await view.page(title, lines)


async def _choose(
    state: RuntimeState,
    title: str,
    items: tuple[tuple[str, str], ...],
    text: str,
    *,
    hide_back: bool = True,
    legend: tuple[tuple[str, str], ...] | None = None,
) -> str:
    """
    Ask which option. Returns the answer the handlers already expect.

    On a terminal this is the numbered block and a typed number, unchanged. On
    the panel it is a selectable list — with the "Back" row left out, because
    the back key is right there on the keypad and a list that spends one of its
    three rows saying so is a list with two rows.
    """
    view = screen()
    if view is None:
        return await _prompt(text)

    await _flush_page(state)

    # The panel draws in Persian. The answers are unchanged — they are still
    # the digits the handlers compare against — so only the words differ.
    fa_items = _FA_ITEMS.get(title, items) if language.is_persian() else items

    shown = [item for item in fa_items if not (hide_back and item[0] == "0")]
    index = _last_choice.get(title, 0)
    chosen = await view.select(
        FA_TITLES.get(title, title) if language.is_persian() else title,
        [label for _, label in shown],
        index=min(index, len(shown) - 1),
        legend=legend,
    )
    if chosen is None:
        return BACK

    _last_choice[title] = chosen
    return shown[chosen][0]


# Where each menu was left, so coming back from a submenu lands on the row it
# was opened from rather than at the top.
_last_choice: dict[str, int] = {}


def _is_yes(answer: str) -> bool:
    """
    Whether a yes/no prompt was answered yes.

    ``1`` and ``0`` are here because the keypad has no letters — the same
    reason parse_state_text has taken them since it was written.
    """
    return answer.strip().lower() in ("y", "yes", "1", "on")


def _is_no(answer: str) -> bool:
    return answer.strip().lower() in ("n", "no", "0", "off")


async def _show_last_readings(state: RuntimeState) -> None:
    snap = await state.get_snapshot()
    read_at = snap["read_at"]
    if read_at is None:
        await state.echo("\n[menu] No readings yet.\n")
        return

    await state.echo(f"\n[menu] Last readings at {read_at.isoformat()} (cycle {snap['cycle_count']})")
    for sensor_id, value in sorted(snap["temperatures"].items()):
        cfg = TEMPERATURE_SENSORS.get(sensor_id, {})
        label = cfg.get("name", f"Sensor {sensor_id}")
        if value is None:
            await state.echo(f"  [{sensor_id}] {label}: unavailable")
        else:
            await state.echo(f"  [{sensor_id}] {label}: {value:.2f} °C")

    for sensor_id, value in sorted(snap["gas"].items()):
        cfg = GAS_SENSORS.get(sensor_id, {})
        label = cfg.get("name", f"Sensor {sensor_id}")
        await state.echo(f"  [{sensor_id}] {label}: {value}")
    await state.echo("")


async def _show_mapping(state: RuntimeState) -> None:
    await state.echo("\n[menu] Equipment units:")
    for unit_id, unit in UNITS.items():
        await state.echo(f"  {unit_id}: {unit['name']}")

    await state.echo("\n[menu] Temperature sensors:")
    for sid, cfg in sorted(TEMPERATURE_SENSORS.items()):
        unit = cfg.get("unit") or "—"
        await state.echo(
            f"  Sensor {sid}: {cfg['name']}  (role={cfg['role']}, unit={unit})"
        )

    await state.echo("\n[menu] Relays:")
    for rid, cfg in sorted(RELAYS.items()):
        unit = cfg.get("unit") or "—"
        await state.echo(
            f"  Relay {rid}: {cfg['name']}  "
            f"(role={cfg['role']}, unit={unit}, GPIO {cfg['gpio']})"
        )
    await state.echo("")


async def _relay_menu(state: RuntimeState) -> None:
    rc = state.relay_controller
    if rc is None:
        await state.echo("\n[menu] Relay controller not available.\n")
        return

    view = screen()
    if view is None:
        # Terminal fallback: keep the old number-based interaction
        await _relay_menu_terminal(state)
        return

    # Build relay table rows
    blocked_relays = {
        relay_for_target(target): reason
        for target, reason in (await state.get_limit_blocks()).items()
        if relay_for_target(target) is not None
    }

    relay_ids = sorted(RELAYS.keys())
    if not relay_ids:
        await view.message("Relay control", ["No relays configured."])
        return

    def build_rows() -> list[str]:
        rows = []
        for rid in relay_ids:
            cfg = RELAYS[rid]
            on = rc.get_state(rid)
            state_str = "ON " if on else "OFF"
            cut = f"  [CUT: {blocked_relays[rid]}]" if rid in blocked_relays else ""
            rows.append(f"{cfg['name']:<16} {state_str}{cut}")
        return rows

    # Legend for the relay table
    legend = (
        SCROLL_KEYS,
        (cap_for(ENTER), "Toggle"),
        (cap_for(NEXT), "Toggle"),
        (cap_for(CANCEL), "Back"),
    )

    index = 0
    while True:
        rows = build_rows()
        chosen = await view.select(
            "Relay control",
            rows,
            index=index,
            legend=legend,
        )
        if chosen is None:
            return

        index = chosen
        rid = relay_ids[index]

        # Check if blocked by temperature limit
        if rid in blocked_relays and not rc.get_state(rid):
            await view.message(
                "Blocked",
                [
                    f"Relay {rid} ({RELAYS[rid]['name']}) is cut off",
                    f"by a temperature limit:",
                    f"  {blocked_relays[rid]}",
                    "",
                    "Refusing to switch it on.",
                ],
            )
            continue

        # Toggle the relay
        await rc.toggle(rid)
        on = rc.get_state(rid)
        await state.log(f"[menu] Relay {rid} switched {'on' if on else 'off'} by operator")
        await state.notify_state_change(
            f"relay {rid} {'on' if on else 'off'} (operator)"
        )
        # Loop continues with updated state


async def _relay_menu_terminal(state: RuntimeState) -> None:
    """Terminal fallback for the old number-based relay control."""
    rc = state.relay_controller
    if rc is None:
        await state.echo("\n[menu] Relay controller not available.\n")
        return

    blocked_relays = {
        relay_for_target(target): reason
        for target, reason in (await state.get_limit_blocks()).items()
        if relay_for_target(target) is not None
    }

    await state.echo("\n[menu] Relay states:")
    for rid, cfg in sorted(RELAYS.items()):
        on = rc.get_state(rid)
        note = (
            f"  [{_t('CUT', 'قطع')}: {blocked_relays[rid]}]"
            if rid in blocked_relays
            else ""
        )
        await state.echo(
            f"  {_t('Relay', 'رله')} {rid}: {cfg['name']} — {_on_off(on)}{note}"
        )

    raw = await _prompt(
        "Enter relay ID to toggle (empty = back): "
    )
    if not raw:
        await state.echo("")
        return
    try:
        relay_id = int(raw)
    except ValueError:
        await state.echo("[menu] Invalid relay ID.\n")
        return

    if relay_id in blocked_relays and not rc.get_state(relay_id):
        await state.echo(
            f"[menu] Relay {relay_id} is cut off by a temperature limit "
            f"({blocked_relays[relay_id]}) — refusing to switch it on.\n"
        )
        await state.log(
            f"[menu] Refused to switch relay {relay_id} on: "
            f"{blocked_relays[relay_id]}",
            level=logging.WARNING,
        )
        return

    await rc.toggle(relay_id)
    on = rc.get_state(relay_id)
    await state.echo(
        f"[menu] {_t('Relay', 'رله')} {relay_id} "
        f"{_t('is now', 'اکنون')} {_on_off(on)}.\n"
    )
    await state.log(f"[menu] Relay {relay_id} switched {'on' if on else 'off'} by operator")
    await state.notify_state_change(
        f"relay {relay_id} {'on' if on else 'off'} (operator)"
    )


async def _show_app_config(state: RuntimeState) -> None:
    interval = await state.get_read_interval()
    telemetry_interval = await state.get_telemetry_interval()
    mapping_path = Path(os.environ.get("BOILERROOM_MAPPING", DEFAULT_MAPPING_PATH))
    await state.echo("\n[menu] App configuration:")
    await state.echo(f"  API base URL:     {API_BASE_URL}")
    await state.echo(f"  WebSocket URL:    {WS_BASE_URL}")
    session = token_manager.session
    await state.echo(
        f"  {_t('Device username', 'نام کاربری دستگاه')}:  "
        f"{device_username() or _t('not set', 'تعیین نشده')}"
    )
    await state.echo(
        f"  {_t('Device id', 'شناسه دستگاه')}:        "
        f"{session.device_id if session else _t('not signed in', 'وارد نشده')}"
    )
    await state.echo(f"  Read interval:    {interval:.0f}s")
    await state.echo(f"  Telemetry every:  {telemetry_interval:.0f}s")
    await state.echo(
        f"  {_t('Mapping source', 'منبع نگاشت')}:   "
        f"{os.environ.get('BOILERROOM_MAPPING_SOURCE', 'file')}"
    )
    await state.echo(f"  {_t('Mapping file', 'فایل نگاشت')}:     {mapping_path}")
    await state.echo(
        f"  {_t('Authenticated', 'احراز هویت')}:    "
        f"{_t('yes', 'بله') if token_manager.is_authenticated else _t('no', 'خیر')}"
    )
    ws = await state.get_ws_status()
    await state.echo(
        f"  WebSocket:        "
        f"{_t('connected', 'متصل') if ws['connected'] else _t('disconnected', 'قطع')}"
    )
    if ws["hello_ack"]:
        await state.echo(
            f"  WS hello_ack:     {_t('yes', 'بله')} "
            f"(server_time={ws.get('server_time')})"
        )
        await state.echo(
            f"  Desired config:   v{ws.get('desired_config_version')}  "
            f"schedule: v{ws.get('desired_schedule_version')}"
        )
        config_v, schedule_v = await state.get_active_versions()
        await state.echo(f"  Active config:    v{config_v}  schedule: v{schedule_v}")

    await state.echo("")
    device_config = await state.get_device_config()
    for line in describe_config(device_config):
        await state.echo(f"  {line}")
    # The guard needs the config too: thresholds depend on it and on how many
    # probes each unit has.
    for line in limit_guard.describe(device_config, await state.get_setpoints()):
        await state.echo(f"  {line}")

    modes = await state.get_modes()
    if modes:
        await state.echo("  Modes: " + ", ".join(f"{t}={m}" for t, m in sorted(modes.items(), key=str)))

    try:
        stats = await reading_store.stats()
        await state.echo(
            f"  Database: {stats['rows']} rows from {stats['sensors']} sensor(s), "
            f"{stats['size_bytes'] / 1024:.0f} KB, keeping {stats['retention_days']} days"
        )
        if stats["oldest"]:
            await state.echo(f"            {stats['oldest']} .. {stats['newest']}")
        await state.echo(f"            {stats['path']}")

        outbox = await reading_store.outbox_stats()
        if outbox["pending"] or outbox["stuck"]:
            await state.echo(
                f"  Outbox:   {outbox['pending']} awaiting retry"
                + (f", {outbox['stuck']} given up on" if outbox["stuck"] else "")
                + (f", oldest {outbox['oldest_pending']}" if outbox["oldest_pending"] else "")
            )
        else:
            await state.echo(
                f"  Outbox:   empty ({outbox['synced']} recovered post(s) on record)"
            )
    except Exception as exc:
        await state.echo(f"  Database: unavailable ({exc})")

    await state.echo("")


async def _show_schedule(state: RuntimeState) -> None:
    await state.echo("")
    for line in schedule_runner.describe():
        await state.echo(f"[menu] {line}")
    await state.echo("")


def _controllable_targets() -> list[Target]:
    """Every boiler and pump that has a relay in the device mapping."""
    targets: set[Target] = set()
    for cfg in RELAYS.values():
        kind = ROLE_TARGET.get(cfg.get("role"))
        unit = cfg.get("unit") or ""
        if kind is None or not unit.startswith("pot_"):
            continue
        try:
            targets.add(Target(kind, int(unit.split("_", 1)[1])))
        except ValueError:
            continue
    return sorted(targets)


async def _mode_menu(state: RuntimeState) -> None:
    """
    Set a unit's control mode locally.

    The same operation the server performs with ``boiler.set_mode``: an
    operator standing in the boiler room should not need the cloud to take a
    boiler off the schedule.
    """
    targets = _controllable_targets()
    if not targets:
        await state.echo("\n[menu] No boilers or pumps in the device mapping.\n")
        return

    view = screen()
    if view is None:
        # Terminal fallback: keep the old number-based interaction
        await _mode_menu_terminal(state)
        return

    rc = state.relay_controller

    async def build_rows() -> list[str]:
        modes = await state.get_modes()
        blocks = await state.get_limit_blocks()
        rows = []
        for target in targets:
            relay_id = relay_for_target(target)
            relay_state = (
                ("ON " if rc.get_state(relay_id) else "OFF")
                if rc is not None and relay_id is not None
                else "???"
            )
            mode = modes.get(target, "automatic")
            cut = f" [CUT: {blocks[target]}]" if target in blocks else ""
            rows.append(f"{str(target):<12} {mode:<10} {relay_state}{cut}")
        return rows

    # Legend for the mode table
    legend = (
        SCROLL_KEYS,
        (cap_for(ENTER), "Change"),
        (cap_for(NEXT), "Change"),
        (cap_for(CANCEL), "Back"),
    )

    index = 0
    while True:
        rows = await build_rows()
        chosen = await view.select(
            "Unit modes",
            rows,
            index=index,
            legend=legend,
        )
        if chosen is None:
            return

        index = chosen
        target = targets[index]

        # Show mode change dialog
        modes = await state.get_modes()
        current = modes.get(target, "automatic")

        # Use a simple select for mode choice
        mode_choices = ["automatic", "manual"]
        mode_index = 0 if current == "automatic" else 1

        mode_chosen = await view.select(
            f"Mode for {target}",
            [f"  {m}" for m in mode_choices],
            index=mode_index,
            legend=(
                SCROLL_KEYS,
                (cap_for(ENTER), "Select"),
                (cap_for(NEXT), "Select"),
                (cap_for(CANCEL), "Cancel"),
            ),
        )

        if mode_chosen is None:
            continue  # Back to unit list

        mode = mode_choices[mode_chosen]

        if mode == current:
            await view.message("No change", [f"{target} is already {mode}."])
            continue

        await state.set_mode(target, mode)

        if mode == "automatic":
            # Hand the unit back to the schedule now rather than leaving it where
            # the operator left it until the next start/end boundary.
            schedule_runner.forget(target)
            await schedule_runner.evaluate(state)
            relay_id = relay_for_target(target)
            now_on = rc.get_state(relay_id) if rc is not None and relay_id is not None else None
            await view.message(
                _t("Mode changed", "حالت تغییر کرد"),
                [
                    f"{target} -> {_t('automatic', 'خودکار')}",
                    _t("the schedule now drives it", "زمان‌بندی اکنون آن را هدایت می‌کند")
                    + (
                        f" ({_t('relay', 'رله')} {relay_id} {_on_off(now_on)})"
                        if now_on is not None
                        else ""
                    ),
                ],
            )
        else:
            await view.message(
                _t("Mode changed", "حالت تغییر کرد"),
                [
                    f"{target} -> {_t('manual', 'دستی')}",
                    _t("the schedule does not touch it,", "زمان‌بندی دست نمی‌زند"),
                    _t("until you set it back.", "تا زمانی که خودتان آن را برگردانید."),
                ],
            )

        await state.log(f"[menu] {target} set to {mode} by operator")
        # Modes appear in telemetry, so report this without waiting for the cycle.
        await state.notify_state_change(f"{target} mode {mode} (operator)")

        # Telemetry settles reported_mode; this is what is meant to settle
        # desired_mode. Fire and forget — see report_unit_mode.
        from ws_client import report_unit_mode

        if await report_unit_mode(state, target, mode):
            await view.message("Reported", ["Mode reported to the server."])
        else:
            await view.message(
                "Offline",
                [
                    "The mode will be reported",
                    "when the device reconnects.",
                ],
            )


async def _mode_menu_terminal(state: RuntimeState) -> None:
    """Terminal fallback for the old number-based mode control."""
    targets = _controllable_targets()
    if not targets:
        await state.echo("\n[menu] No boilers or pumps in the device mapping.\n")
        return

    rc = state.relay_controller
    modes = await state.get_modes()
    blocks = await state.get_limit_blocks()

    await state.echo("\n[menu] Unit modes:")
    for position, target in enumerate(targets, start=1):
        relay_id = relay_for_target(target)
        relay_state = (
            ("ON" if rc.get_state(relay_id) else "OFF")
            if rc is not None and relay_id is not None
            else "unknown"
        )
        mode = modes.get(target, "automatic")
        cut = f"  [CUT: {blocks[target]}]" if target in blocks else ""
        await state.echo(
            f"  {position}) {str(target):<10} {mode:<10} relay {relay_id} {relay_state}{cut}"
        )

    raw = await _prompt("Which unit? (empty = back): ")
    if not raw:
        await state.echo("")
        return
    try:
        target = targets[int(raw) - 1]
    except (ValueError, IndexError):
        await state.echo("[menu] Invalid selection.\n")
        return

    current = modes.get(target, "automatic")
    answer = await _prompt(
        f"  {target} is {current}. Set to 1) automatic or 2) manual? (empty = back): "
    )
    choice = answer.strip().lower()
    if not choice:
        await state.echo("")
        return
    if choice in ("1", "a", "auto", "automatic"):
        mode = "automatic"
    elif choice in ("2", "m", "man", "manual"):
        mode = "manual"
    else:
        await state.echo("[menu] Invalid mode.\n")
        return

    if mode == current:
        await state.echo(f"[menu] {target} is already {mode}.\n")
        return

    await state.set_mode(target, mode)

    if mode == "automatic":
        # Hand the unit back to the schedule now rather than leaving it where
        # the operator left it until the next start/end boundary.
        schedule_runner.forget(target)
        await schedule_runner.evaluate(state)
        relay_id = relay_for_target(target)
        now_on = rc.get_state(relay_id) if rc is not None and relay_id is not None else None
        await state.echo(
            f"[menu] {target} -> {_t('automatic', 'خودکار')}"
            f"{_t('; the schedule now drives it', '؛ زمان‌بندی اکنون آن را هدایت می‌کند')}"
            + (
                f" ({_t('relay', 'رله')} {relay_id} {_on_off(now_on)})"
                if now_on is not None
                else ""
            )
            + "\n"
        )
    else:
        await state.echo(
            f"[menu] {target} -> {_t('manual', 'دستی')}"
            f"{_t('; the schedule does not touch it until you set it back.', '؛ زمان‌بندی تا زمانی که خودتان آن را به حالت خودکار برنگردانید دست نمی‌زند.')}"
            "\n"
        )

    await state.log(f"[menu] {target} set to {mode} by operator")
    # Modes appear in telemetry, so report this without waiting for the cycle.
    await state.notify_state_change(f"{target} mode {mode} (operator)")

    # Telemetry settles reported_mode; this is what is meant to settle
    # desired_mode. Fire and forget — see report_unit_mode.
    from ws_client import report_unit_mode

    if await report_unit_mode(state, target, mode):
        await state.echo("[menu] Reported to the server.\n")
    else:
        await state.echo(
            "[menu] Offline — the mode will be reported when the device "
            "reconnects.\n"
        )


# ---------------------------------------------------------------------------
# Schedule editing
# ---------------------------------------------------------------------------
#
# The operator edits the raw schedule document and it is handed to
# parse_schedule, so a programme built here is checked exactly like one the
# server pushed. See schedule_editor for why a local edit keeps the published
# version number and why a publish supersedes it.


def _edit_token() -> tuple[int, int]:
    """
    What the running schedule is, for detecting a change mid-edit.

    Each prompt is a blocking ``input()`` that can sit there for minutes, and a
    ``schedule.apply`` may land in that time. Applying an edit built on the old
    document would silently undo the push, so the token is re-checked first.
    """
    return schedule_runner.server_version, schedule_runner.local_revision


def _document_for_edit() -> dict:
    document = schedule_runner.document
    if document is not None:
        return document
    # Nothing published or cached yet — a room being commissioned before its
    # schedule exists. Version 0 means the first publish supersedes this.
    return blank_document()


def _schedule_today() -> datetime.date:
    """Today in the schedule's timezone, which is what exceptions match on."""
    schedule = schedule_runner.schedule
    if schedule is None:
        return datetime.date.today()
    return schedule.local_time().date()


async def _schedule_status(state: RuntimeState) -> None:
    schedule = schedule_runner.schedule
    if schedule is None:
        await state.echo(
            "\n[menu] No schedule yet — adding a rule starts one on this device."
        )
        return

    await state.echo(f"\n[menu] Schedule v{schedule.version}", )
    if schedule_runner.is_locally_modified:
        await state.echo(
            f"        locally edited (revision {schedule_runner.local_revision}, "
            f"on top of published v{schedule_runner.server_version})"
        )
    for line in schedule_runner.describe_weekly_rules():
        await state.echo(f"  {line}")
    for line in schedule_runner.describe_exceptions():
        await state.echo(f"  {line}")


async def _select_schedule_targets(state: RuntimeState) -> list[Target] | None:
    """Pick the boilers and pumps a rule applies to. None means cancelled."""
    targets = _controllable_targets()
    if not targets:
        await state.echo("[menu] No boilers or pumps in the device mapping.\n")
        return None

    await state.echo("  Targets:")
    for position, target in enumerate(targets, start=1):
        await state.echo(f"    {position}) {str(target):<10} relay {relay_for_target(target)}")

    raw = await _prompt("  Which? (e.g. 1,3 or 13; 0 = all, empty = cancel): ")
    if not raw:
        return None

    text = raw.replace(" ", "").lower()
    if text in ("all", "0"):
        return targets

    parts = [part for part in text.split(",") if part]

    # A run of digits with no separators — "13" for units 1 and 3 — because the
    # keypad's comma is one more key to find in a boiler room. Only when the
    # number is not itself a position on the list, so an installation with
    # thirteen units still reads "13" as the thirteenth.
    if len(parts) == 1 and parts[0].isdigit() and len(parts[0]) > 1:
        if not 1 <= int(parts[0]) <= len(targets):
            parts = list(parts[0])

    chosen: list[Target] = []
    for part in parts:
        try:
            position = int(part)
        except ValueError:
            await state.echo(f"[menu] {part!r} is not a number from the list.\n")
            return None
        if not 1 <= position <= len(targets):
            await state.echo(f"[menu] There is no target {position}.\n")
            return None
        if targets[position - 1] not in chosen:
            chosen.append(targets[position - 1])

    return chosen or None


async def _apply_local_edit(
    state: RuntimeState,
    document: dict,
    token: tuple[int, int],
    summary: str,
) -> None:
    """Validate an edited document, adopt it, and say plainly what it now means."""
    if _edit_token() != token:
        await state.echo(
            "[menu] The schedule changed while you were editing it — most likely "
            "the server published one. Nothing was saved; take another look and "
            "try again.\n"
        )
        return

    try:
        schedule = await schedule_runner.apply_local_document(document, state)
    except ScheduleError as exc:
        # Same validator the server's documents go through, so this is the same
        # message a bad push would produce.
        await state.echo(f"[menu] Rejected: {exc}\n")
        return

    await state.echo(f"[menu] Schedule updated — {summary}.")
    if not schedule_runner.local_persisted:
        await state.echo(
            "[menu] WARNING: it could not be written to disk, so it will not "
            "survive a restart."
        )
    await state.echo(
        f"[menu] Now running a locally edited v{schedule.version} "
        f"(revision {schedule_runner.local_revision}). The server's next publish "
        "replaces it."
    )
    for line in schedule_runner.describe_now():
        await state.echo(f"  {line}")
    await state.echo("")

    await state.log(
        f"[menu] Operator edited the schedule: {summary} "
        f"(local revision {schedule_runner.local_revision}, "
        f"published v{schedule_runner.server_version})",
        level=logging.WARNING,
    )

    # Relay changes the edit caused are already reported by evaluate(); this
    # tells a dashboard the *programme* changed even when nothing switched yet.
    from ws_client import publish_schedule_update, push_device_state

    await push_device_state(state)

    # Offer it upstream. DEVICE.md's schedule.update makes the edit the room's
    # real schedule rather than a local override, so this is what turns "the
    # cloud disagrees with the boiler room" back into "they agree".
    await state.echo("[menu] Publishing to the server ...")
    ack = await publish_schedule_update(state)

    if ack and ack.get("ok"):
        await state.echo(
            f"[menu] Published — the server created schedule "
            f"v{ack.get('schedule_version')}. This is now the room's schedule, "
            "not a local edit.\n"
        )
    elif ack:
        await state.echo(
            f"[menu] The server refused it: {ack.get('error') or ack}\n"
            "[menu] The change is still running here, and will be offered "
            "again on the next connection.\n"
        )
    else:
        await state.echo(
            "[menu] No answer from the server — the change is running here and "
            "will be published when the device reconnects.\n"
        )


async def _add_weekly_rule(state: RuntimeState) -> None:
    token = _edit_token()
    document = _document_for_edit()

    targets = await _select_schedule_targets(state)
    if not targets:
        await state.echo("[menu] Cancelled.\n")
        return

    try:
        days = parse_days(
            await _prompt("  Days? (1=Mon .. 7=Sun, e.g. 135; 0 = every day): ")
        )
        start = parse_time_text(await _prompt("  Start time (HHMM): "), "start")
        end = parse_time_text(await _prompt("  End time (HHMM): "), "end")
        turn_on = parse_state_text(
            await _prompt("  Switch them on or off in that window? [1 = on, 0 = off]: ")
        )
        edited = add_weekly_rule(
            document,
            days=days,
            start=start,
            end=end,
            state=turn_on,
            targets=targets,
        )
    except ScheduleEditError as exc:
        await state.echo(f"[menu] {exc}\n")
        return

    await _apply_local_edit(
        state,
        edited,
        token,
        f"{start}-{end} {_weekdays(days)} -> "
        f"{_on_off(turn_on)} {_t('for', 'برای')} "
        f"{', '.join(str(t) for t in targets)}",
    )


async def _add_weekly_rule_v2(state: RuntimeState) -> None:
    """
    Add a weekly rule using the table-based UI on the display.

    Multi-step flow:
    1. Select targets (units) with checkboxes
    2. Select days of week with checkboxes
    3. Select start hour
    4. Select start minute
    5. Select end hour
    6. Select end minute
    7. Select state (ON/OFF)
    """
    view = screen()
    if view is None:
        # Terminal fallback: use the original function
        await _add_weekly_rule(state)
        return

    token = _edit_token()
    document = _document_for_edit()

    # Step 1: Select targets with checkboxes
    targets = await _select_targets_table(state, view)
    if not targets:
        return

    # Step 2: Select days with checkboxes
    days = await _select_days_table(state, view)
    if not days:
        return

    # Step 3: Select start hour
    start_hour = await _select_time_component(state, view, "Start hour", (0, 23))
    if start_hour is None:
        return

    # Step 4: Select start minute (0, 15, 30, 45)
    start_minute = await _select_time_component(state, view, "Start minute", [0, 15, 30, 45])
    if start_minute is None:
        return

    # Step 5: Select end hour
    end_hour = await _select_time_component(state, view, "End hour", (0, 23))
    if end_hour is None:
        return

    # Step 6: Select end minute (0, 15, 30, 45)
    end_minute = await _select_time_component(state, view, "End minute", [0, 15, 30, 45])
    if end_minute is None:
        return

    # Step 7: Select state (ON/OFF)
    turn_on = await _select_on_off(state, view)
    if turn_on is None:
        return

    # Build time strings in HH:MM format (required by _parse_time in schedule_runner)
    start = f"{start_hour:02d}:{start_minute:02d}"
    end = f"{end_hour:02d}:{end_minute:02d}"

    try:
        edited = add_weekly_rule(
            document,
            days=days,
            start=start,
            end=end,
            state=turn_on,
            targets=targets,
        )
    except ScheduleEditError as exc:
        await view.message("Error", [str(exc)])
        return

    await _apply_local_edit(
        state,
        edited,
        token,
        f"{start}-{end} {_weekdays(days)} -> "
        f"{_on_off(turn_on)} {_t('for', 'برای')} "
        f"{', '.join(str(t) for t in targets)}",
    )


async def _select_targets_table(state: RuntimeState, view: Screen) -> list[Target] | None:
    """Select targets using a table with checkboxes."""
    targets = _controllable_targets()
    if not targets:
        await view.message("No targets", ["No boilers or pumps", "in the device mapping."])
        return None

    items = [f"{str(target):<12} relay {relay_for_target(target)}" for target in targets]
    legend = (
        SCROLL_KEYS,
        (cap_for(ENTER), "Toggle"),
        (cap_for(NEXT), "Next"),
        (cap_for(CANCEL), "Cancel"),
    )

    selected, _ = await view.select_checkboxes("Select targets", items, legend=legend)
    if selected is None:
        return None

    chosen = [targets[i] for i, sel in enumerate(selected) if sel]
    if not chosen:
        await view.message("No targets", ["At least one target", "must be selected."])
        return await _select_targets_table(state, view)

    return chosen


async def _select_days_table(state: RuntimeState, view: Screen) -> list[str] | None:
    """Select days of week using checkboxes."""
    day_names = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    day_labels = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    items = day_labels
    legend = (
        SCROLL_KEYS,
        (cap_for(ENTER), "Toggle"),
        (cap_for(NEXT), "Next"),
        (cap_for(CANCEL), "Cancel"),
    )

    selected, _ = await view.select_checkboxes("Select days", items, legend=legend)
    if selected is None:
        return None

    chosen = [day_names[i] for i, sel in enumerate(selected) if sel]
    if not chosen:
        await view.message("No days", ["At least one day", "must be selected."])
        return await _select_days_table(state, view)

    return chosen


async def _select_time_component(
    state: RuntimeState,
    view: Screen,
    title: str,
    values: list[int] | tuple[int, int],
) -> int | None:
    """Select a time component (hour or minute) from a list."""
    if isinstance(values, tuple):
        start, end = values
        value_list = list(range(start, end + 1))
    else:
        value_list = values

    items = [f"{v:02d}" for v in value_list]
    legend = (
        SCROLL_KEYS,
        (cap_for(ENTER), "Select"),
        (cap_for(NEXT), "Select"),
        (cap_for(CANCEL), "Cancel"),
    )

    index = await view.select_list(title, items, legend=legend)
    if index is None:
        return None
    return value_list[index]


async def _select_on_off(state: RuntimeState, view: Screen) -> bool | None:
    """Select ON or OFF state."""
    items = ["ON", "OFF"]
    legend = (
        SCROLL_KEYS,
        (cap_for(ENTER), "Select"),
        (cap_for(NEXT), "Select"),
        (cap_for(CANCEL), "Cancel"),
    )

    index = await view.select_list("Switch state", items, legend=legend)
    if index is None:
        return None
    return index == 0


async def _remove_weekly_rule(state: RuntimeState) -> None:
    token = _edit_token()
    document = _document_for_edit()

    await state.echo("")
    for line in schedule_runner.describe_weekly_rules():
        await state.echo(f"  {line}")

    raw = await _prompt("  Remove which rule? (empty = back): ")
    if not raw:
        await state.echo("")
        return

    try:
        edited = remove_weekly_rule(document, int(raw))
    except ValueError as exc:
        # Covers both a non-numeric answer and a position that does not exist.
        message = exc if isinstance(exc, ScheduleEditError) else f"{raw!r} is not a rule number"
        await state.echo(f"[menu] {message}\n")
        return

    await _apply_local_edit(state, edited, token, f"removed weekly rule {int(raw)}")


async def _remove_weekly_rule_v2(state: RuntimeState) -> None:
    """
    Delete a weekly rule using the table-based UI on the display.

    Shows a list of all weekly rules. Press:
    - 2/8: Scroll through rules
    - 5 (OK): View rule details
    - 6 (NEXT): Delete the selected rule (with confirmation)
    - 4 (CANCEL): Back
    """
    view = screen()
    if view is None:
        # Terminal fallback: use the original function
        await _remove_weekly_rule(state)
        return

    schedule = schedule_runner.schedule
    if schedule is None or not schedule.weekly_rules:
        await view.message("No rules", ["No weekly rules", "to delete."])
        return

    rules = schedule.weekly_rules
    index = 0

    legend = (
        SCROLL_KEYS,
        (cap_for(ENTER), "View"),
        (cap_for(NEXT), "Delete"),
        (cap_for(CANCEL), "Back"),
    )

    while not state.shutdown.is_set():
        # Build display rows
        rows = []
        for i, rule in enumerate(rules):
            days = _weekdays(REVERSE_DAYS[d] for d in sorted(rule.days))
            targets = ", ".join(str(t) for t in rule.targets)
            rows.append(
                f"{i+1}) {rule.start:%H:%M}-{rule.end:%H:%M} {days} "
                f"-> {_on_off(rule.state)} [{targets}]"
            )

        chosen = await view.select("Delete weekly rule", rows, index=index, legend=legend)
        if chosen is None:
            return

        index = chosen

        # If we get here, the user pressed ENTER (view) or NEXT (delete)
        # We need to know which key was pressed. Since select() returns index on ENTER/NEXT,
        # we need a different approach. Let me use a custom selector.
        # For now, let's show details first, then confirm deletion

        # Show rule details
        rule = rules[index]
        days = _weekdays(REVERSE_DAYS[d] for d in sorted(rule.days))
        targets = ", ".join(str(t) for t in rule.targets)
        detail_lines = [
            f"Rule {index + 1}:",
            f"  Time: {rule.start:%H:%M}-{rule.end:%H:%M}",
            f"  Days: {days}",
            f"  Action: {'ON' if rule.state else 'OFF'}",
            f"  Targets: {targets}",
        ]

        action = await view.select(
            f"Rule {index + 1}",
            ["View details", "Delete this rule", "Back to list"],
            index=0,
            legend=(
                SCROLL_KEYS,
                (cap_for(ENTER), "Select"),
                (cap_for(NEXT), "Select"),
                (cap_for(CANCEL), "Back"),
            ),
        )

        if action is None:
            continue  # Back to list

        if action == 0:
            # View details - already shown, just wait for key
            await view.message(f"Rule {index + 1}", detail_lines)
            continue

        elif action == 1:
            # Delete this rule
            confirm = await view.select(
                "Confirm delete",
                ["No, keep it", "Yes, delete it"],
                index=0,
                legend=(
                    SCROLL_KEYS,
                    (cap_for(ENTER), "Select"),
                    (cap_for(NEXT), "Select"),
                    (cap_for(CANCEL), "Back"),
                ),
            )

            if confirm == 1:
                token = _edit_token()
                document = _document_for_edit()
                try:
                    edited = remove_weekly_rule(document, index + 1)
                except ScheduleEditError as exc:
                    await view.message("Error", [str(exc)])
                    continue

                await _apply_local_edit(
                    state,
                    edited,
                    token,
                    f"removed weekly rule {index + 1}",
                )
                # Refresh rules list
                schedule = schedule_runner.schedule
                rules = schedule.weekly_rules if schedule else []
                if not rules:
                    await view.message("Done", ["No more weekly rules."])
                    return
                # Adjust index if needed
                if index >= len(rules):
                    index = len(rules) - 1

            continue

        elif action == 2:
            # Back to list
            continue


async def _add_exception(state: RuntimeState) -> None:
    token = _edit_token()
    document = _document_for_edit()

    targets = await _select_schedule_targets(state)
    if not targets:
        await state.echo("[menu] Cancelled.\n")
        return

    try:
        date = parse_date_text(
            await _prompt("  Date? (0 = today, 1 = tomorrow, or MMDD / YYYYMMDD): "),
            today=_schedule_today(),
        )
        turn_on = parse_state_text(
            await _prompt("  Force them on or off? [1 = on, 0 = off]: ")
        )

        all_day = not _is_no(await _prompt("  All day? [0 = no, anything else = yes]: "))
        start = end = None
        if not all_day:
            start = parse_time_text(await _prompt("  Start time (HHMM): "), "start")
            end = parse_time_text(await _prompt("  End time (HHMM): "), "end")

        reason = (await _prompt("  Reason (optional): ")).strip()

        edited = add_exception(
            document,
            date=date,
            state=turn_on,
            targets=targets,
            all_day=all_day,
            start=start,
            end=end,
            reason=reason,
        )
    except ScheduleEditError as exc:
        await state.echo(f"[menu] {exc}\n")
        return

    window = "all day" if all_day else f"{start}-{end}"
    await _apply_local_edit(
        state,
        edited,
        token,
        f"exception {date} {window} -> {'ON' if turn_on else 'OFF'} "
        f"for {', '.join(str(t) for t in targets)}",
    )


async def _add_exception_v2(state: RuntimeState) -> None:
    """
    Add a date exception using the table-based UI on the display.

    Multi-step flow:
    1. Select targets (units) with checkboxes
    2. Select start year
    3. Select start month
    4. Select start day
    5. Select start hour
    6. Select start minute
    7. Select end year
    8. Select end month
    9. Select end day
    10. Select end hour
    11. Select end minute
    12. Select state (ON/OFF)
    """
    view = screen()
    if view is None:
        # Terminal fallback: use the original function
        await _add_exception(state)
        return

    token = _edit_token()
    document = _document_for_edit()

    # Step 1: Select targets with checkboxes
    targets = await _select_targets_table(state, view)
    if not targets:
        return

    # Step 2-6: Select start date/time components
    today = _schedule_today()
    start_year = await _select_year(state, view, "Start year", today.year, today.year + 5)
    if start_year is None:
        return

    start_month = await _select_month(state, view, "Start month")
    if start_month is None:
        return

    start_day = await _select_day(state, view, "Start day", start_year, start_month)
    if start_day is None:
        return

    start_hour = await _select_time_component(state, view, "Start hour", (0, 23))
    if start_hour is None:
        return

    start_minute = await _select_time_component(state, view, "Start minute", [0, 15, 30, 45])
    if start_minute is None:
        return

    # Step 7-11: Select end date/time components
    end_year = await _select_year(state, view, "End year", start_year, start_year + 5)
    if end_year is None:
        return

    end_month = await _select_month(state, view, "End month")
    if end_month is None:
        return

    end_day = await _select_day(state, view, "End day", end_year, end_month)
    if end_day is None:
        return

    end_hour = await _select_time_component(state, view, "End hour", (0, 23))
    if end_hour is None:
        return

    end_minute = await _select_time_component(state, view, "End minute", [0, 15, 30, 45])
    if end_minute is None:
        return

    # Step 12: Select state (ON/OFF)
    turn_on = await _select_on_off(state, view)
    if turn_on is None:
        return

    # Build date/time strings in ISO format
    start_date = f"{start_year:04d}-{start_month:02d}-{start_day:02d}"
    end_date = f"{end_year:04d}-{end_month:02d}-{end_day:02d}"
    start_time = f"{start_hour:02d}:{start_minute:02d}"
    end_time = f"{end_hour:02d}:{end_minute:02d}"

    # For the exception, we use the start date as the exception date
    # and provide start/end times for the window
    date = start_date
    all_day = False
    start = start_time
    end = end_time

    try:
        edited = add_exception(
            document,
            date=date,
            state=turn_on,
            targets=targets,
            all_day=all_day,
            start=start,
            end=end,
            reason="set on the device",
        )
    except ScheduleEditError as exc:
        await view.message("Error", [str(exc)])
        return

    await _apply_local_edit(
        state,
        edited,
        token,
        f"exception {date} {start}-{end} -> {'ON' if turn_on else 'OFF'} "
        f"for {', '.join(str(t) for t in targets)}",
    )


async def _select_year(
    state: RuntimeState,
    view: Screen,
    title: str,
    start_year: int,
    end_year: int,
) -> int | None:
    """Select a year from a range."""
    return await _select_time_component(state, view, title, (start_year, end_year))


async def _select_month(state: RuntimeState, view: Screen, title: str) -> int | None:
    """Select a month (1-12)."""
    return await _select_time_component(state, view, title, (1, 12))


async def _select_day(
    state: RuntimeState,
    view: Screen,
    title: str,
    year: int,
    month: int,
) -> int | None:
    """Select a day (1-28/29/30/31) based on year and month."""
    import calendar
    max_day = calendar.monthrange(year, month)[1]
    return await _select_time_component(state, view, title, (1, max_day))


async def _remove_exception(state: RuntimeState) -> None:
    token = _edit_token()
    document = _document_for_edit()

    await state.echo("")
    for line in schedule_runner.describe_exceptions():
        await state.echo(f"  {line}")

    raw = await _prompt("  Remove which exception? (empty = back): ")
    if not raw:
        await state.echo("")
        return

    try:
        edited = remove_exception(document, int(raw))
    except ValueError as exc:
        message = (
            exc if isinstance(exc, ScheduleEditError) else f"{raw!r} is not an exception number"
        )
        await state.echo(f"[menu] {message}\n")
        return

    await _apply_local_edit(state, edited, token, f"removed exception {int(raw)}")


async def _remove_exception_v2(state: RuntimeState) -> None:
    """
    Delete a date exception using the table-based UI on the display.

    Shows a list of all exceptions. Press:
    - 2/8: Scroll through exceptions
    - 5 (OK): View exception details
    - 6 (NEXT): Delete the selected exception (with confirmation)
    - 4 (CANCEL): Back
    """
    view = screen()
    if view is None:
        # Terminal fallback: use the original function
        await _remove_exception(state)
        return

    schedule = schedule_runner.schedule
    if schedule is None or not schedule.exceptions:
        await view.message("No exceptions", ["No date exceptions", "to delete."])
        return

    exceptions = schedule.exceptions
    index = 0

    while not state.shutdown.is_set():
        # Build display rows
        rows = []
        for i, exc in enumerate(exceptions):
            window = (
                "all day"
                if exc.all_day or exc.start is None
                else f"{exc.start:%H:%M}-{(exc.end or datetime.time.max):%H:%M}"
            )
            targets = ", ".join(str(t) for t in exc.targets)
            rows.append(
                f"{i+1}) {exc.date} {window} -> "
                f"{'ON' if exc.state else 'OFF'} [{targets}]"
            )

        chosen = await view.select("Delete exception", rows, index=index, legend=(
            SCROLL_KEYS,
            (cap_for(ENTER), "View"),
            (cap_for(NEXT), "Delete"),
            (cap_for(CANCEL), "Back"),
        ))
        if chosen is None:
            return

        index = chosen

        # Show exception details and action menu
        exc = exceptions[index]
        window = (
            "all day"
            if exc.all_day or exc.start is None
            else f"{exc.start:%H:%M}-{(exc.end or datetime.time.max):%H:%M}"
        )
        targets = ", ".join(str(t) for t in exc.targets)
        detail_lines = [
            f"Exception {index + 1}:",
            f"  Date: {exc.date}",
            f"  Window: {window}",
            f"  Action: {'ON' if exc.state else 'OFF'}",
            f"  Targets: {targets}",
            f"  Reason: {exc.reason}",
        ]

        action = await view.select(
            f"Exception {index + 1}",
            ["View details", "Delete this exception", "Back to list"],
            index=0,
            legend=(
                SCROLL_KEYS,
                (cap_for(ENTER), "Select"),
                (cap_for(NEXT), "Select"),
                (cap_for(CANCEL), "Back"),
            ),
        )

        if action is None:
            continue  # Back to list

        if action == 0:
            # View details
            await view.message(f"Exception {index + 1}", detail_lines)
            continue

        elif action == 1:
            # Delete this exception
            confirm = await view.select(
                "Confirm delete",
                ["No, keep it", "Yes, delete it"],
                index=0,
                legend=(
                    SCROLL_KEYS,
                    (cap_for(ENTER), "Select"),
                    (cap_for(NEXT), "Select"),
                    (cap_for(CANCEL), "Back"),
                ),
            )

            if confirm == 1:
                token = _edit_token()
                document = _document_for_edit()
                try:
                    edited = remove_exception(document, index + 1)
                except ScheduleEditError as exc:
                    await view.message("Error", [str(exc)])
                    continue

                await _apply_local_edit(
                    state,
                    edited,
                    token,
                    f"removed exception {index + 1}",
                )
                # Refresh exceptions list
                schedule = schedule_runner.schedule
                exceptions = schedule.exceptions if schedule else []
                if not exceptions:
                    await view.message("Done", ["No more exceptions."])
                    return
                # Adjust index if needed
                if index >= len(exceptions):
                    index = len(exceptions) - 1

            continue

        elif action == 2:
            # Back to list
            continue


async def _discard_local_edits(state: RuntimeState) -> None:
    if not schedule_runner.is_locally_modified:
        await state.echo("\n[menu] There are no local edits to discard.\n")
        return

    answer = await _prompt(
        "\n  Discard local edits and go back to the published schedule? "
        "[1 or y = yes]: "
    )
    if not _is_yes(answer):
        await state.echo("[menu] Cancelled.\n")
        return

    schedule = await schedule_runner.revert_local(state)
    if schedule is None:
        await state.echo(
            "[menu] Local edits discarded. Nothing has ever been published to "
            "this device, so no programme is driving the relays — they stay "
            "where they are until the server sends one.\n"
        )
    else:
        await state.echo(f"[menu] Back to the published schedule v{schedule.version}.\n")
        for line in schedule_runner.describe_now():
            await state.echo(f"  {line}")
        await state.echo("")

    await state.log(
        "[menu] Operator discarded the local schedule edits", level=logging.WARNING
    )

    from ws_client import push_device_state

    await push_device_state(state)


async def _schedule_editor_menu(state: RuntimeState) -> None:
    """
    Change the heating programme from the device - table-based UI.

    Shows the four main actions as a selectable table.
    """
    view = screen()
    if view is None:
        # Terminal fallback: use the original menu
        await _schedule_editor_menu_terminal(state)
        return

    legend = (
        SCROLL_KEYS,
        (cap_for(ENTER), "Select"),
        (cap_for(NEXT), "Select"),
        (cap_for(CANCEL), "Back"),
    )

    index = 0
    while not state.shutdown.is_set():
        chosen = await view.select(
            "Change schedule",
            [
                "Add weekly rule",
                "Remove weekly rule",
                "Add date exception",
                "Remove exception",
            ],
            index=index,
            legend=legend,
        )
        if chosen is None:
            return

        index = chosen

        if chosen == 0:
            await _add_weekly_rule_v2(state)
        elif chosen == 1:
            await _remove_weekly_rule_v2(state)
        elif chosen == 2:
            await _add_exception_v2(state)
        elif chosen == 3:
            await _remove_exception_v2(state)


async def _schedule_editor_menu_terminal(state: RuntimeState) -> None:
    """
    Terminal fallback for the original schedule editor menu.
    """
    while not state.shutdown.is_set():
        _set_context("Schedule")
        await _schedule_status(state)

        try:
            choice = await _choose(state, "Schedule", SCHEDULE_V2_ITEMS, """
  1) Add a weekly rule
  2) Remove a weekly rule
  3) Add a date exception
  4) Remove a date exception
  0) Back
> """)
        except EOFError:
            state.shutdown.set()
            return

        _set_context(_label_for(SCHEDULE_V2_ITEMS, choice, "Schedule"))

        if choice == "1":
            await _add_weekly_rule(state)
        elif choice == "2":
            await _remove_weekly_rule(state)
        elif choice == "3":
            await _add_exception(state)
        elif choice == "4":
            await _remove_exception(state)
        elif choice in ("0", "", BACK):
            await state.echo("")
            return
        else:
            await state.echo(f"\n[menu] Unknown option: {choice!r}\n")

        await _flush_page(state)


# ---------------------------------------------------------------------------
# Temperature limits
# ---------------------------------------------------------------------------
#
# Mirrors the schedule editor: the operator edits the raw config document and
# it is handed to parse_config, a local edit keeps the published version
# number, and the server's next publish supersedes it. See config_editor.


def _limits_edit_token() -> tuple[int, int]:
    """What the running config is, for detecting a push mid-edit."""
    return config_store.server_version, config_store.local_revision


def _limits_document_for_edit() -> dict:
    document = config_store.document
    if document is not None:
        return document
    # Nothing published or cached yet — a room being commissioned. Version 0
    # means the server's first publish supersedes these limits.
    return blank_config_document()


async def _boiler_targets_with_relays() -> list[Target]:
    return [t for t in _controllable_targets() if t.type == "boiler"]


async def _show_temperature_status(state: RuntimeState) -> None:
    """Every boiler's setpoint and the cut-out that follows from it."""
    setpoints = await state.get_setpoints()
    config = await state.get_device_config()

    await state.echo("\n[menu] Boiler temperatures:")
    for line in limit_guard.describe(config, setpoints):
        await state.echo(f"  {line}")

    unpublished = await state.unpublished_setpoints()
    if unpublished:
        await state.echo(
            "  Not yet published to the server: "
            + ", ".join(f"boiler {i}" for i in unpublished)
            + " — will be sent on the next connection"
        )


# The temperatures offered on the panel, every TEMP_STEP_C degrees between the
# bounds DEVICE.md gives boiler.set_temperature. Sixteen of them is a list that
# scrolls rather than one that fits, which is the point: the keypad moves a
# highlight, so length costs nothing but a few presses of ``8``, while typing
# "73" costs a hunt for the digits.
TEMP_STEP_C = 5.0

# The last row of the temperature list, which is not a temperature. Where the
# terminal types a negative to clear a limit, the panel needs a row to select,
# and this is the one an operator would otherwise have no way to reach.
DEVICE_LIMIT_ROW = "Device-wide limit"

# How the same choice is put in a table row, where there is no room to spell it
# out. The last row of the list behind it says what this means, so here it only
# has to be short enough that the unit's own number survives the row.
DEVICE_LIMIT_SHORT = "none"

# Both of these screens choose something, so both say the same three things:
# move, choose, go back. The accept key and the next key are the same key here.
_SET_LEGEND = (
    SCROLL_KEYS,
    (cap_for(ENTER), "Set"),
    (cap_for(NEXT), "Set"),
    (cap_for(CANCEL), "Back"),
)


def _unit_label(target: Target) -> str:
    """
    The name the mapping gives a unit, falling back to ``boiler 1``.

    Every unit is ``pot_N`` in the mapping, boiler or pump — the same way
    relay_for_target finds a unit's relay.
    """
    return UNITS.get(f"pot_{target.index}", {}).get("name") or str(target)


def _temperature_row(label: str, shown: str) -> str:
    """
    ``Boiler 1 - 70°C``, on a 122 px row.

    The temperature is the answer, so it keeps its place and the name gives
    way: a mapping may call a unit something long, and a row that has had its
    number truncated off the end has thrown away the only part of it that was
    new.

    The budget is in pixels, not characters. A Persian label is variable width,
    so a row budget spent in characters either overflows the panel or wastes
    most of the row.
    """
    room = BODY_COLUMNS - text_width(shown) - text_width(" - ")
    if text_width(label) > room:
        label = truncate(label, max(1, room))
    return f"{label} - {shown}"


def _shown_temperature(entry) -> str:
    """A boiler's setpoint as the table shows it."""
    if entry is None:
        return DEVICE_LIMIT_SHORT
    return f"{entry.temperature_c:g}{DEGREE}C"


def _temperature_values(current) -> list[float]:
    """
    The temperatures a boiler can be given, ascending.

    The step grid, plus the boiler's own current setpoint when that is not one
    of them — set from the terminal, or pushed by the server. It belongs in the
    list: the highlight opens on it, and an operator comparing what the server
    sent against the choices should find the number they were given rather than
    the nearest one to it.
    """
    values = [
        float(step)
        for step in range(int(TEMP_STEP_C), int(MAX_SETPOINT_C) + 1, int(TEMP_STEP_C))
    ]
    held = current.temperature_c if current is not None else None
    if (
        held is not None
        and MIN_SETPOINT_C <= held <= MAX_SETPOINT_C
        and held not in values
    ):
        values.append(float(held))
        values.sort()
    return values


async def _temperature_table(state: RuntimeState) -> None:
    """
    Set a boiler's temperature from the panel: a table, then a list.

    Two screens because the question has two parts. Which boiler — answered by
    the table below, one row per boiler, scrolling with the scroll keys. And
    which temperature for it — answered by the list behind the accept key or
    the next key, which are the same key here and both open it.

    A list is the right shape for both on a keypad: the selection moves with
    the same two keys that scroll, and neither answer has to be spelled out in
    digits at a panel. The terminal menu asks the same two questions in the same
    order, because it is the same operation — see _set_boiler_temperature.
    """
    view = screen()

    targets = await _boiler_targets_with_relays()
    if not targets:
        await view.message("Temperatures", ["No boilers in the", "device mapping."])
        return

    index = 0
    while not state.shutdown.is_set():
        setpoints = await state.get_setpoints()
        rows = [
            _temperature_row(
                _unit_label(target), _shown_temperature(setpoints.get(target.index))
            )
            for target in targets
        ]

        chosen = await view.select("Temperatures", rows, index=index, legend=_SET_LEGEND)
        if chosen is None:
            return

        index = chosen
        await _temperature_list(state, targets[index])


async def _temperature_list(state: RuntimeState, target: Target) -> None:
    """The temperatures one boiler can be held at, and the choice to leave it."""
    view = screen()
    label = _unit_label(target)

    setpoints = await state.get_setpoints()
    current = setpoints.get(target.index)
    values = _temperature_values(current)

    # Opens on the temperature it is held at now, so the highlight says what
    # this is before a single key is pressed.
    index = values.index(current.temperature_c) if current is not None else 0

    rows = [f"{value:g}{DEGREE}C" for value in values] + [DEVICE_LIMIT_ROW]

    chosen = await view.select(label, rows, index=index, legend=_SET_LEGEND)
    if chosen is None:
        return

    if chosen == len(values):
        await _clear_setpoint(state, target)
        return

    value = values[chosen]
    if current is not None and current.temperature_c == value:
        await view.message(
            "No change", [f"{label} is already at {value:g}{DEGREE}C."]
        )
        return

    await state.set_setpoint(target.index, value)
    await state.log(
        f"[menu] Operator set {target} temperature to {value:g} °C",
        level=logging.WARNING,
    )
    await view.message(
        "Temperature set",
        [
            f"{label} is now held at {value:g}{DEGREE}C.",
            "",
            "Publishing to the server ...",
        ],
    )

    await view.message("Temperature set", await _publish_setpoint(state, target.index, value))


async def _clear_setpoint(state: RuntimeState, target: Target) -> None:
    """
    Drop a boiler's own temperature, handing it back to the device-wide limit.

    The last row of _temperature_list. Said out loud because it cannot be: the
    server keeps the last temperature it was told, and there is no protocol
    message for "no setpoint" — so this changes what this device holds the
    boiler at, and only that.
    """
    view = screen()
    label = _unit_label(target)

    await state.clear_setpoint(target.index)
    await state.log(
        f"[menu] Operator cleared {target}'s temperature — it now follows the "
        "device-wide limit",
        level=logging.WARNING,
    )

    from ws_client import push_device_state

    await push_device_state(state)

    await view.message(
        "Setpoint cleared",
        [
            f"{label} now follows the device-wide limit.",
            "",
            "The server keeps the last temperature it was given.",
        ],
    )


async def _set_boiler_temperature(state: RuntimeState) -> None:
    if screen() is not None:
        # A table of boilers and a list of temperatures, rather than two
        # typed answers. Same operation as below; see _temperature_table.
        await _temperature_table(state)
        return

    targets = await _boiler_targets_with_relays()
    if not targets:
        await state.echo("\n[menu] No boilers in the device mapping.\n")
        return

    setpoints = await state.get_setpoints()
    await state.echo("")
    for position, target in enumerate(targets, start=1):
        entry = setpoints.get(target.index)
        shown = f"{entry.temperature_c:.1f} °C" if entry else "not set"
        await state.echo(f"    {position}) {str(target):<10} {shown}")

    raw = await _prompt("  Which boiler? (empty = back): ")
    if not raw:
        await state.echo("")
        return
    try:
        target = targets[int(raw) - 1]
        if int(raw) < 1:
            raise IndexError
    except (ValueError, IndexError):
        await state.echo("[menu] Invalid selection.\n")
        return

    answer = await _prompt(
        f"  Temperature for {target} in °C "
        f"({MIN_SETPOINT_C:g}–{MAX_SETPOINT_C:g}, empty = cancel): "
    )
    if not answer:
        await state.echo("[menu] Cancelled.\n")
        return

    try:
        temperature = validate_setpoint(answer.strip())
    except SetpointError as exc:
        await state.echo(f"[menu] {exc}\n")
        return

    await state.set_setpoint(target.index, temperature)
    await state.log(
        f"[menu] Operator set {target} temperature to {temperature:.1f} °C",
        level=logging.WARNING,
    )
    await _after_temperature_change(
        state, target.index, temperature, f"{target} set to {temperature:.1f} °C"
    )


async def _clear_boiler_temperature(state: RuntimeState) -> None:
    setpoints = await state.get_setpoints()
    if not setpoints:
        await state.echo("\n[menu] No boiler has its own temperature set.\n")
        return

    indexes = sorted(setpoints)
    await state.echo("")
    for position, index in enumerate(indexes, start=1):
        await state.echo(
            f"    {position}) boiler {index}   {setpoints[index].temperature_c:.1f} °C"
        )

    raw = await _prompt("  Clear which? (empty = back): ")
    if not raw:
        await state.echo("")
        return
    try:
        position = int(raw)
        if not 1 <= position <= len(indexes):
            raise IndexError
    except (ValueError, IndexError):
        await state.echo("[menu] Invalid selection.\n")
        return

    index = indexes[position - 1]
    await state.clear_setpoint(index)
    await state.log(
        f"[menu] Operator cleared boiler {index}'s temperature — "
        "it now follows the device-wide limit",
        level=logging.WARNING,
    )
    await state.echo(
        f"[menu] Boiler {index} now follows the device-wide limit.\n"
    )
    # There is no protocol message for "no setpoint", so the server keeps the
    # last one it was told. Telemetry stops carrying setpoint_c, and the cut-out
    # here follows the device limit again — said plainly rather than implied.
    await state.echo(
        "[menu] Note: the server keeps the last temperature it was given; "
        "this only changes what this device holds the boiler at.\n"
    )
    for line in limit_guard.describe(
        await state.get_device_config(), await state.get_setpoints()
    ):
        await state.echo(f"  {line}")
    await state.echo("")

    from ws_client import push_device_state

    await push_device_state(state)


async def _publish_setpoint(state: RuntimeState, index: int, temperature: float) -> list[str]:
    """
    Offer a setpoint upstream and say what became of it.

    Returned as lines rather than echoed, because the panel shows them as a
    message and the terminal prints them with a ``[menu]`` prefix in front —
    the two faces of the same outcome, not two wordings of it.
    """
    from ws_client import publish_boiler_temperature, push_device_state

    await push_device_state(state)

    ack = await publish_boiler_temperature(state, index, temperature)

    if ack and ack.get("ok"):
        return [
            f"Published — boiler {index} is at",
            f"{ack.get('temperature_c')} °C, and the room's",
            "other devices have been told.",
        ]

    if ack:
        return [
            "The server refused it:",
            f"{ack.get('error') or ack}",
            "Still in force here; offered again",
            "on the next connection.",
        ]

    return [
        "No answer from the server.",
        "In force here; published when",
        "the device reconnects.",
    ]


async def _after_temperature_change(
    state: RuntimeState,
    index: int,
    temperature: float,
    summary: str,
) -> None:
    """Show the new cut-out, then offer the setpoint upstream."""
    await state.echo(f"[menu] {summary}.")
    for line in limit_guard.describe(
        await state.get_device_config(), await state.get_setpoints()
    ):
        await state.echo(f"  {line}")

    await state.echo("[menu] Publishing to the server ...")
    outcome = await _publish_setpoint(state, index, temperature)
    await state.echo("\n".join(f"[menu] {line}" for line in outcome) + "\n")


async def _temperature_menu(state: RuntimeState) -> None:
    """
    Set each boiler's target temperature.

    Per unit, because that is what DEVICE.md models: every boiler has its own
    ``desired_temperature_c``, and one circuit may want 55 °C while another
    wants 75. The device-wide config limits sit underneath as the fallback for
    a boiler with no temperature of its own.
    """
    view = screen()
    if view is not None:
        # Display mode: use the table-based UI directly
        await _temperature_table(state)
        return

    # Terminal fallback: keep the old submenu-based interaction
    while not state.shutdown.is_set():
        _set_context("Temperatures")
        await _show_temperature_status(state)

        try:
            choice = await _choose(
                state, "Temperatures", TEMPERATURE_ITEMS, TEMPERATURE_MENU
            )
        except EOFError:
            state.shutdown.set()
            return

        _set_context(_label_for(TEMPERATURE_ITEMS, choice, "Temperatures"))

        if choice == "1":
            await _set_boiler_temperature(state)
        elif choice == "2":
            await _clear_boiler_temperature(state)
        elif choice == "3":
            await _limits_menu(state)
        elif choice in ("0", "", BACK):
            await state.echo("")
            return
        else:
            await state.echo(f"\n[menu] Unknown option: {choice!r}\n")

        await _flush_page(state)


async def _show_limits_status(state: RuntimeState) -> None:
    document = config_store.document
    version = config_store.server_version

    if document is None:
        await state.echo(
            "\n[menu] No config yet — setting a limit starts one on this device."
        )
    else:
        await state.echo(
            f"\n[menu] {_t('Safety limits', 'حدود')} "
            f"({_t('published config', 'پیکربندی منتشرشده')} v{version})"
        )
        if config_store.is_locally_modified:
            await state.echo(
                f"        locally edited (revision {config_store.local_revision}, "
                f"on top of published v{version})"
            )

    for line in describe_limits(document):
        await state.echo(line)

    # The numbers that actually matter: what each boiler cuts and restores at,
    # which is not the configured value for a single-probe unit.
    await state.echo("")
    for line in limit_guard.describe(
        await state.get_device_config(), await state.get_setpoints()
    ):
        await state.echo(f"  {line}")


async def _apply_limit_edit(
    state: RuntimeState,
    document: dict,
    token: tuple[int, int],
    summary: str,
) -> None:
    if _limits_edit_token() != token:
        await state.echo(
            "[menu] The config changed while you were editing it — most likely "
            "the server published one. Nothing was saved; take another look and "
            "try again.\n"
        )
        return

    try:
        config = await config_store.apply_local(document, state)
    except ConfigError as exc:
        # Same validator the server's documents go through.
        await state.echo(f"[menu] Rejected: {exc}\n")
        return

    await state.echo(f"[menu] Limits updated — {summary}.")
    if not config_store.local_persisted:
        await state.echo(
            "[menu] WARNING: they could not be written to disk, so they will "
            "not survive a restart."
        )
    await state.echo(
        f"[menu] Now running locally edited limits on v{config.version} "
        f"(revision {config_store.local_revision}). The server's next publish "
        "replaces them."
    )
    # A limit change moves every boiler's cut-out, so show the result rather
    # than leaving the operator to work it out from the probe count.
    for line in limit_guard.describe(config, await state.get_setpoints()):
        await state.echo(f"  {line}")
    await state.echo("")

    await state.log(
        f"[menu] Operator changed a temperature limit: {summary} "
        f"(local revision {config_store.local_revision}, published v{config_store.server_version})",
        level=logging.WARNING,
    )

    from ws_client import push_device_state

    await push_device_state(state)


async def _change_limit(state: RuntimeState, field: str, label: str) -> None:
    token = _limits_edit_token()
    document = _limits_document_for_edit()
    current = (document.get("limits") or {}).get(field)
    shown = f"{float(current):.1f} °C" if isinstance(current, (int, float)) else "not set"

    raw = await _prompt(
        f"\n  {label} is {shown}.\n"
        f"  New value in °C ('-' or 'none' removes it, empty = cancel): "
    )
    if not raw:
        await state.echo("[menu] Cancelled.\n")
        return

    try:
        value = parse_temperature_text(raw, label)
        edited = set_limit(document, field, value)
    except ConfigEditError as exc:
        await state.echo(f"[menu] {exc}\n")
        return

    await _apply_limit_edit(
        state,
        edited,
        token,
        f"{label.lower()} {'removed' if value is None else f'{value:g} °C'}",
    )


async def _discard_local_limits(state: RuntimeState) -> None:
    if not config_store.is_locally_modified:
        await state.echo("\n[menu] There are no local limit edits to discard.\n")
        return

    answer = await _prompt(
        "\n  Discard local limits and go back to the published config? "
        "[1 or y = yes]: "
    )
    if not _is_yes(answer):
        await state.echo("[menu] Cancelled.\n")
        return

    config = await config_store.revert_local(state)
    if config is None:
        await state.echo(
            "[menu] Local limits discarded. Nothing has ever been published to "
            "this device, so there are now NO temperature limits and no "
            "over-temperature cut until the server sends one.\n"
        )
    else:
        await state.echo(f"[menu] Back to the published config v{config.version}.")
        for line in limit_guard.describe(config, await state.get_setpoints()):
            await state.echo(f"  {line}")
        await state.echo("")

    await state.log(
        "[menu] Operator discarded the local temperature limits", level=logging.WARNING
    )

    from ws_client import push_device_state

    await push_device_state(state)


async def _limits_menu(state: RuntimeState) -> None:
    """
    Change the temperature limits from the device.

    The same reasoning as options 7 and 9: an operator standing in the boiler
    room should not need the cloud to change how the boilers are protected.
    Edits hold until the server publishes a config, which then wins.
    """
    while not state.shutdown.is_set():
        _set_context("Safety limits")
        await _show_limits_status(state)

        try:
            choice = await _choose(state, "Safety limits", LIMITS_ITEMS, LIMITS_MENU)
        except EOFError:
            state.shutdown.set()
            return

        _set_context(_label_for(LIMITS_ITEMS, choice, "Safety limits"))

        if choice in ("1", "2", "3"):
            field, label = LIMIT_FIELDS[int(choice) - 1]
            await _change_limit(state, field, label)
        elif choice == "4":
            await _discard_local_limits(state)
        elif choice in ("0", "", BACK):
            await state.echo("")
            return
        else:
            await state.echo(f"\n[menu] Unknown option: {choice!r}\n")

        await _flush_page(state)


async def _handle_choice(state: RuntimeState, choice: str) -> None:
    if choice == "1":
        await _show_last_readings(state)
    elif choice == "2":
        await _relay_menu(state)
    elif choice == "3":
        await _mode_menu(state)
    elif choice == "4":
        await _temperature_menu(state)
    elif choice == "5":
        await _schedule_editor_menu(state)
    elif choice == "6":
        await _show_schedule(state)
    elif choice == "7":
        await _show_app_config(state)
    elif choice == "8":
        await _show_mapping(state)
    elif choice == "9":
        await _show_status(state)
    elif choice == "10":
        await _language_menu(state)
    elif choice == "0":
        await state.echo("\n[menu] Shutting down ...")
        state.shutdown.set()
    else:
        await state.echo(f"\n[menu] Unknown option: {choice!r}\n")



def menu_enabled(device=None) -> bool:
    """
    Whether to offer the interactive menu.

    A keyboard needs a terminal: under systemd there is none, ``input()`` would
    raise EOFError immediately, the menu would treat that as "operator chose
    quit", and the service would exit and be restarted forever.

    A keypad needs nothing — it *is* the terminal, and the installed device is
    precisely where there is no TTY and the keypad is the only way in. So the
    device says whether it depends on one. BOILERROOM_MENU=on/off overrides.
    """
    override = os.environ.get("BOILERROOM_MENU", "").strip().lower()
    if override in ("on", "1", "true", "yes"):
        return True
    if override in ("off", "0", "false", "no"):
        return False

    if device is not None and not getattr(device, "needs_tty", True):
        return True

    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


async def _start_input_device(state: RuntimeState):
    """
    Bring up whatever the menu reads from, falling back to the keyboard.

    A keypad that will not start — miswired, pins already claimed, no
    permission — must not take the agent down with it: the boilers are running
    a heating programme and the server can still drive them. It costs the
    operator the local menu, which is worth saying loudly and nothing more.
    """
    from mock_keypad import MockKeypad

    device = getattr(state, "keypad", None) or MockKeypad()

    try:
        await device.start()
        state.keypad_error = None
        return device
    except Exception as exc:
        # The panel gets the short form, the log gets the whole explanation.
        state.keypad_error = getattr(exc, "summary", None) or str(exc)
        await state.log(
            f"[menu] The {device.name} could not be started: {exc}",
            level=logging.ERROR,
        )

    if isinstance(device, MockKeypad):
        return device

    await state.log(
        "[menu] Falling back to the keyboard — nothing on this device can be "
        "controlled by hand until the keypad is fixed",
        level=logging.WARNING,
    )
    fallback = MockKeypad()
    await fallback.start()
    return fallback


# ---------------------------------------------------------------------------
# The graphical display
# ---------------------------------------------------------------------------


# On the main menu there is nothing to go back to, so the back key is given the
# screen an operator standing at the boiler wants most.
ROOT_LEGEND = (SCROLL_KEYS, (cap_for(ENTER), "Open"), (cap_for(CANCEL), "Status"))

# How often the status screen redraws itself when there is no keypad to ask for
# it. Matched to the sensor cadence: anything faster redraws the same numbers.
STATUS_REFRESH_SECONDS = 30.0


def _short_reason(reason: str) -> str:
    """A cut-out reason that fits twenty columns."""
    return {
        "water_over_temperature": "water too hot",
        "ambient_over_temperature": "room too hot",
    }.get(reason, reason.replace("_", " "))


def _role_reading(temperatures: dict, role: str) -> float | None:
    for sensor_id, value in temperatures.items():
        if value is None:
            continue
        if TEMPERATURE_SENSORS.get(sensor_id, {}).get("role") == role:
            return value
    return None


async def _status_lines(state: RuntimeState) -> list[str]:
    """
    What the room is doing, in twenty columns.

    The one screen worth designing around: it is what is on the panel when
    nobody is in a menu, and what an operator checks before touching anything.
    So it leads with the units — temperature, relay, and who is driving them —
    and a boiler held off by the limit guard says so where its mode would be,
    because that is the answer to "why is it not firing".
    """
    from limits_guard import _control_temperature

    snapshot = await state.get_snapshot()
    temperatures = snapshot["temperatures"]
    modes = await state.get_modes()
    blocks = await state.get_limit_blocks()
    setpoints = await state.get_setpoints()
    ws = await state.get_ws_status()
    relay_controller = state.relay_controller

    if ws["connected"]:
        link = "online"
    elif state.authenticated.is_set():
        link = "no socket"
    else:
        link = "offline"

    config_version, schedule_version = await state.get_active_versions()
    lines = [f"Link {link}", f"Cfg v{config_version}  Sched v{schedule_version}", ""]

    targets = _controllable_targets()
    if not targets:
        lines.append("No device mapping —")
        lines.append("waiting for the server")
        return lines

    for target in targets:
        relay_id = relay_for_target(target)
        tag = f"{target.type[0].upper()}{target.index}"

        temperature = (
            _control_temperature(f"pot_{target.index}", temperatures)
            if target.type == "boiler"
            else None
        )
        reading = f"{temperature:5.1f}{DEGREE}C" if temperature is not None else " " * 7

        if relay_controller is not None and relay_id is not None:
            switch = "ON " if relay_controller.get_state(relay_id) else "OFF"
        else:
            switch = "  ?"

        if target in blocks:
            mode = "CUT"
        else:
            mode = "MAN" if modes.get(target, "automatic") == "manual" else "AUT"

        lines.append(f"{tag:<3}{reading} {switch} {mode}")

    for target, reason in sorted(blocks.items(), key=str):
        lines.append(f"  {target} cut:")
        lines.append(f"    {_short_reason(reason)}")

    for index, entry in sorted(setpoints.items()):
        published = "" if entry.published else " (unsent)"
        lines.append(f"Target B{index} {entry.temperature_c:.1f}{DEGREE}C{published}")

    lines.append("")
    inside = _role_reading(temperatures, "environment_inside")
    outside = _role_reading(temperatures, "environment_outside")
    if inside is not None:
        lines.append(f"Inside  {inside:5.1f}{DEGREE}C")
    if outside is not None:
        lines.append(f"Outside {outside:5.1f}{DEGREE}C")

    for sensor_id, value in sorted(snapshot["gas"].items()):
        lines.append(f"Gas {sensor_id}   {value}")

    read_at = snapshot["read_at"]
    lines.append(f"Read {read_at:%H:%M:%S}Z" if read_at else "No readings yet")

    return lines


async def _show_status(state: RuntimeState) -> None:
    view = screen()
    if view is None:
        return
    _set_context("Status")
    await view.page("Status", await _status_lines(state))


async def _language_menu(state: RuntimeState) -> None:
    """
    Let the operator choose the language the panel speaks.

    Saved as soon as it is chosen rather than at shutdown, because shutdown is
    not guaranteed and a language lost on every unclean exit is a language the
    operator has to go through this screen to get back.

    The menu redraws in the new language by simply returning: every caller
    returns to the main loop, which draws the menu again through the same
    lookup that just changed.
    """
    choice = await _choose(
        state,
        "Language",
        LANGUAGE_ITEMS,
        LANGUAGE_MENU,
    )
    if choice == BACK or choice == "0":
        return

    wanted = {"1": language.ENGLISH, "2": language.PERSIAN}.get(choice)
    if wanted is None:
        await state.echo(f"\n[menu] Unknown option: {choice!r}\n")
        return

    applied = await language.save(wanted)
    _log.info("[menu] Panel language set to %s", applied)
    await state.echo(f"\n[menu] Panel language: {applied}\n")


async def _confirm_quit(state: RuntimeState) -> bool:
    """
    Check before stopping the agent.

    Not paranoia about one extra keypress: the selection wraps, so "Quit" is
    one press *up* from the top of the main menu. Nothing else on this device
    is one slip away from stopping the heating.
    """
    view = screen()
    if view is None:
        return True  # the terminal menu needs a typed "0", which is confirmation enough

    chosen = await view.select(
        "Stop the agent?",
        ["No, keep running", "Yes, stop it"],
        index=0,
    )
    return chosen == 1


async def _start_screen(state: RuntimeState, device) -> Screen | None:
    """
    Bring up the panel, tolerating not having one.

    Same rule as the keypad: a display that will not start costs the operator a
    screen and nothing else. The boilers are running a programme and the server
    can still drive them, so this logs loudly and hands the menu back to the
    terminal.
    """
    display = getattr(state, "display", None)
    if display is None or not getattr(display, "available", True):
        return None

    if not hasattr(device, "read_key"):
        # These screens move a selection; they need the keys one at a time.
        await state.log(
            f"[menu] The {device.name} cannot report single keys — the display "
            "stays dark and the menu stays on the terminal",
            level=logging.WARNING,
        )
        return None

    try:
        await display.start()
    except Exception as exc:
        await state.log(
            f"[menu] The {getattr(display, 'name', 'display')} could not be "
            f"started: {exc} — the menu falls back to the terminal",
            level=logging.ERROR,
        )
        return None

    for line in getattr(display, "describe", list)():
        await state.log(f"[menu] {line}")

    # Before the first frame is drawn, so the splash is already in the saved
    # language rather than flashing Persian and then changing.
    await language.load()
    await state.log(f"[menu] Panel language: {language.get()}")

    # Read fresh at every frame rather than sampled once here, so the icon in
    # the title bar follows a reconnect instead of showing whatever the link was
    # doing when the menu came up.
    view = Screen(display, device, echo=state.write, link=lambda: state.ws_connected)
    set_screen(view)
    await view.splash("Boiler room", ["", "  Starting up ...", ""])
    return view


async def _stop_screen(state: RuntimeState) -> None:
    view = screen()
    set_screen(None)
    state.stop_echo_capture()
    if view is None:
        return

    try:
        # Leave something true on the glass. A menu frozen where the operator
        # last left it reads as a working device.
        await view.splash("Boiler room", ["", "  Agent stopped.", ""])
        await view.display.close()
    except Exception as exc:
        await state.log(f"[menu] Display shutdown: {exc}", level=logging.DEBUG)


async def _run_status_display(state: RuntimeState, view: Screen) -> None:
    """
    Hold the status screen up when there is no way to answer a menu.

    The case this is for: a device whose keypad would not start. It still has a
    panel, and an operator in front of it should see temperatures rather than
    nothing. Longer status pages rotate a screenful at a time, since there is
    no key to scroll them with.
    """
    offset = 0

    while not state.shutdown.is_set():
        try:
            lines = await _status_lines(state)
            window = lines[offset : offset + 6] or lines[:6]
            await view.splash(
                "Status",
                window,
                legend=((cap_for(ENTER), "no keypad fitted"),),
            )
        except Exception as exc:
            await state.log(
                f"[menu] Could not draw the status screen: {exc}",
                level=logging.WARNING,
            )
            return

        offset = offset + 6 if offset + 6 < len(lines) else 0

        try:
            await asyncio.wait_for(state.shutdown.wait(), timeout=STATUS_REFRESH_SECONDS)
        except asyncio.TimeoutError:
            pass


# ---------------------------------------------------------------------------
# First boot: the device's credentials
# ---------------------------------------------------------------------------
#
# A device is provisioned with a numeric username and password. Putting them in
# .env means a laptop, an SD card reader or an SSH session at the point of
# installation; typing them at the panel means neither. So a device that has
# none asks for them, once, and keeps what it is given.
#
# Two rules make that safe to do at a keypad:
#
#   * nothing is written to .env until a login has actually succeeded with it,
#     because an installer eventually mistypes a digit and a device that cached
#     the typo is one nobody at the panel could correct
#   * a refusal is told apart from an unreachable server, so a typo asks again
#     and an outage does not


# How long to wait for the server's verdict before letting the operator go. A
# login gets 30 s to time out, and the auth task's first retry is 10 s after
# that; past this it is the network, not the answer.
CREDENTIAL_CHECK_SECONDS = 45.0

# Whether this operator has already been shown the wizard, so declining it is
# not answered by asking again immediately.
_credentials_asked = False


def _credentials_wanted(state: RuntimeState) -> bool:
    """Whether to put the credentials wizard in front of somebody now."""
    # credentials_needed means the auth task asked, which happens at first boot
    # and again whenever the server refuses something typed here. Checked
    # alongside the live state rather than instead of it: at boot the menu can
    # reach its loop before the auth task has had a chance to raise its hand.
    if state.credentials_needed.is_set():
        return True
    return not credentials_present() and not _credentials_asked


async def _credentials_wizard(state: RuntimeState) -> None:
    """
    Ask for the device's credentials and hand them to the auth task.

    Loops while the server keeps refusing what is typed, since that is exactly
    the case an installer needs to be able to correct on the spot. Leaves as
    soon as the credentials are accepted, or as soon as it is clear the server
    cannot be reached to ask — in which case they stay in memory, the agent
    keeps retrying in the background, and they are saved the moment one of
    those retries succeeds.
    """
    global _credentials_asked

    _credentials_asked = True

    # Every screen below is written to fit the panel's six rows, so each one is
    # a single key to move past rather than something to be paged through
    # before you are allowed to start typing.
    await _message(
        state,
        "Sign in",
        [
            "Not signed in yet.",
            "",
            "Type the username",
            "and password from",
            "provisioning.",
            "Digits only.",
        ],
    )

    while not state.shutdown.is_set():
        state.credentials_needed.clear()
        state.credentials_ready.clear()
        _set_context("Sign in")

        username = await _prompt("Device username: ")
        if not username:
            await _declined(state)
            return

        password = await _prompt("Device password: ", mask=True)
        if not password:
            await _declined(state)
            return

        try:
            set_credentials(username, password)
        except CredentialError as exc:
            await _message(
                state, "Sign in", ["Cannot use that:", "", str(exc)]
            )
            continue

        # The auth task is waiting on this, and it is the only thing here that
        # knows how to log in.
        state.credentials_ready.set()
        await _notice(state, "Sign in", ["", "  Checking with the", "  server ..."])

        outcome = await _credential_outcome(state)

        if outcome == "accepted":
            await state.log(f"[menu] Signed in as {username}, entered on the device")
            await _message(
                state,
                "Sign in",
                [
                    "Accepted.",
                    "",
                    "Saved here. You will",
                    "not be asked again.",
                ],
            )
            return

        if outcome == "rejected":
            await state.log(
                "[menu] The server refused the credentials entered on the device",
                level=logging.WARNING,
            )
            await _message(
                state,
                "Sign in",
                [
                    "Not accepted.",
                    "",
                    "Check the username",
                    "and password, then",
                    "try again.",
                ],
            )
            continue

        if outcome == "stopped":
            return

        await state.log(
            "[menu] Credentials entered on the device, but the server could not "
            "be reached to check them — retrying in the background",
            level=logging.WARNING,
        )
        await _message(
            state,
            "Sign in",
            [
                "No answer from the",
                "server yet.",
                "",
                "Kept and retried in",
                "the background;",
                "saved when it works.",
            ],
        )
        return


async def _declined(state: RuntimeState) -> None:
    await state.log(
        "[menu] Sign-in was cancelled — the device stays offline and runs from "
        "its cached schedule",
        level=logging.WARNING,
    )
    await _message(
        state,
        "Sign in",
        [
            "Left unsigned.",
            "",
            "The boilers keep to",
            "the cached schedule.",
            "Restart to be asked.",
        ],
    )


async def _credential_outcome(state: RuntimeState) -> str:
    """
    Wait for the auth task's verdict on what was just typed.

    ``accepted``, ``rejected``, ``stopped`` or ``unanswered`` — the last
    meaning the server could not be reached in time to say, which is not a
    reason to make somebody stand at the panel any longer.
    """
    waiters = {
        "accepted": asyncio.create_task(state.authenticated.wait()),
        "rejected": asyncio.create_task(state.credentials_needed.wait()),
        "stopped": asyncio.create_task(state.shutdown.wait()),
    }

    try:
        done, _ = await asyncio.wait(
            waiters.values(),
            timeout=CREDENTIAL_CHECK_SECONDS,
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        for task in waiters.values():
            if not task.done():
                task.cancel()

    # Checked in this order so a session that came up wins over anything else
    # that happened to land in the same moment.
    for name, task in waiters.items():
        if task in done:
            return name
    return "unanswered"


# ---------------------------------------------------------------------------
# The menu itself
# ---------------------------------------------------------------------------


# How long the panel holds the notice below before the menu takes over. Long
# enough to read twenty characters twice, short enough not to delay a device
# that somebody is standing in front of.
FALLBACK_NOTICE_SECONDS = 8.0


async def _report_input_fallback(state: RuntimeState, view: Screen | None) -> None:
    """
    Put a keypad that would not start on the glass.

    Otherwise this failure is silent where it matters most. The agent logs it
    and carries on with the keyboard, which the installed device does not have
    -- so the panel draws one frame and then waits forever for a key from a
    terminal that is not there. From in front of the device that is
    indistinguishable from a frozen display, and the log saying otherwise is on
    a machine nobody is looking at.
    """

    reason = getattr(state, "keypad_error", None)

    if not reason or view is None:
        return

    lines = ["Keypad did not start:", ""]
    lines.extend(wrap(reason, 20)[:2])
    lines.extend(["", "keypad.py --test"])

    await view.splash("Keypad", lines)

    try:
        await asyncio.wait_for(
            state.shutdown.wait(),
            timeout=FALLBACK_NOTICE_SECONDS,
        )
    except asyncio.TimeoutError:
        pass


async def run_control_menu(state: RuntimeState) -> None:
    global _state
    _state = state

    device = await _start_input_device(state)
    set_input_device(device)
    view = await _start_screen(state, device)
    await _report_input_fallback(state, view)

    if not menu_enabled(device):
        await state.log(
            "[menu] No terminal attached — control menu disabled "
            "(set BOILERROOM_MENU=on to force it)"
        )
        await device.close()
        if view is None:
            await state.shutdown.wait()
            return
        try:
            await _run_status_display(state, view)
        finally:
            await _stop_screen(state)
        return

    try:
        await _run_menu_loop(state, device)
    finally:
        await _stop_screen(state)
        await device.close()


async def _run_menu_loop(state: RuntimeState, device) -> None:
    view = screen()

    for line in getattr(device, "describe", list)():
        await state.echo(line)

    await state.echo(
        f"Control menu ready on the {device.name} — enter a number "
        "(sensor polling continues in background).\n"
    )

    if view is not None:
        # From here the menu's output belongs to the panel: captured and shown
        # a screenful at a time, rather than printed into a journal nobody is
        # reading while the operator looks at six blank rows.
        state.capture_echo()

    while not state.shutdown.is_set():
        # One handler for the whole step, because every screen below reads
        # keys and any of them can find the input device gone — stdin closed,
        # or the keypad shut down. Guarding only some of them is how a heating
        # agent ends on a traceback because a terminal was closed: this way it
        # sets shutdown and leaves through main's ordinary path, which releases
        # the relays and closes the database on the way out.
        try:
            # Before anything else: a device nobody has signed in cannot talk
            # to the server at all, so it is the first thing to put in front of
            # whoever is standing here.
            if _credentials_wanted(state):
                await _credentials_wizard(state)
                continue

            _set_context("Menu")
            choice = await _choose(
                state,
                "Menu",
                MAIN_ITEMS,
                MENU,
                hide_back=False,
                legend=ROOT_LEGEND,
            )

            if state.shutdown.is_set():
                break

            if choice == BACK:
                # Nothing sits above the main menu, so the back key is spent on
                # the screen worth reaching in one press from anywhere.
                await _show_status(state)
                continue

            if choice == "0" and not await _confirm_quit(state):
                continue

            _set_context(_label_for(MAIN_ITEMS, choice, "Menu"))
            await _handle_choice(state, choice)
            await _flush_page(state)
        except EOFError:
            state.shutdown.set()
            break
