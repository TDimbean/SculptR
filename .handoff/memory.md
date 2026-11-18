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
