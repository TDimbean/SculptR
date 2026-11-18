# Todos

## Active

- [ ] T-019 Docs + final handoff -- refresh after the cleanup finishes
- [ ] T-026 Live-Blender check left open by T-025 -- the two notice panels (Add-on Preferences box,
      Models tab box) cannot be rendered here, so their final text and width are unverified; and the
      removed `hunyuan_license_confirmed` property can leave that key in an existing
      `preferences.prop`, whose warn-and-ignore behaviour needs one live enable/disable cycle to
      confirm. Needs the user to load the add-on in Blender 5.2 and report the two boxes.
- [x] T-021 Repo cleanup -- closed 20:31. Deleted the 8 orphan scratch files (diag.txt, eng-run.txt,
      hub-run.txt, proc-run.txt, test-run.txt, tr-run.txt, scratch_diag.py, scratch_lock.py); emptied
      unsloth-tmp (280 to 0 children) and kept the directory because it is the harness temp redirect;
      added graphify-out/, unsloth-tmp/, scratch_*.py, *-run.txt, diag.txt to .gitignore, git
      check-ignore confirms every pattern matches; graphify-out dropped out of git status.
      - Deliberately NOT done: commit (the chosen scope excluded it) and the addCleanup rmtree fix in
        the 5 test setUps -- both left as proposals.

## Done

- [x] T-027 Crash on Blender quit -- closed 08:52. Blender stopped working on exit and wrote
      `%TEMP%\blender.crash.txt`. Cause `sculptr/bridge.py` `__del__` -> `stop("__del__")` ->
      `_log_event` -> `event` -> `diagnostics.py:111` `datetime.now()`: Blender clears its modules at
      quit, which drops the last reference to the module singleton `_CLIENT`, so `__del__` runs while
      CPython 3.13 is finalizing and its C state is already partly gone. `atexit.register(stop)` had
      already retired the session, so `__del__` was re-running the full shutdown a second time.
      Fix: `BridgeClient` gained `_shutdown_done` + `shutdown_once(reason)`; `__exit__`, `__del__` and
      the module-level `stop()` (the exit hook and `unregister()`) route through it, and `configure()`
      clears the flag when it opens a new session. `stop()` itself still does full work for explicit
      calls and marks the flag before any of it. Reproduced and disproved under Blender 5.2 batch:
      `blender -b --python unsloth-tmp/blender_repro.py` before the fix printed
      `Writing: C:\Users\r00st\AppData\Local\Temp\blender.crash.txt` with
      `Error : EXCEPTION_ACCESS_VIOLATION / Address 0x00007FFCBBD89A76 / Module : python313.dll`,
      backtrace `bridge.py:189 in __del__` -> `bridge.py:577 in stop` -> `bridge.py:981 in _log_event`
      -> `diagnostics.py:104 in event` -> `diagnostics.py:111 in _emit`; after the fix the same script
      exits clean (`Blender quit`) and the crash file keeps its old mtime 08:32:29. Added
      `tests/test_bridge_shutdown.py` (5 cases: once-only shutdown, the destructor still retires a
      session nothing else retired, a re-opened session retires again); suite Ran 66 tests, 0 failed.
      `graphify update .` = 965 nodes / 2584 edges / 47 communities. Installed copy in sync (both
      sha256 A9024AA2A42B...). Details: `.handoff/log/2026-11-18-w7fk.md`.
- [x] T-025 License acceptance removed everywhere — Models-tab checkbox (`ui.py` license box `prop(preferences,
      "hunyuan_license_confirmed")` at 599) and the whole acceptance rule are gone. Deleted the
      `hunyuan_license_confirmed` BoolProperty (`sculptr/properties.py:22`) and both boxes now print only the
      statement "This extension is only a hub for you to download and launch the models. License for their
      use can only be granted by Tencent" plus the two license URLs imported from `catalog.py`; both boxes
      use `icon="INFO"`, no `alert`, since nothing is pending. Gates removed: `ui.py` resume-enabled
      conjunct + `Accept the license below to resume` label + Retry/Download enable flags + `_model_requires_license`
      + the now-dead `_draw_transfer_rows` `preferences` parameter; `operators.py` download refusal, the
      `configure` license argument, the `license_confirmed` submit payload key, `_requires_hunyuan_license`,
      and Resume's re-authorization (now `client.authorize_transfer(self.transfer_id, True)`
      unconditionally for an intent still carrying `license_required` without `license_authorized`);
      `bridge.py` `_license_authorized = True`, both `configure(..., license_authorized = True)` defaults,
      and `settings_from_context` returning `(root, True, keep_awake)` in place of the
      `license_authorized` → `hunyuan_license_authorized` → `hunyuan_license_confirmed` chain;
      `catalog.py` `HUNYUAN_LICENSE_NOTICE` and `ui_helpers.py` 401/403/451/`license_authorization_required`
      copy reworded; `README-2.md` 42/47 corrected. Engine keeps `license_required` /
      `license_authorized` and its refusal paths (`daemon.py:582`, `daemon.py:1120`, `protocol.py:122`)
      as inert protocol invariants — nothing reachable from the UI can be refused. All six files
      copied over the Blender 5.2-loaded copy under `%APPDATA%` after a diff proved the copies were in
      sync beforehand. Verified: `compileall` exit 0, `unittest discover` Ran 61 tests 0 failed, repo-wide
      grep for `hunyuan_license_confirmed` / `_model_requires_license` / `_requires_hunyuan_license` /
      `license_confirmed` = no matches, `Accept |accept the|I accept` across py + md = no matches.
      Details: `.handoff/log/2026-11-18-j2vq.md`.
- [x] T-024 Add-on box license notice — removed the `hunyuan_license_confirmed` checkbox from
      `sculptr/properties.py:35` (`SculptRPreferences.draw`), replaced it with the statement
      "This extension is only a hub for you to download and launch the models. License for their
      use can only be granted by Tencent" (two labels, Blender labels do not wrap) plus both
      license URLs imported from `catalog.py`; mirrored into the Blender 5.2-loaded copy;
      `README-2.md` lines 47/110/440 corrected to match. Suite still 61/0.
      Details: `.handoff/log/2026-11-17-z9kx.md`.
- [x] T-023 Flicker fix — UI banner `I/O operation on closed file.` was `bridge.py:963`
      `_write_heartbeat` writing `"\n"` AFTER the `with` closed the handle (ValueError, not caught
      by `except OSError`), escaping `snapshot()` into `_poll_runtime` (ui.py:118) → `_RUNTIME_ERROR`
      row (ui.py:379). Rate-limited by `BRIDGE_HEARTBEAT_INTERVAL`, hence the flicker. Write moved
      inside the `with`; handler now `(OSError, ValueError)` (telemetry-only, logged); same patch in
      the Blender 5.2-loaded copy under `%APPDATA%`; added `tests/test_bridge_heartbeat.py`; suite 61/0.
      Details: `.handoff/log/2026-11-17-q4zn.md`.
- [x] T-022 Add-on enable fix -- `sculptr/ui.py:3` was `from typing import Mapping` while
      `ui.py:94` annotates `_age_label(epoch: Any)`; annotations run at module import, so
      enabling the extension died with Blender's one-line report `name 'Any' is not defined`.
      Import now `from typing import Any, Mapping`; same patch applied to the installed copy
      Blender 5.2 loads (`%APPDATA%\Blender Foundation\Blender\5.2\extensions\user_default\sculptr`);
      added `tests/test_extension_imports.py` (stubbed bpy, imports all 16 modules) and proved it
      fails on the reverted line; suite 59 tests, 0 failed.
- [x] T-001..T-007, T-010 -- prior sessions, unchanged
- [x] T-011 process.py -- ALIVE/EXITED/UNKNOWN, typed c_void_p handle, WaitForSingleObject,
      ES_SYSTEM_REQUIRED, EngineLock pid byte at seek(0)
- [x] T-012 transfers.py -- journals, identity-bound partials, monotonic durable offsets
- [x] T-013 hub.py -- strict 206 Content-Range, strong-ETag If-Range, 200-after-Range never appends,
      416 never unlinks, one RetryController + no-progress window, free-space gate, fsync-before-offset,
      verify before promote, install marker last
- [x] T-014 daemon.py -- INSTALLED stale-snapshot finish fixed; paused-then-discarded close fixed;
      _pause_active reload; interrupted ids recorded before drain; lock released after workers joined
- [x] T-015 bridge.py -- snapshot observes only; configure/recover with backoff; UNKNOWN process defers;
      intent written before the command file; heartbeat is rate-limited telemetry only
- [x] T-016 ui/operators/ui_helpers -- queued/downloading/retrying/waiting/paused-resume/verifying/
      installed/discarded labels, Pause, Resume, two-step confirmed Discard
- [x] T-017 tests/ -- support.py fake CDN + 4 modules; 3 new acceptance cases added (journal
      monotonic x2, refused flush/fsync surfaced x1); 58 cases total
- [x] T-018 Verification -- compileall exit 0; final suite 19:51 = Ran 58 tests, 0 failed, 0 skipped
- [x] T-020 monotonic durable journal offsets -- transfers.py `_advance_from_durable` + daemon
      `_pause_active` reload
- [~] T-008 / T-009 -- replaced by T-017 / T-018 + T-019

## Research

Log required by the AGENTS.md debugging rule (research before retrying a failed fix). Tried first,
and rejected, for T-027: wrapping `diagnostics.py:111` `datetime.now()` in try/except plus a
`sys.stdout is None` guard -- the crash stayed, because the fault is not a Python exception to catch.

- https://blender.stackexchange.com/questions/23126/ -- "Add-on cleanup when Blender quits": an add-on
  gets no handler for Blender closing; `unregister()` is not called on quit, and `__del__` might be
  called as Blender terminates. Matches our shape exactly: the destructor is the only cleanup that
  fires, and it fires during teardown.
- https://developer.blender.org/T125376 (issue 125376, "Standalone `bpy` module crashes Python on
  exit"): Blender frees `bpy` after the interpreter has begun shutting down and calls back into
  Python; the reported fault is inside `python3xx.dll`, not in the add-on's own `.pyd`. Our crash has
  `Module : python313.dll`, `Parameters[0] (action) : 0x0` (a read at `0x40`), and no `_datetime*.pyd`
  in the loaded-module list -- same interpreter-level fault, reached from a Python frame.
- Local probe on this machine (`blender -b --python`, CPython 3.13.13150 / python313.dll): at
  `__main__`-module dealloc and inside an `atexit` callback, `sys.modules` is already emptied,
  `sys.argv`/`sys.path` are `None`, and C-extension module attributes are cleared --
  `datetime.now()` raises `AttributeError: module 'datetime' has no attribute 'now'`,
  `open` is gone. So shutdown work that calls the stdlib is unreliable at that point, and no
  exception handler can keep the interpreter alive.
- `bpy` exposes no exit hook here: `dir(bpy)` has no `*exit*` name, `hasattr(bpy, "atexit")` is False,
  `bpy.app.timers` only has `register`/`unregister`. BlenderMCP's own log line during a batch quit
  shows `BlenderMCP server stopped` then `BlenderMCP addon unregistered`, i.e. an add-on's registered
  exit path does run at quit.
