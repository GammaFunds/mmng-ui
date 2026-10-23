"""Opt-in RTL-SDR acquisition through rtl_fm."""

from __future__ import annotations

import asyncio
import math
from asyncio.subprocess import PIPE
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from collections.abc import Callable

_MIN_FREQUENCY_HZ = 1
_MAX_FREQUENCY_HZ = 2_000_000_000
_MAX_GAIN_DB = 50
_MAX_PPM_CORRECTION = 1000
_PPM_TYPE_ERROR = 'PPM correction must be an integer'
_PPM_RANGE_ERROR = 'PPM correction must be between -1000 and 1000'
_STDERR_TAIL_SIZE = 8192

_FREQUENCY_TYPE_ERROR = 'Frequency must be an integer number of Hz'
_FREQUENCY_RANGE_ERROR = 'Frequency must be between 1 and 2000000000 Hz'
_DEVICE_INDEX_TYPE_ERROR = 'Device index must be an integer'
_DEVICE_INDEX_RANGE_ERROR = 'Device index must be a non-negative integer'
_GAIN_TYPE_ERROR = 'Gain must be a finite number of dB'
_GAIN_CONVERSION_ERROR = 'Gain must be a finite number of dB'
_GAIN_RANGE_ERROR = 'Gain must be between 0 and 50 dB'
_CLEANUP_RETRY_REQUIRED = 'RTL-SDR cleanup is unresolved; retry Stop before starting'


class SdrError(RuntimeError):
    """An RTL-SDR process could not be started or did not run cleanly."""


class DeviceBusyError(SdrError):
    """An RTL-SDR device already has an active receiver in this process."""


@dataclass(frozen=True)
class SDRConfig:
    """Validated rtl_fm settings. Frequency is in Hz and gain is in dB."""

    frequency_hz: int
    gain_db: float
    device_index: int = 0
    ppm_correction: int = 0

    def __post_init__(self) -> None:
        frequency_hz = self.frequency_hz
        gain_db = self.gain_db
        device_index = self.device_index
        if isinstance(self.ppm_correction, bool) or not isinstance(self.ppm_correction, int):
            raise TypeError(_PPM_TYPE_ERROR)
        if not -_MAX_PPM_CORRECTION <= self.ppm_correction <= _MAX_PPM_CORRECTION:
            raise ValueError(_PPM_RANGE_ERROR)
        if isinstance(frequency_hz, bool) or not isinstance(frequency_hz, int):
            raise TypeError(_FREQUENCY_TYPE_ERROR)
        if not _MIN_FREQUENCY_HZ <= frequency_hz <= _MAX_FREQUENCY_HZ:
            raise ValueError(_FREQUENCY_RANGE_ERROR)
        if isinstance(device_index, bool) or not isinstance(device_index, int):
            raise TypeError(_DEVICE_INDEX_TYPE_ERROR)
        if device_index < 0:
            raise ValueError(_DEVICE_INDEX_RANGE_ERROR)
        if isinstance(gain_db, bool) or not isinstance(gain_db, (int, float)):
            raise TypeError(_GAIN_TYPE_ERROR)
        try:
            gain_db = float(gain_db)
        except (OverflowError, ValueError) as exc:
            raise ValueError(_GAIN_CONVERSION_ERROR) from exc
        if not math.isfinite(gain_db):
            raise ValueError(_GAIN_CONVERSION_ERROR)
        if not 0 <= gain_db <= _MAX_GAIN_DB:
            raise ValueError(_GAIN_RANGE_ERROR)
        object.__setattr__(self, 'gain_db', gain_db)


def build_rtl_fm_args(config: SDRConfig) -> list[str]:
    """Build rtl_fm arguments for demodulated mono PCM at 22050 Hz."""
    args = [
        '-d',
        str(config.device_index),
        '-f',
        str(config.frequency_hz),
        '-g',
        f'{config.gain_db:g}',
        '-s',
        '22050',
        '-M',
        'fm',
    ]
    if config.ppm_correction:
        args.extend(['-p', str(config.ppm_correction)])
    return args


class RTLFMReceiver:
    """Own one rtl_fm process and forward its PCM bytes to a decoder pipe."""

    _device_owners: ClassVar[dict[int, RTLFMReceiver]] = {}

    def __init__(
        self,
        config: SDRConfig,
        sink,
        executable: str = 'rtl_fm',
        termination_timeout: float = 2.0,
        on_state: Callable[[str, str | None], None] | None = None,
    ) -> None:
        self.config = config
        self.sink = sink
        self.executable = executable
        self.termination_timeout = termination_timeout
        self.on_state = on_state
        self.process = None
        self.state = 'stopped'
        self.error: str | None = None
        self._task: asyncio.Task | None = None
        self._start_task: asyncio.Task | None = None
        self._stop_task: asyncio.Task | None = None
        self._session_id = object()
        self._stderr_tail = bytearray()
        self._stopping = False
        self._stop_requested = False
        self._terminate_lock = asyncio.Lock()
        self._process_wait_task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._stop_task is not None and not self._stop_task.done():
            return
        if self.state in ('starting', 'running', 'stopping'):
            return
        if self.state == 'cleanup_failed':
            raise SdrError(_CLEANUP_RETRY_REQUIRED)
        if self._task is not None and not self._task.done():
            return
        owner = self._device_owners.get(self.config.device_index)
        if owner is not None and owner is not self:
            message = f'RTL-SDR device {self.config.device_index} is already in use'
            raise DeviceBusyError(message)

        self._device_owners[self.config.device_index] = self
        self._session_id = object()
        self.error = None
        self._stderr_tail.clear()
        self._stopping = False
        self._stop_requested = False
        self._process_wait_task = None
        self._set_state('starting')
        startup = asyncio.create_task(self._start_receiver(self._session_id))
        self._start_task = startup
        try:
            await asyncio.shield(startup)
        except asyncio.CancelledError:
            if startup is not None and not startup.done():
                self._stop_requested = True
                self._set_state('stopping')
                with suppress(SdrError):
                    await asyncio.shield(startup)
            else:
                with suppress(SdrError):
                    startup.result()
                if self._task is not None and not self._task.done():
                    await self.stop()
            raise

    async def _start_receiver(self, session_id: object) -> None:
        try:
            try:
                self.process = await asyncio.create_subprocess_exec(
                    self.executable,
                    *build_rtl_fm_args(self.config),
                    stdout=PIPE,
                    stderr=PIPE,
                )
            except (OSError, RuntimeError, ValueError, TypeError) as exc:
                self.error = str(exc)
                self._finish_session(session_id, 'error')
                raise SdrError(self.error) from exc

            self._task = asyncio.create_task(self._pump(session_id))
            if self._stop_requested:
                await self._stop_running()
            else:
                self._set_state('running')
        finally:
            if self._start_task is asyncio.current_task():
                self._start_task = None

    async def stop(self) -> None:
        startup = self._start_task
        if startup is not None and not startup.done():
            self._stop_requested = True
            self._set_state('stopping')
            with suppress(SdrError):
                await asyncio.shield(startup)
            return
        if self.process is None:
            return
        cleanup = self._stop_task
        if cleanup is None or cleanup.done():
            if self.state == 'stopped' and self._device_owners.get(self.config.device_index) is not self:
                return
            cleanup = asyncio.create_task(self._stop_running())
            self._stop_task = cleanup
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            await asyncio.shield(cleanup)
            raise

    async def _stop_running(self) -> None:
        session_id = self._session_id
        self._stop_task = asyncio.current_task()
        self._stopping = True
        self._set_state('stopping')
        try:
            await self._terminate_process()
            if self._task is not None and not self._task.done():
                try:
                    await asyncio.wait_for(asyncio.shield(self._task), timeout=self.termination_timeout)
                except asyncio.TimeoutError:
                    self._task.cancel()
                    await asyncio.gather(self._task, return_exceptions=True)
            self.error = None
            self._finish_session(session_id, 'stopped')
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = str(exc)
            self._set_state('cleanup_failed')
            raise

    async def wait(self) -> None:
        if self._task is not None:
            await self._task

    async def _pump(self, session_id: object) -> None:
        stderr_task = asyncio.create_task(self._drain_stderr())
        cleanup_confirmed = False
        terminal_state = None
        try:
            while chunk := await self.process.stdout.read(8192):
                self.sink.write(chunk)
                await self.sink.drain()
            if self.process.returncode is None and not self._stopping:
                try:
                    return_code = await self._wait_for_process()
                except asyncio.TimeoutError:
                    self._stopping = True
                    await self._terminate_process()
                    return_code = await self._wait_for_process()
            else:
                return_code = await self._wait_for_process()
            cleanup_confirmed = True
            await stderr_task
            if return_code and not self._stopping:
                detail = self._stderr_tail.decode(errors='replace').strip()
                self.error = detail or f'rtl_fm exited with status {return_code}'
                terminal_state = 'error'
            else:
                terminal_state = 'stopped'
        except asyncio.CancelledError:
            self._stopping = True
            try:
                await self._terminate_process()
            except Exception as exc:  # noqa: BLE001 - preserve unexpected cleanup failures
                self.error = str(exc)
                self._set_state('cleanup_failed')
                return
            else:
                cleanup_confirmed = True
                terminal_state = 'stopped'
            raise
        except Exception as exc:  # noqa: BLE001 - preserve every cleanup failure for retry
            self.error = str(exc)
            if self.process is not None:
                try:
                    await self._terminate_process()
                except Exception as cleanup_error:  # noqa: BLE001 - preserve unexpected cleanup failures
                    self.error = str(cleanup_error)
                    self._set_state('cleanup_failed')
                    return
                cleanup_confirmed = True
            terminal_state = 'error'
        finally:
            if not stderr_task.done():
                stderr_task.cancel()
                await asyncio.gather(stderr_task, return_exceptions=True)
            if cleanup_confirmed and (self._stop_task is None or self._stop_task.done()):
                self._finish_session(session_id, terminal_state)

    async def _wait_for_process(self) -> int:
        if self._process_wait_task is None:
            self._process_wait_task = asyncio.create_task(self.process.wait())
        try:
            return await asyncio.wait_for(asyncio.shield(self._process_wait_task), timeout=self.termination_timeout)
        except Exception:
            if self._process_wait_task.done():
                self._process_wait_task = None
            raise

    async def _drain_stderr(self) -> None:
        while chunk := await self.process.stderr.read(4096):
            self._stderr_tail.extend(chunk)
            if len(self._stderr_tail) > _STDERR_TAIL_SIZE:
                del self._stderr_tail[:-_STDERR_TAIL_SIZE]

    async def _terminate_process(self) -> None:
        if self.process is None:
            return
        async with self._terminate_lock:
            if self.process.returncode is None:
                with suppress(ProcessLookupError):
                    self.process.terminate()
            try:
                await self._wait_for_process()
            except asyncio.TimeoutError:
                with suppress(ProcessLookupError):
                    self.process.kill()
                await self._wait_for_process()

    def _finish_session(self, session_id: object, state: str) -> None:
        if session_id is self._session_id:
            # Release and publish together, after all awaited cleanup has finished.
            self._release_device(session_id)
            self._set_state(state)

    def _release_device(self, session_id: object) -> None:
        if session_id is self._session_id and self._device_owners.get(self.config.device_index) is self:
            del self._device_owners[self.config.device_index]

    def _set_state(self, state: str) -> None:
        self.state = state
        if self.on_state is not None:
            with suppress(Exception):
                self.on_state(state, self.error)
