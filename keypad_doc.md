# Keypad Documentation

## Overview

The system uses a **4×4 matrix keypad** as the primary input device for an
operator standing at the boiler room. It replaces keyboard input on the
installed hardware and drives the interactive menu shown on the LCD display.

## Hardware Wiring

### Matrix Theory

A 4×4 matrix keypad has 8 pins: 4 rows and 4 columns. Keys are detected by
driving one row LOW at a time and reading the columns. If a column reads LOW
while its row is driven LOW, the key at that intersection is pressed.

- **Row pins** are outputs, driven LOW to scan
- **Column pins** are inputs with internal pull-ups
- When idle, all rows are held LOW; columns read HIGH (no key pressed)
- Pressing a key bridges a column to a row, pulling the column LOW

### Connections (BCM GPIO numbering)

| Key | BCM GPIO | Physical Pin | Direction |
|-----|----------|-------------|-----------|
| **Row A** | GPIO 17 | Pin 11 | Output |
| **Row B** | GPIO 27 | Pin 13 | Output |
| **Row C** | GPIO 22 | Pin 15 | Output |
| **Row D** | GPIO 5 | Pin 29 | Output |
| **Col 1** | GPIO 6 | Pin 31 | Input (pull-up) |
| **Col 2** | GPIO 13 | Pin 33 | Input (pull-up) |
| **Col 3** | GPIO 19 | Pin 35 | Input (pull-up) |
| **Col 4** | GPIO 26 | Pin 37 | Input (pull-up) |

These are the default pins from `DEFAULT_ROW_GPIO` and `DEFAULT_COL_GPIO` in
`src/keypad_layout.py`.

### Override via Environment Variables

The default pins can be changed without editing code:

```bash
export BOILERROOM_KEYPAD_ROWS="11,13,15,29"    # BCM GPIO numbers, comma-separated
export BOILERROOM_KEYPAD_COLS="31,33,35,37"    # BCM GPIO numbers, comma-separated
```

### Pin Conflict Detection

On startup, `Keypad.conflicting_pins()` checks all keypad pins against:
- **Relay pins** (loaded from device mapping)
- **1-Wire bus** (GPIO 4)
- **SPI bus** (GPIO 8, 10, 11 — used by gas ADC and display)

If a conflict exists, the keypad will **refuse to start** and the menu falls
back to keyboard/TTY input. This prevents a keypress from accidentally
switching a relay. Check the log output for specifics.

### Current Default Pin Conflicts

With the default relay mapping in `config_cache.json` (GPIOs 17, 18, 19, 20,
21, 22, 23), the default keypad pins conflict with:
- GPIO 17 (Row A) — Relay rly_1 (Boiler 1)
- GPIO 19 (Col 3) — Relay rly_3 (Pump 1)
- GPIO 22 (Row C) — Relay rly_6 (Fan)

Override via environment variables or reassign relay pins before deploying.

## Key Layout

The default layout matches the physical keycaps:

```
Cap layout (DEFAULT_CAPS)        Token layout (DEFAULT_LAYOUT)
┌─────┬─────┬─────┬─────┐       ┌─────┬─────┬─────┬─────┐
│  1  │  2  │  3  │  A  │       │  1  │  2  │  3  │Del │
├─────┼─────┼─────┼─────┤       ├─────┼─────┼─────┼─────┤
│  4  │  5  │  6  │  B  │       │  4  │  5  │  6  │  .  │
├─────┼─────┼─────┼─────┤       ├─────┼─────┼─────┼─────┤
│  7  │  8  │  9  │  C  │       │  7  │  8  │  9  │  ,  │
├─────┼─────┼─────┼─────┤       ├─────┼─────┼─────┼─────┤
│  *  │  0  │  #  │  D  │       │Cancel│ 0  │Enter│  -  │
└─────┴─────┴─────┴─────┘       └─────┴─────┴─────┴─────┘
```

### Key Tokens

Each key emits a **token** rather than its cap, since cap labels vary:

| Token | Alias | Cap Label | Function |
|-------|-------|-----------|----------|
| `enter` | `#`, `ok`, `select` | `#` | Accept / select |
| `cancel` | `*`, `back`, `esc` | `*` | Go back / cancel |
| `del` | `backspace`, `delete` | `A` | Delete last character |
| `2` | — | `2` | Scroll up (in scrollable screens) |
| `8` | — | `8` | Scroll down (in scrollable screens) |
| `0`-`9` | — | Digits | Numeric input |
| `.` | `dot`, `point`, `decimal` | `6` or `B` | Decimal point |
| `,` | `comma` | `9` or `C` | List separator |
| `-` | `minus`, `dash` | `D` | Clear value / negative |

### Layout Override

```bash
export BOILERROOM_KEYPAD_LAYOUT="1,2,3,enter/4,5,6,del/7,8,9,comma/cancel,0,enter,minus"
export BOILERROOM_KEYPAD_CAPS="1,2,3,A/4,5,6,B/7,8,9,C/*,0,#,D"
```

The layout uses `/` to separate rows and commas within rows. Caps must match
the physical key labels.

## Scanning Behavior

| Parameter | Value | Description |
|-----------|-------|-------------|
| Idle poll rate | 25 Hz (40 ms) | Cheap column check with all rows LOW |
| Debounce | 20 ms | Second scan confirms the press |
| Row settle time | 1 ms | Wait after switching active row |
| Auto-repeat | No | One press = one key, even if held |

Scanning runs on a **dedicated daemon thread**, not on `asyncio.to_thread`,
because the event loop's thread pool is limited and the control menu already
occupies a worker with `input()`.

Keys are delivered to the asyncio event loop via `call_soon_threadsafe`.

## Software Architecture

### Module: `src/keypad.py`

The `Keypad` class manages GPIO and scanning:

```python
from keypad import Keypad

keypad = Keypad()
await keypad.start()      # Configure GPIO, start scan thread
key = await keypad.read_key()   # Block for one keypress token
line = await keypad.read_line("Prompt: ")  # Block for full line
await keypad.close()    # Stop thread, release GPIO pins
```

### Module: `src/keypad_layout.py`

Contains all key mappings, token normalization, and the `LineEditor` class.
Has no hardware dependencies — can be imported on a development laptop.

### Module: `src/mock_keypad.py`

A keyboard-based emulator for bench testing:
- Reads single keys from stdin by default
- `BOILERROOM_KEYPAD_EMULATE=on` restricts input to actual keypad tokens
- Uses the same `LineEditor` as the real keypad

## Display Integration

The keypad's `read_key()` method is the input mechanism for the graphical
display's menu:

- **2/8** — scroll through menu lists (wrapping at both ends)
- **#** — select the highlighted menu item
- **\*** — go back to the parent menu

The display's `Screen` class (`src/screen.py`) consumes these tokens to drive:
- `select()` — pick from a list
- `page()` — scroll through text pages
- `read_line()` — text input with the `LineEditor`
- `splash()` — static message screens

## Testing

```bash
# Test mode: press each key and see what token/position it reports
python src/keypad.py --test
```

This prints the layout grid and, for each keypress, the token emitted and its
row/column position — useful for verifying wiring against the keycap labels.

## Dependencies

- `RPi.GPIO` — GPIO pin control
- `src/keypad_layout.py` — key tables, tokens, line editor
- `src/config.py` — SPI and 1-Wire pin definitions (for conflict detection)
- `src/control_menu.py` — menu integration
- `src/screen.py` — display rendering
