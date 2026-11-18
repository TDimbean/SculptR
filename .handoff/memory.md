# Project Memory

<!-- Session-borne knowledge only; stable conventions belong in
     CLAUDE.md/AGENTS.md. One dated bullet per entry. Decisions need a WHY.
     Graduated entries get suffix "(graduated → <file>, <date>)". -->

## Decisions

- 2026-11-15 Engine keeps running while Blender's main thread stalls, by checking
  the recorded client PID for liveness instead of trusting heartbeat freshness alone.
  WHY: Blender `bpy.app.timers` only fire when the main thread is free, so a long
  mesh/import/undo operation makes `client-heartbeat.json` stale and the daemon used
  to self-terminate mid-download. PID check is stdlib ctypes, no new dependency
  (Constitution Article X).
- 2026-11-15 Logs and terminal job state move to `models_root/.sculptr/logs` and
  `.sculptr/jobs`, not the per-session directory. WHY: `BridgeClient.stop()` rmtree's
  the session dir, which destroyed the only `engine.log` and every job state — the
  "no useful diagnostic information" symptom.
- 2026-11-15 Interrupted downloads are recorded as `status="failed"` +
  `error_code="engine_interrupted"` rather than a new status. WHY: `ui_helpers`
  already renders `failed` with a Retry button, so no UI redesign is needed.
- 2026-11-15 Tests use stdlib `unittest` under `tests/` at repo root, importing the
  package through an explicit `importlib` alias because the package directory is
  `SculptR` while code refers to `sculptr` (works on case-insensitive Windows only).
- 2026-11-18 One card owns one transfer's controls: Resume, Pause, Discard live on the
  model card in the Models section; the `Downloads (n)` rows under the Folder tab only
  report (status, progress bar, reason, `Saved x of y`, `Attempt n`). WHY: the same
  action offered from two cards means two controls that can disagree about one transfer,
  and the reader has to find which card matches what they are looking at; the
  report-only half keeps the fields the model card does not carry (attempt count, the
  transfer-side reason). `tests/test_ui_transfer_controls.py` pins the single owner.
- 2026-11-18 A card's notice row is reserved and drawn above the progress bar, always,
  with a whitespace label (`NOTICE_SLOT = " "`) when there is no message. WHY: a row that
  exists only when a message exists slides the bar and the buttons one line on every
  redraw, which reads as flicker; an empty label claims no height, so the placeholder has
  to be whitespace rather than `""`. Position follows reading: the reason explains the
  status, so it sits under the status line, not under the bar.
  `tests/test_ui_reason_slots.py` pins the shape in both card drawers.

## Gotchas

- 2026-11-17 Blender swallows an extension import failure into one reported line
  (`name 'Any' is not defined`), so a whole extension dies silently. The trigger class:
  parameter annotations are evaluated when the module body runs, and `sculptr/ui.py` had
  `def _age_label(epoch: Any)` without importing `Any`. WHY it bit twice: the repo copy and
  the copy Blender loads (`%APPDATA%\Blender Foundation\Blender\<ver>\extensions\user_default\sculptr`)
  are separate trees; fixing one does not fix the other. `tests/test_extension_imports.py`
  guards this class of bug Blender-free.
- 2026-11-15 `protocol.atomic_write_json()` fsyncs every call. It is used for the
  engine heartbeat every 0.1 s and for job state on every 1 MiB chunk — that is
  ~10 and ~500 fsyncs per file-download respectively.
- 2026-11-15 `hub.download()` decides append from HTTP status 206 alone; it never
  validates `Content-Range`, and `BridgeClient._remove_session()` deletes logs.

## Dead ends

## Pointers

- 2026-11-15 Spec for this task: `SculptR_Download_Reliability.md` (repo root, untracked).
- 2026-11-15 Binding law: `Constitution/CONSTITUTION.md`, `Constitution/AI_AGENT_LAW.md`.
- 2026-11-15 Engine entry: `SculptR/engine/__main__.py` → `EngineDaemon.run()`.
## 2026-11-17 (session 2026-11-17-imja)

- Decision: a state-only write to the transfer state journal must never lower a recorded file
  offset. Why: shutdown/pause writes are made from an in-memory copy of the record while a worker
  is still checkpointing, and the journal replace is atomic, so the copy can be older than disk.
  Checkpoint writes stay authoritative because they are the only writer that knows a payload was
  legitimately restarted from zero. Regression coverage: tests/test_transfers.py
  `test_a_state_write_never_rewinds_bytes_another_writer_committed`.
- Dead end (do not retry): fixing the shutdown race by reordering worker joins, shortening the
  shutdown grace, or killing workers. The bytes were already fsynced; the journal write is what
  was wrong.
- Gotcha: `_download` must finish from `self._records[job_id]`, never from the `begin()` snapshot,
  or an installed transfer reports the resumed prefix as its size.
- Gotcha: in `_resume_transfers` the DISCARDED intent branch must be evaluated before the
  PAUSED-state gate, otherwise a paused-then-discarded transfer is never closed and its payload
  bytes are never removed.
- Environment limit: no Blender, no real CDN here. Fake CDN fixtures (`tests/support.py`) stand in
  for 206/200/416/Retry-After/abort-mid-body; live Windows sleep inhibition and live Blender
  process lifetime remain unverified by design.

## 2026-11-17 (session 2026-11-17-z9kx)

- Decision: the Add-on Preferences box shows the license notice and the two license URLs as text,
  no acceptance checkbox. Why: acceptance already lives on the Models tab (`sculptr/ui.py:594-601`
  draws the same `hunyuan_license_confirmed` property), so the add-on toggle duplicated state without
  unlocking anything; every gate still reads the one property. Reuse of `catalog.py`
  `HUNYUAN_LICENSE_URL` / `HUNYUAN_V2_LICENSE_URL` avoids a second copy of the URLs (Constitution
  Article X — no new dependency).
- Gotcha: Blender `label()` text is single-line and clips at the panel width, so the 124-character
  hub statement is split into two labels; a 62-character URL fits.
- Gotcha: Blender panels have no clickable hyperlinks, so "link to the license" means the URL printed
  as label text; a browser-open from the add-on page would need a new operator and was not asked for.

## 2026-11-18 (session 2026-11-18-j2vq)

- Decision: the license acceptance switch is gone from BOTH places and no download depends on it. The
  client always asserts authorization (`bridge.py` `_license_authorized = True`, both `configure`
  defaults True, `settings_from_context` returns `True`), so the engine's `license_required` /
  `license_authorized` pair and its refusal paths (`engine/daemon.py:582` pause,
  `engine/daemon.py:1120` `LicenseAuthorizationError`, `engine/protocol.py:122` boolean check) are
  kept as inert protocol invariants. Why: stripping the fields from `transfers.py`, `daemon.py` and
  `protocol.py` breaks 11 call sites in `tests/test_transfers.py` and `tests/test_engine_recovery.py`
  plus the protocol validation — a public-contract change that the law requires asking about before
  making. Nothing in the UI can reach those paths any more.
- Decision: Resume re-authorizes unconditionally (`operators.py` calls
  `client.authorize_transfer(transfer_id, True)` when the intent says `license_required` and not
  `license_authorized`). Why: a journal written by an older build carries `license_authorized: false`
  and would otherwise re-pause at `daemon.py:582` on every Resume, so the saved bytes never move.
  This is the durable-intent hole that the user's screenshot showed (`hunyuan`, Discarded, Attempt 1).
- Gotcha: deleting a property from `AddonPreferences` means every remaining read of it raises
  `AttributeError` at draw time; the reads lived in `ui.py` (4), `properties.py` (1), `operators.py`
  (4) and the `bridge.py` `settings_from_context` getattr chain, and a missed one would surface as a
  redraw error, not an import error. Verified by repo-wide grep after the edits: zero matches for
  `hunyuan_license_confirmed`, `_model_requires_license`, `_requires_hunyuan_license`, `license_confirmed`.
- Gotcha: `get_preferences(context)` in `SCULPTR_PT_models.draw` and the `preferences` parameter of
  `_draw_transfer_rows` became dead with the gates; leaving them would keep an unused lookup on the
  draw path, so both were removed (signature `_draw_transfer_rows(layout)` and its one call site).
- Gotcha: `ui.py` needed `HUNYUAN_LICENSE_URL` / `HUNYUAN_V2_LICENSE_URL` imported for the Models-tab
  notice; `test_extension_imports.py` (stubbed bpy) proves the relative import still registers.
- Style: `FAILURE_MESSAGES` values stay one-per-logical-line; the reworded 401/403/
  `license_authorization_required` strings are parenthesized implicit concatenations to keep the lines
  inside the file's width.
- Kept: `catalog.py` `restricted=True` and `license_notice` still mark the three Hunyuan entries.
  `restricted` now feeds only the journal's `authorization` block (`bridge.py:620`
  `license_required=bool(getattr(spec, "restricted", False))`) and nothing else reads it as a gate.
- Open: the removed property can leave `hunyuan_license_confirmed` in an existing
  `preferences.prop`; Blender is expected to warn and ignore an unregistered key, untested here
  (no Blender). See T-026.

## 2026-11-18 (session 2026-11-18-w7fk)

- Decision: shutdown work runs at most once per session, and a destructor never repeats it. `BridgeClient`
  carries `_shutdown_done`; `__exit__`, `__del__` and the module-level `stop()` call `shutdown_once(reason)`;
  `configure()` clears the flag when it opens a session (that is where `_model_root` and `_session` come
  back). Why: Blender clears its modules at quit, so the last reference to the singleton `_CLIENT` goes
  away and `__del__` executes inside a finalizing interpreter; CPython 3.13 has already cleared C-extension
  module state there, so `datetime.now()` (and `open`) fault inside `python313.dll` -- `EXCEPTION_ACCESS_VIOLATION`,
  read at `0x40`, uncatchable from Python. The explicit calls (`unregister()` at quit, `stop()` from an
  operator, the `atexit` hook) do the real work first and mark the flag, so the net fires zero further
  times. See T-027 and `.handoff/log/2026-11-18-w7fk.md`.
- Dead end (do not retry): guarding `diagnostics.py:111` `datetime.now()` with try/except and a
  `sys.stdout is None` check. Tested, the crash stayed: the fault is a memory fault in the interpreter,
  not an exception, so no handler can keep it alive. The fix has to remove the WORK from the teardown
  path, not absorb its symptoms.
- Gotcha: two trees again -- the repo `sculptr/` and the copy Blender 5.2 loads under
  `%APPDATA%\Blender Foundation\Blender\5.2\extensions\user_default\sculptr`. Verified by sha256 after
  the copy: both A9024AA2A42B.
- Probe fact (proven locally, not assumed): in `blender -b --python` and plain CPython, at a `__main__`
  dealloc and inside an `atexit` callback, `sys.modules` is emptied, `sys.argv`/`sys.path` are `None`,
  `sys.stdout`/`sys.stderr` are still live `TextIOWrapper`s, and C-extension module attributes are
  already cleared. `bpy` has no exit hook at all: no `*exit*` name in `dir(bpy)`, `hasattr(bpy, "atexit")`
  False, `bpy.app.timers` = `register`/`unregister` only.
- Verification this session: `blender -b --python unsloth-tmp/blender_repro.py` crashes on the unfixed
  copy (crash file written, mtime 08:32:29) and exits clean on the fixed one; suite Ran 66 tests, 0 failed;
  `graphify update .` = 965 nodes / 2584 edges / 47 communities.

## 2026-11-18 (session 2026-11-18-p9mc)

- Decision: a collapsible sidebar section IS a child panel -- a class with
  `bl_parent_id = "SCULPTR_PT_root"` and a `bl_order`. Blender 5.2 collects the registered
  subclasses of the parent panel and draws each child behind its own disclosure row (the
  "v Models" row in the screenshot), so putting the license notice in a `layout.box()` inside
  the Models child panel would only nest it and keep it inside that section. See T-028.
- Decision: distinct `bl_order` within one tab (MODELS: Folder 0, License 1, Models 2) rather
  than reusing the neighbour's number. Why: a tie leaves the section order up to Blender's sort,
  and the repo relies on the drawn order being the declared one.
- Gotcha: Blender 5.2 offers no panel-tree helper for a batch probe -- `bpy.types.Panel`
  exposes only `bl_rna_get_subclass` / `bl_rna_get_subclass_py` (`subclasses_all` is gone),
  `bpy` has no `bpy.panel` module, and `bpy.utils.registered_classes()` does not exist. What
  works: the extension is auto-enabled from the saved preferences in `blender -b`, so import
  the installed package, read `sculptr.ui.CLASSES`, and let `bpy.context.scene.sculptr` supply
  the real `poll()` context.
- Gotcha: `graphify update .` refuses to shrink the graph ("new graph has 958 nodes but existing
  graph.json has 965. Refusing to overwrite -- you may be missing chunk files from a previous
  session.") and writes nothing. `--force` is documented for refactors that delete code, so a
  code-adding change gets the stale graph reported, not a forced rebuild.

## 2026-11-18 (session 2026-11-18-t4kx)

- Gotcha: `row.label()` called N times draws N items SIDE BY SIDE inside that one row, each
  clipped to its own column and each carrying its own `icon`; that is what made a hand-wrapped
  notice (`_wrap_text`, width 46) read as three truncated bubbles on one line. `box.label()` and
  `column.label()` are different -- those mint a fresh row per call, so a wrapped loop over a box
  already gives one row per line, but N rows each with its own icon still reads as N messages.
  WHY the binding rule is stronger than either fix: one message must be ONE label, because a
  Blender row stretches to its label (the long `Paused -- resume when ready: ...` status row
  proves it) and an icon belongs to a message, not to a line. Final rule for this repo: no panel
  message is split before it is drawn, and `_wrap_text` is deleted rather than kept for re-use.
  Regression coverage: `tests/test_ui_notice_rows.py` scans `sculptr/ui.py` for a `for` loop that
  draws its own loop variable through `.label(...)` (T-029, then T-030 for the split itself).
- Gotcha: `title.label(name); title.label(size)` on one model-card row is deliberate, so a
  "no shared row may carry two labels" rule would be wrong; the binding shape is a message
  split into several labels.
- Gotcha: an empty transfer-card reason must print nothing; the old `for line in _wrap_text(reason)`
  loop gave that for free (empty tuple), so the single-label form needs `if reason:`.
- Gotcha: a panel label never widens its drawer, and Blender replaces the middle of a label
  that overflows the row with "...", which reads as a split message even when it is one label.
  WHY it matters for T-030's "one message = one label" rule: the row width comes from the other
  rows (here the "Change" / "Open in Explorer" button pair, ~470 px in the Folder panel,
  ~6.3 px/glyph, so ~70-75 glyphs), and capacity is per panel -- the Models tab fits 86-char
  status text because its name+size row is wider. Final rule: one message = one label AND the
  label text fits the drawer. The Folder hint was shortened to 64 chars accordingly. Coverage:
  `MAX_LABEL_CHARS = 70` in `tests/test_ui_notice_rows.py` (T-031).
- Update superseding p9mc's note: `graphify update .` no longer refuses on this tree -- it rebuilt
  to 971 nodes / 2603 edges / 47 communities after the first ui.py edit, then 964 / 2588 / 45 once
  `_wrap_text` was deleted.

## 2026-11-18 (session 2026-11-18-kv8x)

- Decision: a pointer-stored file (LFS or XET) is verified with `lfs.sha256`, the SHA-256 of the
  served content, and its `blobId` is never carried as a digest. WHY: `blobId` is the SHA-1 of the
  git blob, and for such a file the blob is the ~135-byte pointer text -- the checkpoint's
  `blobId` `3dd05dc4116bcbb36a6d82d6431abca613c035dc` is sha1 of `blob 135\0` + pointer, while
  `file_digest` hashes `blob 7366389768\0` + served bytes, so the comparison can never hold. An
  always-failing check is not benign: `_finish_payload` quarantines a COMPLETE payload, raises
  `_IntegrityRestart`, `download_model` re-downloads from byte zero, and the user sees the progress
  reach ~100% then restart, forever. `lfs.sha256` was proven to be the content hash for both storage
  backends on this pinned revision (XET file streamed in full: 334,707,217 bytes ->
  `1b4889b6...` == `lfs.sha256`; and both checkpoints hashed end to end, dit `6b519fc7...` over
  7,366,389,768 bytes, vae `5cbe97f2...` over 655,648,152). A plain file still uses the git blob
  hash, which does describe its content (`LICENSE`: blob sha1 `d875bf81...` == its `blobId`).
- Decision: those two measured SHA-256s are pinned in `HUNYUAN_FILES` (catalog.py) in ADDITION to
  the resolver fix, and `_remote_file` refuses a catalog pin the pinned revision contradicts
  before requesting a byte. WHY: fail-fast before 8 GB is moved matches the existing `hunyuan_mv`
  precedent, and turns a bad digest into one instant `HubError` instead of a written-and-quarantined
  payload plus a full second pass.
- Gotcha: the pinned-revision API (`/api/models/{repo}/revision/{rev}?blobs=true`) names the content
  hash `lfs.sha256`; the tree form (`/api/models/{repo}/tree/main/...`) names the same value
  `lfs.oid`. Also `blobId`, not `oid`, is the sibling field in the revision form.
- Dead end (do not repeat): do not "fix" this by weakening `integrity_retry` /
  `_IntegrityRestart` / `retry_allowed`, or by skipping verification. The one-retry-then-`IntegrityError`
  shape is the invariant; the loop disappeared because the digest became the right one.
- Pointers: probes that proved all of the above live in `unsloth-tmp/` (gitignored):
  `lfs_probe.py` (pointer reconstruction), `xet_probe.py` (single XET file streamed),
  `hunyuan_digest_probe.py` (both checkpoints hashed), `hub_copy_probe.py` (the fix exercised inside
  the INSTALLED copy Blender loads). Test-side: `ServerBehaviour(stored_as_pointer=True)` in
  `tests/support.py` makes the fake CDN report a pointer sibling exactly as the real API does.

## 2026-11-18 (session 2026-11-18-b8qm)

- Decision: the busy gate is expressed against the user's intent, not only the engine's state journal --
  `merged_records(models_root, *, engine_alive=True)` reports a state nothing can advance through the
  intent (`_reconciled_state`: PAUSED intent over a resumable state -> `paused`, DISCARDED -> `discarded`,
  and the engine's phase/message cleared because they describe a process that is gone). WHY: the state
  journal has exactly ONE writer (the engine, transfers.py docstring:13-14), so a session that died mid-chunk
  leaves `queued`/`active`/`verifying` forever and no Pause or Cancel can change what the panel reads --
  the reported symptom was exactly that: a red "Download running" alert, a grey Change button, and the
  advice to pause a download that cannot run. The job side of the same rule already existed
  (`_has_active_job` clears a dead engine's pending claims, bridge.py:677-681); this is the transfer side.
- Decision: liveness is part of the transfer-record cache key in `BridgeClient.transfer_records`
  (`_transfers_alive`, bridge.py:176 init / :354 configure reset / :527 check). WHY: the merged records now
  depend on whether an engine is running, and the 1s cache would otherwise serve a pre-start/post-exit merge.
- Decision: a transfer the user never stopped keeps the folder locked even with no engine (intent `requested`
  + state `queued`/`active` stays busy). WHY: those bytes belong to that models folder and the next engine
  resumes them from the journal -- the design intent recorded in `ui_helpers.has_active_jobs`.
- Gotcha: `sculptr/ui.py` Folder drawer draws ONE gate for two symptoms -- `ui.py:512-528`
  `busy = has_active_jobs(_SNAPSHOT)` sets `change.enabled` and the red hint together, so a wrong gate
  explains "button always grey" and "alert always on" at once. Fix the predicate, not the two call sites.
- Gotcha: Blender 5.2.3 rejects `--vanilla` and `--python-at-exit` ("unknown argument, loading as file"),
  and `addon_utils.enable("sculptr", always_load=True)` raises `TypeError` there. The add-on registration
  proof that works: `tests/test_extension_imports.py` (stubbed bpy) plus a clean `blender -b` start --
  grep its output for `sculptr|Traceback|Error|Access violation`, empty means clean.
- Gotcha: the models root's only transfer record on disk today is `t-284ef2be...` state `discarded`
  (busy False), so a user screenshot showing `6.41 GB / 6.41 GB` and a full gauge cannot be reproduced
  from the live journals -- the stuck record belongs to a session whose journals were rotated. Reproduce
  against synthetic journals instead: `unsloth-tmp/busy_gate_probe.py` writes intent+state shapes copied
  from the real files (both need `"schema": 1` or `iter()` skips them) and reads the merge back.
- Dead end (do not repeat): do not fix this by making `ui_helpers.transfer_busy` ignore paused/stopped
  transfers on its own, or by clearing the state journal from the bridge. The state journal has one writer
  by design; the merge layer is the single reader every gate goes through, so the reconciliation lives there
  and both the panel (`has_active_jobs`) and the operator (`has_busy_work`) agree by construction.
- Pointers: gate layers -- `ui.py:512-528` (Folder drawer), `ui_helpers.has_active_jobs`/`transfer_busy`
  (:358-:375), `bridge.has_busy_work`/`transfer_records`, `transfers.merged_records` + `_reconciled_state`;
  new guard `tests/test_bridge_busy_gate.py` pins button-vs-operator agreement.

## 2026-11-18 (session 2026-11-18-j4vt)

- Decision: the Folder gate's JOB clause reads `IN_PROGRESS_STATUSES`, not `ACTIVE_STATUSES`
  (`ui_helpers.py:378`). WHY: `ACTIVE_STATUSES` contains `paused` on purpose -- a paused download is
  unfinished work, its row still shows progress and Resume is offered, and the engine keeps the
  committed bytes -- but it does not claim a WRITER, and the gate asks "is anything writing the
  models folder". The live root carries 7 job archives `status=paused` plus 32 completed / 18 failed /
  13 not_downloaded / 5 installed / 1 cancelled, so the old clause kept the red hint and the grey
  Change button on permanently after the user paused once; Pause was already pressed and Cancel never
  rewrites the job archive. `bridge.py:90 ACTIVE_STATUSES` never contained `paused` ("terminal for a
  job, the transfer behind it stays resumable"), so bridge + operator + `transfer_busy` already agreed.
- Decision (guard shape): `tests/test_ui_folder_busy_gate.py` pins `paused` INSIDE `ACTIVE_STATUSES`
  and OUT of `IN_PROGRESS_STATUSES`. WHY: a test that only asserted "paused does not block" would pass
  if someone deleted `paused` from the unfinished set, which would silently break the progress row and
  the Resume button; pinning both sets keeps the two vocabularities distinct and load-bearing.
- Gotcha: there are TWO models roots and the running add-on uses the other one. `properties.py:16`
  defaults `models_directory` to `bpy.utils.user_resource("DATAFILES", path="sculptr/models")` =
  `C:\Users\r00st\AppData\Roaming\Blender Foundation\Blender\5.2\datafiles\sculptr\models`, and that
  value is NOT persisted (`userpref.blend` 189292 B contains no path string, `preferences.prop` absent),
  so every restart retargets the DATAFILES default. The hand-configured
  `C:\Users\r00st\Documents\Projects\Local AI\SculptR_Models` stopped being written 2026-11-17 23:47.
  Any journal read done from `%USERPROFILE%\Documents` alone describes a dead root.
  Proof through the add-on's own accessor: live Blender 5.2.3 `-b --python unsloth-tmp/blender_gate_check.py`
  prints `models_directory` = the DATAFILES root.
- Gotcha: an earlier filesystem scan of `%APPDATA%` was never run, and a
  `glob **/downloads/state/*.json` under `C:\Users\r00st` times out (30 s); walk the Blender subtree
  explicitly with `os.scandir` instead (`appdata_root_probe.py`, `gate_replay.py`).
- Gotcha: the installed extension copy `%APPDATA%\Blender Foundation\Blender\5.2\extensions\user_default\sculptr`
  can lag the repo, and a probe that does `sys.path.insert(0, ...extensions\user_default)` then
  `import sculptr.X` exercises the STALE copy, not the repo edit -- that is what made the first
  replay print `has_active_jobs=True` after the fix landed. After every code edit run
  `unsloth-tmp/sync_installed.py` (copies all 11 files repo -> extension and hashes both sides)
  before any live-blender or installed-import probe.
- Dead end (do not repeat): the second screenshot's alert is NOT explained by the transfer merge.
  Root B's 5 merged transfers were all `installed` and busy False for `engine_alive` True and False
  (labels `Saved 7.47 GB of 7.47 GB`, ..., `Saved 0 B of 7.47 GB`); the truth came from the job clause
  of `has_active_jobs`. Reading only transfers again wastes a pass.
- Pointers: job-status side of the gate -- `ui_helpers.py` sets at :24-:41 and the clauses at
  :373-:378, `bridge.py:90` (its own ACTIVE/TERMINAL sets), `bridge._has_active_job` :674-:686
  (dead engine clears `_pending`), `bridge._active_download_job` :688-:696 (KEEPS `paused` in scope:
  that is the job the user acts on); job archives live at `<root>/.sculptr/jobs/*.json` and their
  `status` vocabulary is `{paused, installed, failed, cancelled, completed, not_downloaded}`.
