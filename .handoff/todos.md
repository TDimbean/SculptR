# Todos

<!-- Format: - [STATUS] T-NNN Title — context. Statuses: [ ]=pending,
     [>]=in progress, [x]=done, [~]=dropped (reason required).
     Blocked items keep their mark; note the blocker in the context
     ("— blocked by T-NNN"). Move [x]/[~] items to ## Done at save.
     IDs increase monotonically and are never reused.
     Full spec: see file-formats.md in the handoff skill. -->

## Active

- [x] T-001 Scaffold .handoff/ — user approved init; templates copied
- [x] T-002 Process liveness helper + engine instance lock — now
      `SculptR/process.py` (top level, so bridge can import it without pulling
      engine package); ctypes OpenProcess on Windows, `os.kill(pid,0)` POSIX;
      exclusive `models/.sculptr/engine.lock` so two engines cannot write one
      checkpoint
- [x] T-003 Persistent rotating diagnostics log + redaction —
      `SculptR/diagnostics.py` (`DiagnosticsLog`, `rotate_if_needed`, `redact`);
      logs live in `models/.sculptr/logs/`, outside the disposable session dir
- [x] T-004 hub.py HTTP resilience — socket timeout 60 s (configurable), bounded
      backoff+jitter, Retry-After, Range resume with 206/Content-Range validation,
      restart-not-append when server ignores Range, periodic fsync, partial kept
- [x] T-005 Daemon lifecycle — client PID liveness instead of heartbeat expiry,
      stalled-client log instead of exit, bounded shutdown drain, terminal-state
      archive (`models/.sculptr/jobs`), throttled progress writes (0.35 s),
      `engine-exit.json` record, `--client-pid`
- [x] T-006 Bridge lifecycle — background heartbeat thread (2 s, no bpy), passes
      `--client-pid`, no silent session wipe (records failure + restart throttle),
      snapshot read-only and merges archive, reports `failure`/`log_path`,
      `stop(reason)` bounded grace, engine stdout in `.sculptr/logs/`
- [x] T-007 UI exposes log location and failure reason — root panel failure box +
      Folder panel engine state + `sculptr.open_diagnostics` operator;
      `unregister()` now calls `bridge.stop()` (never re-configures an engine)
- [ ] T-008 tests/ with stdlib unittest — lifecycle, HTTP resume, retry, state
      persistence
- [ ] T-009 Run tests, verification, docs, handoff save
- [x] T-010 .gitignore: stop ignoring `tests/` (spec requires committed tests),
      ignore `__pycache__/` and `*.py[cod]` instead

## Done
