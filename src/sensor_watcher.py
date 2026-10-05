"""
The 1-Wire bus, watched.

Thermal probes appear and disappear from /sys/bus/w1/devices as they
are fitted and pulled out, and the directory says nothing about the
order they arrived in: it is sorted, and a reboot forgets it anyway.
Which probe was connected first is what an installer needs while
wiring a room — the server record matches probes to units by
physical_id, and "the third probe I fitted" is how a room is actually
described.

So the directory is polled, and every thermometer on the bus (an entry
carrying a w1_slave file — the interface that tells a DS18B20 from a
bus master or an EEPROM) is remembered in the order it was first seen,
with the times it was first and last seen. The order is cached, so it
survives a restart: a probe fitted before the agent was installed is
still first.

In simulated hardware there is no bus to watch, and the watcher says so
rather than watching a directory that is not there.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from config import ONE_WIRE_PATH
from json_store import read_json, write_json_sync
from load_env import env_path
from logging_setup import get_logger
from runtime_state import RuntimeState

_log = get_logger("sensor")

# How often to look at the bus. A probe is either there or not, so a
# few seconds is plenty to catch a plug; the cache is rewritten only
# when something actually changed, not on every scan.
try:
    SENSOR_WATCH_INTERVAL = max(
        1.0, float(os.environ.get("BOILERROOM_SENSOR_WATCH_INTERVAL", "5"))
    )
except ValueError:
    SENSOR_WATCH_INTERVAL = 5.0

# The connection order outlives the agent: a probe fitted before this
# card was written is still first after a reboot.
SENSOR_WATCH_CACHE_PATH = env_path(
    "BOILERROOM_SENSOR_WATCH_CACHE", "sensor_watch_cache.json"
)

# The bus master the kernel lists alongside the sensors, and the file
# that marks an entry as a thermometer rather than any other 1-Wire
# device.
BUS_MASTER_PREFIX = "w1_bus_master"
THERMOMETER_FILE = "w1_slave"


def _now() -> datetime.datetime:
    return datetime.datetime.now(datetime.UTC)


def _iso(moment: datetime.datetime) -> str:
    return moment.isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso(raw: str) -> datetime.datetime | None:
    try:
        return datetime.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class SensorEntry:
    """One thermal sensor: when it was first and last seen, and whether it is there now."""

    sensor_id: str
    first_seen: datetime.datetime
    last_seen: datetime.datetime
    connected: bool = True


class SensorWatcher:
    """
    Remembers every thermal sensor the bus has ever seen, in the order it was connected.

    ``sensors`` is a dict on purpose: insertion order is the connection
    order, and re-fitting a probe updates its place in the bus, not its
    place in the history.
    """

    def __init__(self) -> None:
        self.sensors: dict[str, SensorEntry] = {}
        # None until the bus has been looked at, True when the directory
        # is there, False when it is not. The menu tells the difference
        # between "not checked yet" and "no bus".
        self.bus_available: bool | None = None
        # Set before the task starts: simulated hardware has no bus to
        # watch, and saying so beats watching a directory that is not
        # there.
        self.mock = False
        self.interval = SENSOR_WATCH_INTERVAL
        # Set by a scan that changed something, so the cache is written
        # on the event and not on the tick.
        self._dirty = False

    async def load(self) -> None:
        """Restore the connection order from the cache."""
        payload = await read_json(SENSOR_WATCH_CACHE_PATH)
        if not payload:
            return

        raw_sensors = payload.get("sensors")
        if not isinstance(raw_sensors, list):
            _log.warning("Sensor watch cache has no 'sensors' list — ignoring it")
            return

        for raw in raw_sensors:
            if not isinstance(raw, dict):
                continue
            sensor_id = raw.get("id")
            first_seen = (
                _parse_iso(raw["first_seen"])
                if isinstance(raw.get("first_seen"), str)
                else None
            )
            if not isinstance(sensor_id, str) or first_seen is None:
                _log.warning("Ignoring unusable sensor watch entry %r", raw)
                continue
            last_seen = (
                _parse_iso(raw["last_seen"])
                if isinstance(raw.get("last_seen"), str)
                else None
            )
            # Nothing is connected until the bus says so: the cache
            # describes history, not the present.
            self.sensors[sensor_id] = SensorEntry(
                sensor_id, first_seen, last_seen or first_seen, connected=False
            )

    async def scan(self) -> None:
        """One look at the bus, off the event loop."""
        await asyncio.to_thread(self._scan_sync)

    def _scan_sync(self) -> None:
        now = _now()
        try:
            names = os.listdir(ONE_WIRE_PATH)
        except OSError:
            self.bus_available = False
            return

        self.bus_available = True
        base = Path(ONE_WIRE_PATH)
        seen: set[str] = set()
        for name in names:
            if name.startswith(BUS_MASTER_PREFIX):
                continue
            if not (base / name / THERMOMETER_FILE).exists():
                continue
            seen.add(name)
            entry = self.sensors.get(name)
            if entry is None:
                self.sensors[name] = SensorEntry(name, now, now)
                self._dirty = True
                _log.info("[sensor] Thermal sensor %s connected", name)
            else:
                if not entry.connected:
                    entry.connected = True
                    self._dirty = True
                    _log.info("[sensor] Thermal sensor %s reconnected", name)
                entry.last_seen = now

        for sensor_id, entry in self.sensors.items():
            if sensor_id not in seen and entry.connected:
                entry.connected = False
                self._dirty = True
                _log.info(
                    "[sensor] Thermal sensor %s disconnected", sensor_id
                )

        if self._dirty:
            self._dirty = False
            self._save_sync()

    def _save_sync(self) -> None:
        """Write the connection order. Runs on the scan's thread."""
        payload = {
            "saved_at": _iso(_now()),
            "sensors": [
                {
                    "id": entry.sensor_id,
                    "first_seen": _iso(entry.first_seen),
                    "last_seen": _iso(entry.last_seen),
                    "connected": entry.connected,
                }
                for entry in self.sensors.values()
            ],
        }
        try:
            write_json_sync(SENSOR_WATCH_CACHE_PATH, payload)
        except OSError as exc:
            _log.warning("[sensor] Could not write the sensor watch cache: %s", exc)

    async def run(self, state: RuntimeState) -> None:
        """Watch the bus until shutdown."""
        if self.mock:
            self.bus_available = False
            await state.log("[sensor] Simulated hardware — no 1-Wire bus to watch")

        while not state.shutdown.is_set():
            if not self.mock:
                try:
                    await self.scan()
                except Exception as exc:
                    await state.log(
                        f"[sensor] Watch failed: {exc}", level=logging.ERROR
                    )

            try:
                await asyncio.wait_for(
                    state.shutdown.wait(), timeout=self.interval
                )
            except asyncio.TimeoutError:
                pass

    def rows(self) -> list[str]:
        """
        The connection order as the panel draws it: one row per sensor.

        The order number and the sensor id are the whole of it — a row
        that has had its number cut off has thrown away the only part
        of it that is news. A sensor that is not on the bus any more
        carries a ``~``, because whether it is still there is the one
        other thing an operator wants at a glance, and there is no room
        on a twenty-column row to spell it out.
        """
        return [
            f"{order}  {entry.sensor_id}" + ("" if entry.connected else "~")
            for order, entry in enumerate(self.sensors.values(), start=1)
        ]

    def describe(self) -> list[str]:
        """The connection order as the terminal prints it."""
        if self.bus_available is False:
            return [
                f"[menu] No 1-Wire bus at {ONE_WIRE_PATH}",
                "[menu] Thermal sensor IDs cannot be watched.",
                "",
            ]

        connected = sum(1 for entry in self.sensors.values() if entry.connected)
        lines = [
            "[menu] Thermal sensor IDs — "
            f"{len(self.sensors)} seen, {connected} connected, "
            "in the order they were connected",
        ]
        if not self.sensors:
            lines.append("  No thermal sensors on the bus yet.")
        for order, entry in enumerate(self.sensors.values(), start=1):
            state = (
                f"connected since {_iso(entry.first_seen)}"
                if entry.connected
                else f"disconnected — last seen {_iso(entry.last_seen)}"
            )
            lines.append(f"  {order}) {entry.sensor_id}  {state}")
        lines.append("")
        return lines


# The one watcher, like the schedule runner and the limit guard: the
# menu reads it, and main starts it.
sensor_watcher = SensorWatcher()
