# SPI Architecture: Gas Reader (ATtiny13) and Display (ST7920)

## Overview

This system uses a **single SPI bus (SPI0)** on the Raspberry Pi with **two peripherals** sharing the bus via different Chip Select (CS) lines:

| Peripheral | SPI Bus | SPI Device | CS Pin (BCM) | CS Behavior | SPI Mode | Speed |
|------------|---------|------------|--------------|-------------|----------|-------|
| ATtiny13A (Gas ADC) | 0 | 0 (CE0) | GPIO 8 (CE0) | Kernel-driven, Active LOW | 0 (CPOL=0, CPHA=0) | 1 MHz |
| ST7920 Display | 0 | 1 (CE1) | GPIO 7 | Manual, Active HIGH | 3 (CPOL=1, CPHA=1) | 500 kHz |

---

## Hardware Wiring

### ATtiny13A Gas Sensor ADC
```
Raspberry Pi          ATtiny13A
-----------          ---------
GPIO10 (MOSI)  --->  PB0 (SS)  [Pin 5]  *Not used by ATtiny (slave only)*
GPIO9  (MISO)  <---  PB1 (MISO) [Pin 6]
GPIO11 (SCLK)  --->  PB2 (SCK)  [Pin 7]
GPIO8  (CE0)   --->  PB0 (SS)  [Pin 5]  *Kernel drives this LOW during transfer*
```

**Key Point**: The ATtiny is an **SPI slave**. The Pi (master) pulls CE0 LOW to select it. The ATtiny firmware detects SS transitions via pin-change interrupt on PB0.

### ST7920 Display
```
Raspberry Pi          ST7920 Module
-----------          -------------
GPIO10 (MOSI)  --->  SID (Serial Data In)
GPIO11 (SCLK)  --->  CLK (Serial Clock)
GPIO7  (GPIO)  --->  CS  (Chip Select)  *Driven manually by display driver*
GPIO8  (CE1)   --->  (Not connected)    *Kernel CS disabled via no_cs=True*
```

**Key Point**: The ST7920 requires **Active HIGH** chip select. The kernel's SPI driver only supports Active LOW, so the display driver:
1. Opens the SPI device with `spi.no_cs = True` (disables kernel CS)
2. Manually drives GPIO7 HIGH/LOW around each transfer

---

## SPI Bus Sharing Strategy

### Same Bus, Different Chip Selects
Both devices share **SPI0** (MOSI=GPIO10, MISO=GPIO9, SCLK=GPIO11) but use different CS lines:
- **Gas ADC**: CE0 (GPIO8) - kernel controlled
- **Display**: GPIO7 - software controlled

This is the standard and correct way to share an SPI bus.

### Critical: SPI Mode Difference
| Device | SPI Mode | CPOL | CPHA | Clock Idle | Data Sampled |
|--------|----------|------|------|------------|--------------|
| ATtiny13A | 0 | 0 | 0 | LOW | Rising edge |
| ST7920 | 3 | 1 | 1 | HIGH | Rising edge |

**The kernel SPI driver reconfigures the bus mode on every `xfer2()` call.** Since each device opens its own `SpiDev` handle and sets its own mode, the kernel handles the mode switching automatically when each device transmits. There is **no conflict** because:
1. Only one CS is active at a time
2. The kernel applies the correct mode for the active device's `spi.mode` setting

---

## ATtiny13A Firmware Protocol (ADC.c)

The ATtiny13A acts as an **SPI slave** with a custom bit-banged implementation (no hardware SPI peripheral on ATtiny13A):

### Transaction Flow
1. **Pi pulls SS (PB0) LOW** → ATtiny detects falling edge via pin-change interrupt → `startTransmission()`
2. ATtiny drives MISO (PB1) with first bit of `txBuffer[0]` (MQ2 high byte)
3. **Pi generates SCK clock** → ATtiny detects SCK falling edges → shifts out next bit
4. After 8 bits → moves to next byte (MQ2 low, MQ9 high, MQ9 low)
5. **Pi pulls SS HIGH** → ATtiny detects rising edge → `stopTransmission()` (releases MISO)

### Data Format (4 bytes sent by ATtiny)
```
Byte 0: MQ2 High Byte  (ADC3 / PB3 / Pin 2)
Byte 1: MQ2 Low Byte
Byte 2: MQ9 High Byte  (ADC2 / PB4 / Pin 3)
Byte 3: MQ9 Low Byte
```

### SPI Mode 0 Timing (ATtiny side)
- **CPOL=0**: Clock idles LOW
- **CPHA=0**: Data changes on **falling** edge, sampled on **rising** edge
- ATtiny updates MISO on SCK falling edge
- Pi samples MISO on SCK rising edge

---

## Gas Reader Python Driver (gas_reader.py)

```python
def _open_spi(self):
    self.spi = spidev.SpiDev()
    self.spi.open(SPI_BUS, SPI_DEVICE)  # SPI_BUS=0, SPI_DEVICE=0 (CE0)
    self.spi.mode = 0                    # SPI Mode 0
    self.spi.max_speed_hz = SPI_SPEED    # 1 MHz
    self.spi.lsbfirst = False            # MSB first

def _read_raw_sync(self):
    # Send 4 dummy bytes, receive 4 bytes from ATtiny
    response = self.spi.xfer2([0x00, 0x00, 0x00, 0x00])
    
    # Combine into two 16-bit values
    mq2 = (response[0] << 8) | response[1]
    mq9 = (response[2] << 8) | response[3]
    return mq2, mq9
```

**Key behaviors**:
- Uses kernel-driven CS (standard `spidev` behavior)
- Sends dummy bytes (0x00) to clock out data from ATtiny
- Reads exactly 4 bytes per transaction
- Runs in thread pool via `asyncio.to_thread()` (blocking SPI call)

---

## Display Driver (display.py)

```python
def _open(self):
    spi = spidev.SpiDev()
    spi.open(self.bus, self.device)      # SPI_BUS=0, SPI_DEVICE=1 (CE1)
    spi.max_speed_hz = self.speed_hz     # 500 kHz
    spi.mode = self.mode                 # SPI Mode 3 (0b11)
    spi.no_cs = True                     # CRITICAL: disable kernel CS

    # Manual CS on GPIO7 (Active HIGH)
    GPIO.setup(self.cs_pin, GPIO.OUT, initial=GPIO.LOW)

def _transfer(self, payload):
    GPIO.output(self.cs_pin, GPIO.HIGH)  # Assert CS (Active HIGH)
    try:
        self._spi.xfer2(payload)
    finally:
        GPIO.output(self.cs_pin, GPIO.LOW)  # Deassert CS
```

**Key behaviors**:
- Opens SPI0.1 (CE1) but **disables kernel CS** with `no_cs=True`
- Manually drives GPIO7 as Active HIGH chip select
- Uses SPI Mode 3 (CPOL=1, CPHA=1)
- Each byte sent as 3-byte sequence: sync + high nibble + low nibble
- Uses `threading.RLock()` to serialize all bus access (prevents interleaved transfers)

---

## Conflict Detection (display.py:conflicts())

The display driver detects and warns about SPI conflicts at startup:

```python
def conflicts(self) -> dict[int, str]:
    from config import SPI_BUS, SPI_DEVICE, SPI_GPIO
    
    clashes = {}
    
    # Same bus AND same device = direct conflict
    if self.bus == SPI_BUS and self.device == SPI_DEVICE:
        clashes[self.cs_pin] = f"SPI {SPI_BUS}.{SPI_DEVICE}, which the gas ADC also opens"
    
    # Display CS pin overlaps with gas ADC's SPI pins
    elif self.cs_pin in SPI_GPIO:
        clashes[self.cs_pin] = "a pin the gas ADC's SPI bus uses"
    
    # PSB pin (if used) overlaps with gas ADC's SPI pins
    if self.psb_pin is not None and self.psb_pin in SPI_GPIO:
        clashes[self.psb_pin] = "a pin the gas ADC's SPI bus uses"
    
    return clashes
```

**Current config avoids conflict**:
- Gas ADC: SPI0.0 (CE0/GPIO8)
- Display: SPI0.1 (GPIO7 for CS)
- No pin overlap

---

## Initialization Sequence (Main.py)

```python
# In sensor_loop():
temperature_reader = TemperatureReader()
gas_reader = GasReader()
relay_controller = RelayController()

await asyncio.gather(
    temperature_reader.start(),
    gas_reader.start(),      # Opens SPI0.0, mode 0
    relay_controller.start(),
)

# In main() - display started by control menu:
state.display = Display()    # Opens SPI0.1, mode 3, no_cs=True
await state.display.start()
```

Both can be initialized concurrently - they use different `SpiDev` handles and different CS lines.

---

## Runtime Operation

### Sensor Loop (60-second interval)
```python
while not shutdown:
    temperatures, gas = await asyncio.gather(
        temperature_reader.read_all(),
        gas_reader.read_all(),   # SPI0.0 transaction (4 bytes)
    )
    # ... process readings ...
    await asyncio.sleep(interval)
```

### Display Refresh (on-demand)
```python
async def show(self, canvas):
    frame = canvas.snapshot()
    return await asyncio.to_thread(self._flush, frame)

def _flush(self, frame):
    with self._bus_lock:           # Serializes all display SPI traffic
        # Send only changed rows via SPI0.1
        for row in changed_rows:
            self._set_address(row)  # SPI command (mode 3)
            self._data(chunk)       # SPI data (mode 3)
```

---

## Bus Locking & Concurrency

### Display Driver: `threading.RLock()`
```python
self._bus_lock = threading.RLock()

def _flush(self, frame):
    with self._bus_lock:   # Held for entire frame refresh
        return self._flush_locked(frame)
```

**Purpose**: Prevents a display refresh from being interrupted by another display operation (or close), which would leave the ST7920 controller mid-frame.

### Gas Reader: No explicit lock
- Uses `asyncio.to_thread()` for each read
- Each `xfer2()` is a single atomic kernel transaction
- Reads are infrequent (once per minute) and fast (4 bytes)

### Cross-Device Concurrency
**No shared lock between gas reader and display** - they use different CS lines and the kernel SPI driver handles bus arbitration. However, the display's `RLock` only protects display-internal operations.

**Potential issue**: If gas reader and display transfer simultaneously:
- Kernel will serialize at the SPI controller level
- Display's manual CS (GPIO7) and gas ADC's kernel CS (GPIO8) are independent
- **No data corruption** - each device only responds to its own CS
- Minor timing interference possible but unlikely at 1 min vs on-demand rates

---

## Configuration Reference (config.py)

```python
# ATtiny13A Gas ADC - SPI0.0
SPI_BUS = 0
SPI_DEVICE = 0          # CE0
SPI_SPEED = 1_000_000   # 1 MHz
SPI_GPIO = (9, 11, 8)   # MISO, SCLK, CE0

# ST7920 Display - SPI0.1
DISPLAY_SPI_BUS = 0
DISPLAY_SPI_DEVICE = 1  # CE1 (but kernel CS disabled)
DISPLAY_SPI_SPEED = 500_000
DISPLAY_SPI_MODE = 0b11  # Mode 3
DISPLAY_CS_GPIO = 7      # Manual CS on GPIO7 (Active HIGH)
DISPLAY_GPIO = (10, 11, 7)  # MOSI, SCLK, CS
```

---

## Troubleshooting Guide

### Display shows garbage / frozen frame
- **Cause**: ST7920 left mid-frame from previous run
- **Fix**: Display driver's `_bus_reset()` clocks out zeros + toggles CS to resync receiver

### Display blank but gas readings work
- **Cause**: CS pin mismatch (display driving wrong GPIO)
- **Check**: `display.describe()` logs actual CS pin; verify wiring to GPIO7 (pin 26)

### Gas readings fail / return zeros
- **Cause**: ATtiny not selected (CE0 not driven LOW)
- **Check**: `spi.open(0, 0)` uses CE0; verify ATtiny SS connected to GPIO8 (pin 24)

### Both devices on CE0 (conflict)
- **Symptom**: Display goes blank when gas reader runs
- **Fix**: Move display to CE1 (SPI_DEVICE=1, CS_PIN=7) - current config

### SPI mode errors
- **Gas ADC**: Must be Mode 0 (ATtiny firmware hardcoded)
- **Display**: Mode 3 default; use `--mode0` flag for clones needing Mode 0

---

## Design Decisions Summary

| Decision | Rationale |
|----------|-----------|
| Single SPI bus (SPI0) | Pi only has one SPI bus exposed on header |
| Different CS lines (CE0 vs GPIO7) | Standard SPI multi-device practice |
| Display uses `no_cs=True` + manual CS | ST7920 requires Active HIGH CS; kernel only does Active LOW |
| ATtiny uses kernel CS | Standard SPI slave; simpler firmware |
| Different SPI modes (0 vs 3) | Each device's requirement; kernel switches per-transfer |
| Display serializes with RLock | Prevents controller state corruption from interleaved transfers |
| Gas reader no lock needed | Single atomic 4-byte transaction per read |
| 1 MHz vs 500 kHz speeds | ATtiny bit-banged SPI handles 1 MHz; ST7920 more reliable at 500 kHz |

---

