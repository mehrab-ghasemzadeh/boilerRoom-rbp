# LCD Display Documentation

## Overview

The system uses an **ST7920 128×64 graphical LCD** driven over hardware SPI.
The display serves as the primary visual interface for an operator standing
at the boiler room, showing real-time status readings, an interactive menu,
and prompts. The same menu is also available on a terminal for bench work.

## Hardware Wiring

### Connections (BCM GPIO numbering)

| ST7920 Pin | Function | Raspberry Pi GPIO | Physical Pin |
|------------|----------|-------------------|-------------|
| SID | Serial data in | GPIO 10 (MOSI) | Pin 19 |
| CLK | Serial clock | GPIO 11 (SCLK) | Pin 23 |
| CS | Chip select (active HIGH) | GPIO 7 | Pin 26 |
| VCC | Power | 5V (physical) | Pin 2 or 4 |
| GND | Ground | GND | Pin 6 or 9 |
| PSB | Serial mode select | GND (tied low) | — |

**PSB must be tied LOW** — this is a jumper or solder pad on the module, not
a pin on the connector. Left high, the controller expects a parallel bus.

### Shared SPI Bus

The display shares SPI bus 0 with the gas ADC (on CE0). The two devices run
at different clocks and SPI modes, so each opens its own `spidev.SpiDev`
handle. The Linux SPI subsystem serializes transfers, so they do not need
to know about each other.

| Device | CE Line | SPI Mode | Speed |
|--------|---------|----------|-------|
| Gas ADC | CE0 (GPIO 8, pin 24) | 1 (0b01) | 1 MHz |
| LCD | CE1 (GPIO 7, pin 26) | 3 (0b11) | 800 kHz |

### Key Gotcha: CS is Active HIGH

Every other SPI peripheral on the header asserts chip select low. The ST7920
asserts it **high**. The driver uses `spi.no_cs = True` and drives the CS pin
manually via GPIO. A panel that stays blank with good signals on SID and CLK
is almost always this issue.

## Software Architecture

### Module: `src/display.py`

The `ST7920Display` class implements the hardware driver:

```python
from display import ST7920Display

display = ST7920Display()
await display.start()       # Open SPI, init controller
await display.show(canvas)   # Push a frame (only changed rows sent)
await display.clear()        # Clear the graphics RAM
await display.close()        # Release SPI and GPIO
```

**Constructor parameters** (all overridable via environment variables):

| Parameter | Env Var | Default | Description |
|-----------|---------|---------|-------------|
| `bus` | `BOILERROOM_DISPLAY_SPI_BUS` | 0 | SPI bus number |
| `device` | `BOILERROOM_DISPLAY_SPI_DEVICE` | 1 | CE line (0=CE0, 1=CE1) |
| `speed_hz` | `BOILERROOM_DISPLAY_SPI_SPEED` | 800,000 | SPI clock in Hz |
| `cs_pin` | `BOILERROOM_DISPLAY_CS_PIN` | 7 | GPIO pin for CS (active HIGH) |

### Serial Protocol

Each byte is sent as two nibbles, each padded with four zero bits:

```
Value 0xAB → [0xA0, 0xB0]
```

Transfers open with a sync byte:
- **`0xF8`** — command transfer (RW=0, RS=0)
- **`0xFA`** — data transfer (RW=0, RS=1)

A burst of data needs only one sync byte at the front, making row writes
efficient.

### Initialization Sequence

```
1. Power-on delay (50 ms)
2. Function set (basic instruction set)   — 0x30
3. Display ON, cursor off, blink off    — 0x0C
4. Clear display                        — 0x01   (1.6 ms delay)
5. Entry mode set (increment)           — 0x06
6. Function set (extended instruction)  — 0x34
7. Graphics mode ON                     — 0x36
8. Clear entire graphics RAM (64 rows)
```

### Differential Refresh

The driver tracks the previous frame and sends **only rows that changed**.
A full frame is 64 rows × 16 bytes = 1 KB of data. At 800 kHz, a full refresh
takes ~2.5 ms. Navigating a menu typically changes 1-2 rows, costing ~1 ms.

## Display Content

### Layout (128×64, 4 body rows × 16 columns)

```
┌────────────────┐
│ TITLE      1/N │  ← Title bar (8 px)
├────────────────┤
│ menu item 1    │  ← Body (4 rows × 8 px)
│ menu item 2    │
│ menu item 3    │
│ menu item 4    │
├────────────────┤
│ 2▲ #OK 8▼ *Bk │  ← Legend strip (8 px)
└────────────────┘
```

### Screens

1. **Status screen** — Default view when idle. Shows link status, config/schedule
   versions, per-unit temperature/relay/mode, cut-out reasons, setpoints,
   environmental readings, gas levels, and last reading time. Auto-refreshes
   every 30 seconds with scrolling.

2. **Main menu** — 10 options:
   - Sensor readings
   - Device mapping
   - App configuration
   - Active schedule
   - Server record
   - Relay control
   - Unit modes
   - Reload mapping
   - Change schedule
   - Temperatures
   - Quit

3. **Sub-menus** — Each option opens a context-specific screen with relevant data
   or editing prompts.

4. **Text input** — For entering temperatures, dates, times, and credentials.
   Passwords are masked with dots.

5. **Splash screens** — Startup ("Starting up...") and shutdown ("Agent stopped.")

### Navigation

| Key | BCM GPIO | Physical Pin | Function |
|-----|----------|-------------|----------|
| 2 | GPIO 27 | Pin 13 | Scroll up |
| 8 | GPIO 19 | Pin 35 | Scroll down |
| # | GPIO 22 | Pin 15 | Select / accept |
| * | GPIO 5 | Pin 29 | Back / cancel |

Rows and columns are configurable via environment variables.

## Dependencies

- `spidev` — hardware SPI
- `RPi.GPIO` — manual chip select control
- `src/display_canvas.py` — pixel canvas abstraction (128×64, 1-bit)
- `src/display_font.py` — 5×7 bitmap font with degree sign
- `src/screen.py` — menu/list rendering and keypad interaction

## Configuration Constants (`src/config.py`)

```python
DISPLAY_SPI_BUS = 0      # SPI0
DISPLAY_SPI_DEVICE = 1   # CE1
DISPLAY_SPI_SPEED = 800_000  # 800 kHz
DISPLAY_GPIO = (10, 11, 7)  # MOSI, SCLK, CS
```
