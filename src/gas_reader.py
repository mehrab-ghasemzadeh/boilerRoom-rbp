import asyncio

import spidev

from config import (
    GAS_SENSORS,
    SPI_BUS,
    SPI_DEVICE,
    SPI_SPEED,
)


class GasReader:

    def __init__(self):
        self.spi = None

    async def start(self):
        await asyncio.to_thread(self._open_spi)

    def _open_spi(self):

        self.spi = spidev.SpiDev()

        self.spi.open(SPI_BUS, SPI_DEVICE)

        # ATtiny protocol is SPI mode 0
        self.spi.mode = 0

        self.spi.max_speed_hz = SPI_SPEED

        # ATtiny sends MSB first
        self.spi.lsbfirst = False

    def _read_raw_sync(self):

        # ATtiny sends exactly 4 bytes:
        #
        # [MQ2_H, MQ2_L, MQ9_H, MQ9_L]
        #
        response = self.spi.xfer2([
            0x00,
            0x00,
            0x00,
            0x00,
        ])

        if len(response) != 4:
            raise RuntimeError(
                f"Expected 4 bytes from ATtiny, "
                f"received {len(response)}"
            )

        mq2 = (
            (response[0] << 8) |
            response[1]
        )

        mq9 = (
            (response[2] << 8) |
            response[3]
        )

        return mq2, mq9

    def _read_all_sync(self):

        mq2, mq9 = self._read_raw_sync()

        values = {}

        for sensor_id, sensor in GAS_SENSORS.items():

            channel = sensor["channel"]

            if channel == 0:
                values[sensor_id] = mq2

            elif channel == 1:
                values[sensor_id] = mq9

            else:
                raise ValueError(
                    f"Unsupported gas sensor channel: {channel}"
                )

        return values

    async def read_all(self):

        return await asyncio.to_thread(
            self._read_all_sync
        )

    async def close(self):

        if self.spi is not None:

            await asyncio.to_thread(
                self.spi.close
            )

            self.spi = None



# import asyncio

# import spidev

# from config import (
#     GAS_SENSORS,
#     SPI_BUS,
#     SPI_DEVICE,
#     SPI_SPEED,
# )


# class GasReader:

#     def __init__(self):
#         self.spi = None

#     async def start(self):
#         await asyncio.to_thread(self._open_spi)

#     def _open_spi(self):
#         self.spi = spidev.SpiDev()
#         self.spi.open(SPI_BUS, SPI_DEVICE)
#         self.spi.max_speed_hz = SPI_SPEED
#         self.spi.mode = 0b01

#     def _build_command(self, channel: int):
#         mux = {
#             0: 0b100,
#             1: 0b101,
#             2: 0b110,
#             3: 0b111,
#         }[channel]

#         command = (
#             (1 << 15) |
#             (mux << 12) |
#             (0 << 11) |
#             (1 << 8) |
#             (0b100 << 5) |
#             (0 << 4) |
#             (0b11)
#         )

#         return [
#             (command >> 8) & 0xFF,
#             command & 0xFF,
#         ]

#     def _read_channel(self, channel):
#         command = self._build_command(channel)
#         response = self.spi.xfer2(command)
#         raw = (response[0] << 8) | response[1]
#         return raw & 0xFFFF

#     def _read_all_sync(self):
#         values = {}

#         for sensor_id, sensor in GAS_SENSORS.items():
#             values[sensor_id] = self._read_channel(sensor["channel"])

#         return values

#     async def read_all(self):
#         return await asyncio.to_thread(self._read_all_sync)

#     async def close(self):
#         if self.spi is not None:
#             await asyncio.to_thread(self.spi.close)
#             self.spi = None
