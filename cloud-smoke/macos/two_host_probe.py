"""
Probe whether a filelock lock excludes a peer on another machine, and whether it frees up when the holder dies.

``tasks/verify_filesystem.py`` compares hold intervals on one host's monotonic clock, so it cannot judge two machines
sharing a mount. This probe needs no shared clock. The holder takes the lock and publishes a ``held`` marker. The
contender waits for that marker, tries to acquire for a fixed window, and publishes its verdict. Only after the holder
sees the verdict does it release, or kill itself with SIGKILL. Any acquisition inside the window therefore overlapped a
live holder. Slow marker visibility on a network mount only lengthens the run; it cannot change a verdict.

Run both roles against one directory on the shared filesystem, one per machine, with the same ``--run`` and ``--lock``.
Start the holder first::

    python two_host_probe.py hold    /Volumes/share/probe --run r1 [--crash]
    python two_host_probe.py contend /Volumes/share/probe --run r1

``selftest`` runs every case as two processes on this host. One case is a mixed-contract control that must not exclude.
The self test checks the probe and the target directory's single-host contracts; it says nothing about cross-host
behavior. Each role prints one JSON line. Use a dedicated, empty directory and synthetic files only. A killed strict
holder leaves its claim behind by design.
"""

# ruff: file-ignore[implicit-namespace-package]  # a standalone script beside its notes, run by path, never imported

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import signal
import socket
import subprocess  # ruff:ignore[suspicious-subprocess-import]  # runs this script and the system mount table only
import sys
import time
import uuid
import warnings
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING, Final

import filelock
from filelock import FileLock, SoftFileLock, StrictSoftFileLock, Timeout

if TYPE_CHECKING:
    from filelock import BaseFileLock

_LOCKS: Final[dict[str, type[BaseFileLock]]] = {
    "filelock": FileLock,
    "soft": SoftFileLock,
    "strict": StrictSoftFileLock,
}
_POLL_SECONDS: Final[float] = 0.05
_SELFTEST_WAIT: Final[float] = 60.0
_SELFTEST_RECOVER: Final[float] = 15.0
_DARWIN_MOUNT: Final = re.compile(r"^(?P<source>.+?) on (?P<point>/.*) \((?P<kind>[^,)]+)", re.MULTILINE)

#: Holder lock, contender lock, whether the holder dies holding, the verdict and the recovery the contract implies for
#: two processes on one host. None means recovery is not measured.
_SELFTEST: Final[tuple[tuple[str, str, bool, str, bool | None], ...]] = (
    ("filelock", "filelock", False, "EXCLUDED", True),
    ("filelock", "filelock", True, "EXCLUDED", True),  # the kernel drops a dead process's flock
    ("soft", "soft", False, "EXCLUDED", True),
    ("soft", "soft", True, "EXCLUDED", True),  # one host proves the recorded PID gone once it is reaped
    ("strict", "strict", False, "EXCLUDED", True),
    ("strict", "strict", True, "EXCLUDED", False),  # fail-closed: only force_break reclaims a dead claim
    ("filelock", "soft", False, "NOT EXCLUDED", None),  # mixed contracts: soft reclaims the native holder's empty file
)


def main(argv: list[str] | None = None) -> int:
    """Run one role, or the self test, and return the exit code: 0 as expected, 1 not excluded, 2 inconclusive."""
    args: Final = _parser().parse_args(argv)
    directory: Final[Path] = args.directory
    directory.mkdir(parents=True, exist_ok=True)
    if args.role == "selftest":
        return _selftest(directory, args.window)
    report, code = (_hold if args.role == "hold" else _contend)(args)
    _emit(report)
    return code


def _parser() -> argparse.ArgumentParser:
    parser: Final = argparse.ArgumentParser(description=(__doc__ or "").strip().split("\n\n", 1)[0])
    roles: Final = parser.add_subparsers(dest="role", required=True)
    hold: Final = roles.add_parser("hold", help="take the lock and keep it until the contender reports")
    contend: Final = roles.add_parser("contend", help="try the held lock, then time how long it takes to free up")
    selftest: Final = roles.add_parser("selftest", help="run every case as two processes on this host")
    for role in (hold, contend, selftest):
        role.add_argument("directory", type=Path, help="a dedicated directory on the filesystem under test")
    for role in (hold, contend):
        role.add_argument("--run", required=True, help="the same identifier for both roles")
        role.add_argument("--lock", choices=sorted(_LOCKS), default="filelock", help="lock type (default filelock)")
        role.add_argument("--max-wait", type=float, default=300.0, help="seconds to wait for the peer (default 300)")
    hold.add_argument("--crash", action="store_true", help="die by SIGKILL while holding instead of releasing")
    for role in (contend, selftest):
        # Longer than the 2 s a soft lock watches an unparsable marker before reclaiming it, so the mixed control shows.
        role.add_argument("--window", type=float, default=5.0, help="seconds to try the held lock (default 5)")
    contend.add_argument("--recover", type=float, default=15.0, help="seconds to wait for it afterwards (default 15)")
    return parser


def _hold(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    directory: Final[Path] = args.directory
    report: dict[str, object] = {"role": "hold", "run": args.run, "lock": args.lock, **_environment(directory)}
    lock: Final = _LOCKS[args.lock](str(directory / f"{args.run}.lock"))
    try:
        report |= _acquire(lock, args.max_wait)
    except OSError as error:  # Timeout included: the holder never took the lock
        report["error"] = _describe(error)
        _publish(directory / f"{args.run}.held", report)
        return report, 2
    _publish(directory / f"{args.run}.held", report)
    report["verdict_seen"] = _await(directory / f"{args.run}.tried", args.max_wait) is not None
    if args.crash:
        report["ending"] = "SIGKILL while holding"
        _emit(report)
        os.kill(os.getpid(), signal.SIGKILL)
    lock.release()
    report["ending"] = "released"
    return report, 0 if report["verdict_seen"] else 2


def _contend(args: argparse.Namespace) -> tuple[dict[str, object], int]:
    directory: Final[Path] = args.directory
    report: dict[str, object] = {"role": "contend", "run": args.run, "lock": args.lock, "window_s": args.window}
    report |= _environment(directory)
    if (held := _await(directory / f"{args.run}.held", args.max_wait)) is None or "error" in held:
        reason = "no holder marker within --max-wait" if held is None else "the holder could not take the lock"
        return report | {"holder": held, "verdict": "INCONCLUSIVE", "reason": reason}, 2
    report["holder"] = held
    lock: Final = _LOCKS[args.lock](str(directory / f"{args.run}.lock"))
    started: Final = time.monotonic()
    try:
        report |= _acquire(lock, args.window)
    except Timeout:
        report["verdict"] = "EXCLUDED"
    except OSError as error:
        report |= {"verdict": "INCONCLUSIVE", "reason": _describe(error)}
    else:
        # The holder keeps its lock until it reads our verdict, so this acquisition overlapped it.
        lock.release()
        report |= {"verdict": "NOT EXCLUDED", "acquired_after_s": round(time.monotonic() - started, 3)}
    _publish(directory / f"{args.run}.tried", {"verdict": report["verdict"]})
    if report["verdict"] == "EXCLUDED":
        report["recovered_after_s"] = _recover(lock, args.recover)
    # A FileLock that met ENOSYS has turned itself into a SoftFileLock; the class names what actually contended.
    report["backend"] = type(lock).__name__
    return report, {"EXCLUDED": 0, "NOT EXCLUDED": 1}.get(str(report["verdict"]), 2)


def _acquire(lock: BaseFileLock, timeout: float) -> dict[str, object]:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        lock.acquire(timeout=timeout)
    return {"backend": type(lock).__name__, "warnings": [str(warning.message) for warning in caught]}


def _recover(lock: BaseFileLock, recover: float) -> float | str | None:
    # The holder releases, or dies, once it reads the verdict; None means the lock stayed taken for the whole wait.
    started: Final = time.monotonic()
    try:
        lock.acquire(timeout=recover)
    except Timeout:
        return None
    except OSError as error:
        return _describe(error)
    lock.release()
    return round(time.monotonic() - started, 3)


def _selftest(directory: Path, window: float) -> int:
    _emit({"role": "selftest", **_environment(directory)})
    script: Final = [sys.executable, str(Path(__file__).resolve())]
    failures = 0
    for holder_lock, contender_lock, crash, expected, recovers in _SELFTEST:
        run = f"selftest-{uuid.uuid4().hex[:12]}"
        target = [str(directory), "--run", run, "--max-wait", str(_SELFTEST_WAIT)]
        hold = [*script, "hold", *target, "--lock", holder_lock, *(["--crash"] if crash else [])]
        contend = [*script, "contend", *target, "--lock", contender_lock, "--window", str(window)]
        with (
            subprocess.Popen(hold, stdout=subprocess.PIPE, text=True) as holder,  # ruff:ignore[subprocess-without-shell-equals-true]  # this script
            ThreadPoolExecutor(max_workers=1) as reaper,
        ):
            # Reap the holder the moment it exits. A killed holder left unreaped keeps its PID and start time, so a soft
            # lock rightly treats the marker's owner as alive and never reclaims it.
            holder_output = reaper.submit(holder.communicate, timeout=_SELFTEST_WAIT + _SELFTEST_RECOVER)
            contender = subprocess.run(  # ruff:ignore[subprocess-without-shell-equals-true]  # this script
                [*contend, "--recover", str(_SELFTEST_RECOVER)], capture_output=True, text=True, check=False
            )
            held = _last_json(holder_output.result()[0])
        tried = _last_json(contender.stdout)
        recovered = tried.get("recovered_after_s")
        ok = tried.get("verdict") == expected and (recovers is None or isinstance(recovered, float) is recovers)
        failures += not ok
        case = f"{holder_lock}{' killed' if crash else ''} vs {contender_lock}"
        backends = f"{held.get('backend')} vs {tried.get('backend')}"
        _write(
            f"{'PASS' if ok else 'FAIL'}  {case:24} {backends:30} verdict={tried.get('verdict')} "
            f"(expected {expected}) recovered_after_s={recovered}"
        )
    return 1 if failures else 0


def _environment(directory: Path) -> dict[str, object]:
    return {
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "filelock": filelock.__version__,
        "directory": str(directory.resolve()),
        "filesystem": _filesystem(directory),
    }


def _filesystem(directory: Path) -> str | None:
    # Best effort: the mount whose mount point is the longest prefix of the directory's real path.
    target: Final = os.path.realpath(directory)
    mounts: list[tuple[str, str, str]] = []
    with suppress(OSError):
        if sys.platform == "linux":
            for line in Path("/proc/self/mounts").read_text(encoding="utf-8").splitlines():
                source, point, kind = (_unescape(field) for field in line.split()[:3])
                mounts.append((point, kind, source))
        else:
            table = subprocess.run(["/sbin/mount"], capture_output=True, text=True, check=False).stdout
            mounts.extend((found["point"], found["kind"], found["source"]) for found in _DARWIN_MOUNT.finditer(table))
    inside: Final = [mount for mount in mounts if target == mount[0] or target.startswith(mount[0].rstrip("/") + "/")]
    if not inside:
        return None
    point, kind, source = max(inside, key=lambda mount: len(mount[0]))
    return f"{kind} {source} on {point}"


def _unescape(field: str) -> str:
    # /proc/self/mounts writes a space in a path as \040.
    return re.sub(r"\\([0-7]{3})", lambda escape: chr(int(escape[1], 8)), field)


def _publish(path: Path, payload: dict[str, object]) -> None:
    # Write aside and rename, so a peer polling for the marker never reads half of it.
    staging: Final = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    staging.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    staging.replace(path)


def _await(path: Path, max_wait: float) -> dict[str, object] | None:
    deadline: Final = time.monotonic() + max_wait
    while True:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            if time.monotonic() >= deadline:
                return None
            time.sleep(_POLL_SECONDS)


def _last_json(output: str) -> dict[str, object]:
    lines: Final = [line for line in output.splitlines() if line.startswith("{")]
    return json.loads(lines[-1]) if lines else {}


def _describe(error: BaseException) -> str:
    return f"{type(error).__name__}: {error}"


def _emit(report: dict[str, object]) -> None:
    _write(json.dumps(report, sort_keys=True))


def _write(line: str) -> None:
    sys.stdout.write(f"{line}\n")
    sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
