"""
ST7920 128x64 graphical LCD, driven over hardware SPI.

Wiring (BCM numbering, physical pin in brackets)::

    ST7920      Pi
    SID    ->   GPIO10  MOSI    [19]
    CLK    ->   GPIO11  SCLK    [23]
    CS     ->   GPIO8   CE0     [24]   driven by hand, not by the kernel
    PSB    ->   GND on the module (a solder pad, not a header pin)
    VCC    ->   5V
    GND    ->   GND

Do not power the panel from a GPIO pin or ground it through one. Use the
header's own 5V and GND.

Four things about this controller are easy to get wrong and expensive to debug:

* **CS is active HIGH.** Every other SPI peripheral on the header asserts chip
  select low; the ST7920 asserts it high. The kernel cannot drive it that way,
  so the handle is opened with ``no_cs`` and CS is driven as an ordinary GPIO
  around each transfer. A panel that stays blank with good signal on SID and
  CLK is almost always this.
* **PSB must be LOW** for serial mode. On this module it is a solder pad rather
  than a pin on the connector, so nothing here drives it. Where a panel does
  bring it out, ``BOILERROOM_DISPLAY_PSB_PIN`` makes this drive it and go on
  holding it after close -- see ``_close_sync`` -- but note that the obvious
  choice, GPIO6, is the keypad's column 1.
* **Mode 3.** Clock idles high, sampled on the rising edge. Some clones want
  mode 0; ``BOILERROOM_DISPLAY_SPI_MODE`` switches.
* **The controller is never reset.** There is no RST line here, so every run
  after the first one opens onto a controller that is still exactly where the
  last run left it: extended instruction set, graphics on, its RAM intact, and
  -- if the process was killed mid-transfer -- part way through a frame. This
  is what used to leave the panel frozen on the previous run's last screen.
  ``_bus_reset`` and the repeated function sets in ``_initialise`` exist for
  that alone, and everything here is written to be safe to run twice.

**Chip select and the gas ADC.** The ADC in ``gas_reader`` sits on SPI0 CE0 and
lets the kernel drive its chip select; this panel defaults to the same CE0 with
the kernel's chip select disabled, because that is the wiring the bring-up
sample was proven on. Both cannot have GPIO8 at once. If the two are on one
bus, move the panel to CE1 -- ``BOILERROOM_DISPLAY_SPI_DEVICE=1`` and
``BOILERROOM_DISPLAY_CS_PIN=7`` -- and rewire CS to physical pin 26.
``describe()`` says so out loud when it sees the clash.

Refreshes send only the rows that changed. Every SPI call blocks, so each one
goes through ``asyncio.to_thread`` like the rest of the hardware here.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import time

import spidev

import RPi.GPIO as GPIO

from config import (
    DISPLAY_SPI_BUS,
    DISPLAY_SPI_DEVICE,
    DISPLAY_SPI_MODE,
    DISPLAY_SPI_SPEED,
)
from display_canvas import BYTES_PER_ROW, HEIGHT, WIDTH, Canvas


# ============================================================================
# DEFAULTS
# ============================================================================

# Chip select, driven as a plain GPIO because the ST7920 wants it active high.
DISPLAY_CS_GPIO = 8

# Serial-mode select.
#
# None, because on this build the module ties PSB low itself -- it is a solder
# pad, not a wire on the connector -- and GPIO6 is the keypad's column 1.
# Physical pin 31 cannot be both. Set BOILERROOM_DISPLAY_PSB_PIN only on a
# panel that really does bring PSB out to the header, and then move the keypad
# column with BOILERROOM_KEYPAD_COLS.
DISPLAY_PSB_GPIO = None


# ============================================================================
# ST7920 SERIAL PROTOCOL
# ============================================================================

# Sync bytes: 11111, then RW and RS.
#
#   command: RW=0 RS=0
#   data:    RW=0 RS=1
_SYNC_COMMAND = 0xF8
_SYNC_DATA = 0xFA


# ============================================================================
# ST7920 COMMANDS
# ============================================================================

# Basic instruction set
_CMD_FUNCTION_BASIC = 0x30
_CMD_DISPLAY_ON = 0x0C
_CMD_DISPLAY_OFF = 0x08
_CMD_CLEAR = 0x01
_CMD_ENTRY_MODE = 0x06
_CMD_RETURN_HOME = 0x02

# Extended instruction set: graphics off, then graphics on.
_CMD_FUNCTION_EXTENDED = 0x34
_CMD_GRAPHICS_ON = 0x36


# ============================================================================
# TIMING
# ============================================================================

# Controller settling time before the first instruction.
_POWER_ON_DELAY = 0.1

# After a function set. The controller needs longer to change instruction sets
# than it does for an ordinary command, and this is the one place where being
# impatient costs the whole session.
_FUNCTION_SET_DELAY = 0.01

# Display Clear is the slowest instruction there is: 1.6 ms on paper, and clone
# controllers have been seen to want more.
_CLEAR_DELAY = 0.005

# After an ordinary command.
_COMMAND_DELAY = 0.001

# Between rows of a forced clear, which is the one loop that runs against a
# controller in an unknown state.
_ROW_DELAY = 0.001

# How many times to repeat a function set before believing it. A controller
# that is mid-frame will eat the first one or two as data.
_FUNCTION_SET_REPEATS = 5

# Zero bytes clocked out to flush a half-received frame. A frame is three
# bytes, so three finishes the longest possible remainder; the fourth is slack.
_RESYNC_BYTES = 4


# ============================================================================
# ENVIRONMENT HELPERS
# ============================================================================


def _env_int(name: str, default: int) -> int:
    """Read an integer environment variable safely."""

    raw = (os.environ.get(name) or "").strip()

    if not raw:
        return default

    try:
        return int(raw, 0)
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    """Read a float environment variable safely."""

    raw = (os.environ.get(name) or "").strip()

    if not raw:
        return default

    try:
        return float(raw)
    except ValueError:
        return default


def _env_flag(name: str, default: bool) -> bool:
    """Read an on/off environment variable safely."""

    raw = (os.environ.get(name) or "").strip().lower()

    if not raw:
        return default

    if raw in ("on", "1", "true", "yes"):
        return True

    if raw in ("off", "0", "false", "no"):
        return False

    return default


def _env_pin(name: str, default: int | None) -> int | None:
    """
    Read a pin number, or None where the board wires it for us.

    ``none`` is spelled out rather than left blank, so that an empty variable
    still means "unset, use the default" like everywhere else here.
    """

    raw = (os.environ.get(name) or "").strip().lower()

    if not raw:
        return default

    if raw in ("none", "off", "-", "unused"):
        return None

    try:
        return int(raw, 0)
    except ValueError:
        return default


class DisplayError(RuntimeError):
    """Raised when the panel cannot be brought up."""


class ST7920Display:
    """The 128x64 panel on the SPI header."""

    name = "ST7920 128x64 LCD"

    width = WIDTH
    height = HEIGHT

    available = True

    def __init__(
        self,
        bus: int | None = None,
        device: int | None = None,
        speed_hz: int | None = None,
        mode: int | None = None,
        cs_pin: int | None = None,
        psb_pin: int | None = None,
    ):
        """
        Create the display driver.

        Everything is overridable from the constructor or the environment:

            BOILERROOM_DISPLAY_SPI_BUS
            BOILERROOM_DISPLAY_SPI_DEVICE
            BOILERROOM_DISPLAY_SPI_SPEED
            BOILERROOM_DISPLAY_SPI_MODE
            BOILERROOM_DISPLAY_CS_PIN
            BOILERROOM_DISPLAY_PSB_PIN      a pin, or "none"
            BOILERROOM_DISPLAY_CLEAR_ON_EXIT
            BOILERROOM_DISPLAY_SELFTEST
            BOILERROOM_DISPLAY_COMMAND_DELAY
            BOILERROOM_DISPLAY_DATA_DELAY
        """

        self.bus = (
            _env_int("BOILERROOM_DISPLAY_SPI_BUS", DISPLAY_SPI_BUS)
            if bus is None
            else bus
        )

        self.device = (
            _env_int("BOILERROOM_DISPLAY_SPI_DEVICE", DISPLAY_SPI_DEVICE)
            if device is None
            else device
        )

        self.speed_hz = (
            _env_int("BOILERROOM_DISPLAY_SPI_SPEED", DISPLAY_SPI_SPEED)
            if speed_hz is None
            else speed_hz
        )

        self.mode = (
            _env_int("BOILERROOM_DISPLAY_SPI_MODE", DISPLAY_SPI_MODE)
            if mode is None
            else mode
        )

        self.cs_pin = (
            _env_int("BOILERROOM_DISPLAY_CS_PIN", DISPLAY_CS_GPIO)
            if cs_pin is None
            else cs_pin
        )

        self.psb_pin = (
            _env_pin("BOILERROOM_DISPLAY_PSB_PIN", DISPLAY_PSB_GPIO)
            if psb_pin is None
            else psb_pin
        )

        # Clearing on the way out is what stops a stale frame outliving the
        # process that drew it. Turn it off to leave the last screen up -- the
        # menu draws one saying the agent has stopped, which is worth more to
        # somebody walking up to the panel than a blank rectangle.
        self.clear_on_exit = _env_flag("BOILERROOM_DISPLAY_CLEAR_ON_EXIT", True)

        # A checkerboard written and immediately erased at the end of init.
        # Off by default: this link is write-only, so it cannot report back
        # whether the panel took it, and all it costs a working device is a
        # visible flash on every start.
        self.selftest = _env_flag("BOILERROOM_DISPLAY_SELFTEST", False)

        self.command_delay = _env_float(
            "BOILERROOM_DISPLAY_COMMAND_DELAY", _COMMAND_DELAY
        )

        self.data_delay = _env_float("BOILERROOM_DISPLAY_DATA_DELAY", 0.0)

        self._spi: spidev.SpiDev | None = None

        self._started = False

        # The last frame actually on the glass, so a refresh can send only what
        # moved. None forces the next flush to send everything.
        self._previous: bytearray | None = None

        # Held across every transfer, and across open and close. Two threads on
        # this bus at once would interleave their bytes, and a close landing in
        # the middle of a refresh would drop CS half way through a frame. Both
        # leave the controller holding an incomplete frame, and that is a fault
        # which outlives the process -- see _bus_reset.
        #
        # Reentrant because _open holds it while _initialise calls _blank.
        self._bus_lock = threading.RLock()

    # ========================================================================
    # LIFECYCLE
    # ========================================================================

    async def start(self) -> None:
        """Claim the bus and bring the controller up."""

        try:
            await asyncio.to_thread(self._open)

        except Exception as exc:
            raise DisplayError(
                f"could not open SPI {self.bus}.{self.device} "
                f"for the display: {exc}"
            ) from exc

    def _open(self) -> None:
        """
        Claim the pins and the SPI handle, then initialize the controller.

        Nothing here may assume a freshly powered panel. The GPIO configuration
        outlives the process that set it, and the controller keeps its mode,
        its instruction set and its RAM across a restart of the agent.
        """

        GPIO.setwarnings(False)
        GPIO.setmode(GPIO.BCM)

        with self._bus_lock:
            # PSB first, and held: if this pin was left floating, the module's
            # own pull-up has had it HIGH, and a controller in parallel mode
            # ignores the serial lines completely.
            if self.psb_pin is not None:
                GPIO.setup(self.psb_pin, GPIO.OUT, initial=GPIO.LOW)

            # Chip select, deasserted. Active HIGH on this controller, so LOW
            # is deselected.
            GPIO.setup(self.cs_pin, GPIO.OUT, initial=GPIO.LOW)

            spi = spidev.SpiDev()
            spi.open(self.bus, self.device)
            spi.max_speed_hz = self.speed_hz
            spi.mode = self.mode
            # The kernel's chip select is active low and cannot be inverted, so
            # it is left out of it entirely and CS is driven above.
            spi.no_cs = True

            self._spi = spi
            self._previous = None
            self._started = True

            try:
                self._bus_reset()
                self._initialise()

            except Exception:
                self._started = False
                self._spi = None

                try:
                    spi.close()
                except Exception:
                    pass

                raise

    def _bus_reset(self) -> None:
        """
        Put the controller's serial receiver back in step.

        A transfer is three bytes: a sync byte, then the two padded nibbles of
        one value. Stop the agent part way through one -- Ctrl-C during a
        refresh, or SIGTERM from systemd -- and the controller is left holding
        an incomplete frame, waiting for the bits that would finish it. It has
        no timeout; it waits across the shutdown and into the next process.

        Everything the next run sends then lands one frame behind. Its sync
        bytes are swallowed as the tail of the stale frame, its commands are
        read as sync bytes, and nothing it asks for happens -- so the panel
        goes on showing the last frame of the previous run, which is
        indistinguishable from a hung device.

        Two things fix that. Zero bytes finish the stale frame: a sync byte is
        five consecutive 1 bits, so zeros can never begin one, only complete
        one, which is then discarded as an instruction that does not exist.
        And deselecting clears the receiver whatever state it ended in, which
        is what the deliberate CS pattern below is for.
        """

        spi = self._spi

        if spi is None:
            return

        GPIO.output(self.cs_pin, GPIO.LOW)
        time.sleep(0.05)

        # Selected, with nothing but zeros: finishes a partial frame.
        GPIO.output(self.cs_pin, GPIO.HIGH)
        time.sleep(0.05)
        spi.xfer2([0x00] * _RESYNC_BYTES)

        GPIO.output(self.cs_pin, GPIO.LOW)
        time.sleep(0.05)

        # And once more, so a controller that came up mid-instruction sees a
        # clean select/deselect edge before the first real command.
        GPIO.output(self.cs_pin, GPIO.HIGH)
        time.sleep(0.05)

        GPIO.output(self.cs_pin, GPIO.LOW)
        time.sleep(0.05)

    def _initialise(self) -> None:
        """
        Bring the controller to basic mode, then graphics, then blank it.

        Written to be safe on a controller that was never power cycled: on a
        restart it is still where the previous run left it, in the extended
        instruction set with graphics on. Each function set is therefore sent
        several times over. The first one is what a listening controller acts
        on; the rest cost a few milliseconds and cover the case where it was
        not listening yet.
        """

        time.sleep(_POWER_ON_DELAY)

        # Basic instruction set.
        for _ in range(_FUNCTION_SET_REPEATS):
            self._command(_CMD_FUNCTION_BASIC)
            time.sleep(_FUNCTION_SET_DELAY)

        # Clear the text RAM. This does *not* touch the graphics RAM, which is
        # why the frozen frame survived it -- see _force_clear_graphics.
        self._command(_CMD_CLEAR)
        time.sleep(_CLEAR_DELAY)

        self._command(_CMD_DISPLAY_ON)
        self._command(_CMD_ENTRY_MODE)
        self._command(_CMD_RETURN_HOME)

        # Extended instruction set, then graphics on, then wipe what the last
        # run left in the graphics RAM.
        self._force_clear_graphics()

        # Whatever was on the glass is gone, so nothing may be assumed about
        # what a differential refresh can skip.
        self._previous = bytearray(BYTES_PER_ROW * HEIGHT)

        if self.selftest:
            self._self_test()

    def _force_clear_graphics(self) -> None:
        """
        Write zeros to every graphics address, from an unknown starting state.

        Unlike ``_blank`` this re-asserts the instruction set and the graphics
        bit first, several times each, rather than trusting that ``_initialise``
        got there. It is the one operation that has to work on a controller
        which may still be ignoring us.
        """

        for _ in range(_FUNCTION_SET_REPEATS):
            self._command(_CMD_FUNCTION_EXTENDED)
            time.sleep(_FUNCTION_SET_DELAY)

        for _ in range(_FUNCTION_SET_REPEATS):
            self._command(_CMD_GRAPHICS_ON)
            time.sleep(_FUNCTION_SET_DELAY)

        empty = bytes(BYTES_PER_ROW)

        for row in range(HEIGHT):
            self._set_address(row)
            self._data(empty)
            time.sleep(_ROW_DELAY)

        self._previous = bytearray(BYTES_PER_ROW * HEIGHT)

    def _self_test(self) -> None:
        """
        Write a checkerboard to the top row and erase it again.

        Proves nothing on its own -- this link is write-only -- but it is
        visible, so on a panel that shows the flash and nothing else the fault
        is above this driver rather than in it.
        """

        pattern = bytes(
            0x55 if index % 2 == 0 else 0xAA for index in range(BYTES_PER_ROW)
        )

        self._set_address(0)
        self._data(pattern)
        time.sleep(0.05)

        self._set_address(0)
        self._data(bytes(BYTES_PER_ROW))

    async def close(self) -> None:
        """Stop driving the panel and release the SPI handle."""

        if not self._started:
            return

        await asyncio.to_thread(self._close_sync)

    def _close_sync(self) -> None:
        """
        Clear the glass, then let the bus go.

        Clearing is the point: a frame left in the graphics RAM outlives the
        process, and the controller is not reset when the next one starts, so
        whatever is on the panel at exit is what an operator sees until
        something overwrites it. Leaving the agent's last screen up looks like
        a running device.

        The pins stay claimed, and PSB stays driven LOW. GPIO configuration
        survives this process, and a released PSB is taken HIGH by the pull-up
        on the module -- which puts the controller into parallel mode, where
        the next run's traffic is ignored entirely.
        """

        with self._bus_lock:
            spi = self._spi

            try:
                if spi is not None and self.clear_on_exit:
                    self._blank_locked()
                    time.sleep(0.01)

                    self._command(_CMD_FUNCTION_BASIC)
                    self._command(_CMD_DISPLAY_OFF)

            except Exception:
                pass  # a panel we cannot talk to is not worth failing shutdown

            self._spi = None
            self._started = False
            self._previous = None

            try:
                GPIO.output(self.cs_pin, GPIO.LOW)

                if self.psb_pin is not None:
                    GPIO.output(self.psb_pin, GPIO.LOW)

            except Exception:
                pass

            if spi is not None:
                try:
                    spi.close()
                except Exception:
                    pass

    # ========================================================================
    # SPI TRANSPORT
    # ========================================================================

    def _transfer(self, payload: list[int]) -> None:
        """
        One selected transfer: assert CS, send, deassert.

        Deasserting every time is deliberate. It is slower than holding CS
        across a whole row, and it is what makes the controller's receiver
        self-correcting: a byte lost to a signal-integrity problem costs one
        value rather than every value after it.
        """

        spi = self._spi

        if spi is None:
            return

        GPIO.output(self.cs_pin, GPIO.HIGH)

        try:
            spi.xfer2(payload)

        finally:
            GPIO.output(self.cs_pin, GPIO.LOW)

    def _command(self, value: int) -> None:
        """
        Send one command.

        Three bytes: the sync byte, the high nibble padded, the low nibble
        padded.
        """

        value &= 0xFF

        self._transfer(
            [
                _SYNC_COMMAND,
                value & 0xF0,
                (value << 4) & 0xF0,
            ]
        )

        if self.command_delay > 0:
            time.sleep(self.command_delay)

    def _data(self, payload: bytes) -> None:
        """
        Send graphics data, one byte per transfer.

        The controller's address counter advances on its own as the bytes
        arrive, so a row does not have to be sent inside a single chip select.
        """

        for byte in payload:
            self._transfer(
                [
                    _SYNC_DATA,
                    byte & 0xF0,
                    (byte << 4) & 0xF0,
                ]
            )

            if self.data_delay > 0:
                time.sleep(self.data_delay)

    # ========================================================================
    # GRAPHICS RAM ADDRESSING
    # ========================================================================

    def _set_address(self, row: int) -> None:
        """
        Point the controller at one logical 128-pixel graphics row.

        Rows 0-31 are vertical address ``row`` at horizontal word 0; rows 32-63
        are vertical address ``row - 32`` at horizontal word 8. The panel is
        two 128x32 halves side by side internally, which is why the bottom half
        is addressed as a horizontal offset rather than a vertical one.
        """

        self._command(0x80 | (row & 0x1F))
        self._command(0x80 | (0 if row < 32 else 8))

    # ========================================================================
    # DRAWING
    # ========================================================================

    def _blank(self) -> None:
        """Clear all 64 graphics rows."""

        with self._bus_lock:
            self._blank_locked()

    def _blank_locked(self) -> None:
        """Clear every row. The caller holds the bus."""

        empty = bytes(BYTES_PER_ROW)

        for row in range(HEIGHT):
            self._set_address(row)
            self._data(empty)

        self._previous = bytearray(BYTES_PER_ROW * HEIGHT)

    async def show(self, canvas: Canvas) -> int:
        """
        Push a Canvas frame to the panel.

        Only rows that differ from the previous frame are transmitted. Returns
        the number of rows sent.
        """

        if not self._started:
            return 0

        frame = canvas.snapshot()

        return await asyncio.to_thread(self._flush, frame)

    def _flush(self, frame: bytes) -> int:
        """
        Send the rows that changed.

        Serialised against everything else on the bus: a refresh interrupted
        part way through -- by a close, or by another thread starting its own
        transfer -- leaves the controller waiting for the rest of a frame that
        never arrives.
        """

        with self._bus_lock:
            if not self._started:
                return 0

            return self._flush_locked(frame)

    def _flush_locked(self, frame: bytes) -> int:
        """Compare against the previous frame and send the difference."""

        previous = self._previous

        sent = 0

        for row in range(HEIGHT):
            start = row * BYTES_PER_ROW
            end = start + BYTES_PER_ROW

            chunk = frame[start:end]

            if previous is not None and previous[start:end] == chunk:
                continue

            self._set_address(row)
            self._data(chunk)

            sent += 1

        self._previous = bytearray(frame)

        return sent

    async def clear(self) -> None:
        """Clear the entire graphics display."""

        if not self._started:
            return

        await asyncio.to_thread(self._blank)

    # ========================================================================
    # STATUS
    # ========================================================================

    def conflicts(self) -> dict[int, str]:
        """
        Pins or buses this panel wants that something else already drives.

        The gas ADC is the one that matters. It sits on SPI0 CE0 and lets the
        kernel drive its chip select; this panel drives CS itself. Two owners
        of one chip select is not a configuration that half works -- it is a
        panel that goes blank the first time the ADC is read.
        """

        from config import SPI_BUS, SPI_DEVICE, SPI_GPIO

        clashes: dict[int, str] = {}

        if self.bus == SPI_BUS and self.device == SPI_DEVICE:
            clashes[self.cs_pin] = (
                f"SPI {SPI_BUS}.{SPI_DEVICE}, which the gas ADC also opens"
            )

        elif self.cs_pin in SPI_GPIO:
            clashes[self.cs_pin] = "a pin the gas ADC's SPI bus uses"

        if self.psb_pin is not None and self.psb_pin in SPI_GPIO:
            clashes[self.psb_pin] = "a pin the gas ADC's SPI bus uses"

        return clashes

    def describe(self) -> list[str]:
        """Human-readable configuration, for the log and the bring-up mode."""

        psb = (
            f"PSB=GPIO{self.psb_pin} held LOW"
            if self.psb_pin is not None
            else "PSB tied LOW on the module"
        )

        lines = [
            f"Display: {self.name}",
            (
                f"SPI {self.bus}.{self.device} at {self.speed_hz} Hz, "
                f"mode {self.mode}, CS=GPIO{self.cs_pin} active high "
                f"(kernel CS off)"
            ),
            f"{psb}; clear on exit: {'yes' if self.clear_on_exit else 'no'}",
        ]

        for pin, what in sorted(self.conflicts().items()):
            lines.append(f"WARNING: GPIO {pin} is also {what}")

        return lines


# ---------------------------------------------------------------------------
# Bring-up
# ---------------------------------------------------------------------------
#
# The panel is the one part of this device that cannot be checked from a log:
# it either has the right picture on it or it does not, and nobody can tell you
# which from the other end of a WebSocket. Run this standing in front of it,
# with the agent stopped:
#
#     python src/display.py --test
#
# It draws a frame nothing else would produce by accident, so a partly working
# panel can be told from a working one. Each switch isolates one thing that has
# been wrong before:
#
#     --mode0        SPI mode 0 instead of 3, for a clone that wants it
#     --slow         250 kHz, for a panel showing torn or partial rows
#     --no-reset     skip the receiver flush, to see whether that is what is
#                    rescuing a restart
#     --keep         leave the frame up instead of clearing on the way out
#     --selftest     flash a checkerboard on the top row during init


def _test_canvas(label: str) -> Canvas:
    """A frame that is obviously this one and not a leftover."""

    canvas = Canvas()
    canvas.rect(0, 0, WIDTH, HEIGHT)
    canvas.text(6, 6, "ST7920 bring-up")
    canvas.text(6, 18, label)
    canvas.hline(4, 30, WIDTH - 8)

    for x in range(6, WIDTH - 6):
        canvas.pixel(x, 34 + (x - 6) * 22 // (WIDTH - 12))

    canvas.text(6, 54, "border line text")

    return canvas


async def _test_mode(argv: list[str]) -> None:
    display = ST7920Display(
        mode=0 if "--mode0" in argv else None,
        speed_hz=250_000 if "--slow" in argv else None,
    )

    if "--keep" in argv:
        display.clear_on_exit = False

    if "--selftest" in argv:
        display.selftest = True

    if "--no-reset" in argv:
        display._bus_reset = lambda: None  # noqa: SLF001 -- this is the owner
        print("  receiver flush disabled")

    for line in display.describe():
        print("  " + line)

    print("  opening the panel ...")
    await display.start()
    print("  init sent, graphics mode on, graphics RAM cleared")

    rows = await display.show(_test_canvas(time.strftime("run at %H:%M:%S")))
    print("  drew a test frame (%d row(s) sent)" % rows)
    print()
    print("  Blank now means nothing reached the controller: check PSB is LOW,")
    print("  that CS is on the pin named above, and the 5V logic-high margin.")
    print("  Garbled or torn rows mean the bus is too fast -- try --slow, or")
    print("  --mode0 if the panel is a clone.")
    print()

    input("  Press Enter to finish ...")

    await display.close()

    print(
        "  closed; "
        + ("frame left on the glass" if "--keep" in argv else "panel cleared")
    )


if __name__ == "__main__":
    if "--test" not in sys.argv:
        print(
            "Usage: python src/display.py --test "
            "[--mode0] [--slow] [--no-reset] [--keep] [--selftest]"
        )
        raise SystemExit(2)

    try:
        asyncio.run(_test_mode(sys.argv))
    except KeyboardInterrupt:
        print()
