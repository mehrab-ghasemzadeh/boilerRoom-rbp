#!/usr/bin/env python3

import time
import spidev
import RPi.GPIO as GPIO

# GPIO numbers use BCM numbering
CS_PIN = 8       # GPIO8, physical pin 24

# Open SPI0, CE0
spi = spidev.SpiDev()
spi.open(0, 0)

# ST7920 commonly works with SPI mode 3.
# If your display does not work, try mode 0.
spi.mode = 0b11
spi.max_speed_hz = 500000
spi.no_cs = True

GPIO.setmode(GPIO.BCM)
GPIO.setup(CS_PIN, GPIO.OUT, initial=GPIO.LOW)


def send_byte(value):
    """
    Send one 8-bit ST7920 value as two 4-bit serial transfers.

    The ST7920 serial protocol sends:
    - high nibble followed by four zero bits
    - low nibble followed by four zero bits
    """
    value &= 0xFF

    high_nibble = value & 0xF0
    low_nibble = (value << 4) & 0xF0

    spi.xfer2([high_nibble, low_nibble])


def send_command(command):
    """
    Send an ST7920 command.

    0xF8 is the serial synchronization/control byte for commands.
    """
    GPIO.output(CS_PIN, GPIO.HIGH)
    spi.xfer2([0xF8])
    send_byte(command)
    GPIO.output(CS_PIN, GPIO.LOW)
    time.sleep(0.001)


def send_data(data):
    """
    Send one data byte.

    0xFA is the serial synchronization/control byte for display data.
    """
    GPIO.output(CS_PIN, GPIO.HIGH)
    spi.xfer2([0xFA])
    send_byte(data)
    GPIO.output(CS_PIN, GPIO.LOW)


def initialize_display():
    time.sleep(0.1)

    # Basic ST7920 initialization
    send_command(0x30)   # Basic instruction set
    time.sleep(0.01)

    send_command(0x30)
    time.sleep(0.01)

    send_command(0x0C)   # Display on, cursor off, blink off
    send_command(0x01)   # Clear display
    time.sleep(0.01)

    send_command(0x06)   # Entry mode: increment address
    send_command(0x02)   # Return home


def write_text(text):
    """
    Write text using the ST7920's character display mode.
    This example writes to the first text line.
    """
    send_command(0x30)   # Basic instruction set
    send_command(0x80)   # Set DDRAM address to first line

    for character in text:
        send_data(ord(character))


try:
    initialize_display()
    write_text("Hello Raspberry Pi")

    while True:
        time.sleep(1)

except KeyboardInterrupt:
    pass

finally:
    GPIO.output(CS_PIN, GPIO.LOW)
    spi.close()
    GPIO.cleanup()