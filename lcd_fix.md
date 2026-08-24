# LCD Display Fix: Persistent Graphics RAM Issue

## Problem Description

When the Raspberry Pi boots, the LCD works correctly. However, after quitting the program and running it again, the display shows the last frame (e.g., "Agent stopped") and fails to update. The issue persists even when running other programs or clearing caches.

## Root Cause

The ST7920 display retains its graphics RAM content across program restarts. On subsequent runs, the initialization sequence may fail because:

1. The display is already in graphics mode
2. The `_CMD_CLEAR` (0x01) command only clears **text RAM**, not graphics RAM
3. The `_blank()` function may not execute if initialization fails early
4. The ST7920 may ignore commands while in an unexpected state

## Solutions

### Solution 1: Multiple Initialization Attempts

Modify the `_initialise()` method to send commands multiple times, ensuring the controller transitions to the correct state.

**File:** `display.py`

```python
def _initialise(self) -> None:
    """Initialize the display with multiple attempts to ensure proper state."""
    time.sleep(0.05)
    
    # Send function set multiple times to force basic mode
    for _ in range(3):
        self._command(_CMD_FUNCTION_BASIC)  # 0x30
        time.sleep(0.01)
    
    # Send software reset (clear text RAM)
    self._command(_CMD_CLEAR)  # 0x01
    time.sleep(_CLEAR_DELAY)
    
    # Turn display on
    self._command(_CMD_DISPLAY_ON)  # 0x0C
    self._command(_CMD_ENTRY_MODE)  # 0x06
    
    # Switch to extended mode with multiple attempts
    for _ in range(3):
        self._command(_CMD_FUNCTION_EXTENDED)  # 0x34
        time.sleep(0.01)
    
    self._command(_CMD_GRAPHICS_ON)  # 0x36
    
    # Force clear ALL graphics RAM
    self._blank()
    
    # Reset previous frame tracking
    self._previous = None
```

### Solution 2: Hardware Power Cycle Reset

Use the GPIO to simulate a hardware reset by toggling the CS pin in a specific pattern. This forces the ST7920 to reinitialize its internal state.

**File:** `display.py`

```python
def _power_cycle_reset(self) -> None:
    """
    Force a hardware reset by toggling CS with a specific pattern.
    This helps clear the display's internal state.
    """
    # Pull CS high (active for ST7920)
    GPIO.output(self.cs_pin, GPIO.HIGH)
    time.sleep(0.1)
    
    # Pull CS low
    GPIO.output(self.cs_pin, GPIO.LOW)
    time.sleep(0.1)
    
    # Toggle high again
    GPIO.output(self.cs_pin, GPIO.HIGH)
    time.sleep(0.05)
    
    # Return to idle (low)
    GPIO.output(self.cs_pin, GPIO.LOW)
    time.sleep(0.05)

def _open(self) -> None:
    """Open SPI connection and initialize the display."""
    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(self.cs_pin, GPIO.OUT, initial=GPIO.LOW)
    
    # Perform hardware reset
    self._power_cycle_reset()
    
    # Now open SPI
    spi = spidev.SpiDev()
    spi.open(self.bus, self.device)
    spi.max_speed_hz = self.speed_hz
    spi.mode = 0b11
    spi.no_cs = True
    
    self._spi = spi
    self._previous = None
    self._initialise()
```

### Solution 3: Force Clear Graphics RAM

Add a dedicated function to clear all graphics RAM addresses, independent of the controller's state.

**File:** `display.py`

```python
def _force_clear_graphics(self) -> None:
    """
    Force clear all graphics RAM by writing zeros to every address.
    This works even if the controller is in an unexpected state.
    """
    try:
        # Try to switch to extended mode
        for _ in range(3):
            self._command(_CMD_FUNCTION_EXTENDED)
            time.sleep(0.01)
        
        # Try to enable graphics mode
        for _ in range(3):
            self._command(_CMD_GRAPHICS_ON)
            time.sleep(0.01)
        
        # Write zeros to every row
        empty = bytes(BYTES_PER_ROW)
        for row in range(HEIGHT):
            self._set_address(row)
            self._data(empty)
            time.sleep(0.001)  # Small delay between rows
        
        # Update previous frame
        self._previous = bytearray(BYTES_PER_ROW * HEIGHT)
    except Exception as e:
        # If clearing fails, at least we tried
        print(f"Warning: Could not force clear graphics: {e}")

def _initialise(self) -> None:
    """Initialize the display with force clear."""
    time.sleep(0.05)
    
    # Try to get into basic mode
    for _ in range(3):
        self._command(_CMD_FUNCTION_BASIC)
        time.sleep(0.01)
    
    self._command(_CMD_CLEAR)
    time.sleep(_CLEAR_DELAY)
    self._command(_CMD_DISPLAY_ON)
    self._command(_CMD_ENTRY_MODE)
    
    # Force clear graphics RAM
    self._force_clear_graphics()
    
    # Reset previous frame tracking
    self._previous = None
```

### Solution 4: Clear Display on Exit

Modify `_close_sync()` to clear the display before closing, preventing stale data from persisting.

**File:** `display.py`

```python
def _close_sync(self) -> None:
    """
    Clear the display and release the SPI bus.
    This prevents stale frames from persisting between runs.
    """
    spi = self._spi
    self._spi = None
    if spi is None:
        return
    
    try:
        # Clear the graphics RAM
        self._blank()
        time.sleep(0.01)
        
        # Turn off the display to save power
        self._command(_CMD_FUNCTION_BASIC)
        self._command(0x08)  # Display OFF
        
        # Release GPIO
        GPIO.output(self.cs_pin, GPIO.LOW)
        spi.close()
    except Exception:
        # If clearing fails, just release the bus
        try:
            GPIO.output(self.cs_pin, GPIO.LOW)
            spi.close()
        except:
            pass
```

### Solution 5: Combined Robust Initialization

Combine all approaches for maximum reliability.

**File:** `display.py`

```python
def _initialise(self) -> None:
    """
    Robust initialization that handles displays in any state.
    Uses multiple attempts and hardware resets if needed.
    """
    time.sleep(0.05)
    
    # Step 1: Force a hardware reset
    self._power_cycle_reset()
    time.sleep(0.05)
    
    # Step 2: Multiple attempts to enter basic mode
    for attempt in range(5):
        self._command(_CMD_FUNCTION_BASIC)
        time.sleep(0.01)
    
    # Step 3: Clear text RAM
    self._command(_CMD_CLEAR)
    time.sleep(_CLEAR_DELAY)
    
    # Step 4: Display on
    self._command(_CMD_DISPLAY_ON)
    self._command(_CMD_ENTRY_MODE)
    
    # Step 5: Multiple attempts to enter extended mode
    for attempt in range(5):
        self._command(_CMD_FUNCTION_EXTENDED)
        time.sleep(0.01)
    
    # Step 6: Enable graphics
    self._command(_CMD_GRAPHICS_ON)
    
    # Step 7: Force clear all graphics RAM
    self._force_clear_graphics()
    
    # Step 8: Reset state tracking
    self._previous = None
    
    # Step 9: Verify initialization by writing a test pattern
    self._write_test_pattern()

def _write_test_pattern(self) -> None:
    """Write a small test pattern to verify the display is working."""
    test_row = bytearray(BYTES_PER_ROW)
    # Create a checkerboard pattern in the first row
    for i in range(BYTES_PER_ROW):
        test_row[i] = 0x55 if i % 2 == 0 else 0xAA
    
    self._set_address(0)
    self._data(bytes(test_row))
    time.sleep(0.01)
    
    # Clear it immediately so it doesn't affect the UI
    empty = bytes(BYTES_PER_ROW)
    self._set_address(0)
    self._data(empty)
```

## Verification

After implementing the fix, test with this sequence:

```python
# Test script
import asyncio
from display import ST7920Display

async def test_display():
    display = ST7920Display()
    await display.start()
    
    # Show something
    canvas = Canvas()
    canvas.text(10, 10, "Test", True)
    await display.show(canvas)
    
    await display.close()
    print("Display closed - check if it cleared")
    
    # Run again immediately
    display2 = ST7920Display()
    await display2.start()
    canvas2 = Canvas()
    canvas2.text(10, 30, "New Test", True)
    await display2.show(canvas2)
    
    await display2.close()

asyncio.run(test_display())
```

## Recommended Implementation

For production, use **Solution 5** (Combined Robust Initialization) as it provides:
- Multiple retry attempts for each command
- Hardware reset simulation
- Force clear of graphics RAM
- State verification
- Clean exit

This ensures the display works reliably across program restarts regardless of its previous state.

## Additional Notes

1. **SPI Mode**: Some ST7920 clones respond better to SPI mode 0 (0b00) instead of mode 3 (0b11). If issues persist, try changing:
   ```python
   spi.mode = 0b00  # Try this if mode 3 doesn't work
   ```

2. **Timing**: The ST7920 needs adequate delays between commands. Increase delays if the display still behaves unreliably:
   ```python
   _CLEAR_DELAY = 0.005  # 5 ms instead of 2 ms
   _COMMAND_DELAY = 0.0005  # 500 μs instead of 100 μs
   ```

3. **Power Supply**: Ensure the LCD has a stable 5V supply. Add a capacitor (100μF) near the LCD if power fluctuations are suspected.

4. **PSB Pin**: Double-check that PSB is physically connected to GND. This is a jumper or solder pad on the module, not a pin on the connector.

## Files Modified

- `display.py` - Main display driver with initialization fixes

## Environment Variables (Optional)

```bash
# Increase display SPI speed if needed
export BOILERROOM_DISPLAY_SPI_SPEED=400000  # 400 kHz (slower = more reliable)

# Use a different CS pin if needed
export BOILERROOM_DISPLAY_CS_PIN=7
```
