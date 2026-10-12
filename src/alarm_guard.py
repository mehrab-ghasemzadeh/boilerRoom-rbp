"""
Alarm guard for the boiler room.

Relay 1 is designated as the alarm relay. It activates in these scenarios:
1. User manually turns relay 1 on
2. Gas sensor readings exceed defined thresholds
3. Thermal readings exceed cut limit by more than 5°C
4. Any unit temperature (except environment sensors) reaches 0°C
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any

from config import RELAYS, TEMPERATURE_SENSORS
from runtime_state import RuntimeState


def get_logger(name: str):
    from logging_setup import get_logger as _get_logger
    return _get_logger(name)


_log = get_logger("alarm")

# Alarm relay is always relay 1
ALARM_RELAY_ID = 1

# Gas sensor thresholds (can be overridden via environment)
MQ2_THRESHOLD = int(os.environ.get("BOILERROOM_MQ2_THRESHOLD", "400"))
MQ9_THRESHOLD = int(os.environ.get("BOILERROOM_MQ9_THRESHOLD", "400"))

# How many degrees above the cut limit triggers the alarm
ALARM_OVER_TEMP_MARGIN_C = float(os.environ.get("BOILERROOM_ALARM_OVER_TEMP_MARGIN", "5.0"))


@dataclass
class AlarmState:
    """Tracks why the alarm is active."""
    reason: str | None = None
    active: bool = False


class AlarmGuard:
    """Manages the alarm relay based on safety conditions."""

    def __init__(self) -> None:
        self._state = AlarmState()
        self._user_activated = False  # Track if user manually turned alarm on

    @property
    def is_active(self) -> bool:
        return self._state.active

    @property
    def reason(self) -> str | None:
        return self._state.reason

    @property
    def user_activated(self) -> bool:
        return self._user_activated

    async def check(
        self,
        state: RuntimeState,
        temperatures: dict[int, float | None],
        gas: dict[str, int],
    ) -> None:
        """Evaluate alarm conditions and act on them."""
        relay_controller = state.relay_controller
        if relay_controller is None:
            return

        config = await state.get_device_config()
        if config is None:
            return

        limits = config.limits
        max_water_temp = limits.max_water_temperature_c

        # 1. Check if user manually activated the alarm relay
        alarm_relay_on = relay_controller.get_state(ALARM_RELAY_ID)
        if alarm_relay_on and not self._user_activated:
            self._user_activated = True
            await self._activate(state, relay_controller, "user_activated")

        # If user manually turned it off, clear user activation
        if not alarm_relay_on and self._user_activated:
            self._user_activated = False
            await self._maybe_deactivate(state, relay_controller, "user_deactivated")

        # If user has it manually activated, don't auto-manage it
        if self._user_activated:
            return

        # 2. Check gas sensors
        mq2_value = gas.get("mq2")
        mq9_value = gas.get("mq9")

        gas_alarm = False
        gas_reason = ""

        if mq2_value is not None and mq2_value >= MQ2_THRESHOLD:
            gas_alarm = True
            gas_reason = f"MQ2 gas sensor reading {mq2_value} >= threshold {MQ2_THRESHOLD}"

        if mq9_value is not None and mq9_value >= MQ9_THRESHOLD:
            gas_alarm = True
            if gas_reason:
                gas_reason += f"; "
            gas_reason += f"MQ9 gas sensor reading {mq9_value} >= threshold {MQ9_THRESHOLD}"

        if gas_alarm:
            await self._activate(state, relay_controller, gas_reason)
            return

        # 3. Check thermal readings > cut limit + 5°C
        if max_water_temp is not None:
            alarm_temp = max_water_temp + ALARM_OVER_TEMP_MARGIN_C

            for sensor_id, temp in temperatures.items():
                if temp is None:
                    continue

                cfg = TEMPERATURE_SENSORS.get(sensor_id, {})
                role = cfg.get("role")
                unit = cfg.get("unit")

                # Skip environment sensors
                if role == "environment_inside" or role == "environment_outside":
                    continue

                # Only check boiler sensors (water temperature roles)
                if role not in ("boiler_output_water", "boiler_input_water", "boiler_body"):
                    continue

                if temp >= alarm_temp:
                    await self._activate(
                        state,
                        relay_controller,
                        f"Temperature {temp:.1f}°C exceeds cut limit {max_water_temp:.1f}°C + {ALARM_OVER_TEMP_MARGIN_C:.1f}°C "
                        f"on {cfg.get('name', f'sensor {sensor_id}')} ({role})"
                    )
                    return

        # 4. Check for 0°C on any unit (except environment)
        for sensor_id, temp in temperatures.items():
            if temp is None:
                continue

            cfg = TEMPERATURE_SENSORS.get(sensor_id, {})
            role = cfg.get("role")

            # Skip environment sensors
            if role == "environment_inside" or role == "environment_outside":
                continue

            # Check if temperature is at or near 0°C (within 0.5°C to account for precision)
            if abs(temp) <= 0.5:
                await self._activate(
                    state,
                    relay_controller,
                    f"Temperature reading {temp:.1f}°C (near 0°C) on {cfg.get('name', f'sensor {sensor_id}')} ({role})"
                )
                return

        # If no alarm conditions, deactivate if we were active
        if self._state.active:
            await self._maybe_deactivate(state, relay_controller, "conditions_cleared")

    async def _activate(
        self,
        state: RuntimeState,
        relay_controller,
        reason: str,
    ) -> None:
        """Activate the alarm relay."""
        if self._state.active and self._state.reason == reason:
            return  # Already active for this reason

        self._state.active = True
        self._state.reason = reason

        await relay_controller.turn_on(ALARM_RELAY_ID)
        await state.log(
            f"[alarm] ALARM ACTIVATED (relay {ALARM_RELAY_ID}): {reason}",
            level=logging.CRITICAL,
        )
        await state.notify_state_change(f"alarm relay on ({reason})")

    async def _maybe_deactivate(
        self,
        state: RuntimeState,
        relay_controller,
        reason: str,
    ) -> None:
        """Deactivate the alarm relay if no other conditions require it."""
        if not self._state.active:
            return

        # Double-check all conditions before deactivating
        config = await state.get_device_config()
        if config is None:
            return

        temperatures = (await state.get_snapshot())["temperatures"]
        gas = (await state.get_snapshot())["gas"]

        # Re-evaluate all conditions
        limits = config.limits
        max_water_temp = limits.max_water_temperature_c

        # Check gas
        mq2_value = gas.get("mq2")
        mq9_value = gas.get("mq9")
        if (mq2_value is not None and mq2_value >= MQ2_THRESHOLD) or \
           (mq9_value is not None and mq9_value >= MQ9_THRESHOLD):
            return  # Gas still alarming

        # Check over-temp
        if max_water_temp is not None:
            alarm_temp = max_water_temp + ALARM_OVER_TEMP_MARGIN_C
            for sensor_id, temp in temperatures.items():
                if temp is None:
                    continue
                cfg = TEMPERATURE_SENSORS.get(sensor_id, {})
                role = cfg.get("role")
                if role in ("environment_inside", "environment_outside"):
                    continue
                if role in ("boiler_output_water", "boiler_input_water", "boiler_body"):
                    if temp >= alarm_temp:
                        return  # Over-temp still alarming

        # Check 0°C
        for sensor_id, temp in temperatures.items():
            if temp is None:
                continue
            cfg = TEMPERATURE_SENSORS.get(sensor_id, {})
            role = cfg.get("role")
            if role in ("environment_inside", "environment_outside"):
                continue
            if abs(temp) <= 0.5:
                return  # 0°C still alarming

        # All clear
        self._state.active = False
        self._state.reason = None

        await relay_controller.turn_off(ALARM_RELAY_ID)
        await state.log(
            f"[alarm] ALARM DEACTIVATED (relay {ALARM_RELAY_ID}): {reason}",
            level=logging.INFO,
        )
        await state.notify_state_change(f"alarm relay off ({reason})")

    def describe(self) -> list[str]:
        """Human-readable status for the control menu."""
        if self._state.active:
            return [
                f"Alarm: ACTIVE - {self._state.reason}",
                f"  Relay {ALARM_RELAY_ID} is ON"
            ]
        else:
            return [
                "Alarm: inactive",
                f"  Relay {ALARM_RELAY_ID} is OFF"
            ]


alarm_guard = AlarmGuard()