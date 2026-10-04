"""
Anti-freeze protection.

A boiler room that is allowed to get cold is a boiler room that bursts. This
module holds every unit on whenever a probe drops to freezing, whatever the
programme says, and lets go once the room is warm again:

  * below ``BOILERROOM_ANTIFREEZE_ON_C`` (10 °C) — engage. Every unit in
    automatic mode is switched on, whether or not the schedule wants it, and
    whether or not it was on a moment ago.
  * above ``BOILERROOM_ANTIFREEZE_OFF_C`` (20 °C) — release. Units go back to
    whatever the schedule wants right now, so a programme that says "on" at that
    hour keeps them on and a programme that says "off" turns them off.

The ten-degree gap between the two is the whole point of the deadband: a probe
sitting at 10.4 °C must not switch the room on and off on alternate read
cycles. It is one latch for the whole room rather than one per unit, because
the thing that is freezing is the pipework and the collector, not a boiler —
one cold probe is enough reason to move water everywhere. Releasing needs every
watched probe to report *and* be clear: the probe that engaged the latch is
often the one that has since failed, and reading the room as warm off the
survivors is exactly how a freeze protection stands the heating down in a
freeze.

Manual mode is out of reach, in both directions. A unit the operator has taken
over is theirs: anti-freeze will not switch it on when the room freezes, and it
will not switch it off when the room warms. Leaving a manual unit alone is a
deliberate hole in the protection, so it is logged as a warning every time the
room is freezing and a manual unit is standing in the way — an operator has to
be able to see the cost of that choice.

What anti-freeze is not: it is not a cut-out, and the two do not collide. An
over-temperature cut still wins, because a cut is a decision already taken
about a boiler that is running away; ``limits_guard`` blocks that unit from
being switched on and anti-freeze leaves it blocked. The reverse is also true —
anti-freeze does not make ``boiler.turn_on`` fail, it holds the unit on, so a
command to stop heating during a freeze is undone on the next read cycle and
logged. Water moving past a freezing sensor is what protects the pipe; a
temperature limit protects the boiler.

Which probes count: every water probe — inlet, outlet and body — and neither of
the environment sensors. That is the installation's choice and it has a cost
worth stating plainly: a room whose only probes are the ambient ones has no
anti-freeze at all, and no amount of freezing will show it. That case is
warned about once, loudly, rather than sitting silent like the equivalent hole
in the over-temperature protection.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
from pathlib import Path

from config import RELAYS, TEMPERATURE_SENSORS
from errors_client import build_error, schedule_post_errors
from json_store import read_json, write_json
from load_env import env_path
from logging_setup import get_logger
from mapping_schema import api_sensor_id
from schedule_runner import Target, relay_for_target, schedule_runner

_log = get_logger("antifreeze")


class AntiFreezeError(ValueError):
    """Raised when a pair of thresholds cannot be used."""


def _grid() -> list[float]:
    """Every whole degree the panel offers, from the bottom of the range up."""
    steps = int(round((LIST_MAX_C - LIST_MIN_C) / LIST_STEP_C))
    return [round(LIST_MIN_C + index * LIST_STEP_C, 1) for index in range(steps + 1)]


def choices(field: str, on_c: float, off_c: float) -> list[float]:
    """
    The temperatures one threshold may be set to, lowest first.

    The list is **cut by the other threshold**, so a pair that would engage and
    release on the same reading cannot be built by scrolling: the engage list
    stops a degree below the release temperature and the release list starts a
    degree above it. It is the same rule :func:`validate_thresholds` enforces on
    a typed value — here it is enforced by not offering the value in the first
    place, which is the difference between a rule and a rule with an exception.

    The value already in force is always in the list, even when the environment
    gave it a tenth the grid has no row for. It is running; the operator has to
    be able to see it, and to be able to leave it alone.
    """
    current = round(on_c if field == "on_c" else off_c, 1)

    if field == "on_c":
        top = min(LIST_MAX_C, round(off_c - MIN_DEADBAND_C, 1))
        values = [value for value in _grid() if value <= top]
    else:
        bottom = max(LIST_MIN_C, round(on_c + MIN_DEADBAND_C, 1))
        values = [value for value in _grid() if value >= bottom]

    if current not in values:
        values.append(current)
        values.sort()

    return values


async def _clear_file(path: Path) -> None:
    """Remove the saved thresholds, as schedule_editor does its override file."""
    await asyncio.to_thread(path.unlink, missing_ok=True)


def validate_thresholds(on_c: float, off_c: float) -> tuple[float, float]:
    """
    Check a pair, and round it to the panel's one decimal.

    Both numbers are refused rather than clamped: an operator who types 400 on a
    keypad is not asking for the ceiling to be clamped to 100, and quietly
    answering with something else is how a protection ends up set to a value
    nobody chose.
    """
    for name, value in (("on", on_c), ("off", off_c)):
        if not MIN_THRESHOLD_C <= value <= MAX_THRESHOLD_C:
            raise AntiFreezeError(
                f"{name} must be between {MIN_THRESHOLD_C:g} and "
                f"{MAX_THRESHOLD_C:g} °C, not {value:g}"
            )

    if off_c - on_c < MIN_DEADBAND_C:
        raise AntiFreezeError(
            f"the release temperature must be at least {MIN_DEADBAND_C:g} °C "
            f"above the engage temperature ({off_c:g} is not above {on_c:g}) — "
            "closer than that and the latch switches the room on and off every "
            "read cycle"
        )

    return round(float(on_c), 1), round(float(off_c), 1)

# The probes that can freeze. The environment sensors are deliberately not in
# this list: what has to be kept moving is the water, and an ambient probe in a
# cold room is cold whether or not anything is at risk of freezing.
WATCHED_ROLES = (
    "boiler_output_water",
    "boiler_input_water",
    "boiler_body",
)

# Relay role -> the unit kind it drives. Only the two kinds that appear as
# schedule targets: a circulation pump has no mode of its own, so nothing could
# say whether it was meant to follow the schedule or the operator.
RELAY_TARGET_KIND = {"pot": "boiler", "pump": "pump"}

# The two numbers an operator can change from the panel. The environment
# variables are the defaults a fresh card runs on; a value set on the device is
# kept in antifreeze_local.json and wins over them until it is discarded.
DEFAULT_ON_C = float(os.environ.get("BOILERROOM_ANTIFREEZE_ON_C", "10.0"))
DEFAULT_OFF_C = float(os.environ.get("BOILERROOM_ANTIFREEZE_OFF_C", "20.0"))

# How far apart the two have to be, and how far either may travel. The band is
# the whole reason the latch does not chatter, so a pair closer together than
# this is refused rather than accepted and left to oscillate: a card that
# switches a room on and off every read cycle is worse than one with no
# anti-freeze, because it looks like protection.
MIN_DEADBAND_C = 1.0
MIN_THRESHOLD_C = -50.0
MAX_THRESHOLD_C = 100.0

# The rows the panel offers for each threshold. A degree at a time across the
# range any water system could plausibly want a freeze point in — not the whole
# −50…100 the validator accepts, because a list an operator has to scroll
# through a hundred times to reach the end is a list nobody scrolls.
LIST_STEP_C = 1.0
LIST_MIN_C = -20.0
LIST_MAX_C = 40.0

ANTIFREEZE_PATH = env_path("BOILERROOM_ANTIFREEZE_LOCAL", "antifreeze_local.json")

if DEFAULT_OFF_C <= DEFAULT_ON_C:
    # Read once at import, so a card that is restarted with this set picks the
    # change up. Anything else would be a deadband of the wrong sign: the latch
    # would engage and release on the same reading and chatter the relays.
    _log.warning(
        "Anti-freeze thresholds are inverted: off (%.1f C) is not above on "
        "(%.1f C) — anti-freeze will not run",
        DEFAULT_OFF_C,
        DEFAULT_ON_C,
    )

REASON = "anti_freeze"
EVENT_ENGAGED = "anti_freeze_engaged"
EVENT_RELEASED = "anti_freeze_released"

# A mode of "manual" means the operator owns the unit. Anything else —
# including a unit nobody has set a mode for — follows the schedule.
MANUAL = "manual"


def _watched_readings(
    temperatures: dict[int, float | None],
) -> list[tuple[int, float, str]]:
    """``(sensor_id, value, name)`` for every readable water probe, in sensor order."""
    readings: list[tuple[int, float, str]] = []
    for sensor_id, value in sorted(temperatures.items()):
        if value is None:
            continue
        cfg = TEMPERATURE_SENSORS.get(sensor_id, {})
        if cfg.get("role") not in WATCHED_ROLES:
            continue
        readings.append(
            (sensor_id, float(value), cfg.get("name", f"Sensor {sensor_id}"))
        )
    return readings


def _watched_sensor_ids() -> list[int]:
    """Every probe the mapping puts on the water, whether or not it reads."""
    return [
        sensor_id
        for sensor_id, cfg in sorted(TEMPERATURE_SENSORS.items())
        if cfg.get("role") in WATCHED_ROLES
    ]


def _mapped_targets() -> list[Target]:
    """Every boiler and pump the device mapping gives a relay."""
    targets: list[Target] = []
    for cfg in RELAYS.values():
        kind = RELAY_TARGET_KIND.get(cfg.get("role"))
        unit = cfg.get("unit") or ""
        if kind is None or not unit.startswith("pot_"):
            continue
        try:
            targets.append(Target(kind, int(unit.split("_", 1)[1])))
        except ValueError:
            continue
    return sorted(set(targets))


class AntiFreezeGuard:
    """Holds automatic units on while the room is below freezing."""

    def __init__(self) -> None:
        self._engaged = False
        self._trigger: tuple[int, float, str] | None = None
        # Only the units this guard actually switched on. A unit it never
        # touched is never one it puts out again, so a relay left on by a
        # command or by the programme is left to whoever turned it on.
        self._held: set[Target] = set()
        self._manual_warned: set[Target] = set()
        self._unwatched_warned = False

        # The thresholds in force. The environment supplies the defaults; a pair
        # set on the device replaces them and is written to disk, because a
        # freeze protection that forgets itself over a power cut is worse than
        # one nobody could change.
        self._on_c = DEFAULT_ON_C
        self._off_c = DEFAULT_OFF_C
        self._is_local = False
        self._persisted = True

    # -- thresholds ---------------------------------------------------------

    @property
    def on_c(self) -> float:
        """The temperature below which the latch engages (°C)."""
        return self._on_c

    @property
    def off_c(self) -> float:
        """The temperature above which it releases (°C)."""
        return self._off_c

    @property
    def is_local(self) -> bool:
        """Whether the thresholds in force were set on this device."""
        return self._is_local

    @property
    def local_persisted(self) -> bool:
        """False when the last change could not be written to disk."""
        return self._persisted

    async def set_thresholds(self, on_c: float, off_c: float, *, log=None) -> None:
        """
        Adopt a new pair and remember it across a restart.

        Raises :class:`AntiFreezeError` for a pair that cannot be used, leaving
        the running values untouched — a refused edit must not half-apply.
        """
        on_c, off_c = validate_thresholds(on_c, off_c)
        self._on_c, self._off_c = on_c, off_c
        self._is_local = True
        await self._save(log=log)

        if log is not None:
            await log(
                f"[antifreeze] Operator set the thresholds to on below "
                f"{on_c:.1f} °C, off above {off_c:.1f} °C",
                level=logging.WARNING,
            )

    async def restore_defaults(self, *, log=None) -> None:
        """
        Go back to the thresholds the environment supplies.

        Validated like any other pair, because the environment is not a safer
        place to put a bad number than the keypad is: an installation with
        ``ON_C`` above ``OFF_C`` would otherwise be one keystroke away from a
        latch that cannot run.
        """
        on_c, off_c = validate_thresholds(DEFAULT_ON_C, DEFAULT_OFF_C)
        self._on_c, self._off_c = on_c, off_c
        self._is_local = False
        await self._save(log=log)

        if log is not None:
            await log(
                f"[antifreeze] Operator restored the default thresholds: on below "
                f"{self._on_c:.1f} °C, off above {self._off_c:.1f} °C",
                level=logging.WARNING,
            )

    async def load_local(self, state=None) -> None:
        """
        Adopt the pair an operator saved, at boot.

        A missing or unusable file leaves the environment defaults in force: a
        corrupt threshold file must not stop the agent starting, and the defaults
        are a working protection even if they are not the ones that were chosen.
        """
        payload = await read_json(ANTIFREEZE_PATH)
        if not payload:
            return

        raw_on = payload.get("on_c")
        raw_off = payload.get("off_c")
        try:
            on_c = float(raw_on)
            off_c = float(raw_off)
        except (TypeError, ValueError):
            message = f"ignoring unusable anti-freeze cache {payload!r}"
            if state is not None:
                await state.log(f"[antifreeze] {message}", level=logging.WARNING)
            else:
                _log.warning("%s", message)
            return

        try:
            on_c, off_c = validate_thresholds(on_c, off_c)
        except AntiFreezeError as exc:
            message = f"ignoring saved anti-freeze thresholds: {exc}"
            if state is not None:
                await state.log(f"[antifreeze] {message}", level=logging.WARNING)
            else:
                _log.warning("%s", message)
            return

        self._on_c, self._off_c = on_c, off_c
        self._is_local = True

        if state is not None:
            await state.log(
                f"[antifreeze] Restored the thresholds set on this device: on "
                f"below {on_c:.1f} °C, off above {off_c:.1f} °C"
            )

    async def _save(self, *, log=None) -> None:
        if not self._is_local:
            # Defaults in force: the file goes, so a card that was changed and
            # then put back does not keep re-reading the old pair at every boot.
            try:
                await _clear_file(ANTIFREEZE_PATH)
            except Exception as exc:
                self._persisted = False
                message = f"could not remove the saved thresholds: {exc}"
                if log is not None:
                    await log(f"[antifreeze] {message}", level=logging.WARNING)
                else:
                    _log.warning("%s", message)
            else:
                self._persisted = True
            return

        payload = {
            "saved_at": datetime.datetime.now(datetime.UTC)
            .isoformat(timespec="seconds")
            .replace("+00:00", "Z"),
            "on_c": self._on_c,
            "off_c": self._off_c,
        }
        try:
            await write_json(ANTIFREEZE_PATH, payload)
        except Exception as exc:
            # The change is already driving the relays; a card that cannot be
            # written must not undo it. It simply will not survive a restart.
            self._persisted = False
            message = (
                f"thresholds are in effect but could not be saved: {exc}"
            )
            if log is not None:
                await log(f"[antifreeze] {message}", level=logging.WARNING)
            else:
                _log.warning("%s", message)
        else:
            self._persisted = True

    @property
    def engaged(self) -> bool:
        return self._engaged

    @property
    def trigger(self) -> tuple[int, float, str] | None:
        """``(sensor_id, value, name)`` of the probe that engaged the latch."""
        return self._trigger

    @property
    def held(self) -> frozenset[Target]:
        """The units anti-freeze is currently holding on."""
        return frozenset(self._held)

    def holds(self, target: Target) -> bool:
        """
        Whether anti-freeze is holding this unit on right now.

        Read by ``schedule_runner.evaluate``: while a unit is held, the
        programme must not switch it off, which without this check the
        schedule's own tick would do a minute after the latch engaged.
        """
        return target in self._held

    async def check(
        self,
        state,
        temperatures: dict[int, float | None],
    ) -> None:
        """Engage or release the latch against the latest readings."""
        relay_controller = state.relay_controller
        if relay_controller is None:
            return

        readings = _watched_readings(temperatures)

        if not readings:
            # Nothing to judge. The latch stays as it is: an unreadable probe is
            # not a warm probe, and dropping the latch here would let a wiring
            # fault stand the heating down in a freeze.
            await self._report_unwatched(state)
            return

        if self._unwatched_warned:
            self._unwatched_warned = False
            await state.log("[antifreeze] Water probes readable again — protection active")

        if not self._engaged:
            coldest = min(readings, key=lambda reading: reading[1])
            if coldest[1] < self._on_c:
                await self._engage(state, relay_controller, coldest)
            return

        # Re-asserted every cycle, not once at the latch. A command to stop
        # heating during a freeze is undone here rather than being left to the
        # operator's judgement, because the judgement has already been made.
        await self._assert_held(state, relay_controller)

        # Released only when every watched probe is clear. One probe sitting at
        # 11 °C holds the room on; that is the cold pipe, not a stale reading.
        #
        # "Every" includes *reporting*. A probe that has gone unreadable cannot
        # clear the latch, because the probe that engaged it is very often the
        # one that has failed — and reading the room as warm off the survivors
        # is how a freeze protection stands the heating down in a freeze. The
        # missing reading is reported as a sensor fault by the error reporter,
        # so nothing is lost by waiting for it here.
        if len(readings) == len(_watched_sensor_ids()) and all(
            value > self._off_c for _, value, _ in readings
        ):
            await self._release(state, relay_controller, readings)

    # -- engaging -----------------------------------------------------------

    async def _engage(
        self,
        state,
        relay_controller,
        coldest: tuple[int, float, str],
    ) -> None:
        sensor_id, value, name = coldest
        self._engaged = True
        self._trigger = coldest
        self._manual_warned = set()

        await state.log(
            f"[antifreeze] ENGAGED — {name} at {value:.1f} °C is below "
            f"{self._on_c:.1f} °C: every automatic unit is being switched on",
            level=logging.WARNING,
        )

        schedule_post_errors(
            build_error(
                code=EVENT_ENGAGED,
                message=(
                    f"Anti-freeze engaged: {name} at {value:.1f} °C is below "
                    f"{self._on_c:.1f} °C. Automatic units are being held on."
                ),
                severity="warning",
                device_state="degraded",
                sensor_id=api_sensor_id(
                    "temp", sensor_id, TEMPERATURE_SENSORS.get(sensor_id, {})
                ),
            ),
            log=state.log,
        )

        await self._assert_held(state, relay_controller)

    async def _assert_held(self, state, relay_controller) -> None:
        """Switch every eligible unit on, and keep the held ones on."""
        held: set[Target] = set()

        for target in _mapped_targets():
            relay_id = relay_for_target(target)
            if relay_id is None:
                continue

            if await state.get_mode(target) == MANUAL:
                # Rule 4: never touched, in either direction.
                if target not in self._manual_warned:
                    self._manual_warned.add(target)
                    await state.log(
                        f"[antifreeze] {target} is in manual mode and will NOT be "
                        "switched on to protect it — the operator owns it",
                        level=logging.WARNING,
                    )
                continue

            # An over-temperature cut outranks this: that unit has already been
            # judged to be running away and is held off deliberately.
            if await state.is_limit_blocked(target):
                continue

            if target in self._held and relay_controller.get_state(relay_id):
                held.add(target)
                continue

            if not relay_controller.get_state(relay_id):
                await relay_controller.turn_on(relay_id)
                name = RELAYS.get(relay_id, {}).get("name", f"Relay {relay_id}")
                await state.log(
                    f"[antifreeze] {name} (relay {relay_id}) ON for {target} — "
                    "anti-freeze, overriding the programme",
                    level=logging.WARNING,
                )
                await state.notify_state_change(
                    f"relay {relay_id} on (anti-freeze)"
                )

            held.add(target)

        self._held = held

    # -- releasing ----------------------------------------------------------

    async def _release(
        self,
        state,
        relay_controller,
        readings: list[tuple[int, float, str]],
    ) -> None:
        self._engaged = False
        self._trigger = None
        self._manual_warned = set()

        held = sorted(self._held)
        self._held = set()

        schedule = schedule_runner.schedule
        wanted = schedule.desired_states() if schedule is not None else {}
        warmest = max(readings, key=lambda reading: reading[1])
        switched = False

        for target in held:
            relay_id = relay_for_target(target)
            if relay_id is None:
                continue
            name = RELAYS.get(relay_id, {}).get("name", f"Relay {relay_id}")

            if await state.get_mode(target) == MANUAL:
                # Switched on by anti-freeze, then handed to the operator
                # mid-freeze: it stays on. Rule 4 again, and the reason it is
                # worth a warning rather than a shrug.
                await state.log(
                    f"[antifreeze] {target} went to manual mode while the room was "
                    f"freezing and is still on — {name} left exactly as it is",
                    level=logging.WARNING,
                )
                continue

            if await state.is_limit_blocked(target):
                continue

            if wanted.get(target):
                # Rule 2: the programme wants it on at this hour, so it stays on.
                # forget() hands the decision back to the schedule, which will
                # switch it on if anything has turned it off since.
                if relay_controller.get_state(relay_id):
                    await state.log(
                        f"[antifreeze] {target} stays on — the schedule wants it on"
                    )
                    continue
                schedule_runner.forget(target)
                switched = True
                continue

            if relay_controller.get_state(relay_id):
                await relay_controller.turn_off(relay_id)
                switched = True
                await state.log(
                    f"[antifreeze] {name} (relay {relay_id}) OFF for {target} — "
                    f"{warmest[2]} at {warmest[1]:.1f} °C, and the schedule wants it off"
                )
                await state.notify_state_change(
                    f"relay {relay_id} off (anti-freeze cleared)"
                )

        await state.log(
            f"[antifreeze] RELEASED — every water probe above {self._off_c:.1f} °C "
            f"(coldest was {warmest[2]} at {warmest[1]:.1f} °C)"
        )

        schedule_post_errors(
            build_error(
                code=EVENT_RELEASED,
                message=(
                    f"Anti-freeze released: all water probes above {self._off_c:.1f} °C. "
                    f"Coldest is {warmest[2]} at {warmest[1]:.1f} °C."
                ),
                severity="info",
                device_state="ok",
            ),
            log=state.log,
        )

        if switched:
            # Let the programme re-assert whatever it wants for the rest of the
            # room. The units above were handled here; this is for the ones it
            # had left alone.
            await schedule_runner.evaluate(state)

    # -- reporting ----------------------------------------------------------

    async def _report_unwatched(self, state) -> None:
        if self._unwatched_warned:
            return
        self._unwatched_warned = True
        await state.log(
            "[antifreeze] NO anti-freeze protection — the mapping puts no readable "
            "water probe (input, output or body) on this device, so nothing here "
            "can detect a freeze",
            level=logging.WARNING,
        )

    def status_line(self) -> str | None:
        """
        One line for the status screen, or None when nothing is happening.

        The held units are written as the same ``B1``/``P2`` tags the unit rows
        above use, so the line needs no translation and reads as a continuation
        of them rather than a sentence in a different language.
        """
        if not self._engaged:
            return None
        trigger = self._trigger
        detail = f"{trigger[1]:.1f}°C" if trigger is not None else ""
        tags = ",".join(
            f"{target.type[0].upper()}{target.index}" for target in sorted(self._held)
        )
        return f"Freeze ON {detail} {tags}".rstrip()

    def describe(self) -> list[str]:
        """Human-readable summary for the control menu."""
        probes = sum(
            1
            for cfg in TEMPERATURE_SENSORS.values()
            if cfg.get("role") in WATCHED_ROLES
        )
        lines = [
            f"Anti-freeze: on below {self._on_c:.1f} °C, off above "
            f"{self._off_c:.1f} °C "
            f"({probes} water probe{'s' if probes != 1 else ''})"
        ]
        if self._is_local:
            lines[-1] += "  [set on this device]"

        if not probes:
            lines.append("  NO protection — no water probe to watch")
            return lines

        if self._engaged:
            trigger = self._trigger
            if trigger is not None:
                lines.append(f"  ENGAGED by {trigger[2]} at {trigger[1]:.1f} °C")
            held = sorted(self._held)
            # The same B1/P2 tags the status screen uses for its unit rows, so
            # this line needs no translation and reads as a continuation of them.
            lines.append(
                "  holding on: "
                + (
                    ", ".join(
                        f"{target.type[0].upper()}{target.index}" for target in held
                    )
                    or "nothing"
                )
            )
        else:
            lines.append("  not engaged")

        return lines


anti_freeze = AntiFreezeGuard()
