import RPi.GPIO as GPIO
import time

SCLK_PIN = 11
MISO_PIN = 9
CS_PIN = 8

VREF_MV = 5000

GPIO.setmode(GPIO.BCM)
GPIO.setup(SCLK_PIN, GPIO.OUT)
GPIO.setup(MISO_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(CS_PIN, GPIO.OUT)

GPIO.output(SCLK_PIN, GPIO.LOW)
GPIO.output(CS_PIN, GPIO.HIGH)

print("=" * 50)
print("     ATtiny13a SPI Debugger Ready")
print("=" * 50)

try:
    while True:
        bytes_received = [0, 0, 0, 0]

        GPIO.output(CS_PIN, GPIO.LOW)
        time.sleep(0.0002)

        if GPIO.input(MISO_PIN):
            bytes_received[0] |= 0x80

        for i in range(1, 32):
            GPIO.output(SCLK_PIN, GPIO.HIGH)
            time.sleep(0.00015)

            GPIO.output(SCLK_PIN, GPIO.LOW)
            time.sleep(0.00015)

            bit_val = GPIO.input(MISO_PIN)

            byte_idx = i // 8
            bit_idx = 7 - (i % 8)
            if bit_val:
                bytes_received[byte_idx] |= (1 << bit_idx)

        GPIO.output(CS_PIN, GPIO.HIGH)

        raw_bits = " ".join(
            "".join(str((bytes_received[b] >> bit) & 1) for bit in range(7, -1, -1))
            for b in range(4)
        )
        print(f"Raw Bits: [ {raw_bits} ]")

        raw_hex = " ".join(
            f"{bytes_received[b]:02X}" for b in range(4)
        )
        print(f"Raw Hex:  [ {raw_hex} ]")

        raw0 = (bytes_received[0] << 8) | bytes_received[1]
        raw1 = (bytes_received[2] << 8) | bytes_received[3]

        mv0 = raw0 * VREF_MV // 1023
        mv1 = raw1 * VREF_MV // 1023

        print(f"CH1 (A3): Raw = {raw0}\t-> Volt = {mv0} mV")
        print(f"CH2 (A2): Raw = {raw1}\t-> Volt = {mv1} mV")
        print("-" * 50)

        time.sleep(0.5)

except KeyboardInterrupt:
    pass
finally:
    GPIO.cleanup()




'''
import asyncio
import time

import RPi.GPIO as GPIO

from config import (
    SCLK_PIN,
    MISO_PIN,
    CS_PIN,
)


VREF_MV = 5000


class GasReader:
    """
    Raspberry Pi reader for the ATtiny13/ATtiny13A ADC.

    Communication is implemented using GPIO bit-banged SPI to
    match the Arduino reference implementation.

    ATtiny13 response:

        Byte 0 = A3 ADC value, high byte
        Byte 1 = A3 ADC value, low byte
        Byte 2 = A2 ADC value, high byte
        Byte 3 = A2 ADC value, low byte

    SPI timing:

        Mode 0
        CS LOW
        200 us delay
        Read first bit
        31 clock cycles:
            SCLK HIGH
            150 us
            SCLK LOW
            150 us
            Read MISO
        CS HIGH
    """

    def __init__(self):
        self.started = False

    async def start(self):
        await asyncio.to_thread(self._setup_gpio)

    def _setup_gpio(self):
        # Use BCM GPIO numbering.
        GPIO.setmode(GPIO.BCM)

        # Clock from Raspberry Pi -> ATtiny13 PB2 / physical pin 7
        GPIO.setup(
            SCLK_PIN,
            GPIO.OUT,
            initial=GPIO.LOW,
        )

        # MISO from ATtiny13 PB1 / physical pin 6 -> Raspberry Pi
        #
        # Arduino reference uses INPUT_PULLUP.
        GPIO.setup(
            MISO_PIN,
            GPIO.IN,
            pull_up_down=GPIO.PUD_UP,
        )

        # CS from Raspberry Pi -> ATtiny13 PB0 / physical pin 5
        GPIO.setup(
            CS_PIN,
            GPIO.OUT,
            initial=GPIO.HIGH,
        )

        self.started = True

    def _read_sync(self):
        if not self.started:
            raise RuntimeError("GasReader has not been started")

        # Four received bytes.
        bytes_received = [0, 0, 0, 0]

        try:
            # --------------------------------------------------
            # 1. Start transaction
            # --------------------------------------------------

            GPIO.output(CS_PIN, GPIO.LOW)

            # Give the ATtiny13 time to detect SS falling edge
            # and prepare the first MISO bit.
            self._delay_us(200)

            # --------------------------------------------------
            # 2. Read first bit
            # --------------------------------------------------

            if GPIO.input(MISO_PIN):
                bytes_received[0] |= 0x80

            # --------------------------------------------------
            # 3. Generate remaining 31 clock cycles
            # --------------------------------------------------

            for i in range(1, 32):

                # Rising edge
                GPIO.output(SCLK_PIN, GPIO.HIGH)

                self._delay_us(150)

                # Falling edge.
                #
                # ATtiny13 detects this edge and changes MISO
                # to the next bit.
                GPIO.output(SCLK_PIN, GPIO.LOW)

                # Give ATtiny13 time to execute INT0 ISR
                # and stabilize MISO.
                self._delay_us(150)

                # Read the new MISO bit.
                bit_val = GPIO.input(MISO_PIN)

                byte_idx = i // 8
                bit_idx = 7 - (i % 8)

                if bit_val:
                    bytes_received[byte_idx] |= (1 << bit_idx)

        finally:
            # --------------------------------------------------
            # 4. End transaction
            # --------------------------------------------------

            GPIO.output(CS_PIN, GPIO.HIGH)

            # Leave clock LOW.
            GPIO.output(SCLK_PIN, GPIO.LOW)

        # ------------------------------------------------------
        # 5. Reconstruct the two 16-bit values
        # ------------------------------------------------------

        raw0 = (
            (bytes_received[0] << 8)
            | bytes_received[1]
        )

        raw1 = (
            (bytes_received[2] << 8)
            | bytes_received[3]
        )

        # ------------------------------------------------------
        # 6. Convert ADC values to millivolts
        # ------------------------------------------------------

        mv0 = (
            raw0 * VREF_MV
            // 1023
        )

        mv1 = (
            raw1 * VREF_MV
            // 1023
        )

        return {
            "a3": {
                "raw": raw0,
                "mv": mv0,
            },
            "a2": {
                "raw": raw1,
                "mv": mv1,
            },
        }

    async def read_all(self):
        """
        Read both ATtiny13 ADC channels.

        Returns:

        {
            "a3": {
                "raw": 512,
                "mv": 2502
            },
            "a2": {
                "raw": 750,
                "mv": 3663
            }
        }
        """

        return await asyncio.to_thread(self._read_sync)

    async def close(self):
        await asyncio.to_thread(self._close_gpio)

    def _close_gpio(self):
        if self.started:
            try:
                # Put both control lines into a safe state.
                GPIO.output(CS_PIN, GPIO.HIGH)
                GPIO.output(SCLK_PIN, GPIO.LOW)
            finally:
                # Release only the pins this reader uses. A bare GPIO.cleanup()
                # would also release relay and keypad pins, dropping every relay
                # mid-shutdown.
                GPIO.cleanup([SCLK_PIN, MISO_PIN, CS_PIN])
                self.started = False

    @staticmethod
    def _delay_us(microseconds):
        """
        Delay approximately the requested number of microseconds.

        time.sleep() accepts seconds, so convert microseconds
        to seconds.
        """
        time.sleep(microseconds / 1_000_000.0)

'''