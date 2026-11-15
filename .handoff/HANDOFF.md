# Project Handoff

> **Agent protocol — read this if you have no handoff skill installed.**
> This repo uses `.handoff/` for session continuity between AI agents.
> 1. `todos.md` is your working task list. Read it and work from it. For
>    any `[>]` in-progress item, also read the log file it references.
> 2. Create your session log: `log/<YYYY-MM-DD>-<4 random a-z0-9 chars>.md`
>    (random chars, not a word — prevents collisions) with frontmatter
>    `session`, `agent`, `goal` (see existing logs).
> 3. Every time a todo changes state (started/done/blocked/dropped), update
>    `todos.md` AND append one line to your session log:
>    `HH:MM <kind> <T-id if any> — <short note>`.
> 4. Record durable decisions (with the why), gotchas, and dead ends in
>    `memory.md`, dated.
> 5. Before stopping: append a `## Handoff summary` (done / in flight / next /
>    open questions) to your session log and update the Status section below.
> 6. NEVER write secrets, tokens, or credentials into these files.

## Status

- **Last updated:** 2026-11-15 08:58 · session 2026-11-15-k7x2 · gyro-gyro-m (Codex-style harness, Windows)
- **Current state:** Download-reliability task (spec: `SculptR_Download_Reliability.md`)
  in flight. Root-cause reading of the code confirms two failure paths: the engine
  self-terminates when Blender's main-thread timer cannot refresh
  `client-heartbeat.json` within `client_timeout` (10 s), and `BridgeClient.configure()`
  then wipes the whole session directory (engine.log + job states) on the next poll.
  HTTP layer has no retry (socket timeout 15 s) and every 1 MiB chunk triggers an
  fsync'd JSON state write. Implementation of lifecycle + logging + HTTP resume/retry +
  throttled state writes in progress; nothing committed yet.
- **Next steps:**
  1. T-002 — process-liveness helper + engine instance lock
  2. T-003 — persistent rotating diagnostics log with redaction
  3. T-004 — hub.py timeout/retry/Range-resume rewrite
