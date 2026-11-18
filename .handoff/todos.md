# Todos

## Active

- [ ] T-019 Docs + final handoff -- refresh after the cleanup finishes
- [ ] T-026 Live-Blender check left open by T-025 -- the two notice panels (Add-on Preferences box,
      Models tab box) cannot be rendered here, so their final text and width are unverified; and the
      removed `hunyuan_license_confirmed` property can leave that key in an existing
      `preferences.prop`, whose warn-and-ignore behaviour needs one live enable/disable cycle to
      confirm. Needs the user to load the add-on in Blender 5.2 and report the two boxes.
      - 09:05 partial progress from T-028: the MODELS-tab SECTION STRUCTURE is now proven in live
        Blender 5.2 batch (Folder / License / Models as three child panels, real scene poll), so what
        is still unverified is only the rendered label text and wrapping inside the License section.
- [x] T-021 Repo cleanup -- closed 20:31. Deleted the 8 orphan scratch files (diag.txt, eng-run.txt,
      hub-run.txt, proc-run.txt, test-run.txt, tr-run.txt, scratch_diag.py, scratch_lock.py); emptied
      unsloth-tmp (280 to 0 children) and kept the directory because it is the harness temp redirect;
      added graphify-out/, unsloth-tmp/, scratch_*.py, *-run.txt, diag.txt to .gitignore, git
      check-ignore confirms every pattern matches; graphify-out dropped out of git status.
      - Deliberately NOT done: commit (the chosen scope excluded it) and the addCleanup rmtree fix in
        the 5 test setUps -- both left as proposals.

## Done

- [x] T-034 Downloads that reach ~100% then restart at 0 forever -- fixed at `sculptr/engine/hub.py`.
      Cause: `_remote_file` set `git_sha1 = sibling["blobId"]` for every file and ignored
      `sibling["lfs"]`, but for an LFS/XET file `blobId` is the SHA-1 of the ~135-byte POINTER text
      (`3dd05dc4...` = sha1 of `blob 135\0` + pointer) while `file_digest` hashes
      `blob 7366389768\0` + served bytes, so verification of the checkpoint failed always:
      quarantine -> `_IntegrityRestart` -> `download_model` re-download from byte 0 ->
      `IntegrityError`. Engine.log evidence: `partial_quarantined ... bytes=7366389768
      moved_to=model.fp16.ckpt.part.unverified-1794950708`, then `download_progress offset=33554432`
      from zero. Fix: new `_lfs_content_digest` (`lfs.sha256` or the tree-API `lfs.oid`, 64-hex
      checked, else no digest); `_remote_file` uses it as `sha256`, refuses a pointer size that
      disagrees with the manifest size, refuses a catalog pin the pinned revision contradicts
      BEFORE requesting a byte, and carries `blobId` as `git_sha1` only for a file with no pointer
      and no content digest. Proven by hashing the served bytes: both checkpoints' content SHA-256
      equals their `lfs.sha256` (dit `6b519fc7...`, 7,366,389,768 bytes; vae `5cbe97f2...`,
      655,648,152 bytes), so those values are now pinned in `HUNYUAN_FILES`. Docs: README-2.md
      safeguards section and the `hub.py` docstring now state the digest rules. New
      `tests/test_hub_manifest.py` (5 cases) + `ServerBehaviour.stored_as_pointer` in the fake CDN.
      Suite Ran 80, 0 failed (baseline 75); `compileall` exit 0; Blender 5.2.3 batch imports the
      INSTALLED copy and prints `resolved git_sha1: None`, clean quit, crash mtime still 08:32:29;
      installed `%APPDATA%` copy re-synced (`hub.py` 492041DAD72B..., `catalog.py` 23BA4D407E7A...
      both trees); `graphify update .` 1007 nodes / 2685 edges / 49 communities. NOT verified: a
      live 7.37 GB install through the engine (needs Blender + bandwidth). Details:
      `.handoff/log/2026-11-18-kv8x.md` 12:48.

- [x] T-033 Stall/failure notices no longer move the card -- the "connection ended at ..."
      row flickered in and out and slid the progress bar and buttons one line. Cause:
      the reason was drawn by `if error_code: card.label(...)` (model card, `ui.py:579`) and
      `if reason: card.label(...)` (`_draw_transfer_rows`, `ui.py:260`) -- a row that only
      exists when a message exists, drawn below `card.progress(...)`. Fix, same shape in both
      drawers: `reason_row = card.row()` made outside any condition, message label in the `if`,
      `NOTICE_SLOT` label in the `else` (`NOTICE_SLOT = " "`, `ui.py:42`, whitespace so the line
      is claimed but no words show), whole block moved above `card.progress(...)` so the reason
      reads under the status line and above the bar. No `alert` set, so the look (warning-triangle
      icon, untinted row) stays as before. Guard `tests/test_ui_reason_slots.py` (3 AST cases:
      row made once, never inside a condition, labelled twice; labels precede the `progress` call
      and no `card.label(` left on the box; the slot is whitespace, not empty) — sensitivity
      proved with `unsloth-tmp/guard_slot_proof.py`, which re-ran the guard on three broken
      variants and got one failure each ("builds the row inside a condition", "596 not less than
      588 ... below the progress bar", "'' == '' : an empty label claims no height"). Suite Ran
      75 tests, 0 failed (was 72); `compileall` exit 0; Blender 5.2 batch probe loads the new
      ui.py (`scene properties: True`, exit 0); installed copy re-synced at
      `%APPDATA%\Roaming\Blender Foundation\Blender\5.2\extensions\user_default\sculptr`
      (path is `Blender\5.2`, not `Blender 5.2`) — `ui.py` 704B99A2ABD890A1... equal both trees.
      `graphify update .` = 986 nodes / 2614 edges / 52 communities. NOT verified: the rendered
      height -- the guard pins the draw shape, not pixels; a live GUI look at a message-free card
      is still open (T-026). Details: `.handoff/log/2026-11-18-n6rq.md` 11:26 section.
- [x] T-032 Download controls only on the model card -- the transfer row under the Folder
      tab and the model card under Models both offered controls for one transfer
      (`Resume | Pause | Discard` on the Downloads (n) card vs `Cancel | Pause | Check` on
      the card). Fix in `sculptr/ui.py`: `_draw_transfer_rows` lost its whole `actions` block
      and its unused `transfer_id` local, keeping only the report rows
      (`Downloads (n)`, name + size, status, progress bar, reason, `Saved x of y` +
      `Attempt n`) because that is engine state the card lacks; `SCULPTR_PT_models.draw`
      gained the delete control on its `actions` row -- `Discard`, or `Confirm Discard |
      Keep` while `discard_armed(transfer_id)`, gated `transfer_id and status not in
      {"installed", "discarded"}` so a finished transfer shows no dead button. The two-step
      discard (arm/confirm, `ui.py:58-75` + `operators.py:509`) preserved. `transfer_resumable`
      deleted from the ui.py imports and from `ui_helpers.py` (repo-wide grep: nothing else
      called it). Guard `tests/test_ui_transfer_controls.py` (4 AST cases: download rows press
      no controls; the three lifecycle operators belong to exactly one unit, the card, and
      nowhere else; the card keeps the two-step pair and reads `discard_armed`; the dead helper
      is gone) — sensitivity proved with `unsloth-tmp/guard_proof.py`. Suite Ran 72 tests, 0
      failed; `compileall` exit 0; installed `%APPDATA%` copy re-synced after a diff proved the
      copies differed only by this change (`ui.py` 61DF0EC7710B..., `ui_helpers.py`
      71E604097ACA... both trees); Blender 5.2.3 batch loads the new ui.py clean (exit 0,
      `Blender quit`, no crash file). README-2.md section 2 documents the card controls.
      `graphify update .` = 974 nodes / 2598 edges / 46 communities. NOT verified: the rendered
      rows, including the armed five-column card row width. Details:
      `.handoff/log/2026-11-18-n6rq.md`.
- [x] T-031 Blender clipped the one-row message with "..." -- after T-030 the hint read as
      "A download is still running. Pause ...before changing the models folder.": one row, one
      icon, but the ellipsis is Blender's own mid-elision, so it still looked split. A panel
      label never widens its drawer; the row keeps the width the button rows asked for, and an
      overflowing label loses its middle (90 chars needed ~576 px, the row had ~470 px at
      ~6.3 px/glyph -> capacity ~70-75 glyphs; capacity is per panel, the Models tab is wider
      because of its name+size row). Fix: hint shortened to 64 chars
      "Download running. Pause or cancel it before changing the folder." (`sculptr/ui.py:540`)
      with a comment stating the width rule. Guard extended: `MAX_LABEL_CHARS = 70` plus the
      text must still be present. Suite Ran 68 tests, 0 failed; `compileall` exit 0; installed
      `%APPDATA%` ui.py sha256 49A1C41DE002... both trees; `graphify update .` refreshed.
      Web search for Blender elision reports returned only appliance results -- findings
      unusable, so the fix rests on the screenshot measurement. NOT touched: engine notices in
      the same drawer can exceed 70 glyphs (`operators.py:500` ~103, `:557` ~72). Details:
      `.handoff/log/2026-11-18-t4kx.md`.

- [x] T-030 One message = one label -- the red Folder-panel hint still read as two stacked
      rows, each with its own icon ("A download is still running. Pause or cancel" /
      "it before changing the models folder."); one row per fragment was not enough. Fix in
      `sculptr/ui.py`: all five message sites draw ONE label so the row stretches to the text,
      matching the Hunyuan `Paused -- resume when ready: ...` row -- engine notice, the busy
      hint, `_draw_engine_failure` first line, the transfer-card reason (now `if reason:` so
      an empty reason still prints nothing), the Models-tab card error. `_wrap_text` deleted:
      nothing feeds it, and a dead splitter is debt. Guard rewritten: no `for` loop may draw
      its own loop variable through `.label(...)`, the splitter name must not survive, and the
      hint text must stay. Suite Ran 68 tests, 0 failed; `compileall` exit 0; installed
      `%APPDATA%` ui.py sha256 7ECE43D829AD... both trees; `graphify update .` = 964 nodes /
      2588 edges / 45 communities. Rendered rows not verified. Details:
      `.handoff/log/2026-11-18-t4kx.md`.

- [x] T-029 Split notification messages -- a hand-wrapped notice was drawn as several clipped
      columns on ONE row, each with its own icon (screenshots: engine notice under "Engine
      running", and the red "A download is still running..." line under Change / Open in
      Explorer). Cause: `row.label()` called per wrapped line puts every label side by side in
      that row; `ui.py:202` `notice_row = layout.row()` and `ui.py:554` `hint = layout.row()`
      sat OUTSIDE the `for line in _wrap_text(...)` loop. Fix: `.row()` + its `alert` moved
      inside both loops, one row per line; `icon="INFO"` and alert state unchanged. The three
      `box.label()` loops (failure message, transfer-card reason, model-card error) already
      mint a row per call, so they were left alone. Guard `tests/test_ui_notice_rows.py`;
      suite Ran 68 tests, 0 failed; `compileall` exit 0; installed `%APPDATA%` ui.py re-synced
      (both sha256 EA464B03AB47...); `graphify update .` = 971 nodes / 2603 edges / 47
      communities. Rendered pixels not verified (no live panel here). Details:
      `.handoff/log/2026-11-18-t4kx.md`.

- [x] T-028 License as its own section -- the Models-tab license notice moved out of the
      Models card list into a sibling child panel `SCULPTR_PT_license` (`ui.py`): `bl_parent_id`
      `SCULPTR_PT_root`, `bl_label` "License", `bl_order` 1, so MODELS sections now render
      Folder (0) / License (1) / Models (2) and each is collapsible. `SCULPTR_PT_models` now
      `bl_order` 2 and draws only model cards; `CLASSES` gained the new class; label text unchanged.
      Verified in Blender 5.2.3 batch (`blender -b --python unsloth-tmp/panel_sections.py`, real
      `bpy.context.scene.sculptr`): `sections for MODELS : ['Folder (order 0)', 'License (order 1)',
      'Models (order 2)']`, GENERATE unchanged, clean quit, crash-file mtime still 08:32:29.
      Installed `%APPDATA%` copy re-synced (both sha256 28103b042d78...). New guard test
      `tests/test_ui_panel_sections.py`; suite Ran 67 tests, 0 failed; `compileall` exit 0.
      `graphify update .` refused (958 vs 965 nodes) and left graph.json untouched, so the graph
      is now stale for the new class (grep `SCULPTR_PT_license` in `graphify-out/graph.json` = no
      matches). Details: `.handoff/log/2026-11-18-p9mc.md`.
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
