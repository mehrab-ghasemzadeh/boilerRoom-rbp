"""
System configuration.

Device mapping is loaded at startup via ``await load_device_mapping()``.
Until then, sensor/relay dicts below are empty.

Source is selected by BOILERROOM_MAPPING_SOURCE (file now, server later).
"""

# GPIO pin used by the 1-Wire bus
ONE_WIRE_GPIO = 4

# Linux 1-Wire device directory
ONE_WIRE_PATH = "/sys/bus/w1/devices"

SPI_BUS = 0
SPI_DEVICE = 0
SPI_SPEED = 1_000_000

# BCM pins the gas ADC occupies: MOSI, SCLK, CE0.
SPI_GPIO = (10, 11, 8)

# ST7920 128x64 graphical display. The controller wants an active-HIGH chip
# select, which the kernel cannot drive, so the handle is opened with no_cs and
# display.py drives CS itself.
#
# That puts it in direct conflict with the gas ADC below, which is on this same
# CE0 and expects the kernel to drive it. One of the two has to move: put the
# panel on CE1 with BOILERROOM_DISPLAY_SPI_DEVICE=1 and _CS_PIN=7 if both are
# fitted. display.describe() reports the clash.
DISPLAY_SPI_BUS = 0
DISPLAY_SPI_DEVICE = 0  # CE0, with the kernel's chip select disabled
DISPLAY_SPI_SPEED = 500_000
DISPLAY_SPI_MODE = 0b11  # clock idles high, sampled on the rising edge

# BCM pins the panel occupies: SID -> MOSI, CLK -> SCLK, CS -> CE0 by hand.
DISPLAY_GPIO = (10, 11, 8)

# Populated by load_device_mapping() at startup
UNITS: dict[str, dict[str, str]] = {}
TEMPERATURE_SENSORS: dict[int, dict] = {}
GAS_SENSORS: dict[int, dict] = {}
RELAYS: dict[int, dict] = {}


async def load_device_mapping():
    """Fetch mapping from the configured provider and sync into module-level dicts."""
    from mapping_store import mapping_store

    device_mapping = await mapping_store.load()

    UNITS.clear()
    UNITS.update(device_mapping.units)

    TEMPERATURE_SENSORS.clear()
    TEMPERATURE_SENSORS.update(device_mapping.temperature_sensors)

    GAS_SENSORS.clear()
    GAS_SENSORS.update(device_mapping.gas_sensors)

    RELAYS.clear()
    RELAYS.update(device_mapping.relays)

    return device_mapping
