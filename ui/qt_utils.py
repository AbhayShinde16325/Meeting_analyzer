"""Small Qt helpers shared by the screens.

The screens start a ``QThread`` per request and ``deleteLater()`` it when it
finishes.  A screen can therefore still hold a Python reference to a worker
whose C++ object has already been destroyed; calling ``isRunning()`` on that
reference raises ``RuntimeError``.  These helpers make the lifecycle checks
safe instead of scattering ``try``/``except`` blocks through every screen.
"""

from __future__ import annotations

from typing import Any, Iterable


def worker_running(worker: Any | None) -> bool:
    """Return True only while ``worker`` exists *and* is still running.

    A deleted (``deleteLater()``-ed) worker counts as not running.
    """

    if worker is None:
        return False
    try:
        return bool(worker.isRunning())
    except RuntimeError:
        # libshiboken: the underlying C++ object is gone.
        return False


def wait_for_workers(workers: Iterable[Any | None], timeout: int = 5000) -> None:
    """Block briefly for each still-running worker to finish."""

    for worker in workers:
        if worker_running(worker):
            worker.wait(timeout)
