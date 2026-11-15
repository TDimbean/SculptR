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
