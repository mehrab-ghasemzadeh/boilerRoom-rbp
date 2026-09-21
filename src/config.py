"""
System configuration.

Device mapping is loaded at startup via `await load_device_mapping()`.

The MQ2 and MQ9 gas sensor mappings are LOCAL hardware configuration.
They are deliberately NOT loaded from the server because their physical
connections to the ATtiny13A are fixed on this device.
"""

# ---------------------------------------------------------------------------

# 1-Wire

# ---------------------------------------------------------------------------

# GPIO pin used by the 1-Wire bus

ONE_WIRE_GPIO = 4

# Linux 1-Wire device directory

ONE_WIRE_PATH = "/sys/bus/w1/devices"

# ---------------------------------------------------------------------------

# ATtiny13A / Gas Sensor SPI

# ---------------------------------------------------------------------------

SPI_BUS = 0
SPI_DEVICE = 0

# SPI clock speed for the ATtiny interface.

SPI_SPEED = 1_000_000

# Current ATtiny SPI pins:

#

# MISO -> GPIO9  -> physical pin 21 -> ATtiny pin 6 / PB1

# SCLK -> GPIO11 -> physical pin 23 -> ATtiny pin 7 / PB2

# CE0  -> GPIO8  -> physical pin 24 -> ATtiny pin 5 / PB0

#

# NOTE:

# GPIO11 is currently also connected to the ST7920 E pin.

# For the final hardware design, the ATtiny SCLK should be moved

# to a separate GPIO to eliminate interference with the display.

SPI_GPIO = (9, 11, 8)

# ---------------------------------------------------------------------------

# Local Gas Sensor Mapping

# ---------------------------------------------------------------------------

#

# These mappings describe the physical hardware installed on this device.

#

# ATtiny13A:

#

# ADC3 / PB3 / physical pin 2 -> MQ2

# ADC2 / PB4 / physical pin 3 -> MQ9

#

# The ATtiny sends four bytes:

#

# byte 0 = MQ2 high byte

# byte 1 = MQ2 low byte

# byte 2 = MQ9 high byte

# byte 3 = MQ9 low byte

#

# GasReader channel mapping:

#

# channel 0 -> first 16-bit value -> MQ2

# channel 1 -> second 16-bit value -> MQ9

#

# This dictionary is intentionally NOT populated from the server.

GAS_SENSORS: dict[str, dict] = {
"mq2": {
"name": "MQ2",
"channel": 0,
"adc_channel": 3,
"adc_pin": 2,
},
"mq9": {
"name": "MQ9",
"channel": 1,
"adc_channel": 2,
"adc_pin": 3,
},
}

# ---------------------------------------------------------------------------

# ST7920 128x64 Graphical Display

# ---------------------------------------------------------------------------

# The ST7920 controller uses an active-HIGH chip select.

# The kernel cannot directly provide the required CS behavior, so the

# display driver opens the SPI device without kernel-controlled CS and

# drives DISPLAY_CS_GPIO manually.

DISPLAY_SPI_BUS = 0
DISPLAY_SPI_DEVICE = 1
DISPLAY_SPI_SPEED = 500_000

# ST7920 SPI mode:

# CPOL = 1

# CPHA = 1

DISPLAY_SPI_MODE = 0b11

# BCM GPIO used for display chip select.

# Physical pin 26.

DISPLAY_CS_GPIO = 7

# Display GPIO mapping:

#

# SID -> GPIO10 -> physical pin 19

# E   -> GPIO11 -> physical pin 23

# RS  -> GPIO7  -> physical pin 26

DISPLAY_GPIO = (10, 11, 7)

# ---------------------------------------------------------------------------

# Relay Board

# ---------------------------------------------------------------------------

# Relay number -> BCM GPIO

#

# Relay 1 -> physical pin 12 -> GPIO18

# Relay 2 -> physical pin 16 -> GPIO23

# Relay 3 -> physical pin 18 -> GPIO24

# Relay 4 -> physical pin 22 -> GPIO25

# Relay 5 -> physical pin 32 -> GPIO12

# Relay 6 -> physical pin 36 -> GPIO16

# Relay 7 -> physical pin 38 -> GPIO20

# Relay 8 -> physical pin 40 -> GPIO21

RELAY_GPIO = {
1: 18,
2: 23,
3: 24,
4: 25,
5: 12,
6: 16,
7: 20,
8: 21,
}

# ---------------------------------------------------------------------------

# Server-provided Device Mapping

# ---------------------------------------------------------------------------

# These mappings are populated by load_device_mapping() at startup.

UNITS: dict[str, dict[str, str]] = {}

TEMPERATURE_SENSORS: dict[int, dict] = {}

RELAYS: dict[int, dict] = {}

# ---------------------------------------------------------------------------

# Load Device Mapping

# ---------------------------------------------------------------------------

async def load_device_mapping():
    """
    Fetch the device mapping from the configured provider.

    Server-controlled mappings:
        - UNITS
        - TEMPERATURE_SENSORS
        - RELAYS

    Local hardware mapping:
        - GAS_SENSORS

    GAS_SENSORS is intentionally NOT read from the server.
    """

    from mapping_store import mapping_store

    device_mapping = await mapping_store.load()

    # Server-controlled unit mapping
    UNITS.clear()
    UNITS.update(device_mapping.units)

    # Server-controlled temperature sensor mapping
    TEMPERATURE_SENSORS.clear()
    TEMPERATURE_SENSORS.update(
        device_mapping.temperature_sensors
    )

    # IMPORTANT:
    # Do NOT load device_mapping.gas_sensors.
    #
    # GAS_SENSORS is fixed locally because the MQ2 and MQ9 sensors
    # are physically connected to fixed ATtiny ADC channels.

    # Server-controlled relay mapping
    RELAYS.clear()
    RELAYS.update(device_mapping.relays)

    return device_mapping
