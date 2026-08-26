import asyncio
import subprocess
from pathlib import Path

from config import ONE_WIRE_GPIO, ONE_WIRE_PATH, TEMPERATURE_SENSORS
from logging_setup import get_logger

_log = get_logger("temperature")


class TemperatureReader:

    def __init__(self):
        self.base_path = Path(ONE_WIRE_PATH)
        # physical_ids we have already warned about, so a missing probe is
        # logged once rather than every minute.
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

    def _read_sensor(self, physical_id: str):
        device = self.base_path / physical_id / "w1_slave"

        if not device.exists():
            if physical_id not in self._missing_warned:
                self._missing_warned.add(physical_id)
                _log.warning(
                    "[temperature] No 1-Wire device %s (looked for %s). "
                    "Check the sensor is wired to GPIO %s and that the server "
                    "record's physical_id matches the directory under "
                    "/sys/bus/w1/devices (e.g. '28-0123456789ab').",
                    physical_id,
                    device,
                    ONE_WIRE_GPIO,
                )
            return None

        for _ in range(5):
            lines = device.read_text().splitlines()

            if lines[0].endswith("YES"):
                pos = lines[1].find("t=")

                if pos != -1:
                    return round(
                        float(lines[1][pos + 2:]) / 1000,
                        2,
                    )

        return None

    async def read_all(self):
        items = list(TEMPERATURE_SENSORS.items())
        results = await asyncio.gather(*[
            asyncio.to_thread(self._read_sensor, cfg["physical_id"])
            for _, cfg in items
        ])
        return {
            sensor_id: value
            for (sensor_id, _), value in zip(items, results)
        }

    async def close(self):
        pass
