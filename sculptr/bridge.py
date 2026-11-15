"""Process and file-IPC bridge between Blender and SculptR's engine.

The UI process owns three things the engine reads: a session directory, a client
heartbeat, and command files. Blender only runs `bpy.app.timers` when its main
thread is free, so a heartbeat written from a timer goes stale during a long
modal operation while Blender itself is alive. Two changes keep the engine
alive across that stall:

* the heartbeat is written by a background thread, independent of UI timers;
* the engine decides client death from the client process id, not from a stale
  heartbeat file (see `sculptr/process.py`).

Evidence outlives the session. Logs, engine stdout, and terminal job states live
under `<models root>/.sculptr/{logs,jobs}/` instead of inside the disposable
session directory, so a crash or a restart cannot delete the records needed to
explain it.
"""

from __future__ import annotations

import atexit
import json
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .diagnostics import DEFAULT_BACKUPS, DEFAULT_LOG_BYTES, DiagnosticsLog, rotate_if_needed

SCHEMA_VERSION = 1
ALLOWED_ACTIONS = frozenset(
    {
        "check_model",
        "download_model",
        "generate",
        "repaint_texture",
        "enhance_detail",
        "clear_cache",
        "shutdown",
    }
)
_BOOTSTRAP = (
    "import runpy,sys;"
    "sys.path.insert(0,sys.argv.pop(1));"
    "runpy.run_module('sculptr.engine',run_name='__main__')"
)
_RESERVED_COMMAND_FIELDS = frozenset({"schema", "session", "job_id", "action", "created_at"})

# Written by a thread, not by a UI timer, so a stalled main thread does not look
# like a dead client. Must stay well below the engine's client timeout.
BRIDGE_HEARTBEAT_INTERVAL = float(os.environ.get("SCULPTR_BRIDGE_HEARTBEAT", "2.0"))
SHUTDOWN_GRACE_SECONDS = float(os.environ.get("SCULPTR_SHUTDOWN_GRACE", "10.0"))
# An engine that dies again within this window is crash-looping; restarting it
# every poll would burn CPU and hide the original failure.
RESTART_BACKOFF_SECONDS = float(os.environ.get("SCULPTR_ENGINE_RESTART_BACKOFF", "5.0"))

ACTIVE_STATUSES = frozenset({"queued", "checking", "downloading", "running"})
TERMINAL_STATUSES = frozenset(
    {"cancelled", "completed", "failed", "installed", "not_downloaded", "invalid"}
)
_INTERRUPTED_MESSAGE = "Engine stopped before this job finished; partial download preserved"


def logs_directory(model_root: str | os.PathLike[str]) -> Path:
    return Path(model_root) / ".sculptr" / "logs"


def jobs_directory(model_root: str | os.PathLike[str]) -> Path:
    return Path(model_root) / ".sculptr" / "jobs"


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


class BridgeClient:
    """Own one isolated engine process and its nonce-scoped session.

    `snapshot` never restarts the engine and never deletes a session; only
    `configure` may start a process, and it records why the previous one ended.
    """

    def __init__(self, project_root: str | os.PathLike[str] | None = None) -> None:
        self._project_root = Path(project_root or Path(__file__).resolve().parents[1]).resolve()
        self._lock = threading.RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._log_stream: Any = None
        self._model_root: Path | None = None
        self._session: Path | None = None
        self._license_authorized = False
        self._pending: dict[str, dict[str, Any]] = {}
        self._failure: dict[str, Any] | None = None
        self._log: DiagnosticsLog | None = None
        self._archive: dict[str, dict[str, Any]] = {}
        self._archive_stamps: dict[str, float] = {}
        self._heartbeat_stop: threading.Event | None = None
        self._heartbeat_thread: threading.Thread | None = None
        self._engine_started_at: float | None = None
        self._restart_blocked_until: float | None = None

    @property
    def model_root(self) -> Path | None:
        return self._model_root

    @property
    def session(self) -> Path | None:
        return self._session

    @property
    def engine_pid(self) -> int | None:
        process = self._process
        return None if process is None else process.pid

    @property
    def failure(self) -> dict[str, Any] | None:
        return self._failure

    @property
    def log_path(self) -> Path | None:
        if self._model_root is None:
            return None
        return logs_directory(self._model_root) / "engine.log"

    def configure(
        self,
        model_root: str | os.PathLike[str],
        license_authorized: bool = False,
    ) -> "BridgeClient":
        if not str(model_root).strip():
            raise ValueError("Model directory is required")

        root = Path(model_root).expanduser().resolve()
        with self._lock:
            self._license_authorized = bool(license_authorized)
            if root == self._model_root and self._is_running():
                self._write_heartbeat()
                return self

            same_root = root == self._model_root
            if same_root:
                # Reached only when the engine process ended. Say why, keep the
                # evidence, and restart at most once instead of resetting.
                self._note_engine_exit()
                if self._restart_is_throttled():
                    return self
            self._retire(reason="engine_exit" if same_root else "model_root_changed")
            if not same_root:
                self._failure = None
                self._archive.clear()
                self._archive_stamps.clear()

            if root.exists() and not root.is_dir():
                raise NotADirectoryError(root)
            root.mkdir(parents=True, exist_ok=True)
            self._model_root = root
            self._log = DiagnosticsLog(logs_directory(root) / "bridge.log")
            self._session = self._new_session(root)
            self._pending.clear()
            self._log.bind(session=self._session.name, pid=os.getpid())
            self._log.event(
                "bridge_session",
                model_root=str(root),
                previous_failure=None if self._failure is None else self._failure.get("reason"),
            )
            self._start()
            self._engine_started_at = time.monotonic()
            self._restart_blocked_until = None
            self._write_heartbeat()
            self._start_heartbeat()
            return self

    def submit(self, action: str, payload: Mapping[str, Any] | None = None) -> str:
        if action not in ALLOWED_ACTIONS or action == "shutdown":
            raise ValueError(f"Unsupported action: {action}")
        if payload is not None and not isinstance(payload, Mapping):
            raise TypeError("Payload must be a mapping")

        values = dict(payload or {})
        if _RESERVED_COMMAND_FIELDS.intersection(values):
            raise ValueError("Payload contains reserved command fields")
        with self._lock:
            self._require_session()
            self._ensure_running()
            identity = _job_identity(values)
            duplicate = self._duplicate_job(action, identity)
            if duplicate is not None:
                # A repeated panel refresh must not queue a second job for the
                # same model, nor restart the engine behind a live download.
                self._log.event("submit_duplicate", action=action, job=duplicate, **identity)
                self._write_heartbeat()
                return duplicate
            job_id = uuid.uuid4().hex
            command = self._base_command(job_id, action)
            command.update(values)
            if action == "download_model":
                command["license_authorized"] = self._license_authorized
            _atomic_json(self._session / "commands" / f"{job_id}.json", command)
            self._pending[job_id] = {
                "job_id": job_id,
                "action": action,
                "status": "queued",
                "progress": 0.0,
                "message": "Queued",
                **identity,
            }
            self._log.event("submit", action=action, job=job_id, **identity)
            self._write_heartbeat()
            return job_id

    def cancel(self, job_id: str) -> None:
        if not _valid_job_id(job_id):
            raise ValueError("Invalid job id")
        with self._lock:
            self._require_session()
            _atomic_json(
                self._session / "signals" / f"{job_id}.cancel",
                {"schema": SCHEMA_VERSION, "job_id": job_id, "created_at": time.time()},
            )
            self._log.event("cancel_requested", job=job_id)
            self._write_heartbeat()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self._session is None:
                return _empty_snapshot()
            alive = self._is_running()
            if alive:
                # Read-only observation. Never restart or wipe here: the UI
                # polls this every half second.
                self._write_heartbeat()
            else:
                self._note_engine_exit()

            jobs: dict[str, dict[str, Any]] = {}
            for state in self._read_archive().values():
                jobs[state["job_id"]] = _normalize_job(state["job_id"], state, None)
            for state in self._live_states().values():
                job_id = state["job_id"]
                jobs[job_id] = _normalize_job(job_id, state, jobs.get(job_id))
                if jobs[job_id]["status"] not in ACTIVE_STATUSES:
                    self._pending.pop(job_id, None)
            for job_id, pending in self._pending.items():
                jobs.setdefault(job_id, dict(pending))

            resources_document = _read_json(self._session / "resources.json")
            resources = _normalize_resources(resources_document)
            storage = _normalize_storage(resources_document.get("storage")) or {
                "path": str(self._model_root),
                "used": None,
                "total": None,
            }
            process = self._process
            return {
                "engine_alive": alive,
                "engine_returncode": None if process is None else process.poll(),
                "engine_pid": None if process is None else process.pid,
                "engine_heartbeat": _read_json(self._session / "engine-heartbeat.json"),
                "session": str(self._session),
                "jobs": jobs,
                "models": _models_from_jobs(jobs),
                "resources": resources,
                "storage": storage,
                "log_path": str(logs_directory(self._model_root) / "engine.log"),
                "bridge_log_path": str(logs_directory(self._model_root) / "bridge.log"),
                "jobs_path": str(jobs_directory(self._model_root)),
                "failure": self._failure,
            }

    def stop(self, reason: str = "bridge_stop") -> None:
        with self._lock:
            self._log_event("bridge_stop", reason=reason)
            self._retire(reason=reason)
            self._failure = None
            self._archive.clear()
            self._archive_stamps.clear()

    def _base_command(self, job_id: str, action: str, *, reason: str | None = None) -> dict[str, Any]:
        self._require_session()
        command = {
            "schema": SCHEMA_VERSION,
            "session": self._session.name,
            "job_id": job_id,
            "action": action,
            "created_at": time.time(),
        }
        if reason is not None:
            command["reason"] = reason
        return command

    def _build_command(self) -> list[str]:
        self._require_session()
        blender = sys.modules.get("bpy")
        python_args = getattr(getattr(blender, "app", None), "python_args", ()) or ()
        return [
            sys.executable,
            *python_args,
            "-I",
            "-s",
            "-u",
            "-c",
            _BOOTSTRAP,
            str(self._project_root),
            "--session",
            str(self._session),
            "--models",
            str(self._model_root),
            "--nonce",
            self._session.name,
            "--client-pid",
            str(os.getpid()),
        ]

    def _new_session(self, root: Path) -> Path:
        session = root / ".sculptr" / "session" / secrets.token_hex(16)
        for name in ("commands", "states", "signals"):
            (session / name).mkdir(parents=True, exist_ok=False)
        return session

    def _start(self) -> None:
        self._require_session()
        stdout_path = logs_directory(self._model_root) / "engine-stdout.log"
        rotate_if_needed(stdout_path, max_bytes=DEFAULT_LOG_BYTES, backups=DEFAULT_BACKUPS)
        self._log_stream = stdout_path.open("ab", buffering=0)
        options: dict[str, Any] = {
            "cwd": str(self._project_root),
            "stdin": subprocess.DEVNULL,
            "stdout": self._log_stream,
            "stderr": subprocess.STDOUT,
            "close_fds": True,
        }
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            startup = subprocess.STARTUPINFO()
            startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startup.wShowWindow = subprocess.SW_HIDE
            options["startupinfo"] = startup
        else:
            options["start_new_session"] = True
        try:
            self._process = subprocess.Popen(self._build_command(), **options)
            self._log.event("engine_started", pid=self._process.pid)
        except Exception:
            self._log_stream.close()
            self._log_stream = None
            raise

    def _retire(self, *, reason: str) -> None:
        """Shut the engine down, keep its evidence, then drop the session."""
        self._stop_heartbeat()
        process = self._process
        session = self._session
        exited = process is None or process.poll() is not None
        if process is not None and not exited and session is not None:
            self._request_shutdown(process, session, reason)
        self._archive_states(session, interrupted=exited)
        if self._log_stream is not None:
            self._log_stream.close()
        self._log_stream = None
        self._process = None
        self._pending.clear()
        if session is not None:
            self._remove_session(session)
        self._session = None
        self._model_root = None
        self._engine_started_at = None
        self._restart_blocked_until = None

    def _request_shutdown(self, process: subprocess.Popen[bytes], session: Path, reason: str) -> None:
        job_id = uuid.uuid4().hex
        try:
            _atomic_json(
                session / "commands" / f"{job_id}.json",
                self._base_command(job_id, "shutdown", reason=reason),
            )
            process.wait(timeout=SHUTDOWN_GRACE_SECONDS)
            return
        except (OSError, subprocess.TimeoutExpired):
            self._log_event("shutdown_grace_exceeded", reason=reason, grace=SHUTDOWN_GRACE_SECONDS)
        process.terminate()
        try:
            process.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            process.kill()
            try:
                process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._log_event("engine_unkillable", pid=process.pid)

    def _archive_states(self, session: Path | None, *, interrupted: bool) -> None:
        """Copy job states to the durable archive before the session is deleted."""
        if session is None or self._model_root is None:
            return
        directory = jobs_directory(self._model_root)
        for state in self._live_states_from(session).values():
            job_id = state["job_id"]
            if interrupted and state.get("status") not in TERMINAL_STATUSES:
                state = {
                    **state,
                    "status": "failed",
                    "phase": "interrupted",
                    "message": _INTERRUPTED_MESSAGE,
                    "error_code": "engine_interrupted",
                    "updated_at": time.time(),
                }
            existing = self._archive.get(job_id)
            if existing is not None and _timestamp(existing) >= _timestamp(state):
                continue
            path = directory / f"{job_id}.json"
            try:
                _atomic_json(path, state)
                stamp = path.stat().st_mtime
            except OSError:
                continue
            self._archive[job_id] = state
            self._archive_stamps[path.name] = stamp
            self._log_event(
                "job_archived",
                job=job_id,
                status=state.get("status"),
                error_code=state.get("error_code"),
            )

    def _live_states(self) -> dict[str, dict[str, Any]]:
        return {} if self._session is None else self._live_states_from(self._session)

    def _live_states_from(self, session: Path) -> dict[str, dict[str, Any]]:
        states: dict[str, dict[str, Any]] = {}
        try:
            paths = sorted(session.glob("states/*.json"))
        except OSError:
            return states
        for path in paths:
            state = _read_json(path)
            job_id = str(state.get("job_id") or path.stem)
            if not _valid_job_id(job_id):
                continue
            state.setdefault("job_id", job_id)
            states[job_id] = state
        return states

    def _read_archive(self) -> dict[str, dict[str, Any]]:
        """Merge persisted terminal states, re-reading only files that changed."""
        if self._model_root is None:
            return self._archive
        directory = jobs_directory(self._model_root)
        try:
            paths = sorted(directory.glob("*.json"))
        except OSError:
            return self._archive
        for path in paths:
            try:
                stamp = path.stat().st_mtime
            except OSError:
                continue
            if self._archive_stamps.get(path.name) == stamp:
                continue
            state = _read_json(path)
            job_id = str(state.get("job_id") or path.stem)
            if not _valid_job_id(job_id):
                continue
            state.setdefault("job_id", job_id)
            self._archive[job_id] = state
            self._archive_stamps[path.name] = stamp
        return self._archive

    def _duplicate_job(
        self, action: str, identity: Mapping[str, Any]
    ) -> str | None:
        model_id = identity.get("model_id")
        if model_id is None:
            return None
        candidates = list(self._pending.values()) + list(self._live_states().values())
        for job in candidates:
            if job.get("action") != action or job.get("model_id") != model_id:
                continue
            if job.get("status") in ACTIVE_STATUSES:
                return str(job.get("job_id"))
        return None

    def _note_engine_exit(self) -> None:
        process = self._process
        if process is None or self._failure is not None:
            return
        if process.poll() is None:
            return
        self._failure = self._engine_failure()
        if self._failure is not None:
            self._log_event("engine_lost", **self._failure)

    def _engine_failure(self) -> dict[str, Any] | None:
        process = self._process
        if process is None:
            return None
        code = process.poll()
        if code is None:
            return None
        record = {} if self._session is None else _read_json(self._session / "engine-exit.json")
        reason = str(record.get("reason") or f"exit_code_{code}")
        detail = f"SculptR engine stopped ({reason}, exit code {code})"
        if self._model_root is not None:
            detail = f"{detail}. Diagnostics: {logs_directory(self._model_root) / 'engine.log'}"
        return {
            "reason": reason,
            "exit_code": code,
            "engine_pid": record.get("pid"),
            "active_jobs": list(record.get("active_jobs") or []),
            "message": detail,
        }

    def _restart_is_throttled(self) -> bool:
        """Delay restarts while an engine is crash-looping instead of spinning."""
        now = time.monotonic()
        if self._restart_blocked_until is not None and now < self._restart_blocked_until:
            self._log_event(
                "engine_restart_deferred",
                retry_in=round(self._restart_blocked_until - now, 2),
            )
            return True
        started = self._engine_started_at
        if started is not None and now - started < RESTART_BACKOFF_SECONDS:
            self._restart_blocked_until = now + RESTART_BACKOFF_SECONDS
            self._log_event(
                "engine_restart_deferred",
                retry_in=RESTART_BACKOFF_SECONDS,
                reason="crash_loop",
            )
            return True
        return False

    def _start_heartbeat(self) -> None:
        self._stop_heartbeat()
        stop = threading.Event()
        self._heartbeat_stop = stop
        thread = threading.Thread(target=self._heartbeat_loop, name="sculptr-heartbeat", daemon=True)
        self._heartbeat_thread = thread
        thread.start()

    def _stop_heartbeat(self) -> None:
        stop = self._heartbeat_stop
        thread = self._heartbeat_thread
        self._heartbeat_stop = None
        self._heartbeat_thread = None
        if stop is not None:
            stop.set()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)

    def _heartbeat_loop(self) -> None:
        interval = max(0.5, BRIDGE_HEARTBEAT_INTERVAL)
        stop = self._heartbeat_stop
        while stop is self._heartbeat_stop and not stop.wait(interval):
            with self._lock:
                if self._session is None:
                    continue
                try:
                    self._write_heartbeat()
                except OSError as error:
                    self._log_event("heartbeat_failed", message=str(error))

    def _ensure_running(self) -> None:
        if self._is_running():
            return
        code = None if self._process is None else self._process.poll()
        reason = None if self._failure is None else self._failure.get("reason")
        detail = "" if reason is None else f" ({reason})"
        raise RuntimeError(f"SculptR engine is not running{detail}; exit code {code}")

    def _is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _require_session(self) -> None:
        if self._session is None or self._model_root is None:
            raise RuntimeError("Configure a model directory first")

    def _write_heartbeat(self) -> None:
        self._require_session()
        _atomic_json(
            self._session / "client-heartbeat.json",
            {
                "schema": SCHEMA_VERSION,
                "session": self._session.name,
                "pid": os.getpid(),
                "updated_at": time.time(),
            },
        )

    def _log_event(self, kind: str, **fields: Any) -> None:
        log = self._log
        if log is not None:
            log.event(kind, **fields)

    def _remove_session(self, session: Path) -> None:
        if self._model_root is None:
            return
        parent = (self._model_root / ".sculptr" / "session").resolve()
        resolved = session.resolve()
        if resolved.parent == parent and len(resolved.name) == 32:
            shutil.rmtree(resolved, ignore_errors=True)


def _timestamp(state: Mapping[str, Any]) -> float:
    try:
        return float(state.get("updated_at", state.get("created_at", 0.0)) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _valid_job_id(value: str) -> bool:
    return len(value) == 32 and all(character in "0123456789abcdef" for character in value)


def _job_identity(payload: Mapping[str, Any]) -> dict[str, Any]:
    model_id = payload.get("model_id") or payload.get("model")
    return {"model_id": str(model_id)} if model_id else {}


def _normalize_job(
    job_id: str,
    state: Mapping[str, Any],
    previous: Mapping[str, Any] | None,
) -> dict[str, Any]:
    result = dict(previous or {})
    result.update(state)
    percent = result.get("percent", 0.0)
    progress = result.get("progress", percent)
    try:
        progress = float(progress)
    except (TypeError, ValueError):
        progress = 0.0
    if "progress" not in result and progress > 1.0:
        progress /= 100.0
    result.update(
        {
            "job_id": job_id,
            "progress": max(0.0, min(1.0, progress)),
            "bytes_done": result.get("bytes_done", result.get("completed_bytes", 0)),
            "bytes_total": result.get("bytes_total", result.get("total_bytes", 0)),
            "message": result.get("message") or result.get("phase", ""),
        }
    )
    result.setdefault("action", "")
    result.setdefault("status", "running")
    return result


def _models_from_jobs(jobs: Mapping[str, Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    models: dict[str, dict[str, Any]] = {}
    updated: dict[str, float] = {}
    for job in jobs.values():
        model_id = job.get("model_id") or job.get("model")
        if not model_id:
            continue
        key = str(model_id)
        timestamp = _timestamp(job)
        if timestamp < updated.get(key, -1.0):
            continue
        status = str(job.get("status", "not_downloaded"))
        action = job.get("action")
        output = job.get("output") if isinstance(job.get("output"), Mapping) else {}
        if status == "running" and action == "check_model":
            status = "checking"
        elif status == "running" and action == "download_model":
            status = "downloading"
        elif status == "completed" and action in {"check_model", "download_model"}:
            status = "installed" if output.get("installed", True) else "not_downloaded"
        models[key] = {
            "status": status,
            "progress": job.get("progress", 0.0),
            "message": job.get("message", ""),
            "detail": job.get("message", ""),
            "job_id": job.get("job_id", ""),
            "bytes_done": job.get("bytes_done", 0),
            "bytes_total": job.get("bytes_total", 0),
        }
        updated[key] = timestamp
    return models


def _normalize_resources(document: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for name in ("vram", "ram"):
        nested = document.get(name, {})
        nested = nested if isinstance(nested, Mapping) else {}
        used = nested.get("used", document.get(f"{name}_used_bytes"))
        total = nested.get("total", document.get(f"{name}_total_bytes"))
        percent = nested.get("percent")
        if percent is None:
            percent = (float(used) / float(total) * 100.0) if used is not None and total else None
        result[name] = {"used": used, "total": total, "percent": percent}
    for name in ("cpu", "gpu"):
        nested = document.get(name, {})
        if isinstance(nested, Mapping) and "percent" in nested:
            percent = nested.get("percent")
        else:
            percent = document.get(f"{name}_percent", nested if isinstance(nested, (int, float)) else None)
        result[name] = {"percent": percent}
    return result


def _normalize_storage(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {
        "path": str(value.get("path", "")),
        "used": value.get("used", value.get("folder_bytes")),
        "total": value.get("total", value.get("disk_total")),
    }


def _empty_snapshot() -> dict[str, Any]:
    return {
        "engine_alive": False,
        "engine_returncode": None,
        "engine_pid": None,
        "engine_heartbeat": {},
        "session": "",
        "jobs": {},
        "models": {},
        "resources": _normalize_resources({}),
        "storage": {"path": "", "used": None, "total": None},
        "log_path": "",
        "bridge_log_path": "",
        "jobs_path": "",
        "failure": None,
    }


_CLIENT = BridgeClient()


def get_client(context: Any = None) -> BridgeClient:
    if context is not None:
        settings = _settings_from_context(context)
        if settings is not None:
            _CLIENT.configure(*settings)
    return _CLIENT


def configure(
    model_root: str | os.PathLike[str],
    license_authorized: bool = False,
) -> BridgeClient:
    return _CLIENT.configure(model_root, license_authorized)


def submit(action: str, payload: Mapping[str, Any] | None = None) -> str:
    return _CLIENT.submit(action, payload)


def cancel(job_id: str) -> None:
    _CLIENT.cancel(job_id)


def snapshot() -> dict[str, Any]:
    return _CLIENT.snapshot()


def stop() -> None:
    _CLIENT.stop()


def _settings_from_context(context: Any) -> tuple[str, bool] | None:
    preferences = getattr(context, "preferences", None)
    addons = getattr(preferences, "addons", None)
    if addons is None:
        return None
    try:
        entries = list(addons.items())
    except (AttributeError, TypeError):
        try:
            entries = [(key, addons[key]) for key in addons.keys()]
        except (AttributeError, KeyError, TypeError):
            return None
    for key, addon in entries:
        if "sculptr" not in str(key).lower():
            continue
        values = getattr(addon, "preferences", None)
        if values is None:
            continue
        for name in ("model_root", "models_directory", "model_directory"):
            root = getattr(values, name, "")
            if root:
                authorized = bool(
                    getattr(
                        values,
                        "license_authorized",
                        getattr(
                            values,
                            "hunyuan_license_authorized",
                            getattr(values, "hunyuan_license_confirmed", False),
                        ),
                    )
                )
                return str(root), authorized
    return None


atexit.register(stop)
