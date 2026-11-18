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

- **Last updated:** 2026-11-18 12:48 -- session 2026-11-18-kv8x (downloads no longer restart at zero near completion: an LFS/XET file verifies with its pointer SHA-256, not the pointer's blob hash)
- **Downloads, digest kind (kv8x, T-034):** an LFS/XET-stored file has no content blob -- its git blob is the ~135-byte
  pointer text, so `blobId` describes metadata and can never match the payload. `_remote_file` used to carry it
  as `git_sha1` for EVERY file, so the 7,366,389,768-byte Hunyuan checkpoint failed verification always:
  quarantine, `_IntegrityRestart`, `download_model` re-download from byte 0 -- the user-visible "reaches ~100%
  then starts again at 0, never completes". Fix is at the manifest layer (`sculptr/engine/hub.py`): new
  `_lfs_content_digest` (`lfs.sha256`, tree-API spelling `lfs.oid`, 64-hex checked) becomes the content pin,
  `blobId` is kept only for a file with no pointer and no content digest, a pointer/manifest size disagreement
  and a pin the revision contradicts are refused BEFORE any byte is requested, and `resolve_model` logs
  `manifest_without_digest` when a file ends with no digest. Both checkpoints' content SHA-256s were measured
  by streaming the served bytes and are now pinned in `HUNYUAN_FILES`. Docs accurate: README-2.md safeguards
  + `hub.py` docstring. Suite Ran **80, 0 failed** (baseline 75; `tests/test_hub_manifest.py` +
  `ServerBehaviour.stored_as_pointer` in the fake CDN); `compileall` exit 0; Blender 5.2.3 batch imports the
  installed copy and prints `resolved git_sha1: None`, clean quit (crash mtime still 08:32:29); installed
  `%APPDATA%` copy hash-equal (`hub.py` 492041DAD72B..., `catalog.py` 23BA4D407E7A...); `graphify update .`
  = 1007 nodes / 2685 edges / 49 communities. NOT verified: a live 7.37 GB install through the engine. Details:
  `.handoff/log/2026-11-18-kv8x.md` 12:48.
- **Notice rows, reserved (n6rq, T-033):** the transfer reason is one row that exists every frame --
  `reason_row = card.row()` outside any condition, message in the `if`, `NOTICE_SLOT` (`ui.py:42`, `" "`) in
  the `else`, drawn under the status line and above `card.progress(...)` in both the model card and the
  Downloads rows. `tests/test_ui_reason_slots.py` (3 AST cases) pins that shape; proof of sensitivity:
  `unsloth-tmp/guard_slot_proof.py` re-ran it on three broken variants, one failure each. Suite Ran 75,
  0 failed; Blender 5.2 batch loads the new ui.py; installed `%APPDATA%\...\Blender\5.2\extensions\user_default\sculptr`
  re-synced (`ui.py` 704B99A2ABD890A1...). NOT verified: rendered height -- see T-026. Details:
  `.handoff/log/2026-11-18-n6rq.md` 11:26.
- **Transfer controls, one owner (n6rq, T-032):** a transfer's actions are pressed from its model card and
  nowhere else. `_draw_transfer_rows` (`sculptr/ui.py:225`) is observation-only now -- no `actions` row, no
  `transfer_id` local -- keeping `Downloads (n)`, name + size, status, progress bar, reason, `Saved x of y`
  + `Attempt n`. `SCULPTR_PT_models.draw` carries `Discard` next to `Check` (`Confirm Discard | Keep` while
  `discard_armed`, gated out for `installed` / `discarded`), so the two-step delete invariant survived the move.
  `transfer_resumable` deleted from `ui.py` imports and `ui_helpers.py` (nothing else called it). Guard
  `tests/test_ui_transfer_controls.py` (4 AST cases) + sensitivity proof `unsloth-tmp/guard_proof.py`;
  suite Ran 72 tests, 0 failed; `compileall` exit 0; repo and installed `%APPDATA%` copies hash equal
  (`ui.py` 61DF0EC7710B..., `ui_helpers.py` 71E604097ACA...); Blender 5.2.3 batch imports the new ui.py
  (exit 0, `Blender quit`, no crash file). NOT verified: the rendered rows -- worst case is the armed
  five-column card row, and it moved from the narrower Folder drawer to the wider Models drawer. Details:
  `.handoff/log/2026-11-18-n6rq.md`, todo T-032.
- **Earlier state (t4kx, 10:02):** Folder hint fits its row; Blender no longer elides it
- **Panel notices, current state (t4kx, T-031):** a one-label message must ALSO fit the drawer,
  or Blender replaces its middle with "..." and it reads as split text again. The Folder-panel
  row is only ~470 px wide (set by the "Change" / "Open in Explorer" buttons, ~6.3 px/glyph,
  so ~70-75 glyphs). The 90-char hint was shortened to 64 chars:
  `Download running. Pause or cancel it before changing the folder.` (`sculptr/ui.py:540`), with a
  comment stating the rule. Guard `tests/test_ui_notice_rows.py` now pins that text AND `MAX_LABEL_CHARS = 70`.
  Suite Ran 68 tests, 0 failed; `compileall` exit 0; repo and installed
  `%APPDATA%\...\sculptr\ui.py` both sha256 49A1C41DE002...; `graphify update .` refreshed.
  NOT verified: the rendered row -- no live Blender panel render here.
  Open same-class risk (untouched, wording change needs approval): engine notices drawn in that
  same drawer -- `sculptr/operators.py:500` resume notice ~103 glyphs, `:557` discard notice ~72.
  Earlier state (T-029 + T-030) and its sha256 7ECE43D829AD... are history in
  `.handoff/log/2026-11-18-t4kx.md`.
- **Panel notices, final state (t4kx, T-029 + T-030):** a notification message in the sidebar
  must be ONE label on ONE row with ONE icon, like the Hunyuan
  `Paused -- resume when ready: Paused by user; committed bytes kept` row. Both reported symptoms
  came from `sculptr/ui.py` splitting text before drawing it: `row.label()` per wrapped fragment
  put the fragments side by side in one row (each clipped, each with an `INFO` icon), and one-row-
  per-fragment left N stacked rows with N icons. Fix: the five message sites -- engine notice, the
  Folder-panel busy hint ("A download is still running. Pause or cancel it before changing the
  models folder."), `_draw_engine_failure` first line, the transfer-card reason (guarded `if
  reason:` so an empty reason still prints nothing), and the Models-tab card error -- each draw a
  single label and a Blender row stretches to it. The `_wrap_text` helper is deleted (nothing feeds
  it; keeping a dead splitter is debt). Guard `tests/test_ui_notice_rows.py`: fails if any `for`
  loop draws its own loop variable through `.label(...)`, if the splitter name survives, or if the
  hint text is missing. Suite Ran 68 tests, 0 failed; `compileall` exit 0; installed
  `%APPDATA%\Blender Foundation\Blender\5.2\extensions\user_default\sculptr\ui.py` sha256
  7ECE43D829AD... both trees; `graphify update .` = 964 nodes / 2588 edges / 45 communities.
  NOT verified: the rendered rows -- no live Blender panel render here. Details:
  `.handoff/log/2026-11-18-t4kx.md`, todos T-029 and T-030.
- **Side fix this hour (t4kx):** a notification message that `_wrap_text` splits was drawn as
  several clipped columns on ONE row, each with its own icon (engine notice under "Engine
  running", plus the red "A download is still running. Pause or cancel it before changing the
  models folder." under Change / Open in Explorer). Cause `sculptr/ui.py`: `notice_row =
  layout.row()` (202) and `hint = layout.row()` (554) sat OUTSIDE the `for line in _wrap_text(...)`
  loop, and Blender lays every `row.label()` side by side. Fix: the `.row()` plus its `alert`
  moved inside both loops, so each wrapped line is its own row; `icon="INFO"`, alert state, and
  the message text unchanged. The three `box.label()` loops already mint a row per call, untouched.
  Guard `tests/test_ui_notice_rows.py` (AST scan per function scope; the deliberate model-card
  `title.label` name+size pair stays legal). Suite Ran 68 tests, 0 failed; `compileall` exit 0;
  installed `%APPDATA%` ui.py re-synced (both sha256 EA464B03AB47...) after a diff proved the
  copies differed only by this fix; `graphify update .` rebuilt 971 nodes / 2603 edges / 47
  communities (p9mc's refusal recovered). NOT verified: the rendered sidebar pixels -- no live
  panel here. Details: `.handoff/log/2026-11-18-t4kx.md`, todo T-029.
- **Side fix this hour (p9mc):** the Models-tab license notice is now its own collapsible
  sidebar section instead of loose rows above the model cards. `sculptr/ui.py`: new child panel
  `SCULPTR_PT_license` (`bl_parent_id = "SCULPTR_PT_root"`, `bl_label` "License", `bl_order` 1,
  poll `active_tab == "MODELS"`, the five label lines drawn on `self.layout` with text unchanged);
  `SCULPTR_PT_models` renumbered to `bl_order` 2 and its `license_box = layout.box()` rows deleted,
  so the notice lives in exactly one section; `CLASSES` extended. README-2.md line 42 reworded to
  name the License section. Verified in Blender 5.2.3 batch (`blender -b --python unsloth-tmp/panel_sections.py`):
  `sections for MODELS : ['Folder (order 0)', 'License (order 1)', 'Models (order 2)']` against the
  real `bpy.context.scene.sculptr`, GENERATE tab unchanged, clean quit and the crash file keeps
  mtime 08:32:29. Installed `%APPDATA%` copy re-synced (ui.py sha256 `28103b042d78...` both trees).
  Guard test `tests/test_ui_panel_sections.py`; suite Ran 67 tests, 0 failed; `compileall` exit 0.
  NOT refreshed: `graphify update .` refused ("new graph has 958 nodes but existing graph.json has
  965 ... missing chunk files"), nothing written, so `graphify-out/graph.json` no longer knows
  `SCULPTR_PT_license`; `--force` is documented for code-deleting refactors, not this. Details:
  `.handoff/log/2026-11-18-p9mc.md`, todo T-028.
- **Earlier this hour (w7fk):** Blender stopped working on exit and wrote
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
