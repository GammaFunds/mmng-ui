import asyncio

import pytest

import mmng_ui.sdr as sdr_module
from mmng_ui.sdr import DeviceBusyError, RTLFMReceiver, SDRConfig, SdrError, build_rtl_fm_args


def test_ppm_constructor_and_zero_argv_compatibility():
    config = SDRConfig(123_456_789, 17.5, 2)
    assert config.ppm_correction == 0
    assert build_rtl_fm_args(config) == ['-d', '2', '-f', '123456789', '-g', '17.5', '-s', '22050', '-M', 'fm']


@pytest.mark.parametrize('ppm', [-1000, -12, 12, 1000])
def test_ppm_signed_arguments(ppm):
    assert build_rtl_fm_args(SDRConfig(123_456_789, 12, ppm_correction=ppm))[-2:] == ['-p', str(ppm)]


@pytest.mark.parametrize('ppm', [True, False, 1.5, '12', None, -1001, 1001])
def test_ppm_rejects_invalid_values(ppm):
    with pytest.raises((TypeError, ValueError), match='PPM'):
        SDRConfig(123_456_789, 12, ppm_correction=ppm)


def test_sdr_config_requires_a_supported_frequency_and_manual_gain():
    assert SDRConfig(frequency_hz=123_456_789, gain_db=17.5).device_index == 0

    for frequency_hz in (0, -1, 2_000_000_001):
        with pytest.raises(ValueError, match='Frequency must be between'):
            SDRConfig(frequency_hz=frequency_hz, gain_db=17.5)
    for gain_db in (-0.1, 50.1, float('nan'), float('inf')):
        with pytest.raises(ValueError, match='Gain must be'):
            SDRConfig(frequency_hz=123_456_789, gain_db=gain_db)
    for frequency_hz in (True, 123.5, '123'):
        with pytest.raises(TypeError, match='Frequency must be an integer'):
            SDRConfig(frequency_hz=frequency_hz, gain_db=17.5)
    for gain_db in (True, '17.5'):
        with pytest.raises(TypeError, match='Gain must be a finite number'):
            SDRConfig(frequency_hz=123_456_789, gain_db=gain_db)


def test_build_rtl_fm_args_uses_explicit_safe_pcm_settings():
    config = SDRConfig(frequency_hz=123_456_789, gain_db=17.5, device_index=2)

    assert build_rtl_fm_args(config) == [
        '-d',
        '2',
        '-f',
        '123456789',
        '-g',
        '17.5',
        '-s',
        '22050',
        '-M',
        'fm',
    ]


class FakeSink:
    def __init__(self):
        self.data = bytearray()

    def write(self, data):
        self.data.extend(data)

    async def drain(self):
        await asyncio.sleep(0)


class FakeProcess:
    def __init__(self, stdout_data=b'', stderr_data=b'', returncode=None):
        self.stdout = asyncio.StreamReader()
        self.stderr = asyncio.StreamReader()
        self.returncode = None
        self._final_returncode = returncode
        self._exited = asyncio.Event()
        if stdout_data:
            self.stdout.feed_data(stdout_data)
        if stderr_data:
            self.stderr.feed_data(stderr_data)
        if returncode is not None:
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self.returncode = returncode
            self._exited.set()

    async def wait(self):
        await self._exited.wait()
        return self.returncode

    def terminate(self):
        self.returncode = -15
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._exited.set()

    def kill(self):
        self.returncode = -9
        self.stdout.feed_eof()
        self.stderr.feed_eof()
        self._exited.set()


@pytest.mark.asyncio
async def test_restart_waits_for_the_previous_stop_coordinator(monkeypatch):
    processes = [FakeProcess(), FakeProcess()]
    spawned = []
    pump_finished = asyncio.Event()
    complete_stop = asyncio.Event()

    async def spawn(*_args, **_kwargs):
        process = processes[len(spawned)]
        spawned.append(process)
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    config = SDRConfig(123_456_789, 12)
    receiver = RTLFMReceiver(config, FakeSink(), termination_timeout=0.01)
    competitor = RTLFMReceiver(config, FakeSink())
    original_terminate = receiver._terminate_process  # noqa: SLF001
    held_once = False

    async def pause_stop_after_pump():
        nonlocal held_once
        await original_terminate()
        if not held_once:
            held_once = True
            await asyncio.wait_for(asyncio.shield(receiver._task), timeout=1)  # noqa: SLF001
            pump_finished.set()
            await complete_stop.wait()

    monkeypatch.setattr(receiver, '_terminate_process', pause_stop_after_pump)
    await receiver.start()
    stopping = asyncio.create_task(receiver.stop())
    try:
        await asyncio.wait_for(pump_finished.wait(), timeout=1)
        state_during_stop = receiver.state
        assert not stopping.done()
        await receiver.start()
        premature_restart = len(spawned) > 1
        complete_stop.set()
        await asyncio.wait_for(stopping, timeout=1)

        owns_device = config.device_index in receiver._device_owners  # noqa: SLF001
        assert not premature_restart, (
            f'Start admitted B while Stop A was incomplete; old Stop left state={receiver.state}, '
            f'B returncode={processes[1].returncode}, ownership={owns_device}'
        )
        assert state_during_stop == 'stopping'
        assert receiver.state == 'stopped'
        await receiver.start()
        assert spawned == processes
        assert receiver.process is processes[1]
        assert receiver.state == 'running'
        assert receiver._device_owners[config.device_index] is receiver  # noqa: SLF001
        with pytest.raises(DeviceBusyError):
            await competitor.start()
        # An already completed Stop waiter must have no later effect on B.
        await asyncio.wait_for(stopping, timeout=1)
        assert receiver.state == 'running'
        assert processes[1].returncode is None
    finally:
        complete_stop.set()
        await asyncio.wait_for(asyncio.gather(stopping, return_exceptions=True), timeout=1)
        await asyncio.wait_for(receiver.stop(), timeout=1)


@pytest.mark.asyncio
async def test_stop_waiters_share_one_coordinator_and_cannot_invalidate_restart(monkeypatch):
    processes = [FakeProcess(), FakeProcess()]
    spawned = []
    paused = asyncio.Event()
    complete_stop = asyncio.Event()
    second_entered = asyncio.Event()
    restarted = []
    terminal_snapshots = []
    terminate_calls = 0

    async def spawn(*_args, **_kwargs):
        if spawned:
            assert receiver._stop_task.done()  # noqa: SLF001
        process = processes[len(spawned)]
        spawned.append(process)
        return process

    def observe(state, _error):
        if state == 'stopped':
            terminal_snapshots.append(0 in receiver._device_owners)  # noqa: SLF001
            if not restarted:
                restarted.append(asyncio.create_task(receiver.start()))

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    receiver = RTLFMReceiver(SDRConfig(123_456_789, 12), FakeSink(), on_state=observe)
    original_terminate = receiver._terminate_process  # noqa: SLF001

    async def hold_first_stop():
        nonlocal terminate_calls
        terminate_calls += 1
        await original_terminate()
        if terminate_calls == 1:
            await asyncio.wait_for(asyncio.shield(receiver._task), timeout=1)  # noqa: SLF001
            paused.set()
            await complete_stop.wait()

    async def second_stop():
        second_entered.set()
        await receiver.stop()

    monkeypatch.setattr(receiver, '_terminate_process', hold_first_stop)
    await receiver.start()
    first = asyncio.create_task(receiver.stop())
    second = None
    try:
        await asyncio.wait_for(paused.wait(), timeout=1)
        coordinator = receiver._stop_task  # noqa: SLF001
        second = asyncio.create_task(second_stop())
        await asyncio.wait_for(second_entered.wait(), timeout=1)
        assert receiver._stop_task is coordinator  # noqa: SLF001
        assert terminate_calls == 1
        second.cancel()
        complete_stop.set()
        results = await asyncio.wait_for(asyncio.gather(first, second, return_exceptions=True), timeout=1)
        assert results[0] is None
        assert isinstance(results[1], asyncio.CancelledError)
        await asyncio.wait_for(asyncio.gather(*restarted), timeout=1)
        assert terminal_snapshots == [False]
        assert spawned == processes
        assert receiver.state == 'running'
        assert processes[1].returncode is None
        assert receiver._device_owners[0] is receiver  # noqa: SLF001
        await receiver.stop()
        snapshot = terminal_snapshots.copy()
        await receiver.stop()
        assert terminal_snapshots == snapshot
    finally:
        complete_stop.set()
        pending = [task for task in (first, second, *restarted) if task is not None]
        await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=1)
        await asyncio.wait_for(receiver.stop(), timeout=1)


@pytest.mark.asyncio
async def test_session_fence_preserves_new_session_and_other_receiver_ownership(monkeypatch):
    processes = [FakeProcess(), FakeProcess(), FakeProcess()]

    async def spawn(*_args, **_kwargs):
        return processes.pop(0)

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    config = SDRConfig(123_456_789, 12)
    receiver = RTLFMReceiver(config, FakeSink())
    competitor = RTLFMReceiver(config, FakeSink())
    try:
        await receiver.start()
        old_session = receiver._session_id  # noqa: SLF001
        await receiver.stop()
        await receiver.start()
        new_session = receiver._session_id  # noqa: SLF001
        assert new_session is not old_session
        receiver._finish_session(old_session, 'stopped')  # noqa: SLF001
        receiver._release_device(old_session)  # noqa: SLF001
        assert receiver.state == 'running'
        assert receiver.process.returncode is None
        assert receiver._device_owners[0] is receiver  # noqa: SLF001
        with pytest.raises(DeviceBusyError):
            await competitor.start()

        await receiver.stop()
        await competitor.start()
        receiver._release_device(new_session)  # noqa: SLF001
        receiver._finish_session(new_session, 'stopped')  # noqa: SLF001
        await receiver.stop()
        assert competitor.state == 'running'
        assert competitor.process.returncode is None
        assert receiver._device_owners[0] is competitor  # noqa: SLF001
    finally:
        await asyncio.wait_for(receiver.stop(), timeout=1)
        await asyncio.wait_for(competitor.stop(), timeout=1)


@pytest.mark.asyncio
async def test_receiver_streams_pcm_and_stops_cleanly(monkeypatch):
    process = FakeProcess(stdout_data=b'\x00\x80\xff\x7f')
    calls = []

    async def create_subprocess_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create_subprocess_exec)
    sink = FakeSink()
    receiver = RTLFMReceiver(SDRConfig(123_456_789, 12), sink)

    await receiver.start()
    await receiver.stop()

    assert bytes(sink.data) == b'\x00\x80\xff\x7f'
    assert calls[0][0] == ('rtl_fm', *build_rtl_fm_args(receiver.config))
    assert calls[0][1] == {'stdout': asyncio.subprocess.PIPE, 'stderr': asyncio.subprocess.PIPE}
    assert receiver.state == 'stopped'


@pytest.mark.asyncio
async def test_startup_failure_is_reported_and_releases_device(monkeypatch):
    failure_message = 'rtl_fm unavailable'

    async def create_subprocess_exec(*_args, **_kwargs):
        raise FileNotFoundError(failure_message)

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create_subprocess_exec)
    receiver = RTLFMReceiver(SDRConfig(123_456_789, 12), FakeSink())

    with pytest.raises(SdrError, match='rtl_fm unavailable'):
        await receiver.start()

    assert receiver.state == 'error'
    assert 'rtl_fm unavailable' in receiver.error


@pytest.mark.asyncio
async def test_state_observer_failure_does_not_interrupt_process_cleanup(monkeypatch):
    process = FakeProcess()
    failure_message = 'UI widget was already removed'

    async def create_subprocess_exec(*_args, **_kwargs):
        return process

    def broken_observer(_state, _error):
        raise RuntimeError(failure_message)

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create_subprocess_exec)
    receiver = RTLFMReceiver(SDRConfig(123_456_789, 12), FakeSink(), on_state=broken_observer)

    await receiver.start()
    await receiver.stop()

    assert process.returncode == -15
    assert receiver.state == 'stopped'


@pytest.mark.asyncio
async def test_only_one_receiver_owns_a_device_until_stop(monkeypatch):
    processes = [FakeProcess(), FakeProcess()]

    async def create_subprocess_exec(*_args, **_kwargs):
        return processes.pop(0)

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create_subprocess_exec)
    config = SDRConfig(123_456_789, 12)
    first = RTLFMReceiver(config, FakeSink())
    second = RTLFMReceiver(config, FakeSink())

    await first.start()
    with pytest.raises(DeviceBusyError):
        await second.start()

    await first.stop()
    await second.start()
    await second.stop()


@pytest.mark.asyncio
async def test_concurrent_start_calls_on_one_receiver_spawn_only_one_process(monkeypatch):
    spawn_gate = asyncio.Event()
    spawn_started = asyncio.Event()
    process = FakeProcess()
    spawn_count = 0

    async def create_subprocess_exec(*_args, **_kwargs):
        nonlocal spawn_count
        spawn_count += 1
        spawn_started.set()
        await spawn_gate.wait()
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create_subprocess_exec)
    receiver = RTLFMReceiver(SDRConfig(123_456_789, 12), FakeSink())
    first_start = asyncio.create_task(receiver.start())
    await asyncio.wait_for(spawn_started.wait(), timeout=1)
    second_start = asyncio.create_task(receiver.start())
    await asyncio.wait_for(second_start, timeout=1)
    spawn_gate.set()
    await asyncio.wait_for(asyncio.gather(first_start, second_start), timeout=1)

    assert spawn_count == 1
    await receiver.stop()


@pytest.mark.asyncio
async def test_cancelling_pcm_pump_terminates_child_and_releases_device(monkeypatch):
    processes = [FakeProcess(), FakeProcess()]

    async def create_subprocess_exec(*_args, **_kwargs):
        return processes.pop(0)

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create_subprocess_exec)
    receiver = RTLFMReceiver(SDRConfig(123_456_789, 12), FakeSink())
    await receiver.start()
    process = receiver.process
    cancellation = asyncio.create_task(receiver.wait())
    await asyncio.sleep(0)
    cancellation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancellation

    assert process.returncode == -15
    assert receiver.state == 'stopped'
    await receiver.start()
    await receiver.stop()


@pytest.mark.asyncio
async def test_stop_kills_child_after_bounded_terminate_timeout(monkeypatch):
    class SlowProcess(FakeProcess):
        def terminate(self):
            pass

    processes = [SlowProcess()]

    async def create_subprocess_exec(*_args, **_kwargs):
        return processes.pop(0)

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create_subprocess_exec)
    receiver = RTLFMReceiver(SDRConfig(123_456_789, 12), FakeSink(), termination_timeout=0.01)
    await receiver.start()
    await receiver.stop()

    assert receiver.process.returncode == -9
    assert receiver.state == 'stopped'


@pytest.mark.asyncio
async def test_stop_handles_child_exit_race_before_terminate(monkeypatch):
    class ExitedProcess(FakeProcess):
        def terminate(self):
            self.returncode = 0
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self._exited.set()
            raise ProcessLookupError

    processes = [ExitedProcess(), FakeProcess()]

    async def create_subprocess_exec(*_args, **_kwargs):
        return processes.pop(0)

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create_subprocess_exec)
    receiver = RTLFMReceiver(SDRConfig(123_456_789, 12), FakeSink())

    await receiver.start()
    await receiver.stop()

    assert receiver.state == 'stopped'
    await receiver.start()
    await receiver.stop()


@pytest.mark.asyncio
async def test_stop_handles_child_exit_between_grace_timeout_and_kill(monkeypatch):
    class KillRaceProcess(FakeProcess):
        def __init__(self):
            super().__init__()
            self.terminate_called = False
            self.kill_called = False
            self.wait_calls = 0

        def terminate(self):
            self.terminate_called = True

        def kill(self):
            self.kill_called = True
            assert self.terminate_called
            self.returncode = 0
            self.stdout.feed_eof()
            self.stderr.feed_eof()
            self._exited.set()
            raise ProcessLookupError

        async def wait(self):
            self.wait_calls += 1
            return await super().wait()

    process = KillRaceProcess()
    original_wait_for = asyncio.wait_for
    wait_for_calls = 0

    async def controlled_wait_for(awaitable, timeout):
        nonlocal wait_for_calls
        wait_for_calls += 1
        if wait_for_calls == 1:
            assert process.returncode is None
            if hasattr(awaitable, 'close'):
                awaitable.close()
            else:
                awaitable.cancel()
            raise asyncio.TimeoutError
        return await original_wait_for(awaitable, timeout)

    async def create_subprocess_exec(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create_subprocess_exec)
    monkeypatch.setattr(sdr_module.asyncio, 'wait_for', controlled_wait_for)
    config = SDRConfig(123_456_789, 12)
    receiver = RTLFMReceiver(config, FakeSink())

    await receiver.start()
    await receiver.stop()
    await receiver.stop()

    assert process.terminate_called
    assert process.kill_called
    assert process.returncode == 0
    assert process.wait_calls >= 1
    assert receiver.state == 'stopped'
    # Verify both internal cleanup invariants, including the ownership registry.
    assert receiver._task is None or receiver._task.done()  # noqa: SLF001
    assert config.device_index not in receiver._device_owners  # noqa: SLF001


@pytest.mark.asyncio
async def test_failed_child_cleanup_retains_state_reservation_and_udp_gate(monkeypatch):
    class PermissionDeniedProcess(FakeProcess):
        def terminate(self):
            raise PermissionError('signal denied')  # noqa: EM101, TRY003

        def kill(self):
            raise PermissionError('kill denied')  # noqa: EM101, TRY003

    process = PermissionDeniedProcess()

    async def create_subprocess_exec(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create_subprocess_exec)
    config = SDRConfig(123_456_789, 12)
    receiver = RTLFMReceiver(config, FakeSink(), termination_timeout=0.01)
    second = RTLFMReceiver(config, FakeSink())
    await receiver.start()

    assert receiver._task is not None  # noqa: SLF001
    receiver._task.cancel()  # noqa: SLF001
    result = await asyncio.gather(receiver._task, return_exceptions=True)  # noqa: SLF001

    assert result == [None]
    assert process.returncode is None
    assert receiver.state == 'cleanup_failed'
    assert receiver.process is process
    assert receiver.error is not None
    assert 'signal denied' in receiver.error
    assert receiver._device_owners[config.device_index] is receiver  # noqa: SLF001
    with pytest.raises(DeviceBusyError):
        await second.start()

    process.terminate = FakeProcess.terminate.__get__(process, PermissionDeniedProcess)
    await receiver.stop()
    assert process.returncode == -15
    assert receiver.state == 'stopped'
    assert config.device_index not in receiver._device_owners  # noqa: SLF001
    await second.start()
    await second.stop()


@pytest.mark.asyncio
async def test_stop_timeout_is_cleanup_failed_and_can_be_retried(monkeypatch):
    class DelayedTerminationProcess(FakeProcess):
        def __init__(self):
            super().__init__()
            self.allow_termination = False

        def terminate(self):
            if self.allow_termination:
                FakeProcess.terminate(self)

        def kill(self):
            pass

    process = DelayedTerminationProcess()

    async def create_subprocess_exec(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create_subprocess_exec)
    config = SDRConfig(123_456_789, 12)
    receiver = RTLFMReceiver(config, FakeSink(), termination_timeout=0.01)
    await receiver.start()

    with pytest.raises(asyncio.TimeoutError):
        await receiver.stop()
    assert receiver.state == 'cleanup_failed'
    assert receiver.process is process
    assert receiver._device_owners[config.device_index] is receiver  # noqa: SLF001

    process.allow_termination = True
    await receiver.stop()
    assert receiver.state == 'stopped'
    assert config.device_index not in receiver._device_owners  # noqa: SLF001


@pytest.mark.asyncio
async def test_stop_retries_cleanup_even_after_pcm_pump_finishes(monkeypatch):
    class UnreapedProcess(FakeProcess):
        def terminate(self):
            raise PermissionError('temporary denial')  # noqa: EM101, TRY003

    process = UnreapedProcess()

    async def create_subprocess_exec(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create_subprocess_exec)
    receiver = RTLFMReceiver(SDRConfig(123_456_789, 12), FakeSink())
    await receiver.start()
    receiver._task.cancel()  # noqa: SLF001
    await asyncio.gather(receiver._task, return_exceptions=True)  # noqa: SLF001
    assert receiver._task.done()  # noqa: SLF001

    with pytest.raises(PermissionError):
        await receiver.stop()
    assert receiver.state == 'cleanup_failed'
    assert receiver.process is process
    assert receiver._device_owners[receiver.config.device_index] is receiver  # noqa: SLF001
    process.terminate = FakeProcess.terminate.__get__(process, UnreapedProcess)
    await receiver.stop()


@pytest.mark.asyncio
async def test_unexpected_child_exit_is_reaped_before_stopped_and_released(monkeypatch):
    child_waiting = asyncio.Event()
    allow_reap = asyncio.Event()
    states = []

    class UnexpectedExitProcess(FakeProcess):
        async def wait(self):
            child_waiting.set()
            await allow_reap.wait()
            self.returncode = 0
            self.stderr.feed_eof()
            return self.returncode

    process = UnexpectedExitProcess()
    process.stdout.feed_eof()

    async def create_subprocess_exec(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create_subprocess_exec)
    config = SDRConfig(123_456_789, 12)
    receiver = RTLFMReceiver(config, FakeSink(), on_state=lambda state, _error: states.append(state))
    await receiver.start()
    await asyncio.wait_for(child_waiting.wait(), timeout=1)

    assert receiver.state == 'running'
    assert receiver._device_owners[config.device_index] is receiver  # noqa: SLF001
    allow_reap.set()
    await asyncio.wait_for(receiver.wait(), timeout=1)

    assert receiver.state == 'stopped'
    assert states[-1] == 'stopped'
    assert config.device_index not in receiver._device_owners  # noqa: SLF001


@pytest.mark.asyncio
async def test_cancelling_start_waits_for_spawn_and_terminates_child(monkeypatch):
    spawn_gate = asyncio.Event()
    spawn_started = asyncio.Event()
    process = FakeProcess()

    async def create_subprocess_exec(*_args, **_kwargs):
        spawn_started.set()
        await spawn_gate.wait()
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create_subprocess_exec)
    receiver = RTLFMReceiver(SDRConfig(123_456_789, 12), FakeSink())
    startup = asyncio.create_task(receiver.start())
    await asyncio.wait_for(spawn_started.wait(), timeout=1)
    startup.cancel()
    spawn_gate.set()
    with pytest.raises(asyncio.CancelledError):
        await startup

    assert process.returncode == -15
    assert receiver.state == 'stopped'


@pytest.mark.asyncio
async def test_stop_requested_during_spawn_reaps_child_before_stopped(monkeypatch):
    spawn_started = asyncio.Event()
    allow_spawn = asyncio.Event()
    stop_requested = asyncio.Event()
    states = []

    class DelayedProcess(FakeProcess):
        def __init__(self):
            super().__init__()
            self.wait_calls = 0

        async def wait(self):
            self.wait_calls += 1
            return await super().wait()

    process = DelayedProcess()

    async def create_subprocess_exec(*_args, **_kwargs):
        spawn_started.set()
        await allow_spawn.wait()
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create_subprocess_exec)
    config = SDRConfig(123_456_789, 12)
    sink = FakeSink()

    def observe_state(state, _error):
        states.append(state)
        if state == 'stopping':
            stop_requested.set()

    receiver = RTLFMReceiver(config, sink, on_state=observe_state)

    start_task = asyncio.create_task(receiver.start())
    await asyncio.wait_for(spawn_started.wait(), timeout=1)
    assert receiver.state == 'starting'

    stop_task = asyncio.create_task(receiver.stop())
    stop_signal_task = asyncio.create_task(stop_requested.wait())
    try:
        done, _ = await asyncio.wait_for(
            asyncio.wait({stop_task, stop_signal_task}, return_when=asyncio.FIRST_COMPLETED), timeout=1
        )
        assert stop_signal_task in done
        assert receiver.state == 'stopping'
        assert not stop_task.done()

        allow_spawn.set()
        await asyncio.wait_for(asyncio.gather(start_task, stop_task), timeout=1)

        assert process.returncode == -15
        assert process.wait_calls >= 1
        assert receiver.state == 'stopped'
        assert 'running' not in states
        # A pending startup Stop must complete the pump and release arbitration.
        assert receiver._task is None or receiver._task.done()  # noqa: SLF001
        assert config.device_index not in receiver._device_owners  # noqa: SLF001
    finally:
        allow_spawn.set()
        if not stop_signal_task.done():
            stop_signal_task.cancel()
        await asyncio.gather(start_task, stop_task, return_exceptions=True)
        await asyncio.gather(stop_signal_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_spawn_failure_during_pending_stop_releases_device(monkeypatch):
    spawn_started = asyncio.Event()
    allow_failure = asyncio.Event()
    stop_requested = asyncio.Event()

    async def create_subprocess_exec(*_args, **_kwargs):
        spawn_started.set()
        await allow_failure.wait()
        message = 'rtl_fm unavailable'
        raise FileNotFoundError(message)

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create_subprocess_exec)
    config = SDRConfig(123_456_789, 12)
    receiver = RTLFMReceiver(
        config,
        FakeSink(),
        on_state=lambda state, _error: stop_requested.set() if state == 'stopping' else None,
    )
    start_task = asyncio.create_task(receiver.start())
    await asyncio.wait_for(spawn_started.wait(), timeout=1)

    stop_task = asyncio.create_task(receiver.stop())
    stop_signal_task = asyncio.create_task(stop_requested.wait())
    try:
        done, _ = await asyncio.wait_for(
            asyncio.wait({stop_task, stop_signal_task}, return_when=asyncio.FIRST_COMPLETED), timeout=1
        )
        assert stop_signal_task in done
        allow_failure.set()
        results = await asyncio.wait_for(asyncio.gather(start_task, stop_task, return_exceptions=True), timeout=1)

        assert isinstance(results[0], SdrError)
        assert results[1] is None
        assert receiver.state == 'error'
        # Failed startup must leave no internal device reservation.
        assert config.device_index not in receiver._device_owners  # noqa: SLF001
    finally:
        allow_failure.set()
        if not stop_signal_task.done():
            stop_signal_task.cancel()
        await asyncio.gather(start_task, stop_task, return_exceptions=True)
        await asyncio.gather(stop_signal_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_unexpected_nonzero_exit_propagates_stderr(monkeypatch):
    process = FakeProcess(stderr_data=b'synthetic decoder input error', returncode=7)

    async def create_subprocess_exec(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create_subprocess_exec)
    receiver = RTLFMReceiver(SDRConfig(123_456_789, 12), FakeSink())
    await receiver.start()
    await asyncio.wait_for(receiver.wait(), timeout=1)

    assert receiver.state == 'error'
    assert 'synthetic decoder input error' in receiver.error
    assert receiver.process.returncode == 7


@pytest.mark.asyncio
async def test_clean_eof_releases_device_for_restart(monkeypatch):
    processes = [FakeProcess(returncode=0), FakeProcess(returncode=0)]

    async def create_subprocess_exec(*_args, **_kwargs):
        return processes.pop(0)

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create_subprocess_exec)
    receiver = RTLFMReceiver(SDRConfig(123_456_789, 12), FakeSink())

    await receiver.start()
    await asyncio.wait_for(receiver.wait(), timeout=1)
    assert receiver.state == 'stopped'
    await receiver.start()
    await asyncio.wait_for(receiver.wait(), timeout=1)
    assert receiver.state == 'stopped'


@pytest.mark.asyncio
async def test_stdout_eof_terminates_a_child_that_is_still_running(monkeypatch):
    process = FakeProcess()
    process.stdout.feed_eof()

    async def create_subprocess_exec(*_args, **_kwargs):
        return process

    monkeypatch.setattr(asyncio, 'create_subprocess_exec', create_subprocess_exec)
    receiver = RTLFMReceiver(SDRConfig(123_456_789, 12), FakeSink(), termination_timeout=0.01)
    await receiver.start()
    await asyncio.wait_for(receiver.wait(), timeout=0.05)

    assert process.returncode == -15
    assert receiver.state == 'stopped'
