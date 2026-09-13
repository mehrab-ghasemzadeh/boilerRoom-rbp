# relay_controller.py

import asyncio
import threading

import RPi.GPIO as GPIO

from config import RELAY_GPIO


class RelayController:
    """
    Controls relays using the local RELAY_GPIO mapping from config.py.

    Relays are active-low:
        LOW  = ON
        HIGH = OFF
    """

    def __init__(self):
        # The local configuration is the source of truth.
        self._relay_gpio = dict(RELAY_GPIO)

        self._states = {
            relay_id: False
            for relay_id in self._relay_gpio
        }

        # Protect GPIO operations if multiple async calls happen together.
        self._gpio_lock = threading.Lock()

    async def start(self):
        await asyncio.to_thread(self._setup_gpio)

    def _setup_gpio(self):
        with self._gpio_lock:
            GPIO.setmode(GPIO.BCM)
            GPIO.setwarnings(False)

            for gpio in self._relay_gpio.values():
                GPIO.setup(gpio, GPIO.OUT, initial=GPIO.HIGH)

            # Active-low relays start off.
            self._states = {
                relay_id: False
                for relay_id in self._relay_gpio
            }

    def _get_gpio(self, relay_id: int) -> int:
        try:
            return self._relay_gpio[relay_id]
        except KeyError:
            raise ValueError(f"Invalid relay ID: {relay_id}")

    def _set_relay_sync(self, relay_id: int, on: bool):
        gpio = self._get_gpio(relay_id)

        with self._gpio_lock:
            # Active-low relay:
            # ON  -> LOW
            # OFF -> HIGH
            GPIO.output(gpio, GPIO.LOW if on else GPIO.HIGH)
            self._states[relay_id] = on

    async def turn_on(self, relay_id: int):
        await asyncio.to_thread(
            self._set_relay_sync,
            relay_id,
            True,
        )

    async def turn_off(self, relay_id: int):
        await asyncio.to_thread(
            self._set_relay_sync,
            relay_id,
            False,
        )

    async def toggle(self, relay_id: int):
        with self._gpio_lock:
            if relay_id not in self._states:
                raise ValueError(f"Invalid relay ID: {relay_id}")

            new_state = not self._states[relay_id]

        await asyncio.to_thread(
            self._set_relay_sync,
            relay_id,
            new_state,
        )

    def get_state(self, relay_id: int) -> bool:
        if relay_id not in self._states:
            raise ValueError(f"Invalid relay ID: {relay_id}")

        return self._states[relay_id]

    async def cleanup(self):
        await asyncio.to_thread(self._cleanup_sync)

    def _cleanup_sync(self):
        with self._gpio_lock:
            # Turn every relay off before releasing the pins.
            for relay_id, gpio in self._relay_gpio.items():
                GPIO.output(gpio, GPIO.HIGH)
                self._states[relay_id] = False

            # Release only the locally configured relay pins.
            GPIO.cleanup(list(self._relay_gpio.values()))

