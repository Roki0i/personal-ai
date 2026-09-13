"""Run blocking provider/tool work with a cancellable process boundary."""
import multiprocessing
from typing import Callable


class OperationError(Exception):
    pass


def _worker(connection, function, args):
    try:
        connection.send((True, function(*args)))
    except Exception:
        # Exception messages can contain file contents or credentials.
        connection.send((False, "worker_failed"))
    finally:
        connection.close()


def run_bounded(function: Callable, args: tuple, timeout: float):
    if timeout <= 0:
        raise OperationError("timeout")
    ctx = multiprocessing.get_context("spawn")
    receiver, sender = ctx.Pipe(duplex=False)
    process = ctx.Process(target=_worker, args=(sender, function, args))
    process.daemon = True
    started = False
    try:
        process.start()
        started = True
        sender.close()
        if not receiver.poll(timeout):
            raise OperationError("timeout")
        try:
            ok, value = receiver.recv()
        except EOFError:
            raise OperationError("worker_failed") from None
        if not ok:
            raise OperationError(value)
        return value
    except (OSError, ValueError, TypeError) as exc:
        raise OperationError("worker_unavailable") from exc
    finally:
        sender.close()
        receiver.close()
        if started:
            if process.is_alive():
                process.terminate()
            process.join(0.5)
            if process.is_alive():
                process.kill()
                process.join(0.5)
            process.close()
