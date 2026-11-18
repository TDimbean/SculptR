# Project Handoff

> **Agent protocol -- read this if you have no handoff skill installed.**
> This repo uses `.handoff/` for session continuity between AI agents.
> 1. `todos.md` is your working task list. Read it and work from it. For
>    any `[>]` in-progress item, also read the log file it references.
> 2. Create your session log: `log/<YYYY-MM-DD>-<4 random a-z0-9 chars>.md`
>    (random chars, not a word -- prevents collisions) with frontmatter
>    `session`, `agent`, `goal` (see existing logs).
> 3. Every time a todo changes state (started/done/blocked/dropped), update
>    `todos.md` AND append one line to your session log:
>    `HH:MM <kind> <T-id if any> -- <short note>`.
> 4. Record durable decisions (with the why), gotchas, and dead ends in
>    `memory.md`, dated.
> 5. Before stopping: append a `## Handoff summary` (done / in flight / next /
>    open questions) to your session log and update the Status section below.
> 6. NEVER write secrets, tokens, or credentials into these files.

## Status

- **Last updated:** 2026-11-18 08:52 -- session 2026-11-18-w7fk (Blender quit crash fixed: once-only shutdown)
- **Side fix this hour (w7fk):** Blender stopped working on exit and wrote
  `%TEMP%\blender.crash.txt`. Cause: at quit Blender clears its modules, which drops the last reference
  to the bridge singleton `_CLIENT`, so `sculptr/bridge.py` `__del__` ran `stop("__del__")` ->
  `_log_event` -> `event` -> `diagnostics.py:111` `datetime.now()` inside a finalizing CPython 3.13
  whose C-extension state is already cleared; the fault is in `python313.dll` (read at `0x40`), not
  catchable from Python. The `atexit` hook had already retired the session, so `__del__` was repeating
  the whole shutdown. Fix: `BridgeClient._shutdown_done` + `shutdown_once(reason)`; `__exit__`, `__del__`
  and the module-level `stop()` route through it, `configure()` clears the flag with the new session,
  and `stop()` marks the flag before it does any work. Reproduced and disproved under Blender 5.2
  batch with `blender -b --python unsloth-tmp/blender_repro.py`: unfixed -> `Writing: ...\blender.crash.txt`
  + `EXCEPTION_ACCESS_VIOLATION` + backtrace `bridge.py:189 in __del__` -> `577 in stop` -> `981 in _log_event`
  -> `diagnostics.py:104 in event` -> `111 in _emit`; fixed -> clean `Blender quit`, crash mtime still
  08:32:29. Added `tests/test_bridge_shutdown.py` (5 cases); suite Ran 66 tests, 0 failed; `graphify update .`
  = 965 nodes / 2584 edges / 47 communities; installed copy in sync (sha256 A9024AA2A42B both trees).
  Details: `.handoff/log/2026-11-18-w7fk.md`, todos T-027.
- **Earlier this day (j2vq):** the Models-tab license checkbox (`ui.py` `prop(preferences,
  "hunyuan_license_confirmed")` at 599) and the whole acceptance rule are gone. Deleted the
  `hunyuan_license_confirmed` BoolProperty (`properties.py:22`) and both boxes now print only the
  statement "This extension is only a hub for you to download and launch the models." /
  "License for their use can only be granted by Tencent" plus the two license URLs as label text
  (`ui.py` needed the `catalog` URL import; boxes use `icon="INFO"`, no `alert`, since nothing is
  pending). Gates removed: `ui.py` Resume-enabled conjunct, the `Accept the license below to resume`
  card label, Retry/Download enable flags and `_model_requires_license`; `operators.py` download
  refusal, `configure` license arg, `license_confirmed` payload key, `_requires_hunyuan_license`,
  and Resume's `if get_preferences(...)` guard (now always `authorize_transfer(transfer_id, True)`
  for a stale intent — that is what lets the screenshot's `hunyuan` transfer resume instead of
  re-pausing at `daemon.py:582`); `bridge.py` always authorizes (`_license_authorized = True`, both
  `configure` defaults, `settings_from_context` returns `True`). Engine keeps the
  `license_required`/`license_authorized` fields and its refusals as inert invariants -- deleting
  them touches 11 test call sites, so it stays a proposal. Six files mirrored into the Blender 5.2
  copy (diff showed the copies were in sync first). Suite still Ran 61 tests, 0 failed; grep proves
  no read of the deleted property survives. Unverified by design: the live panel text (no Blender
  here) and Blender's handling of the now-unregistered `hunyuan_license_confirmed` key in an old
  prefs file -- T-026. Details: `.handoff/log/2026-11-18-j2vq.md`, todos T-025.
- **Side fix this hour (z9kx) [superseded by j2vq for the Models tab]:** the Add-on Preferences box dropped its `I accept the Hunyuan
  license for this US deployment` checkbox (`sculptr/properties.py:35` `warning.prop`) and now prints
  "This extension is only a hub for you to download and launch the models." + "License for their use
  can only be granted by Tencent" plus both license URLs (imported from `catalog.py`). Blender labels
  do not wrap and have no hyperlinks, hence the two-label split and the URL-as-text. Models-tab
  acceptance (`ui.py:594-601`) untouched then, so every license gate still worked at that hour.
  Mirrored into the Blender 5.2-loaded copy; `README-2.md` 47/110/440 corrected. Suite still 61/0.
  Details: `.handoff/log/2026-11-17-z9kx.md`, todos T-024.
- **Objective:** background downloads + crash-safe resume per
  `SculptR_Background_Download_Fix_Updated.md`. **Status: complete.** No open defect from that
  objective. Open follow-ups now: T-019 (docs + handoff refresh) and T-026 (live-Blender panel
  verification). T-027, the quit crash, is closed 08:52.
- **Side fix this hour (q4zn):** the flickering UI banner `I/O operation on closed file.` was
  `sculptr/bridge.py` `_write_heartbeat`: `stream.write("\n")` sat OUTSIDE the `with path.open("w")`
  on a closed handle, raised `ValueError`, and slipped past `except OSError` out of `snapshot()`
  into the UI poll (`ui.py` `_poll_runtime` -> `_RUNTIME_ERROR` row). Rate-limited by
  `BRIDGE_HEARTBEAT_INTERVAL`, hence the flicker. Write moved inside the `with`, handler widened to
  `(OSError, ValueError)`; same patch in the copy Blender 5.2 loads under `%APPDATA%`; guard added
  as `tests/test_bridge_heartbeat.py`. Suite now Ran 61 tests, 0 failed. Details:
  `.handoff/log/2026-11-17-q4zn.md`, todos T-023.
- **Prior side fix (v7dq):** enabling the extension failed with Blender's report
  `name 'Any' is not defined`; cause `sculptr/ui.py` annotated `_age_label(epoch: Any)` while
  importing only `Mapping`. Fixed in the repo and in the copy Blender 5.2 loads
  (`%APPDATA%\...\extensions\user_default\sculptr`); guard added as `tests/test_extension_imports.py`.
  Suite then Ran 59 tests, 0 failed. Details: `.handoff/log/2026-11-17-v7dq.md`, todos T-022.

### Verified with evidence (all mandatory requirements satisfied)

- UI independence: `bridge.py` `snapshot()` (519) only observes; lifecycle is `configure()` (256)
  / `recover()` (337) with own backoff; recovery deferred while the old process is UNKNOWN (353);
  no Blender heartbeat thread -- `_write_heartbeat` (940, interval 83) is telemetry, engine decides
  from the client pid. Engine owns the work: external `sculptr/engine daemon.py` subprocess.
- Process liveness: `process.py` ALIVE/EXITED/UNKNOWN, typed `c_void_p` handle, `WaitForSingleObject`
  (no ambiguous 259), `EngineLock` pid byte at absolute offset 0 (`seek(0)`, 385-490),
  `set_idle_inhibition` ES_SYSTEM_REQUIRED. Lock contention proven by
  `test_a_second_engine_on_the_same_models_root_is_refused`.
- Durable intent + checkpoints: `<models root>/.sculptr/downloads/{intent,state}/<transfer_id>.json`,
  atomic fsync'd replace, identity-bound payload `<model_id>.partial/<local>.part` -> `.staged`
  -> `os.replace` -> install marker last. Monotonic durable offsets via
  `TransferStateJournal._advance_from_durable` (state writes cannot rewind a worker's committed
  bytes; checkpoints stay authoritative).
- HTTP state machine (`hub.py`): Range only for offset>0, `If-Range` strong-ETag re-offer only,
  206 Content-Range validated vs requested offset + manifest size (else `range_mismatch` keeps
  prefix), 200-after-Range never appends (`ResumeUnsupported`, offset reported), 416 never
  unlinks (663), one `RetryController` (backoff, Retry-After, `NO_PROGRESS_WINDOW=900`),
  retryable/permanent status split, free-space gate `remaining + SPACE_SLACK` (954-960),
  write+flush+fsync before the durable offset (`_commit` 896-906), integrity verified before
  promote, install marker written last.
- Recovery/resume: `daemon._resume_transfers` reopens RESUMABLE_STATES with no new command; a pause
  resumes only on a REQUESTED intent; backoff `next_attempt_at` survives restart; quit parks the
  transfer with bytes intact (interrupted ids recorded before drain; engine lock released only after
  all workers join, else `engine_lock_held` warning).
- Telemetry/IPC isolation: daemon "best-effort telemetry write" (151) and "failure is telemetry,
  never fatal" (464) wrap every log/state write; bridge wraps heartbeat (965) and intent (625)
  failures -- none can abort a download.
- UI: `ui_helpers.py` 47-57 queued/downloading/retrying/waiting for network/paused-resume/
  verifying/installed/discarded; `operators.py` Pause, Resume, two-step confirmed Discard;
  `ui.py` routes through operators only.

### Changes made this session

1. `engine/daemon.py` `_download`: INSTALLED finish now uses `self._records.get(job_id) or record`
   (was the `begin()` snapshot, which rewrote `completed_bytes` and file offsets).
2. `engine/daemon.py` `_resume_transfers`: DISCARDED intent branch moved before the PAUSED gate, so
   a paused-then-discarded transfer is closed and its payload store removed.
3. `sculptr/transfers.py`: `TransferStateJournal.update()` merges the newest durable offsets/flags
   for state-only writes (`_advance_from_durable`); `_pause_active` (daemon) reloads the durable
   record and re-applies the skip set.
4. `tests/`: +3 acceptance cases -- journal state write cannot rewind committed bytes; a
   checkpoint may still lower an offset (clean restart); a refused flush/fsync raises
   `CheckpointError` and claims no unflushed byte, keeps the payload, promotes nothing.
5. `graphify update .` run (graphify-out/ was missing; `python -m graphify query` had errored).

### Test results (actual counts)

- Baseline 19:29: `compileall -q sculptr tests` exit 0; suite Ran 55 tests, 2 failed, 0 skipped.
- 19:36 after fixes 1-2: Ran 55, 1 failed, 0 skipped.
- **Final 19:51: Ran 58 tests, 0 failed, 0 skipped.** Module re-runs for flakiness:
  `test_engine_recovery` 6/6 x3, unit modules 40/40 x2.
- Not verifiable here (documented, not claimed): live Blender/Windows process behaviour, real
  multi-GB CDN Range/416/Retry-After responses, real sleep-inhibition effect. Covered instead by
  deterministic local fixtures in `tests/support.py` (no network, no bpy).

### Outstanding / optional

- Leftovers from earlier sessions in the repo root, untracked and deliberately untouched:
  `diag.txt`, `eng-run.txt`, `hub-run.txt`, `proc-run.txt`, `test-run.txt`, `tr-run.txt`,
  `scratch_diag.py`, `scratch_lock.py`, `unsloth-tmp/`. Nothing in the brief asks to remove them;
  a later cleanup commit may.
- Working hypotheses: all three failures seen this session were production defects, not test bugs;
  no assertion was weakened. Do not chase the shutdown race by reordering worker joins or killing
  workers -- see `.handoff/memory.md` 2026-11-17 dead-end entry.

### Next actionable step

Nothing required. If the user wants, next candidates: commit the work (stage `sculptr/transfers.py`,
`process.py`, tests/, modified modules; do not stage the scratch leftovers), and/or delete the
scratch artifacts after explicit confirmation.
