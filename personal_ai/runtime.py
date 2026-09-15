"""Run blocking provider/tool work with a cancellable process boundary."""
import multiprocessing
import os
import signal
import time
from contextlib import contextmanager
from contextvars import ContextVar
from threading import Event
from typing import Callable


_scope = ContextVar("execution_scope", default=None)


class CancellationToken:
    def __init__(self):
        self._event = Event()

    def cancel(self):
        self._event.set()

    def is_cancelled(self):
        return self._event.is_set()


@contextmanager
def execution_scope(token, deadline, poll=None):
    marker = _scope.set((token, deadline, poll))
    try:
        yield
    finally:
        _scope.reset(marker)


def cancel_current():
    scope = _scope.get()
    if scope:
        scope[0].cancel()


def check_pending():
    scope = _scope.get()
    if scope:
        token, deadline, poll = scope
        if poll:
            poll()
        if token.is_cancelled():
            raise KeyboardInterrupt
        if time.monotonic() >= deadline:
            raise OperationError("timeout")


class OperationError(Exception):
    pass


def _worker(connection, function, args, process_group=False, ready=None):
    if ready is not None:
        ready.wait()
    if process_group and os.name != 'nt':
        os.setsid()
    try:
        connection.send((True, function(*args)))
    except Exception:
        # Exception messages can contain file contents or credentials.
        connection.send((False, "worker_failed"))
    finally:
        connection.close()


def run_bounded(function: Callable, args: tuple, timeout: float, *, process_group=False):
    check_pending()
    scope = _scope.get()
    if scope:
        timeout = min(timeout, scope[1] - time.monotonic())
    if timeout <= 0:
        raise OperationError("timeout")
    ctx = multiprocessing.get_context("spawn")
    receiver, sender = ctx.Pipe(duplex=False)
    ready = ctx.Event() if os.name == 'nt' and process_group else None
    job = None
    process = ctx.Process(target=_worker, args=(sender, function, args, process_group, ready))
    process.daemon = True
    started = False
    try:
        process.start()
        started = True
        if ready is not None:
            from .windows_job import WindowsJob
            job = WindowsJob(process.pid)
            ready.set()
        sender.close()
        deadline = time.monotonic() + timeout
        while True:
            check_pending()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise OperationError("timeout")
            if receiver.poll(min(0.05, remaining)):
                break
        check_pending()
        try:
            ok, value = receiver.recv()
        except EOFError:
            raise OperationError("worker_failed") from None
        if not ok:
            raise OperationError(value)
        return value
    except KeyboardInterrupt:
        if scope:
            scope[0].cancel()
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise OperationError("worker_unavailable") from exc
    finally:
        sender.close()
        receiver.close()
        if job is not None:
            job.close()
        if started:
            if process_group and os.name != 'nt':
                # Include any fixed-argv local subprocess on cancellation/timeout.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            if process.is_alive():
                process.terminate()
            process.join(0.5)
            if process.is_alive():
                process.kill()
                process.join(0.5)
            process.close()


def local_provider_class(mode='auto'):
    import platform
    from .local import MacOSLocalProvider, MockLocalProvider
    from .windows import WindowsLocalProvider
    if mode == 'auto':
        mode = {'Windows': 'windows', 'Darwin': 'macos'}.get(platform.system(), 'mock')
    choices = {'windows': WindowsLocalProvider, 'macos': MacOSLocalProvider, 'mock': MockLocalProvider}
    if mode not in choices:
        raise ValueError('invalid_local_provider')
    return choices[mode]
