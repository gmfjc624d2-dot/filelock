# filelock Cloud investigation: closure record

**Closed in Cloud on 2026-10-16, by direction.** This record exists because the session's scratch logs are not durable;
it is the lasting summary of what was done and what was found.

- **Repository**: `gmfjc624d2-dot/filelock`, a public fork of `tox-dev/filelock` (MIT).
- **Upstream base**: `e2e7d70`, tag 4.2.0.
- **Production code**: `src/filelock` was never modified. Every change is a test, a `tasks/` probe, or a note.
- **Three separate statuses**: public-source validation (Cloud, Linux x86_64), macOS qualification, and adoption into a
  private system are different things. Nothing here has been qualified on macOS by Cloud or adopted anywhere.

## Increments

Each commit's parent is the row above it, so `claude/macos-qualification-handoff` (`d0bf423`) carries everything.

| Commit    | Branch head                                   | Change                                                                                                       | Status                                                                                                                           |
| --------- | --------------------------------------------- | ------------------------------------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------- |
| `68da493` | (in later branches)                           | `cloud-smoke/filelock-locking.md`: ownership, timeouts, and a non-cooperating writer modifying a locked file | OBSERVED (Linux)                                                                                                                 |
| `3b48317` | `claude/sharp-cannon-n7ik8g`                  | Regression test: an unlinked holder's release spares the replacement's lock                                  | VALIDATED (Linux). User-reported macOS run, Python 3.12.14: the new test passes; `tests/test_filelock.py` 382 passed, 32 skipped |
| `756bce0` | (in later branches)                           | Injected unlock failures are scoped to the target descriptor; deterministic interleaving test                | VALIDATED (Linux)                                                                                                                |
| `62a05c8` | `claude/harden-unlock-flock-mocks`            | The unrelated-close path is exercised, with `EBADF` proof that the descriptor closed                         | VALIDATED (Linux)                                                                                                                |
| `602c162` | `claude/join-threaded-workers`                | A threaded test joins every started worker when one fails                                                    | VALIDATED (Linux)                                                                                                                |
| `91a2f76` | `claude/isolate-virtualenv-logging`           | `test_virtualenv` calls `cli_run(..., setup_logging=False)`, plus a regression test                          | CORRECTED, VALIDATED (Linux)                                                                                                     |
| `89f018b` | (in `43a791f`)                                | `read-only-file` capability probe; uid gates replaced by it; mock test for the sweep's `OSError` branch      | CORRECTED, VALIDATED (Linux)                                                                                                     |
| `43a791f` | `claude/capability-aware-permission-coverage` | Evidence note for `89f018b`                                                                                  | record                                                                                                                           |
| `d0bf423` | `claude/macos-qualification-handoff`          | `cloud-smoke/macos/`: two-host probe and macOS qualification procedure                                       | VALIDATED (Linux logic); macOS UNTESTED                                                                                          |

## Validation environment and outcome

- **Environment**: `tox run -e 3.13` (tox 4.65.6). The `.pkg` env was built by uv-managed CPython 3.15.0b4, a beta, and
  the tests ran on CPython 3.13.16. Linux x86_64, uid 0, full capabilities.
- **Final result (attempt 4, `89f018b`)**: 2382 passed, 96 skipped, 0 failed. Global branch coverage 100.00% (17349
  statements, 1670 branches). Diff coverage 100% (194 lines). tox exit 0.

## Negative results (kept, not overwritten)

| Run                | Commit                 | Outcome                                                                                                                                                                                                                                      |
| ------------------ | ---------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Attempt 1          | `602c162`              | tox internal error `KeyError: 'skip_missing_interpreters'` before any test: the `.pkg` env needs Python 3.15, which was absent                                                                                                               |
| Attempt 2          | `602c162`              | 2369 passed, 96 skipped, **2 failed**: `test_threaded_shared_lock_obj[UnixFileLock]` and `[SoftFileLock]` timed out at 120 s. Coverage and diff-cover were not evaluated                                                                     |
| Logging A/B/D      | `602c162`              | Same threaded test at 105.39 s after `test_virtualenv` (root logger NOTSET, filelock DEBUG on), 21.66 s when run before it, and 21.21 s with logging restored in between. Cause: virtualenv's in-process `cli_run` reconfigures root logging |
| Fix check          | `91a2f76`              | Without the fix, the Soft variant timed out (122.27 s); with it, it took 60.24 s. Reverting the fix fails 4 of 4 new regression cases                                                                                                        |
| Attempt 3          | `91a2f76`              | All tests passed (2375 passed, 96 skipped, 0 failed), but coverage was **99.82%**, 34 lines missed. All of them sat behind tests skipped as root, plus `_read_write.py:1059-1060`                                                            |
| Probe self test v1 | `d0bf423` (pre-commit) | "soft killed vs soft" failed on ext4 and tmpfs. Cause: the harness, which reaped the killed holder late. Fixed before commit                                                                                                                 |

## Findings that transfer beyond filelock

1. **Gate tests on a probed capability, not on identity.** uid 0 without `CAP_DAC_OVERRIDE` is refused like any user. A
   read-denial probe is not a write-denial probe: Windows enforces the read-only attribute on writers but not readers.
1. **In-process tools can leak process-wide state into later tests.** `virtualenv.cli_run` replaced pytest's logging
   handlers, which slowed unrelated threaded tests several-fold.
1. **One host's clock cannot verify exclusion between hosts.** Use a handshake in which the holder keeps the lock until
   it reads the contender's verdict.
1. **Mixing lock contracts on one path breaks exclusion.** A soft contender reclaimed a live native holder's empty file
   after 2.07 s (three of three runs). filelock's silent `ENOSYS` fallback can create that mix; `fallback_to_soft=False`
   fails closed instead.
1. **PID-based liveness counts an unreaped zombie as alive.** Recovery after a crash therefore depends on the supervisor
   reaping the dead holder.
1. **A lock orders access and nothing more.** It gives no atomicity, durability or rollback, and lock records are not
   fsynced.

## Handed off, not done

- **On macOS:** everything in `cloud-smoke/macos/README.md` (APFS, HFS+, exFAT and FAT volumes, real external and SMB
  storage, two Macs, host loss). This belongs to an authorized local continuation with the hardware.
- **Also not run:** Windows; the tox envs 3.14, 3.15, 3.14t and 3.15t; the `type` env with its full dependency set; a
  non-beta 3.15 build interpreter.
- **Logs:** large logs, JUnit files, coverage data and checksums stayed in the session's scratch area, outside Git. They
  are not durable.
