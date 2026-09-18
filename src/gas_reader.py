import asyncio
import spidev

from config import (
    SPI_BUS,
    SPI_DEVICE,
    SPI_SPEED,
    SPI_MODE,
)

VREF_MV = 5000


class GasReader:
    """
    Raspberry Pi reader for the ATtiny13/ATtiny13A ADC using hardware SPI.

    Communication uses spidev on SPI bus 0, device 0 (CE0).
    Kernel drives CE0 automatically.

    ATtiny13 response (32 bits = 4 bytes):

        Byte 0 = A3 ADC value, high byte
        Byte 1 = A3 ADC value, low byte
        Byte 2 = A2 ADC value, high byte
        Byte 3 = A2 ADC value, low byte

    SPI settings:
        Mode 0
        1 MHz
        CE0 (kernel-driven)
    """

    def __init__(self):
        self._spi = None
        self.started = False

    async def start(self):
        await asyncio.to_thread(self._open)

    def _open(self):
        spi = spidev.SpiDev()
        spi.open(SPI_BUS, SPI_DEVICE)
        spi.max_speed_hz = SPI_SPEED
        spi.mode = SPI_MODE
        self._spi = spi
        self.started = True

    def _read_sync(self):
        if not self.started or self._spi is None:
            raise RuntimeError("GasReader has not been started")

        rx = self._spi.xfer2([0x00, 0x00, 0x00, 0x00])
        bytes_received = list(rx)

        raw0 = (bytes_received[0] << 8) | bytes_received[1]
        raw1 = (bytes_received[2] << 8) | bytes_received[3]

        mv0 = raw0 * VREF_MV // 1023
        mv1 = raw1 * VREF_MV // 1023

        return {
            "a3": {"raw": raw0, "mv": mv0},
            "a2": {"raw": raw1, "mv": mv1},
        }

    async def read_all(self):
        return await asyncio.to_thread(self._read_sync)

    async def close(self):
        await asyncio.to_thread(self._close_sync)

    def _close_sync(self):
        if self._spi is not None:
            try:
                self._spi.close()
            finally:
                self._spi = None
                self.started = False
