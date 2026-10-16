# macOS qualification handoff for filelock

The work in this directory is public only, uses synthetic files, and lives on the fork branch
`claude/macos-qualification-handoff`. That branch builds on `43a791f` (the capability-aware coverage evidence) on top of
upstream `e2e7d70` (Release 4.2.0). Upstream's `src/filelock` is unchanged.

**Nothing here has run on macOS.** Each statement below is labelled:

- **A**: observed in Cloud on Linux x86_64.
- **B**: prepared for macOS but not yet run there.
- **C**: observed on a Mac. There are no C entries yet.
- **Doc/Code**: what filelock states in its own docs or source. These are claims, not observations.

## What a macOS consumer can rely on

| Property                             | What filelock promises (source)                                                                                                                                                                                                                                                                                                      | Evidence here                                                                                                                   |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------- |
| Process exclusion                    | `FileLock` = `UnixFileLock`, `flock` on an open file description; excludes cooperating lockers (`docs/concepts.rst`, "Native locks are advisory")                                                                                                                                                                                    | A: self test on ext4 and tmpfs. Upstream CI runs on `macos-15` (upstream's evidence, not observed here)                         |
| Uncooperative writers                | Not prevented. Every lock is advisory; a process that writes without locking is not stopped (`concepts.rst`, "What filelock doesn't protect against")                                                                                                                                                                                | A: `../filelock-locking.md`                                                                                                     |
| Ownership, reentrancy                | Per lock object and thread (`thread_local=True` default) with a hold counter. Self-deadlock detection and `is_singleton` key on the path's *spelling*: on case-insensitive APFS, `Work.lock`/`work.lock` or NFC/NFD names are one file but two locks, and a second acquire waits forever (`concepts.rst`, "Self-deadlock detection") | Doc. B: spell lock paths one canonical way                                                                                      |
| Garbage collection                   | Collecting an unreferenced lock object releases its holds (`concepts.rst`, thread-local section)                                                                                                                                                                                                                                     | A: a scratch holder that kept no reference lost its lock before it could be killed. Keep a reference for the whole hold         |
| Timeouts, cancellation               | `timeout`, `blocking`, `cancel_check`; an async cancellation rolls a half-done acquire back, and a cancelled *holder* must still release (`concepts.rst`, "Task cancellation")                                                                                                                                                       | Doc                                                                                                                             |
| Holder killed (native)               | The kernel releases `flock` when the process exits                                                                                                                                                                                                                                                                                   | A: available again after 0.05 s                                                                                                 |
| Holder killed (`SoftFileLock`)       | The marker stays and is reclaimed only once the owner is *provably* gone. A marker from another host is never reclaimed, unless the deprecated age-based `lifetime` is set (`src/filelock/_identity.py`, `owner_is_stale`)                                                                                                           | A: same host, reclaimed 0.10 s after the dead holder is reaped; never while it is an unreaped zombie. Cross-host: Code only, B  |
| Holder killed (`StrictSoftFileLock`) | Never reclaimed automatically; an operator calls `force_break` (fail-closed)                                                                                                                                                                                                                                                         | A: still taken after 15 s, as designed                                                                                          |
| Leases, fencing                      | `SoftFileLease` permits overlap by design. Only a resource that rejects a stale generation (`SoftReadWriteLock.generation`) fences a paused holder                                                                                                                                                                                   | Doc                                                                                                                             |
| Integrity of the protected data      | None. A lock orders access; it gives no atomic write, durability or rollback. Lock records skip `fsync` (`src/filelock/_util.py`, `write_all`)                                                                                                                                                                                       | Doc/Code. The consumer needs its own write-temp, `fsync`, rename and recovery                                                   |
| Network filesystems                  | SMB "Verified in CI" and NFS rows (`concepts.rst`, "Filesystem support matrix")                                                                                                                                                                                                                                                      | Code: those CI lanes use the **Linux** kernel NFS and CIFS clients (`.github/workflows/check.yaml`). macOS `smbfs`: untested, B |
| `ReadWriteLock` on macOS             | Never uses `/dev/fd` (kernel panic, issue #818); links a private hard link beside the database and raises if it cannot (`docs/how-to.rst`, "On macOS ... must support hard links"; `src/filelock/_read_write.py`)                                                                                                                    | Code. B: expected to raise on exFAT and FAT volumes                                                                             |

## Findings from this session

1. **Mixed contracts overlap after about 2 s** (A, ext4 and tmpfs, three of three runs at 2.07 s):
   - **Observed:** a native `FileLock` holder leaves an empty lock file, and a `SoftFileLock` contender on the same path
     reclaims it as a malformed marker once it has stayed unchanged for 2 s, while the holder still holds it.
   - **Why it matters across machines:** `UnixFileLock` silently becomes a `SoftFileLock` (with only a warning) when
     `flock` returns `ENOSYS`. Hosts whose clients differ in `flock` support can therefore mix contracts on one share.
   - **Mitigation:** construct `UnixFileLock(path, fallback_to_soft=False)` on every host. Check that each host's probe
     reports the backend you asked for.
1. **Soft-lock recovery waits for the dead holder to be reaped** (A). A SIGKILLed `SoftFileLock` holder that its parent
   has not yet `wait()`ed for keeps its PID and start time. A contender rightly treats it as alive, still blocked after
   5 s; once the holder is reaped, it reclaims the marker in 0.05 s. Recovery therefore depends on the supervisor
   (launchd, a Python parent) reaping promptly. This is conservative behaviour, not a defect. macOS: B.
1. **`tasks/verify_filesystem.py` cannot judge two machines** (Code). It compares hold intervals on one host's monotonic
   clock. Its multi-client mode needs one host to mount a share twice with independent caches (Linux `nosharecache`). A
   single Mac with one SMB mount is one client cache, so its PASS says nothing about exclusion between Macs.
   `two_host_probe.py` fills that gap without any shared clock.

## Tools

- `tasks/verify_filesystem.py` (upstream): 4 processes × 100 holds per lock type on one host.
- `two_host_probe.py` (here):
  - `hold` and `contend` roles, one per machine, with the same `--run` and `--lock`.
  - Lock types: `filelock`, `soft`, `strict`.
  - Holder option `--crash` (SIGKILL while holding).
  - `selftest` runs 7 single-host cases, including the mixed-contract control, and exits non-zero on any mismatch.
  - Each role prints one JSON line with host, OS, machine, Python, filelock version, the backend actually used, and the
    filesystem (best effort; a firmlinked path such as `/Users/...` reports the `/` volume).
  - Contender exit code: 0 `EXCLUDED`, 1 `NOT EXCLUDED`, 2 `INCONCLUSIVE`.
  - A, Linux: ruff and ty clean; `selftest` 7/7 on ext4 and tmpfs. The two roles also ran as separate processes, with a
    killed holder and with no holder at all.
- The existing pytest suite, pointed at a volume: `TMPDIR` moves the capability probes and `--basetemp` moves every
  test's files onto it. A, Linux tmpfs: 96 passed, 8 skipped, all files under `--basetemp`.

## Procedure on a Mac (B: prepared, not run)

Use Python 3.11 or newer; macOS's `/usr/bin/python3` is too old. Use only synthetic data and dedicated empty
directories.

What has been checked on Linux (A):

- Every shell block below passes `bash -n`.
- With `hdiutil` and Python stubbed out, the volume loop creates and visits all five targets under the intended names.
- The install line resolves under pip 26.2.1 (`--dry-run`), and the upstream tag ref matches `e2e7d70`.

Nothing else in the procedure has run.

```bash
git clone https://github.com/gmfjc624d2-dot/filelock && cd filelock
git checkout claude/macos-qualification-handoff
git fetch --no-tags https://github.com/tox-dev/filelock.git refs/tags/4.2.0:refs/tags/4.2.0  # versioning needs the tag
python3.13 -m venv .venv && .venv/bin/python -m pip install -U pip && .venv/bin/python -m pip install -e . --group test
{ sw_vers; uname -m; .venv/bin/python -VV; git rev-parse HEAD; mount; } > qualification-env.txt
```

Create one synthetic volume per filesystem. These are real macOS filesystem drivers on disk images, not simulations.
Then run the same three checks on each volume, and on local APFS first as the baseline:

```bash
mkdir -p ~/flq && cd ~/flq
for spec in APFS:FLQ_APFS JHFS+:FLQ_HFS ExFAT:FLQ_EXFAT MS-DOS:FLQ_FAT; do
  fs=${spec%%:*} name=${spec##*:}
  hdiutil create -size 512m -fs "$fs" -volname "$name" -type UDIF "$name.dmg" && hdiutil attach "$name.dmg"
done
cd /path/to/filelock
for V in "$HOME/flq-local" /Volumes/FLQ_APFS /Volumes/FLQ_HFS /Volumes/FLQ_EXFAT /Volumes/FLQ_FAT; do
  mkdir -p "$V/tmp"
  .venv/bin/python tasks/verify_filesystem.py "$V/verify"             > "verify-$(basename "$V").txt" 2>&1
  .venv/bin/python cloud-smoke/macos/two_host_probe.py selftest "$V/probe" > "selftest-$(basename "$V").txt" 2>&1
  TMPDIR="$V/tmp" .venv/bin/python -m pytest tests/test_filelock.py tests/test_soft_stale.py tests/test_strict_soft.py \
    tests/test_read_write.py tests/test_unix_fallback.py --basetemp="$V/pytest" -rs > "pytest-$(basename "$V").txt" 2>&1
done
for V in /Volumes/FLQ_*; do hdiutil detach "$V"; done
```

Run the same loop on a real external drive and on an SMB mount, with `V` set to a new empty directory there. On non-APFS
volumes, skips and failures are data about that filesystem, not regressions. Compare each volume against the local APFS
baseline. Keep volume names short: macOS caps `AF_UNIX` socket paths at 104 bytes, so a failure that names that limit
comes from the long `TMPDIR` path, not from the filesystem.

**Two Macs on one SMB share** (A holds, B contends; repeat for `--lock filelock`, `soft` and `strict`, each with and
without `--crash`, then swap roles):

```bash
# Mac A
.venv/bin/python cloud-smoke/macos/two_host_probe.py hold    /Volumes/share/flq-probe --run ab-filelock-1 --lock filelock
# Mac B, in either order: each role waits up to --max-wait (300 s) for the other's marker
.venv/bin/python cloud-smoke/macos/two_host_probe.py contend /Volumes/share/flq-probe --run ab-filelock-1 --lock filelock
```

**Host loss** is a manual check that differs from a killed process:

1. Run the holder on Mac A with `--max-wait 900`, and the contender on Mac B with `--recover 600`.
1. Once B reports `EXCLUDED`, cut Mac A's network; do not just kill the process.
1. Record whether and when B acquires, which depends on the SMB server's session and durable-handle timeouts.
1. Record what A's process believes after you reconnect it. A holder that still thinks it holds after a partition is why
   fencing matters.

## Reading the results

- **`FileLock` usable across Macs on that share:** only if both directions report `EXCLUDED`, both report
  `"backend": "UnixFileLock"`, and a `--crash` run recovers.
- **`NOT EXCLUDED`:** do not use that lock type between Macs on that share.
- **Backend reads `SoftFileLock` although `filelock` was requested:** the host fell back on `ENOSYS`, so finding 1
  applies.
- **`soft` with `--crash` across hosts:** expected never to recover, because no host can prove a remote PID dead. Plan
  an operator step, or use a lease with fencing.
- **`ReadWriteLock` tests on exFAT or FAT:** expected to fail on the hard-link requirement. A pass would contradict the
  code reading and should be reported.

## Not covered

This handoff does not cover:

- APFS-specific behaviour (snapshots, clones, case and normalization mixes) beyond what the suite exercises.
- Power loss.
- NFS from macOS.
- Mixed macOS and Linux clients on one share, beyond finding 1's mechanism.
- Long-running soak.

Cloud cannot run any macOS filesystem, `hdiutil`, `smbfs`, launchd or a second host.
