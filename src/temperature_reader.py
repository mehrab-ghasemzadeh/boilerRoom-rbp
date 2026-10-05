import asyncio
import subprocess
import time
from pathlib import Path

from config import ONE_WIRE_GPIO, ONE_WIRE_PATH, TEMPERATURE_SENSORS
from logging_setup import get_logger

_log = get_logger("temperature")

# How many times one probe is asked before it is reported unavailable, and how
# long to wait between attempts. A DS18B20 takes about 750 ms to convert, and
# the kernel asks it for a conversion as soon as the file is opened, so the
# first read of a freshly fitted probe legitimately comes back empty.
READ_ATTEMPTS = 5
READ_RETRY_SECONDS = 0.15

# What the DS18B20 in this design can physically report. A reading outside it
# is a conversion caught mid-write rather than a temperature.
MIN_MILLIDEGREES = -55_000
MAX_MILLIDEGREES = 125_000


class TemperatureReader:
    """Reads every probe the device mapping names, once per sensor cycle.

    Every failure mode here resolves to ``None`` for that probe — a missing
    reading, which is what the rest of the agent already means by it. A probe
    being fitted, re-seated or pulled while this runs is an ordinary event in a
    boiler room, and none of the states a 1-Wire probe passes through on its way
    in or out is allowed to stop the agent.
    """

    def __init__(self):
        self.base_path = Path(ONE_WIRE_PATH)
        # Faults already logged, so a probe that has gone is logged once rather
        # than every cycle. Cleared when it reads again, so a probe that comes
        # back is reported as coming back.
        self._missing_warned: set[str] = set()

    async def start(self):
        # The kernel only exposes /sys/bus/w1/devices once the wire, w1-gpio and
        # w1-therm modules are loaded (or the w1-gpio Device Tree overlay is
        # enabled in /boot/config.txt). On a device that came up without the
        # overlay every probe reads as None and the bus looks "down" with no
        # explanation, so bring the bus up ourselves when it is missing.
        if self.base_path.exists():
            return
        await asyncio.to_thread(self._load_w1_bus)

    def _load_w1_bus(self) -> None:
        for module in ("wire", "w1-gpio", "w1-therm"):
            try:
                subprocess.run(
                    ["modprobe", module]
                    + ([f"gpiopin={ONE_WIRE_GPIO}"] if module == "w1-gpio" else []),
                    check=False,
                    capture_output=True,
                )
            except FileNotFoundError:
                # No modprobe (e.g. a stripped container): nothing we can do here.
                break
        if not self.base_path.exists():
            _log.warning(
                "[temperature] 1-Wire bus not found at %s after modprobe; all "
                "probes will read as unavailable. Enable "
                "'dtoverlay=w1-gpio,gpiopin=%s' in /boot/config.txt and reboot, "
                "or ensure the w1-gpio overlay is active.",
                self.base_path,
                ONE_WIRE_GPIO,
            )

    def _warn_missing(self, physical_id: str, device: Path) -> None:
        """Say a probe is not readable, once until it is readable again."""
        if physical_id in self._missing_warned:
            return
        self._missing_warned.add(physical_id)
        _log.warning(
            "[temperature] No 1-Wire reading for %s (looked for %s). "
            "Check the sensor is wired to GPIO %s and that the server "
            "record's physical_id matches the directory under "
            "/sys/bus/w1/devices (e.g. '28-0123456789ab').",
            physical_id,
            device,
            ONE_WIRE_GPIO,
        )

    def _warn_unreadable(self, physical_id: str, reason: str) -> None:
        """
        Say a probe is on the bus but its file could not be read.

        A different message from a missing probe, and warned about separately,
        because it means something different: the probe is fitted and the
        kernel has not finished with it. That is ordinary for a second or two
        after a probe is connected, and after a read failure while one is being
        pulled out.
        """
        key = f"{physical_id}:{reason}"
        if key in self._missing_warned:
            return
        self._missing_warned.add(key)
        _log.warning(
            "[temperature] 1-Wire device %s is on the bus but unreadable (%s). "
            "A probe that has just been fitted, re-seated or pulled shows this "
            "until the kernel has finished with it; it will read on its own.",
            physical_id,
            reason,
        )

    def _forget_faults(self, physical_id: str) -> bool:
        """
        Drop what has been logged about a probe that has just read.

        Returns whether there was anything to drop, so the caller can say the
        probe is back rather than saying so once a minute for every probe.
        """
        prefix = f"{physical_id}:"
        forgotten = physical_id in self._missing_warned or any(
            key.startswith(prefix) for key in self._missing_warned
        )
        self._missing_warned = {
            key for key in self._missing_warned if not key.startswith(prefix)
        }
        self._missing_warned.discard(physical_id)
        return forgotten

    def _read_sensor(self, physical_id: str) -> float | None:
        """
        One probe's temperature in °C, or None if it cannot be read right now.

        Nothing here is allowed to raise. A probe that has been pulled out of
        the bus takes its directory with it, and the kernel creates
        ``w1_slave`` a moment before it writes the reading into it, so the
        ordinary states of a 1-Wire probe include a file that does not exist, a
        file that is empty, a file with one line rather than two, and a file
        that goes away between being listed and being read. Every one of those
        is a missing reading, which this agent already knows how to report —
        ``None`` — and none of them is a reason to stop heating a boiler room.

        Retrying is kept, because a probe that has just been asked for a
        conversion does answer a moment later.
        """
        device = self.base_path / physical_id / "w1_slave"

        if not device.exists():
            self._warn_missing(physical_id, device)
            return None

        for _ in range(READ_ATTEMPTS):
            try:
                lines = device.read_text().splitlines()
            except OSError as exc:
                # Pulled out between the check and the read: the common case.
                if device.exists():
                    self._warn_unreadable(physical_id, type(exc).__name__)
                else:
                    self._warn_missing(physical_id, device)
                return None

            # The kernel writes the CRC line first and the reading second, so a
            # file that exists is not yet a file with anything in it.
            if len(lines) < 2:
                self._warn_unreadable(
                    physical_id, f"no reading in the file yet ({len(lines)} line(s))"
                )
                time.sleep(READ_RETRY_SECONDS)
                continue

            crc, reading = lines[0], lines[1]

            if not crc.endswith("YES"):
                self._warn_unreadable(physical_id, f"crc not accepted ({crc.strip()!r})")
                time.sleep(READ_RETRY_SECONDS)
                continue

            marker = reading.find("t=")
            if marker == -1:
                self._warn_unreadable(physical_id, "no temperature in the reading")
                time.sleep(READ_RETRY_SECONDS)
                continue

            raw = reading[marker + 2:].split()
            try:
                value = int(raw[0]) if raw else None
            except ValueError:
                value = None

            if value is None or not MIN_MILLIDEGREES <= value <= MAX_MILLIDEGREES:
                # A conversion caught mid-write: the digits are being written
                # one field at a time, so a half-written millidegree count is
                # far outside anything the part can report.
                self._warn_unreadable(
                    physical_id, f"implausible reading ({reading.strip()!r})"
                )
                time.sleep(READ_RETRY_SECONDS)
                continue

            # Only now is the probe known to be readable again, so this is the
            # point at which its earlier fault can be forgotten.
            if self._forget_faults(physical_id):
                _log.info("[temperature] Probe %s is reading again", physical_id)

            return round(value / 1000, 2)

        return None

    async def read_all(self) -> dict[int, float | None]:
        """
        Every probe the mapping names, in one pass.

        A probe that cannot be read is reported as ``None`` rather than as an
        exception, because that is what the rest of the agent already means by
        it: unavailable to the limit guard, unavailable in telemetry, and stored
        locally as unavailable. ``return_exceptions`` is belt and braces on top
        of that — it means a fault in one probe's read cannot cost the other
        probes their readings for this cycle, and cannot end the sensor loop.
        """
        items = [
            (sensor_id, cfg.get("physical_id"))
            for sensor_id, cfg in TEMPERATURE_SENSORS.items()
            if cfg.get("physical_id")
        ]

        results = await asyncio.gather(
            *[
                asyncio.to_thread(self._read_sensor, physical_id)
                for _, physical_id in items
            ],
            return_exceptions=True,
        )

        readings: dict[int, float | None] = {}
        for (sensor_id, physical_id), result in zip(items, results):
            if isinstance(result, BaseException):
                _log.warning(
                    "[temperature] Reading %s (%s) failed: %s",
                    sensor_id,
                    physical_id,
                    result,
                )
                readings[sensor_id] = None
            else:
                readings[sensor_id] = result

        return readings

    async def close(self):
        pass
