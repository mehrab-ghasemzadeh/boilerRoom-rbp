# """
# ST7920 128x64 graphical LCD, driven over SPI.

# Wiring (BCM numbering)::

#     ST7920      Pi
#     SID    ->   GPIO10  MOSI
#     CLK    ->   GPIO11  SCLK
#     CS     ->   GPIO7   CE1
#     VCC    ->   5V
#     GND    ->   GND

# The panel shares the SPI bus with the gas ADC, which sits on CE0. Chip selects
# are separate and the kernel serialises transfers, so the two do not need to
# know about each other — but they do run at different clocks and in different
# SPI modes, which is why each opens its own ``SpiDev`` handle rather than
# sharing one.

# Two things about this controller are easy to get wrong and expensive to debug:

# * **CS is active HIGH.** Every other SPI peripheral on the header asserts chip
#   select low; the ST7920 asserts it high. ``spidev`` can be told this, and is,
#   below. A panel that stays blank with a perfectly good signal on SID and CLK
#   is almost always this.
# * **PSB must be tied LOW** for serial mode. It is a jumper or a solder pad on
#   the module — not a pin on the five-wire connector — and with it left high the
#   controller is listening for a parallel bus that is not there.

# Refreshes send only the rows that changed. A full frame is 64 rows of 16 bytes,
# which becomes about 2.5 KB once each byte is split into the two padded nibbles
# the serial protocol wants — 25 ms of bus time at 800 kHz, on a core that also
# has boilers to run. Moving the selection down a menu changes two rows, so it
# costs about a millisecond.

# Every SPI call blocks, so each one goes through ``asyncio.to_thread`` like the
# rest of the hardware here.
# """

# from __future__ import annotations

# import asyncio
# import os
# import time

# import spidev

# from config import DISPLAY_SPI_BUS, DISPLAY_SPI_DEVICE, DISPLAY_SPI_SPEED
# from display_canvas import BYTES_PER_ROW, HEIGHT, WIDTH, Canvas

# # Sync bytes that open a serial transfer: bit pattern 11111, then RW and RS.
# _SYNC_COMMAND = 0xF8
# _SYNC_DATA = 0xFA

# # Basic instruction set
# _CMD_FUNCTION_BASIC = 0x30
# _CMD_DISPLAY_ON = 0x0C
# _CMD_CLEAR = 0x01
# _CMD_ENTRY_MODE = 0x06

# # Extended instruction set: graphics off, then graphics on.
# _CMD_FUNCTION_EXTENDED = 0x34
# _CMD_GRAPHICS_ON = 0x36

# # The controller needs 1.6 ms to clear its text RAM, and about 72 us for the
# # rest. Only the init sequence waits — the graphics writes below are paced by
# # the bus itself.
# _CLEAR_DELAY = 0.002
# _COMMAND_DELAY = 0.0001


# def _env_int(name: str, default: int) -> int:
#     raw = (os.environ.get(name) or "").strip()
#     if not raw:
#         return default
#     try:
#         return int(raw)
#     except ValueError:
#         return default


# def _env_flag(name: str, default: bool) -> bool:
#     raw = (os.environ.get(name) or "").strip().lower()
#     if not raw:
#         return default
#     return raw in ("on", "1", "true", "yes")


# class DisplayError(RuntimeError):
#     """Raised when the panel cannot be brought up."""


# class ST7920Display:
#     """The 128x64 panel on the SPI header."""

#     name = "ST7920 128x64 LCD"
#     width = WIDTH
#     height = HEIGHT
#     available = True

#     def __init__(
#         self,
#         bus: int | None = None,
#         device: int | None = None,
#         speed_hz: int | None = None,
#     ):
#         self.bus = _env_int("BOILERROOM_DISPLAY_SPI_BUS", DISPLAY_SPI_BUS) if bus is None else bus
#         self.device = (
#             _env_int("BOILERROOM_DISPLAY_SPI_DEVICE", DISPLAY_SPI_DEVICE)
#             if device is None
#             else device
#         )
#         self.speed_hz = (
#             _env_int("BOILERROOM_DISPLAY_SPI_SPEED", DISPLAY_SPI_SPEED)
#             if speed_hz is None
#             else speed_hz
#         )
#         self.cs_high = _env_flag("BOILERROOM_DISPLAY_CS_HIGH", True)

#         self._spi: spidev.SpiDev | None = None
#         # The last frame actually on the glass, so a refresh can send only what
#         # moved. None forces the next flush to send everything.
#         self._previous: bytearray | None = None

#     # -- lifecycle -----------------------------------------------------------

#     async def start(self) -> None:
#         try:
#             await asyncio.to_thread(self._open)
#         except Exception as exc:
#             raise DisplayError(
#                 f"could not open SPI {self.bus}.{self.device} for the display: {exc}"
#             ) from exc

#     def _open(self) -> None:
#         spi = spidev.SpiDev()
#         spi.open(self.bus, self.device)
#         spi.max_speed_hz = self.speed_hz
#         # Idle-low clock, sampled on the rising edge.
#         spi.mode = 0b00

#         if self.cs_high:
#             # The ST7920 asserts chip select high, unlike everything else on
#             # this header. Some kernels refuse the flag after open(); the panel
#             # can still be driven with CS strapped high in hardware, so this is
#             # not worth failing over.
#             try:
#                 spi.cshigh = True
#             except Exception:
#                 pass

#         self._spi = spi
#         self._previous = None
#         self._initialise()

#     def _initialise(self) -> None:
#         time.sleep(0.05)  # the controller's own power-on settling time
#         self._command(_CMD_FUNCTION_BASIC)
#         self._command(_CMD_DISPLAY_ON)
#         self._command(_CMD_CLEAR)
#         time.sleep(_CLEAR_DELAY)
#         self._command(_CMD_ENTRY_MODE)
#         self._command(_CMD_FUNCTION_EXTENDED)
#         self._command(_CMD_GRAPHICS_ON)
#         self._blank()

#     async def close(self) -> None:
#         if self._spi is None:
#             return
#         await asyncio.to_thread(self._close_sync)

#     def _close_sync(self) -> None:
#         """
#         Release the bus, leaving the last frame on the glass.

#         An unpowered panel holds whatever it was last sent, and the menu draws
#         a screen saying the agent has stopped before it gets here — which is
#         worth more to whoever walks up to it than a blank display.
#         """
#         spi = self._spi
#         self._spi = None
#         if spi is None:
#             return
#         try:
#             spi.close()
#         except Exception:
#             pass

#     # -- serial protocol -----------------------------------------------------
#     #
#     # Each byte goes out as two bytes: the high nibble padded with zeros, then
#     # the low nibble padded the same way. A burst of data needs only one sync
#     # byte at the front, which is what makes a row of graphics RAM affordable.

#     def _command(self, value: int) -> None:
#         spi = self._spi
#         if spi is None:
#             return
#         spi.writebytes([_SYNC_COMMAND, value & 0xF0, (value << 4) & 0xF0])
#         time.sleep(_COMMAND_DELAY)

#     def _data(self, payload: bytes) -> None:
#         spi = self._spi
#         if spi is None:
#             return
#         message = [_SYNC_DATA]
#         for byte in payload:
#             message.append(byte & 0xF0)
#             message.append((byte << 4) & 0xF0)
#         spi.writebytes(message)

#     def _set_address(self, row: int) -> None:
#         """
#         Point the controller at one 128-pixel row of graphics RAM.

#         The address is a vertical 0-31 and a horizontal word 0-15. The lower
#         half of the panel is not addressed as rows 32-63: it is the same 32
#         vertical addresses, at horizontal words 8-15.
#         """
#         self._command(0x80 | (row & 0x1F))
#         self._command(0x80 | (0 if row < 32 else 8))

#     def _blank(self) -> None:
#         empty = bytes(BYTES_PER_ROW)
#         for row in range(HEIGHT):
#             self._set_address(row)
#             self._data(empty)
#         self._previous = bytearray(BYTES_PER_ROW * HEIGHT)

#     # -- drawing -------------------------------------------------------------

#     async def show(self, canvas: Canvas) -> int:
#         """Push a frame. Returns how many rows had to be sent."""
#         if self._spi is None:
#             return 0
#         return await asyncio.to_thread(self._flush, canvas.snapshot())

#     def _flush(self, frame: bytes) -> int:
#         previous = self._previous
#         sent = 0

#         for row in range(HEIGHT):
#             start = row * BYTES_PER_ROW
#             end = start + BYTES_PER_ROW
#             chunk = frame[start:end]
#             if previous is not None and previous[start:end] == chunk:
#                 continue
#             self._set_address(row)
#             self._data(chunk)
#             sent += 1

#         self._previous = bytearray(frame)
#         return sent

#     async def clear(self) -> None:
#         if self._spi is None:
#             return
#         await asyncio.to_thread(self._blank)

#     def describe(self) -> list[str]:
#         return [
#             f"Display: {self.name} on SPI {self.bus}.{self.device} "
#             f"at {self.speed_hz / 1000:.0f} kHz"
#             + (", CS active high" if self.cs_high else "")
#         ]


"""
ST7920 128x64 graphical LCD, driven through GPIO bit-banged serial mode.

Wiring (BCM numbering):

    ST7920      Raspberry Pi
    --------------------------------
    PSB     ->  GPIO6      (held LOW: serial mode)
    SID     ->  GPIO24     (serial data)
    CLK     ->  GPIO19     (serial clock)
    CS      ->  GPIO23     (chip select / enable)

    VCC     ->  Physical 5V power pin
    GND     ->  Physical GND pin

IMPORTANT:
    Do not power the LCD from GPIO2 or ground it through GPIO9.
    Use the Raspberry Pi's actual physical 5V and GND pins.

The ST7920 is operated in serial mode by holding PSB LOW.

This implementation replaces hardware SPI/spidev with GPIO bit-banging,
while preserving the original asynchronous API and differential row refresh.

Every blocking display operation runs through asyncio.to_thread() so the
asyncio event loop is not blocked.
"""

from __future__ import annotations

import asyncio
import os
import time

import RPi.GPIO as GPIO

from display_canvas import BYTES_PER_ROW, HEIGHT, WIDTH, Canvas


# ============================================================================
# GPIO MAPPING
# ============================================================================

# ST7920 PSB: LOW = serial mode
PSB_PIN = 6

# ST7920 serial interface
SID_PIN = 24
CLK_PIN = 19
CS_PIN = 23


# ============================================================================
# ST7920 SERIAL PROTOCOL
# ============================================================================

# Sync bytes:
#
# 11111 RW RS 0
#
# Command: RW=0, RS=0
# Data:    RW=0, RS=1

_SYNC_COMMAND = 0xF8
_SYNC_DATA = 0xFA


# ============================================================================
# ST7920 COMMANDS
# ============================================================================

# Basic instruction set
_CMD_FUNCTION_BASIC = 0x30
_CMD_DISPLAY_ON = 0x0C
_CMD_CLEAR = 0x01
_CMD_ENTRY_MODE = 0x06

# Extended instruction set
_CMD_FUNCTION_EXTENDED = 0x34
_CMD_GRAPHICS_ON = 0x36


# ============================================================================
# TIMING
# ============================================================================

# Controller power-on settling time
_POWER_ON_DELAY = 0.05

# Clear command execution time
_CLEAR_DELAY = 0.002

# Normal command execution time
_COMMAND_DELAY = 0.0001


# ============================================================================
# ENVIRONMENT HELPERS
# ============================================================================

def _env_int(name: str, default: int) -> int:
    """Read an integer environment variable safely."""
    raw = (os.environ.get(name) or "").strip()

    if not raw:
        return default

    try:
        return int(raw)
    except ValueError:
        return default


class DisplayError(RuntimeError):
    """Raised when the ST7920 panel cannot be initialized."""


class ST7920Display:
    """
    Driver for a 128x64 ST7920 graphical LCD.

    The public API is intentionally compatible with the previous SPI version:

        await display.start()
        await display.show(canvas)
        await display.clear()
        await display.close()

    The driver uses GPIO bit-banging instead of spidev.
    """

    name = "ST7920 128x64 LCD (GPIO Serial)"

    width = WIDTH
    height = HEIGHT

    available = True

    def __init__(
        self,
        psb_pin: int | None = None,
        sid_pin: int | None = None,
        clk_pin: int | None = None,
        cs_pin: int | None = None,
    ):
        """
        Create the display driver.

        Pins can be overridden through constructor arguments or environment
        variables.

        Environment variables:

            BOILERROOM_DISPLAY_PSB_PIN
            BOILERROOM_DISPLAY_SID_PIN
            BOILERROOM_DISPLAY_CLK_PIN
            BOILERROOM_DISPLAY_CS_PIN
        """

        self.psb_pin = (
            _env_int(
                "BOILERROOM_DISPLAY_PSB_PIN",
                PSB_PIN,
            )
            if psb_pin is None
            else psb_pin
        )

        self.sid_pin = (
            _env_int(
                "BOILERROOM_DISPLAY_SID_PIN",
                SID_PIN,
            )
            if sid_pin is None
            else sid_pin
        )

        self.clk_pin = (
            _env_int(
                "BOILERROOM_DISPLAY_CLK_PIN",
                CLK_PIN,
            )
            if clk_pin is None
            else clk_pin
        )

        self.cs_pin = (
            _env_int(
                "BOILERROOM_DISPLAY_CS_PIN",
                CS_PIN,
            )
            if cs_pin is None
            else cs_pin
        )

        self._started = False

        # Stores the last frame actually sent to the LCD.
        #
        # None means that the next refresh must send the entire frame.
        self._previous: bytearray | None = None

        # Delay between GPIO transitions.
        #
        # Python GPIO bit-banging is much slower than hardware SPI, so this
        # defaults to zero. The GPIO calls themselves provide sufficient delay
        # for the ST7920 on a Raspberry Pi.
        self._bit_delay = 0.0

    # ========================================================================
    # LIFECYCLE
    # ========================================================================

    async def start(self) -> None:
        """
        Initialize GPIO and the ST7920 controller.
        """

        try:
            await asyncio.to_thread(self._open)

        except Exception as exc:
            raise DisplayError(
                f"could not initialize ST7920 display GPIO interface: {exc}"
            ) from exc

    def _open(self) -> None:
        """
        Configure the Raspberry Pi GPIO pins and initialize the LCD.
        """

        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)

        # PSB LOW -> serial mode
        GPIO.setup(
            self.psb_pin,
            GPIO.OUT,
            initial=GPIO.LOW,
        )

        # Serial data
        GPIO.setup(
            self.sid_pin,
            GPIO.OUT,
            initial=GPIO.LOW,
        )

        # Serial clock
        GPIO.setup(
            self.clk_pin,
            GPIO.OUT,
            initial=GPIO.LOW,
        )

        # Chip select.
        #
        # ST7920 serial interface is active HIGH.
        GPIO.setup(
            self.cs_pin,
            GPIO.OUT,
            initial=GPIO.LOW,
        )

        self._previous = None
        self._started = True

        self._initialise()

    async def close(self) -> None:
        """
        Release the GPIO pins.

        The last frame remains displayed.
        """

        if not self._started:
            return

        await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        """
        Return the pins used by the display to input mode.

        We deliberately do not clear the LCD before closing.
        """

        try:
            GPIO.output(
                self.cs_pin,
                GPIO.LOW,
            )

            GPIO.output(
                self.clk_pin,
                GPIO.LOW,
            )

            GPIO.output(
                self.sid_pin,
                GPIO.LOW,
            )

            GPIO.setup(
                self.psb_pin,
                GPIO.IN,
            )

            GPIO.setup(
                self.sid_pin,
                GPIO.IN,
            )

            GPIO.setup(
                self.clk_pin,
                GPIO.IN,
            )

            GPIO.setup(
                self.cs_pin,
                GPIO.IN,
            )

        except Exception:
            pass

        self._started = False
        self._previous = None

    # ========================================================================
    # INITIALIZATION
    # ========================================================================

    def _initialise(self) -> None:
        """
        Initialize the ST7920 and enable graphics mode.
        """

        # Allow controller power to stabilize.
        time.sleep(_POWER_ON_DELAY)

        # Basic instruction mode.
        self._command(_CMD_FUNCTION_BASIC)

        # Display ON.
        self._command(_CMD_DISPLAY_ON)

        # Clear controller memory.
        self._command(_CMD_CLEAR)

        time.sleep(_CLEAR_DELAY)

        # Entry mode.
        self._command(_CMD_ENTRY_MODE)

        # Extended instruction set.
        self._command(_CMD_FUNCTION_EXTENDED)

        # Enable graphics mode.
        self._command(_CMD_GRAPHICS_ON)

        # Clear the graphics display.
        self._blank()

    # ========================================================================
    # GPIO SERIAL TRANSPORT
    # ========================================================================

    def _delay(self) -> None:
        """Optional delay between GPIO transitions."""

        if self._bit_delay > 0:
            time.sleep(self._bit_delay)

    def _begin_transfer(self) -> None:
        """
        Select the ST7920.

        The ST7920 serial chip select is active HIGH.
        """

        GPIO.output(
            self.cs_pin,
            GPIO.HIGH,
        )

        self._delay()

    def _end_transfer(self) -> None:
        """Deselect the ST7920."""

        GPIO.output(
            self.cs_pin,
            GPIO.LOW,
        )

        self._delay()

    def _write_byte(self, value: int) -> None:
        """
        Write one byte MSB first through GPIO.

        Clock mode is equivalent to SPI mode 0:

            CLK idle = LOW
            data sampled on rising edge
        """

        value &= 0xFF

        for bit in range(7, -1, -1):

            bit_value = (
                GPIO.HIGH
                if value & (1 << bit)
                else GPIO.LOW
            )

            # Set data while clock is LOW.
            GPIO.output(
                self.sid_pin,
                bit_value,
            )

            self._delay()

            # Rising edge.
            GPIO.output(
                self.clk_pin,
                GPIO.HIGH,
            )

            self._delay()

            # Return clock to idle LOW.
            GPIO.output(
                self.clk_pin,
                GPIO.LOW,
            )

            self._delay()

    def _write_bytes(self, values: list[int]) -> None:
        """
        Send multiple bytes in one ST7920 serial transfer.
        """

        self._begin_transfer()

        try:
            for value in values:
                self._write_byte(value)

        finally:
            self._end_transfer()

    # ========================================================================
    # ST7920 SERIAL PROTOCOL
    # ========================================================================

    def _command(self, value: int) -> None:
        """
        Send one ST7920 command.

        ST7920 serial format:

            F8
            command high nibble << 4
            command low nibble << 4
        """

        if not self._started:
            return

        message = [
            _SYNC_COMMAND,
            value & 0xF0,
            (value << 4) & 0xF0,
        ]

        self._write_bytes(message)

        time.sleep(_COMMAND_DELAY)

    def _data(self, payload: bytes) -> None:
        """
        Send graphics data.

        One data sync byte is sent followed by every payload byte split into
        two padded nibbles.
        """

        if not self._started:
            return

        message = [_SYNC_DATA]

        for byte in payload:

            message.append(
                byte & 0xF0
            )

            message.append(
                (byte << 4) & 0xF0
            )

        self._write_bytes(message)

    # ========================================================================
    # GRAPHICS RAM ADDRESSING
    # ========================================================================

    def _set_address(self, row: int) -> None:
        """
        Point the ST7920 at one logical 128-pixel graphics row.

        Rows 0-31:
            vertical address = row
            horizontal word = 0

        Rows 32-63:
            vertical address = row - 32
            horizontal word = 8
        """

        # Vertical address.
        self._command(
            0x80 | (row & 0x1F)
        )

        # Horizontal word.
        self._command(
            0x80 | (
                0
                if row < 32
                else 8
            )
        )

    # ========================================================================
    # FULL SCREEN CLEAR
    # ========================================================================

    def _blank(self) -> None:
        """
        Clear all 64 graphics rows.
        """

        empty = bytes(
            BYTES_PER_ROW
        )

        for row in range(HEIGHT):

            self._set_address(row)

            self._data(empty)

        self._previous = bytearray(
            BYTES_PER_ROW * HEIGHT
        )

    # ========================================================================
    # DRAWING / DIFFERENTIAL REFRESH
    # ========================================================================

    async def show(
        self,
        canvas: Canvas,
    ) -> int:
        """
        Push a Canvas frame to the LCD.

        Only rows that differ from the previous frame are transmitted.

        Returns:
            Number of rows sent.
        """

        if not self._started:
            return 0

        frame = canvas.snapshot()

        return await asyncio.to_thread(
            self._flush,
            frame,
        )

    def _flush(
        self,
        frame: bytes,
    ) -> int:
        """
        Compare the frame with the previous frame and send only changed rows.
        """

        previous = self._previous

        sent = 0

        for row in range(HEIGHT):

            start = (
                row * BYTES_PER_ROW
            )

            end = (
                start + BYTES_PER_ROW
            )

            chunk = frame[start:end]

            # Skip rows that have not changed.
            if (
                previous is not None
                and previous[start:end] == chunk
            ):
                continue

            self._set_address(row)

            self._data(chunk)

            sent += 1

        # Store a copy of the frame actually transmitted.
        self._previous = bytearray(
            frame
        )

        return sent

    async def clear(self) -> None:
        """
        Clear the entire graphics display.
        """

        if not self._started:
            return

        await asyncio.to_thread(
            self._blank
        )

    # ========================================================================
    # STATUS / DEBUG
    # ========================================================================

    def describe(self) -> list[str]:
        """
        Return a human-readable description of the display configuration.
        """

        return [
            f"Display: {self.name}",
            (
                f"PSB=GPIO{self.psb_pin} LOW, "
                f"SID=GPIO{self.sid_pin}, "
                f"CLK=GPIO{self.clk_pin}, "
                f"CS=GPIO{self.cs_pin} active high"
            ),
        ]