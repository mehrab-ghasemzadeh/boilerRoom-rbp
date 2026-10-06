from load_env import load_dotenv

load_dotenv()

import asyncio
import logging
import os
import signal
from collections.abc import Awaitable, Callable
from pathlib import Path

from auth import (
    AuthError,
    MissingCredentialsError,
    credentials_rejected,
    credentials_unsaved,
    forget_credentials,
    save_credentials,
    token_manager,
)
from control_menu import run_control_menu
from data_logger import reading_store, save_readings
from device_config import ConfigError, config_store, load_cached_config
from device_record import (
    DeviceRecordError,
    apply_calibration,
    device_record_store,
    fetch_device_record,
    load_cached_device_record,
)
from anti_freeze import anti_freeze
from errors_client import error_reporter
from limits_guard import limit_guard
from logging_setup import configure_logging, shutdown_logging
from mode_store import load_modes
from record_sync import adopt_device_record, load_mapping
from runtime_state import RuntimeState
from schedule_runner import ScheduleError, load_cached_schedule, schedule_runner
from sensor_watcher import sensor_watcher
from state_publisher import run_state_publisher
from telemetry_client import post_telemetry
from ws_client import run_websocket_client

# ----------------------------------------------------
# Configuration
# ----------------------------------------------------

def _is_raspberry_pi() -> bool:
    """Whether there is a GPIO header under this process."""
    try:
        model = Path("/proc/device-tree/model").read_bytes()
    except OSError:
        return False
    return b"raspberry pi" in model.lower()


def _use_mock_hardware() -> bool:
    # """
    # Simulated sensors and relays, or the real ones.

    # Decided by what this is running on rather than by a constant somebody has
    # to remember to flip. The agent starts at boot from systemd, and a device
    # that came up simulating its own boiler room — reporting invented
    # temperatures to the server, driving nothing — is a failure that looks
    # exactly like success from every screen you might check.

    # BOILERROOM_MOCK_HARDWARE=on forces the mocks, which is how you run the real
    # menu against fake sensors on a Pi; =off forces the hardware, and then a
    # missing RPi.GPIO or spidev is an ImportError at startup rather than a
    # silent downgrade.
    # """
    override = os.environ.get("BOILERROOM_MOCK_HARDWARE", "").strip().lower()
    if override in ("on", "1", "true", "yes"):
        return True
    if override in ("off", "0", "false", "no"):
        return False
    return not _is_raspberry_pi()

USE_MOCK_HARDWARE = _use_mock_hardware()

# Sensors are read once a minute, matching the telemetry cadence: at the old
# 10 s there were five discarded cycles for every one that was uploaded, all of
# them costing SD-card writes and CPU on a single-core Pi.
#
# This also sets how quickly the over-temperature cut can react — see the
# limit-latency note in the README.
DEFAULT_READ_INTERVAL = 60

# How often to re-check the active schedule for a state transition
SCHEDULE_TICK_SECONDS = 20

# How often to re-fetch the device's own record. Calibration and enable flags
# change when an installer edits them, which is rare, and nothing pushes the
# change — so poll, but gently.
DEVICE_RECORD_REFRESH_SECONDS = 900

# Login retry backoff, used when the server is unreachable at boot
AUTH_RETRY_DELAY_SECONDS = 10
AUTH_RETRY_MAX_DELAY_SECONDS = 300

# How long to wait before starting a task again after it fell over, and the
# ceiling that wait backs off to. See supervise().
TASK_RESTART_SECONDS = 5.0
TASK_RESTART_MAX_SECONDS = 300.0

# ----------------------------------------------------
# Hardware selection
# ----------------------------------------------------

if USE_MOCK_HARDWARE:
    # from mock_temperature_reader import MockTemperatureReader as TemperatureReader
    from mock_gas_reader import MockGasReader as GasReader
    from mock_relay_controller import MockRelayController as RelayController
    from mock_keypad import MockKeypad as Keypad
    from mock_display import MockDisplay as Display

    from temperature_reader import TemperatureReader
    # from gas_reader import GasReader
    # from relay_controller import RelayController
    # from keypad import Keypad
    # from display import ST7920Display as Display
else:
    from temperature_reader import TemperatureReader
    from gas_reader import GasReader
    from relay_controller import RelayController
    from keypad import Keypad
    from display import ST7920Display as Display


async def print_startup_banner(state: RuntimeState) -> None:
    from config import GAS_SENSORS, RELAYS, TEMPERATURE_SENSORS, UNITS

    await state.echo("========================================")
    await state.echo(" Boiler Room Monitoring System Started")
    await state.echo("========================================\n")

    if not state.mapping_ready.is_set():
        await state.echo(
            "No device mapping yet — this device's wiring comes from the server\n"
            "record. Sensors and relays stay idle until it arrives.\n"
        )
        return

    await state.echo("Equipment units:")
    for unit_id, unit in UNITS.items():
        await state.echo(f"  {unit_id}: {unit['name']}")
    await state.echo("")

    await state.echo("Temperature sensor mapping:")
    for sid, cfg in sorted(TEMPERATURE_SENSORS.items()):
        unit = cfg.get("unit") or "—"
        await state.echo(
            f"  Sensor {sid}: {cfg['name']}  (role={cfg['role']}, unit={unit})"
        )
    await state.echo("")

    await state.echo("Relay mapping:")
    for rid, cfg in sorted(RELAYS.items()):
        unit = cfg.get("unit") or "—"
        await state.echo(
            f"  Relay {rid}: {cfg['name']}  "
            f"(role={cfg['role']}, unit={unit}, GPIO {cfg['gpio']})"
        )
    await state.echo("")


async def _sensor_cycle(
    state: RuntimeState,
    temperature_reader,
    gas_reader,
    *,
    offline_notice_shown: bool,
) -> bool:
    """
    One pass of the read cycle. Returns the updated "offline" flag.

    Kept separate from the loop so the loop can put a try around the whole
    thing: a cycle that raises is a bad cycle, not a reason to stop heating.
    """
    temperatures, gas = await asyncio.gather(
        temperature_reader.read_all(),
        gas_reader.read_all(),
    )

    # Correct readings before anything consumes them, so the database,
    # the limit guard and telemetry all agree on the value.
    temperatures = apply_calibration(temperatures)

    await state.update_readings(temperatures, gas)
    await save_readings(temperatures, gas)

    await error_reporter.check_temperature_faults(
        temperatures,
        log=state.log,
    )

    # Enforce config limits before telemetry, so a cut is reported in
    # the same cycle it happens.
    await limit_guard.check(state, temperatures)

    # Anti-freeze after the limit guard, so a unit cut for running hot is
    # already blocked by the time this looks at it — the cut outranks it.
    await anti_freeze.check(state, temperatures)

    # Pacing lives in RuntimeState because a local relay change also
    # posts telemetry; both have to share one "last posted" clock or
    # they double up. The first cycle posts immediately so a fresh boot
    # shows up on the server without waiting out the interval.
    due = await state.telemetry_due()

    if due and not state.authenticated.is_set():
        if not offline_notice_shown:
            await state.log("[telemetry] Offline — holding until a session exists")
        return True

    if due:
        await state.mark_telemetry_posted()
        try:
            await post_telemetry(state, temperatures, gas)
        except Exception as exc:
            await state.log(f"[telemetry] Failed to post: {exc}", level=logging.WARNING)

    return False


async def sensor_loop(state: RuntimeState) -> None:
    # Hardware is addressed by the mapping — GPIO pins, 1-Wire ROM codes, ADC
    # channels — so there is nothing to construct until one exists.
    if not await state.wait_mapping_ready():
        return

    temperature_reader = TemperatureReader()
    gas_reader = GasReader()
    relay_controller = RelayController()

    state.temperature_reader = temperature_reader
    state.gas_reader = gas_reader
    state.relay_controller = relay_controller

    await asyncio.gather(
        temperature_reader.start(),
        gas_reader.start(),
        relay_controller.start(),
        return_exceptions=True,
    )

    # Relays exist now, so a cached schedule can take effect immediately rather
    # than waiting for the next schedule tick.
    await schedule_runner.evaluate(state)

    offline_notice_shown = False
    failed_cycles = 0

    try:
        while not state.shutdown.is_set():
            try:
                offline_notice_shown = await _sensor_cycle(
                    state,
                    temperature_reader,
                    gas_reader,
                    offline_notice_shown=offline_notice_shown,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A probe being pulled out, a bus that has gone, a database that
                # would not take a write: none of those are reasons for the
                # agent to stop heating. Say it, then go round again — the next
                # cycle is a minute away and usually fine.
                failed_cycles += 1
                await state.log(
                    f"[sensor] Read cycle failed ({failed_cycles} in a row): "
                    f"{type(exc).__name__}: {exc} — retrying next cycle",
                    level=logging.ERROR,
                )
            else:
                if failed_cycles:
                    await state.log(
                        f"[sensor] Read cycle recovered after {failed_cycles} "
                        "failure(s)",
                        level=logging.WARNING,
                    )
                failed_cycles = 0

            interval = await state.get_read_interval()
            try:
                await asyncio.wait_for(state.shutdown.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    finally:
        await asyncio.gather(
            relay_controller.cleanup(),
            gas_reader.close(),
            temperature_reader.close(),
            reading_store.close(),
            return_exceptions=True,
        )


async def schedule_loop(state: RuntimeState) -> None:
    """Re-evaluate the active schedule so relays follow its time boundaries."""
    while not state.shutdown.is_set():
        try:
            await schedule_runner.evaluate(state)
        except Exception as exc:
            await state.log(f"[schedule] Evaluation failed: {exc}", level=logging.ERROR)

        try:
            await asyncio.wait_for(
                state.shutdown.wait(),
                timeout=SCHEDULE_TICK_SECONDS,
            )
        except asyncio.TimeoutError:
            pass


async def auth_loop(state: RuntimeState) -> None:
    """
    Log in, retrying in the background.

    Startup must not depend on the server being reachable: a boiler room that
    reboots during an outage still has to run its heating programme from the
    cached schedule. Everything that needs a session waits on
    ``state.authenticated`` instead of blocking the boot.

    A device with no credentials at all waits for an operator to type them at
    the panel rather than giving up for the run. This task cannot ask — it has
    no screen — so it raises ``credentials_needed`` and waits; the control menu
    asks and answers with ``credentials_ready``. See the credentials wizard in
    ``control_menu``.
    """
    delay = AUTH_RETRY_DELAY_SECONDS
    asked = False

    while not state.shutdown.is_set():
        try:
            session = await token_manager.login()
        except MissingCredentialsError as exc:
            if not asked:
                asked = True
                await state.log(f"[auth] {exc}")
                await state.log(
                    "[auth] Waiting for them to be entered on the device — "
                    "running offline from the cached schedule until then"
                )
            state.credentials_ready.clear()
            state.credentials_needed.set()
            if not await state.wait_credentials():
                return
            continue
        except AuthError as exc:
            # Wrong credentials and an unreachable server both land here, and
            # they want opposite things: one needs the operator, the other
            # needs patience. Only ask again about credentials this device was
            # given by hand and has not saved — a server refusing a paired
            # device is a different fault, and one this must not respond to by
            # throwing away the only copy of its credentials.
            if credentials_unsaved() and credentials_rejected(exc):
                await state.log(
                    f"[auth] The server refused those credentials ({exc}) — "
                    "asking for them again",
                    level=logging.WARNING,
                )
                forget_credentials()
                asked = False
                state.authenticated.clear()
                continue

            await state.log(f"[auth] Login failed: {exc}", level=logging.WARNING)
            await state.log(
                f"[auth] Retrying in {delay:.0f}s — "
                "running from cached schedule until then"
            )
        else:
            state.authenticated.set()
            state.credentials_needed.clear()
            await state.log(
                f"[auth] Device session established "
                f"(device_id={session.device_id or 'unknown'})"
            )
            # Proven, so now it is worth keeping. Never before: a credential
            # that has not logged in once might be a typo, and one written to
            # .env is one nobody at the panel can correct.
            try:
                saved = await save_credentials()
            except Exception as exc:
                await state.log(
                    f"[auth] Logged in, but the credentials could not be saved: "
                    f"{exc} — this device will ask again at the next boot",
                    level=logging.WARNING,
                )
            else:
                if saved is not None:
                    await state.log(f"[auth] Credentials saved to {saved}")
            return

        try:
            await asyncio.wait_for(state.shutdown.wait(), timeout=delay)
        except asyncio.TimeoutError:
            pass

        delay = min(delay * 2, AUTH_RETRY_MAX_DELAY_SECONDS)


async def restore_cached_schedule(state: RuntimeState) -> None:
    """
    Reload the last schedule the server pushed.

    This is what keeps the boilers on programme when the device boots with no
    network. Reporting the cached version in device.hello also stops the server
    re-pushing an unchanged schedule on every boot.
    """
    schedule, document, error = await load_cached_schedule()

    if error:
        await state.log(
            f"[schedule] Ignoring unusable cache ({error}) — waiting for server push",
            level=logging.WARNING,
        )
    elif schedule is None or document is None:
        await state.log("[schedule] No cached schedule — waiting for server push")
    else:
        schedule_runner.set_server_schedule(schedule, document)
        await state.set_active_schedule_version(schedule.version)
        await state.log(
            f"[schedule] Restored cached schedule v{schedule.version} "
            f"({len(schedule.weekly_rules)} weekly rule(s), "
            f"{len(schedule.exceptions)} exception(s), tz={schedule.timezone_name})"
        )

    # Layered on top, so an operator's own programme survives a reboot the same
    # way the published one does.
    await restore_local_schedule(state)


async def restore_local_schedule(state: RuntimeState) -> None:
    """
    Reload a programme edited on this device.

    Kept only while it still sits on the published version it was made against:
    if the server published something newer while this device was off, that
    publish outranks the edits, exactly as a live push would.
    """
    from schedule_editor import clear_local_schedule, load_local_schedule

    local, error = await load_local_schedule()

    if error:
        await state.log(
            f"[schedule] Ignoring unusable local schedule ({error})",
            level=logging.WARNING,
        )
        return
    if local is None:
        return

    if local.based_on_version != schedule_runner.server_version:
        await clear_local_schedule()
        await state.log(
            f"[schedule] Discarded local edits made on v{local.based_on_version} — "
            f"the server has since published v{schedule_runner.server_version}",
            level=logging.WARNING,
        )
        return

    try:
        schedule = await schedule_runner.restore_local(local, state)
    except ScheduleError as exc:
        await clear_local_schedule()
        await state.log(
            f"[schedule] Discarded unusable local edits ({exc}) — "
            "back to the published schedule",
            level=logging.WARNING,
        )
        return

    await state.log(
        f"[schedule] Restored locally edited schedule v{schedule.version} "
        f"(revision {local.revision}, saved {local.saved_at or 'unknown'}; "
        f"{len(schedule.weekly_rules)} weekly rule(s), "
        f"{len(schedule.exceptions)} exception(s)) — "
        "the server's next publish replaces it",
        level=logging.WARNING,
    )


async def restore_cached_device_record(state: RuntimeState) -> None:
    """
    Reload the last device record the server gave us.

    Calibration offsets and sensor enable flags have to survive a reboot with
    no network, or a device that comes up during an outage would silently
    report uncalibrated readings — and feed them to the limit guard.
    """
    record, error = await load_cached_device_record()

    if error:
        await state.log(
            f"[device] Ignoring unusable record cache ({error}) — will re-fetch",
            level=logging.WARNING,
        )
        return
    if record is None:
        await state.log("[device] No cached device record — will fetch after login")
        return

    device_record_store.set_record(record)
    await state.log(
        f"[device] Restored cached record for {record.public_id} "
        f"({len(record.sensors)} server sensor(s), "
        f"{len(device_record_store.offsets)} calibration offset(s), "
        f"{len(device_record_store.disabled)} disabled)"
    )


async def device_record_loop(state: RuntimeState) -> None:
    """
    Keep the device record fresh.

    An installer can change calibration or disable a probe at any time, and
    nothing pushes that over the WebSocket — it only appears in the record, so
    it has to be polled. Slowly: it is a handful of fields that change rarely.
    """
    if not await state.wait_authenticated():
        return

    while not state.shutdown.is_set():
        # Shared with the post-hello reconciler, which fetches a record too.
        # Adoption is not atomic, so without this the two could apply halves of
        # two different records.
        async with state.record_lock:
            try:
                record, payload = await fetch_device_record()
                # Adoption is inside the try, not after it: a record that has
                # just lost a sensor is exactly the record most likely to be
                # refused, and refusing it has to be a log line rather than the
                # end of the loop that would fetch the next one.
                await adopt_device_record(state, record, payload)
            except DeviceRecordError as exc:
                await state.log(
                    f"[device] Server record unusable: {exc}", level=logging.WARNING
                )
            except Exception as exc:
                await state.log(
                    f"[device] Failed to apply the server record: "
                    f"{type(exc).__name__}: {exc}",
                    level=logging.WARNING,
                )

        # Wakes early when something asks for a refresh — reporting modes
        # upstream does, because this poll is what reconciles them.
        state.record_refresh_requested.clear()
        waiters = [
            asyncio.create_task(state.shutdown.wait()),
            asyncio.create_task(state.record_refresh_requested.wait()),
        ]
        try:
            await asyncio.wait(
                waiters,
                timeout=DEVICE_RECORD_REFRESH_SECONDS,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for waiter in waiters:
                if not waiter.done():
                    waiter.cancel()


async def restore_cached_modes(state: RuntimeState) -> None:
    """
    Reload which units were left in manual.

    Restored before the first schedule evaluation, so a unit an operator took
    off the programme stays off it across a reboot instead of being handed
    straight back to the schedule.
    """
    from setpoint_store import load_setpoints

    setpoints = await load_setpoints()
    if setpoints:
        await state.restore_setpoints(setpoints)
        await state.log(
            "[setpoint] Restored "
            + ", ".join(
                f"boiler {i} at {e.temperature_c:.1f} °C"
                + ("" if e.published else " (not yet published)")
                for i, e in sorted(setpoints.items())
            )
        )

    modes = await load_modes()
    if not modes:
        await state.log("[mode] No saved modes — every unit starts on automatic")
        return

    await state.restore_modes(modes)

    manual = sorted(str(t) for t, e in modes.items() if e.mode == "manual")
    if manual:
        await state.log(
            f"[mode] Restored {len(modes)} mode(s); still on manual: {', '.join(manual)}"
        )
    else:
        await state.log(f"[mode] Restored {len(modes)} mode(s), all automatic")

    unreported = sorted(str(t) for t, e in modes.items() if not e.published)
    if unreported:
        await state.log(
            f"[mode] Not yet reported to the server: {', '.join(unreported)} — "
            "will be sent on the next connection"
        )


async def restore_cached_config(state: RuntimeState) -> None:
    """
    Reload the last config the server pushed.

    Without this a restart comes up with no limits and no telemetry cadence
    until the WebSocket reconnects — which may be never, if the network is down.
    Reporting the cached version in device.hello also stops the server
    re-pushing an unchanged document on every boot.
    """
    config, document, error = await load_cached_config()

    if error:
        await state.log(
            f"[config] Ignoring unusable cache ({error}) — waiting for server push",
            level=logging.WARNING,
        )
    elif config is None or document is None:
        await state.log("[config] No cached config — waiting for server push")
    else:
        config_store.set_server_document(document)
        await state.set_device_config(config)
        await state.set_active_config_version(config.version)
        await state.log(
            f"[config] Restored cached config v{config.version} "
            f"(telemetry every {config.telemetry_interval_seconds}s)"
        )

    # Layered on top, so limits an operator set by hand survive a reboot.
    await restore_local_config(state)


async def restore_local_config(state: RuntimeState) -> None:
    """
    Reload limits edited on this device.

    Kept only while they still sit on the published version they were made
    against: if the server published a newer config while the device was off,
    that publish outranks them, exactly as a live push would.
    """
    from config_editor import clear_local_config, load_local_config

    local, error = await load_local_config()

    if error:
        await state.log(
            f"[config] Ignoring unusable local limits ({error})",
            level=logging.WARNING,
        )
        return
    if local is None:
        return

    if local.based_on_version != config_store.server_version:
        await clear_local_config()
        await state.log(
            f"[config] Discarded local limits set on v{local.based_on_version} — "
            f"the server has since published v{config_store.server_version}",
            level=logging.WARNING,
        )
        return

    try:
        config = await config_store.restore_local(local, state)
    except ConfigError as exc:
        await clear_local_config()
        await state.log(
            f"[config] Discarded unusable local limits ({exc}) — "
            "back to the published config",
            level=logging.WARNING,
        )
        return

    await state.log(
        f"[config] Restored locally edited limits on v{config.version} "
        f"(revision {local.revision}, saved {local.saved_at or 'unknown'}): "
        f"water {config.limits.min_water_temperature_c}–"
        f"{config.limits.max_water_temperature_c} °C, "
        f"ambient max {config.limits.max_ambient_temperature_c} °C — "
        "the server's next publish replaces them",
        level=logging.WARNING,
    )


async def supervise(
    state: RuntimeState,
    jobs: dict[str, Callable[[RuntimeState], Awaitable[None]]],
) -> None:
    """
    Run the agent's tasks until shutdown, restarting any that fall over.

    This used to be a plain ``asyncio.gather`` of every task, which meant the
    agent's lifetime was the shortest-lived task's: one unhandled exception
    anywhere — a probe pulled out of the bus at the wrong moment, a record the
    device could not apply, a screen that raised while drawing — propagated out
    of gather, and main released the relays, closed the database and exited.
    systemd restarted it, it crashed again on the same fault, and a heating
    system spent its time in a restart loop with the server seeing a device that
    was reporting nothing.

    A task that *returns* is left alone: several of these are meant to finish
    (``auth_loop`` returns once it has a session, ``run_control_menu`` returns
    when there is nothing to control this device by hand), and restarting them
    would spin. Only a task that *raises* is started again, and only after a
    pause that grows while it keeps happening, so a task failing on every start
    logs rather than burns the CPU.

    The supervisor returns when every task has returned or shutdown is set, so
    an agent with nothing left to do still exits cleanly.
    """

    async def runner(name: str, start: Callable[[RuntimeState], Awaitable[None]]) -> None:
        backoff = TASK_RESTART_SECONDS

        while not state.shutdown.is_set():
            try:
                await start(state)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await state.log(
                    f"[{name}] Died: {type(exc).__name__}: {exc} — restarting in "
                    f"{backoff:.0f}s",
                    level=logging.ERROR,
                )
                try:
                    await asyncio.wait_for(state.shutdown.wait(), timeout=backoff)
                    return  # shutdown, not a retry
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, TASK_RESTART_MAX_SECONDS)
                continue

            # Returned normally: it had its turn and is done.
            return

    tasks = {
        name: asyncio.create_task(runner(name, start), name=name)
        for name, start in jobs.items()
    }

    shutting_down = asyncio.create_task(state.shutdown.wait())

    try:
        # Either shutdown arrives, or every task has finished its turn. The
        # second case is real: a device with no mapping and no input device has
        # nothing left to run, and used to exit rather than sit there.
        #
        # The test is on the tasks alone. Including the shutdown waiter would
        # mean never all of them being done — that one is *supposed* to still
        # be waiting — so the loop would spin on an already-finished wait
        # instead of leaving.
        while not state.shutdown.is_set():
            if all(task.done() for task in tasks.values()):
                break
            await asyncio.wait(
                [*tasks.values(), shutting_down],
                return_when=asyncio.FIRST_COMPLETED,
            )
    except asyncio.CancelledError:
        state.shutdown.set()
        raise
    finally:
        state.shutdown.set()
        shutting_down.cancel()
        for task in tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        await state.log("Shutdown complete.")
        shutdown_logging()


def install_signal_handlers(state: RuntimeState) -> None:
    """
    Ask the loop to shut down cleanly on SIGTERM/SIGINT.

    systemd stops a service with SIGTERM. Without a handler Python would exit
    immediately, skipping relay cleanup and leaving the SQLite connection open.
    Not available on Windows, where the loop has no signal support.
    """
    loop = asyncio.get_running_loop()
    for name in ("SIGTERM", "SIGINT"):
        signal_number = getattr(signal, name, None)
        if signal_number is None:
            continue
        try:
            loop.add_signal_handler(signal_number, state.shutdown.set)
        except (NotImplementedError, RuntimeError):
            pass  # Windows: KeyboardInterrupt handling covers Ctrl-C


async def main() -> None:
    configure_logging()
    state = RuntimeState(read_interval=DEFAULT_READ_INTERVAL)
    install_signal_handlers(state)

    # Said out loud on every boot, because it is the one setting whose being
    # wrong is invisible: a device reporting simulated readings looks healthy
    # from the server, the dashboard and the panel alike.
    await state.log(
        "[main] Hardware: "
        + (
            "SIMULATED — sensors and relays are not real"
            if USE_MOCK_HARDWARE
            else "real sensors, relays, keypad and display"
        ),
        level=logging.WARNING if USE_MOCK_HARDWARE else logging.INFO,
    )

    # Constructed here, started by the control menu — it is the only thing that
    # reads from it, and it has to be able to fall back to the keyboard when a
    # keypad will not come up. A misconfigured pin map is caught in the
    # constructor and must not stop an agent whose real job is the heating.
    try:
        state.keypad = Keypad()
    except Exception as exc:
        await state.log(
            f"[menu] Keypad not configured: {exc} — the menu falls back to "
            "the keyboard, if there is one",
            level=logging.ERROR,
        )

    # The panel the menu is drawn on, constructed and started the same way and
    # for the same reason: a device with no display still runs the heating.
    try:
        state.display = Display()
    except Exception as exc:
        await state.log(
            f"[menu] Display not configured: {exc} — the menu falls back to "
            "the terminal, if there is one",
            level=logging.ERROR,
        )

    # The record comes first: it is the mapping source, so it has to be in
    # place before the mapping is built from it.
    await restore_cached_device_record(state)
    await load_mapping(state)
    await restore_cached_config(state)
    await restore_cached_schedule(state)
    # Before any task starts, so the first schedule tick already knows which
    # units are hands-off.
    await restore_cached_modes(state)
    # The anti-freeze thresholds an operator set on this device, before any task
    # starts: they decide whether the very first read cycle engages the latch,
    # and a pair that only came back after the first check would be a pair that
    # had already been ignored once.
    await anti_freeze.load_local(state)
    # The 1-Wire connection order, before any task starts: the
    # watcher task polls the bus, and the cache is what says
    # which probe was fitted first when the agent itself was
    # not running to see it.
    sensor_watcher.mock = USE_MOCK_HARDWARE
    await sensor_watcher.load()
    if sensor_watcher.sensors:
        await state.log(
            f"[sensor] Restored {len(sensor_watcher.sensors)} thermal "
            "sensor(s) from the watch cache"
        )
    await print_startup_banner(state)

    await supervise(
        state,
        {
            "auth_loop": auth_loop,
            "sensor_loop": sensor_loop,
            "control_menu": run_control_menu,
            "websocket_client": run_websocket_client,
            "schedule_loop": schedule_loop,
            "device_record": device_record_loop,
            "state_publisher": run_state_publisher,
            "sensor_watcher": sensor_watcher.run,
        },
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # main() has already stopped the logging listener by this point, so a
        # log call here would go nowhere.
        print("\nStopping...")
