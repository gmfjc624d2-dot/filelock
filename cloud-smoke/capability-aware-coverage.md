# Capability-aware coverage correction (evidence record)

Correction commit `89f018b` on `claude/capability-aware-permission-coverage`, parent `91a2f76`. It depends on the
earlier public branches of this fork, through `claude/isolate-virtualenv-logging`. Upstream base: `e2e7d70` (Release
4.2.0). The change touches tests and `tasks/` only; `src/filelock` is untouched.

## Problem

At 91a2f76, `tox run -e 3.13` as uid 0 passed every test, but coverage reached only 99.82%: 34 lines were missed against
`fail_under = 100`.

- **Test-file lines**: all but two of the missed lines belong to tests that skipped on `os.geteuid() == 0`, or on the
  `file-permissions` capability, while coverage still required their lines.
- **`_read_write.py:1059-1060`**: these are the sweep's `except OSError: return`. Only a test gated on
  `file-permissions` reached them.
- **Ruled out**: a stray uncombined data file held 0 line hits, so it played no part.

## Diagnosis

The uid-gated tests assert that a file without write bits refuses writes. The uid is only a proxy for that. The existing
`file-permissions` probe asks a different question, namely whether a mode-0 file refuses *reads*. With uid 0 held fixed,
`setpriv` shows how the answers come apart:

| root capabilities dropped                 | uid gate | `file-permissions` | write denied |
| ----------------------------------------- | -------- | ------------------ | ------------ |
| none                                      | skip     | False              | False        |
| `CAP_DAC_OVERRIDE`                        | skip     | False              | True         |
| `CAP_DAC_OVERRIDE`, `CAP_DAC_READ_SEARCH` | skip     | True               | True         |

Windows is the other case where the answers split: `chmod` there sets the read-only attribute, which refuses writers but
never readers. These tests ran on Windows under the uid gate.

## Change

- **New probe**: a `read-only-file` capability that writes to a 0o400 file. It is tested like the `file-permissions`
  probe: mocked privileged, EACCES and EPERM outcomes, and I/O errors propagating.
- **Gates**: `test_ro_file`, `test_readonly_lock_file_with_mtime_zero_raises`,
  `test_writability_check_rejects_a_readonly_file` and `test_writability_check_does_not_follow_symlink_to_readonly` are
  gated on `NEEDS_READ_ONLY_FILE` with `# pragma: needs read-only-file`, as are their helpers.
- **Sweep test**: a new mock test fails `os.scandir` on the database's parent only (EACCES and EIO cases), so the
  sweep's `except OSError` runs on every runtime. Other `scandir` calls are delegated; `shutil.rmtree` lists the link
  directory through a descriptor. The permission-based test is kept.
- **Unchanged**: no assertion, workload or timeout changed.

## Validation (Linux container, uid 0)

| gate                                    | 91a2f76                           | 89f018b                                   |
| --------------------------------------- | --------------------------------- | ----------------------------------------- |
| pytest (`tox run -e 3.13`)              | 2375 passed, 96 skipped, 0 failed | 2382 passed, 96 skipped, 0 failed         |
| global coverage, branch, fail_under 100 | 99.82% (34 lines missed)          | 100.00% (17349 statements, 1670 branches) |
| diff-cover vs origin/main               | 100%                              | 100% (194 lines)                          |
| tox exit                                | 1                                 | 0                                         |

- **Skips**: the same 96 tests skip in both runs. Only the 7 converted tests changed skip reason.
- **Installed wheel**: `4.2.1.dev7+g89f018bad`, with `src/filelock` identical to the commit.
- **Focused runs, uid 0, all capabilities**: every converted test skips. Each previously missed line is either excluded
  or executed; `_read_write.py:1059-1060` now executes.
- **Focused runs, uid 0 without both DAC capabilities**: all converted tests run and pass, and none of those lines is
  excluded.
- **Focused runs, uid 0 without `CAP_DAC_OVERRIDE` only**: the write tests run and the `file-permissions` tests skip.
- **Mutation checks**: the sweep test fails when `except OSError` is narrowed to `PermissionError` and when the error
  propagates. Where writes are denied, the symlink test fails when the writability check follows the link (`lstat` →
  `stat`). Under full root it passes either way, which is why it is gated.
- **Static checks**: ruff 0.17.0 (the pre-commit pin) is clean. `ty` reports no new diagnostics.

## Reproduce

```bash
tox run -e 3.13                                   # the gates above
setpriv --inh-caps=-dac_override,-dac_read_search --bounding-set=-dac_override,-dac_read_search -- \
  env PYTHONPATH=tasks .tox/3.13/bin/python -m pytest \
  tests/test_filelock.py::test_ro_file tests/test_util.py::test_writability_check_rejects_a_readonly_file -rs
```

## Not established

- Windows and macOS. The fork runs no CI; the probe's Windows answer follows from CPython's documented `os.chmod`
  semantics there but was not observed.
- The other tox envs: 3.14, 3.15, 3.14t, 3.15t, `type` and `coverage`.
- A non-beta 3.15 build interpreter. The `.pkg` env here used CPython 3.15.0b4.

Full logs, JUnit, coverage data and checksums were kept in the session's scratch area, outside Git.
