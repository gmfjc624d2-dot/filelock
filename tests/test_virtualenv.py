from __future__ import annotations

import logging
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest
from virtualenv import cli_run

from filelock import FileLock, SoftFileLock

if TYPE_CHECKING:
    from pathlib import Path

    from filelock import BaseFileLock

_SKIP_ON_PYPY = pytest.mark.skipif(
    hasattr(sys, "pypy_version_info"),
    reason="PyPy segfaults in SQLite Cursor.__del__._reset when GC runs during virtualenv imports",
)


def _create_venv(dest: Path, *extra: str) -> None:
    # With its default setup_logging=True the in-process CLI swaps every root handler, pytest's capture handlers among
    # them, for its own, drops the root level to NOTSET and raises distlib's, for the rest of the session. Every later
    # filelock DEBUG call then becomes a captured record, which slowed the threaded lock tests several-fold.
    cli_run([str(dest), "--no-pip", "--no-setuptools", "--no-periodic-update", *extra], setup_logging=False)


@_SKIP_ON_PYPY
def test_virtualenv(tmp_path: Path) -> None:
    _create_venv(tmp_path)


def _logging_state() -> tuple[int, list[logging.Handler], int, bool]:
    root, filelock_logger = logging.getLogger(), logging.getLogger("filelock")
    return (
        root.level,
        list(root.handlers),
        logging.getLogger("distlib").level,
        filelock_logger.isEnabledFor(logging.DEBUG),
    )


@_SKIP_ON_PYPY
@pytest.mark.parametrize("lock_type", [FileLock, SoftFileLock])
@pytest.mark.parametrize("missing_python", [pytest.param(False, id="completes"), pytest.param(True, id="fails")])
def test_virtualenv_leaves_process_logging_as_found(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, lock_type: type[BaseFileLock], missing_python: bool
) -> None:
    before = _logging_state()
    assert caplog.handler in before[1]  # pytest's own capture handler is among the handlers that must survive
    if missing_python:  # fails during interpreter discovery, after the point where virtualenv would set up logging
        with pytest.raises(RuntimeError, match="failed to find interpreter"):
            _create_venv(tmp_path / "venv", "--python", str(tmp_path / "missing-python"))
    else:
        _create_venv(tmp_path / "venv")
    assert _logging_state() == before

    # A concurrent filelock workload afterwards logs DEBUG records only if DEBUG was already enabled beforehand.
    caplog.clear()
    lock = lock_type(tmp_path / "a.lock")

    def work() -> None:
        for _ in range(5):
            with lock:
                assert lock.is_locked

    with ThreadPoolExecutor(max_workers=2) as pool:
        for future in [pool.submit(work) for _ in range(2)]:
            future.result()
    debug = [record for record in caplog.records if record.name == "filelock" and record.levelno == logging.DEBUG]
    assert bool(debug) is before[3]
