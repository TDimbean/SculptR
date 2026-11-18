"""Process and file-IPC bridge between Blender and SculptR's engine.

Blender is a *client* of the download engine, never its scheduler. That rule
shapes this module:

* `snapshot()` only observes. It starts nothing, retires nothing, and reconfigures
  nothing, because Blender polls it several times a second and a poll must never
  interrupt a transfer.
* Lifecycle belongs to explicit calls: `configure()` (first start or a deliberate
  model-root change) and `recover()` (restart after the old process is *confirmed*
  gone). Both own their own backoff.
* There is no long-lived Blender heartbeat thread. The engine judges client
  lifetime from the client process id, and the UI writes a heartbeat only as
  rate-limited telemetry so a stalled UI can be reported without being killed.
* What the user asked for is recorded durably *before* a command file is written.
  `<models root>/.sculptr/downloads/intent/` survives Blender closing, so the
  next engine can resume the same logical transfer instead of replaying IPC.

Evidence outlives the session. Logs, engine stdout, and terminal job states live
under `<models root>/.sculptr/{logs,jobs}/` instead of inside the disposable
session directory, so a crash or a restart cannot delete the records needed to
explain it.
"""

from __future__ import annotations

import atexit
import json
import os
import re
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

from .catalog import get_model
from .diagnostics import DEFAULT_BACKUPS, DEFAULT_LOG_BYTES, DiagnosticsLog, rotate_if_needed
from .process import EXITED, probe
from .transfers import (
    ACTIVE,
    DISCARDED,
    DISCARDED_STATE,
    IntentJournal,
    JournalError,
    PAUSED,
    PAUSED_STATE,
    QUEUED,
    RESUMABLE_STATES,
    REQUESTED,
    VERIFYING,
    WAITING,
    merged_records,
)

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
_JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Rate limit for the client heartbeat written from UI calls. It is telemetry for
# "the UI looks stalled" logging, and the engine ignores it when deciding to quit.
BRIDGE_HEARTBEAT_INTERVAL = float(os.environ.get("SCULPTR_BRIDGE_HEARTBEAT", "2.0"))
SHUTDOWN_GRACE_SECONDS = float(os.environ.get("SCULPTR_SHUTDOWN_GRACE", "10.0"))
# An engine that dies again within this window is crash-looping; restarting it
# every poll would burn CPU and hide the original failure.
RESTART_BACKOFF_SECONDS = float(os.environ.get("SCULPTR_ENGINE_RESTART_BACKOFF", "5.0"))

# `paused` is terminal for a *job* but the transfer behind it stays resumable.
ACTIVE_STATUSES = frozenset(
    {"queued", "checking", "downloading", "retrying", "waiting", "verifying", "running"}
)
TERMINAL_STATUSES = frozenset(
    {"cancelled", "completed", "failed", "installed", "not_downloaded", "invalid", "paused"}
)
_TRANSFER_MODEL_STATUS = {
    QUEUED: "queued",
    ACTIVE: "downloading",
    "retrying": "retrying",
    WAITING: "waiting",
    VERIFYING: "verifying",
    PAUSED_STATE: "paused",
    "failed": "failed",
    DISCARDED_STATE: "cancelled",
}
_INTERRUPTED_MESSAGE = "Engine stopped before this job finished; saved bytes resume automatically"


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

    Only `configure`, `recover`, and `stop` change lifecycle. `snapshot` reads,
    and durable transfer records — not the live session — decide what progress
    the UI can show when no engine exists.
    """

    def __init__(self, project_root: str | os.PathLike[str] | None = None) -> None:
        self._project_root = Path(project_root or Path(__file__).resolve().parents[1]).resolve()
        self._lock = threading.RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._log_stream: Any = None
        self._model_root: Path | None = None
        self._configured_root: Path | None = None
        self._session: Path | None = None
        # The hub never asks the user to accept a license, so it always asserts
        # authorization on the transfer intents and commands. The engine keeps
        # the field as a protocol invariant and can only refuse a hand-written
        # command that omits it; nothing reachable from the UI can be refused.
        self._license_authorized = True
        self._keep_system_awake = True
        self._pending: dict[str, dict[str, Any]] = {}
        self._failure: dict[str, Any] | None = None
        self._log: DiagnosticsLog | None = None
        self._intents: IntentJournal | None = None
        self._archive: dict[str, dict[str, Any]] = {}
        self._archive_stamps: dict[str, float] = {}
        self._engine_started_at: float | None = None
        self._restart_blocked_until: float | None = None
        self._heartbeat_written_at = 0.0
        self._transfers_read_at = 0.0
        self._transfers_cache: list[dict[str, Any]] = []
        self._shutdown_done = False

    def __enter__(self) -> "BridgeClient":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Ensure cleanup when used as a context manager."""
        self.shutdown_once("__exit__")

    def shutdown_once(self, reason: str) -> None:
        """Retire the session at most once, so a destructor cannot redo it.

        A destructor runs whenever the last reference to the client goes away,
        and Blender drops that reference while it tears its interpreter down at
        quit: clearing the `sculptr.bridge` module deallocates `_CLIENT`, and
        `__del__` then executes inside a finalizing interpreter whose C state is
        already partly gone. `%TEMP%\blender.crash.txt` recorded exactly that --
        `EXCEPTION_ACCESS_VIOLATION` in `python313.dll` with the Python backtrace
        `__del__` -> `stop` -> `_log_event` -> `event` -> `_emit`, faulting at
        `diagnostics.py:111`, the `datetime.now()` stamp of a second shutdown log.
        Real shutdown belongs to an explicit call (`unregister()` at quit, or
        `stop()` from an operator); the destructor is only a safety net, and this
        flag is what makes the net fire once instead of crashing at teardown.
        """
        if self._shutdown_done:
            return
        self._shutdown_done = True
        try:
            self.stop(reason)
        except Exception:
            pass

    def __del__(self):
        """Retire the session when nothing else retired it, once only."""
        self.shutdown_once("__del__")

    @property
    def model_root(self) -> Path | None:
        return self._model_root

    def _inhibit_sleep(self) -> None:
        """Prevent system sleep while transfers are active.
        
        On Windows: call SetThreadExecutionState with ES_SYSTEM_REQUIRED
        On Linux: use D-Bus to inhibit idle on session bus
        On other platforms: no-op but documented for consistency
        """
        if sys.platform == "win32":
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32
                # ES_SYSTEM_REQUIRED (0x1) prevents system from sleeping
                # SRES_CONTINUOUS (0x2) keeps the flag set after return
                result = kernel32.SetThreadExecutionState(
                    0x00000001 | 0x00000002
                )
                if not result:
                    self._log_event("sleep_inhibit_failed", error="SetThreadExecutionState failed")
            except Exception as e:
                self._log_event("sleep_inhibit_failed", error=str(e))
        elif sys.platform.startswith("linux"):
            try:
                import dbus
                bus = dbus.SystemBus()
                # Inhibit idle on GNOME/Unity/etc. desktop environments
                result = bus.call(
                    "org.freedesktop.Desktop" ".Inhibit",
                    None,  # application_name
                    "SculptR download",  # application_id
                    "Downloading model",  # reason
                    2  # inhibitor_type: IDLE_INHIBIT (value 2)
                )
                if not result:
                    self._log_event("sleep_inhibit_failed", error="D-Bus inhibit failed")
            except Exception as e:
                self._log_event("sleep_inhibit_failed", error=str(e))
        else:
            # Other platforms: no implementation needed
            pass

    @property
    def configured_root(self) -> Path | None:
        """The root the user last chose, even while no engine is running."""
        return self._configured_root or self._model_root

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
        root = self._model_root or self._configured_root
        return None if root is None else logs_directory(root) / "engine.log"

    def configure(
        self,
        model_root: str | os.PathLike[str],
        license_authorized: bool = True,
        *,
        keep_system_awake: bool = True,
    ) -> "BridgeClient":
        """Explicit lifecycle entry: first start, or a deliberate root change.

        Changing the models root while bytes are still being written is refused:
        the session, the lock, and the journal all belong to the old root, and
        moving them mid-transfer is how a download loses its progress. The user
        pauses or discards first.
        """
        if not str(model_root).strip():
            raise ValueError("Model directory is required")

        root = Path(model_root).expanduser().resolve()
        with self._lock:
            # Close any existing log stream before reconfiguring so temp
            # directory cleanup can succeed.
            if self._log_stream is not None:
                try:
                    self._log_stream.close()
                except Exception:
                    pass
                self._log_stream = None
            self._license_authorized = bool(license_authorized)
            self._keep_system_awake = bool(keep_system_awake)
            if root == self._model_root and self._is_running():
                self._configured_root = root
                self._write_heartbeat()
                return self

            same_root = root == self._model_root
            if same_root:
                # Reached only when the engine process ended. Say why, keep the
                # evidence, and restart at most once instead of resetting.
                self._note_engine_exit()
                if self._restart_is_throttled():
                    return self
            elif self._model_root is not None:
                if self.has_busy_work():
                    # Refused: nothing about the current root is touched, so the
                    # unfinished transfers still resume from their own journal.
                    raise RuntimeError(
                        "A download is still running. Pause or cancel it before "
                        "changing the models folder."
                    )
            if same_root:
                self._retire(reason="engine_exit")
            else:
                self._retire(reason="model_root_changed")
                # The failure belongs to the old engine; the job archive stays, so
                # the user can still read what happened to earlier transfers.
                self._failure = None
                self._archive_stamps.clear()

            if root.exists() and not root.is_dir():
                raise NotADirectoryError(root)
            root.mkdir(parents=True, exist_ok=True)
            self._model_root = root
            self._configured_root = root
            self._log = DiagnosticsLog(logs_directory(root) / "bridge.log")
            self._intents = IntentJournal(root)
            self._session = self._new_session(root)
            # A live session re-opens the right to a shutdown: the flag tracks
            # "this session was retired", not "this client ever stopped".
            self._shutdown_done = False
            self._pending.clear()
            self._transfers_cache = []
            self._log.bind(session=self._session.name, pid=os.getpid())
            self._log.event(
                "bridge_session",
                model_root=str(root),
                previous_failure=None if self._failure is None else self._failure.get("reason"),
            )
            self._inhibit_sleep()
            self._start()
            self._engine_started_at = time.monotonic()
            self._restart_blocked_until = None
            self._write_heartbeat(force=True)
            return self

    def recover(self, *, reason: str = "user_recovery") -> dict[str, Any]:
        """Restart deliberately, and only after the old process is confirmed gone.

        `probe()` answers `unknown` when we cannot verify a process; that is not
        proof of death, and starting a second engine against the same models root
        would give two writers to one journal. So recovery refuses until it sees
        `exited`, and otherwise reports what it is waiting for.
        """
        with self._lock:
            root = self._configured_root or self._model_root
            if root is None:
                raise RuntimeError("Configure a model directory first")
            process = self._process
            if process is not None:
                state = probe(process.pid)
                if state != EXITED:
                    self._log_event("recovery_deferred", process_state=state, pid=process.pid)
                    return {
                        "started": False,
                        "reason": "engine_not_confirmed_dead",
                        "process_state": state,
                        "engine_pid": process.pid,
                    }
            if self._restart_is_throttled():
                retry_in = max(0.0, (self._restart_blocked_until or 0.0) - time.monotonic())
                return {
                    "started": False,
                    "reason": "restart_backoff",
                    "retry_in_seconds": round(retry_in, 1),
                }
            self._failure = None
            self._restart_blocked_until = None
            try:
                self.configure(root, self._license_authorized, keep_system_awake=self._keep_system_awake)
            except (OSError, ValueError, RuntimeError) as error:
                # `RuntimeError` is the deliberate refusal to move the root while a
                # download is running: report it as a reason, never as a traceback.
                self._log_event("recovery_failed", message=str(error))
                return {"started": False, "reason": "start_failed", "message": str(error)}
            self._log_event("engine_recovered", reason=reason, pid=self.engine_pid)
            return {"started": True, "engine_pid": self.engine_pid, "reason": reason}

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
                self._log_event("submit_duplicate", action=action, job=duplicate, **identity)
                self._write_heartbeat()
                return duplicate
            job_id = uuid.uuid4().hex
            command = self._base_command(job_id, action)
            command.update(values)
            if action == "download_model":
                command["license_authorized"] = self._license_authorized
                intent = self._record_intent(str(identity.get("model_id") or ""))
                if intent:
                    # Intent is durable *before* the command exists: if Blender
                    # dies mid-submit, the next engine still knows what was asked.
                    command["transfer_id"] = intent["transfer_id"]
            _atomic_json(self._session / "commands" / f"{job_id}.json", command)
            self._pending[job_id] = {
                "job_id": job_id,
                "action": action,
                "status": "queued",
                "progress": 0.0,
                "message": "Queued",
                **identity,
            }
            self._log_event("submit", action=action, job=job_id, **identity)
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
            self._log_event("cancel_requested", job=job_id)
            self._write_heartbeat()

    def pause_transfer(self, transfer_id_value: str) -> dict[str, Any]:
        """Ask the engine to stop writing soon, keeping every committed byte."""
        return self._set_intent_request(transfer_id_value, PAUSED, "transfer_pause")

    def resume_transfer(self, transfer_id_value: str) -> dict[str, Any]:
        """Re-request a paused transfer.

        With a running engine this enqueues a job carrying the same transfer id;
        without one the intent is enough, because the next engine resumes from the
        journal on startup.
        """
        with self._lock:
            record = self._set_intent_request(transfer_id_value, REQUESTED, "transfer_resume")
            model_id = str(record.get("model_id") or "")
            if self._is_running() and model_id:
                try:
                    job_id = self.submit("download_model", {"model_id": model_id, "transfer_id": transfer_id_value})
                    record["job_id"] = job_id
                except (OSError, RuntimeError, ValueError) as error:
                    self._log_event(
                        "transfer_resume_deferred",
                        transfer=transfer_id_value,
                        message=str(error)[:200],
                    )
            return record

    def discard_transfer(self, transfer_id_value: str) -> dict[str, Any]:
        """Confirmed discard: the engine stops the transfer and removes its payload."""
        with self._lock:
            record = self._set_intent_request(transfer_id_value, DISCARDED, "transfer_discard")
            model_id = str(record.get("model_id") or "")
            job_id = self._active_download_job(model_id)
            if job_id is not None:
                try:
                    self.cancel(job_id)
                except (OSError, RuntimeError, ValueError) as error:
                    self._log_event(
                        "transfer_discard_cancel_failed",
                        transfer=transfer_id_value,
                        job=job_id,
                        message=str(error)[:200],
                    )
            return record

    def authorize_transfer(self, transfer_id_value: str, authorized: bool) -> dict[str, Any]:
        """Record a license decision on the intent so a resume can proceed."""
        with self._lock:
            intents = self._require_intents()
            try:
                record = intents.authorize(transfer_id_value, bool(authorized))
            except KeyError as error:
                raise KeyError(f"Unknown transfer: {transfer_id_value}") from error
            self._log_event("transfer_authorized", transfer=transfer_id_value, authorized=bool(authorized))
            return record

    def transfer_records(self) -> list[dict[str, Any]]:
        """Durable intent + engine progress, readable with no engine running."""
        with self._lock:
            root = self._model_root or self._configured_root
            if root is None:
                return []
            now = time.monotonic()
            # Transfer records change at most a few times a second; reading a
            # handful of small files every 0.5s poll would be pure overhead.
            if now - self._transfers_read_at >= 1.0:
                self._transfers_read_at = now
                try:
                    self._transfers_cache = merged_records(root)
                except OSError as error:
                    self._log_event("transfers_read_failed", message=str(error)[:200])
            return [dict(record) for record in self._transfers_cache]

    def has_busy_work(self) -> bool:
        """True when a job is active or a transfer could still be writing."""
        if self._has_active_job():
            return True
        root = self._model_root or self._configured_root
        if root is None:
            return False
        try:
            records = merged_records(root)
        except OSError:
            return False
        return any(str(record.get("state")) in RESUMABLE_STATES for record in records)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self._session is None:
                return self._empty_snapshot()
            alive = self._is_running()
            if alive:
                # Read-only observation. Never restart or wipe here: the UI
                # polls this several times a second.
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
            transfers = self.transfer_records()
            process = self._process
            return {
                "engine_alive": alive,
                "engine_returncode": None if process is None else process.poll(),
                "engine_pid": None if process is None else process.pid,
                "engine_heartbeat": _read_json(self._session / "engine-heartbeat.json"),
                "session": str(self._session),
                "jobs": jobs,
                "models": _models_from_jobs(jobs, transfers),
                "transfers": transfers,
                "transfers_by_model": {
                    str(record["model_id"]): record for record in transfers if record.get("model_id")
                },
                "resources": resources,
                "storage": storage,
                "log_path": str(logs_directory(self._model_root) / "engine.log"),
                "bridge_log_path": str(logs_directory(self._model_root) / "bridge.log"),
                "jobs_path": str(jobs_directory(self._model_root)),
                "failure": self._failure,
            }

    def stop(self, reason: str = "bridge_stop") -> None:
        with self._lock:
            # Marked before any work: an explicit stop is what the destructor
            # would otherwise repeat at interpreter teardown, and repeating it
            # there is what crashes Blender on quit.
            self._shutdown_done = True
            self._log_event("bridge_stop", reason=reason)
            # The engine pauses transfers on shutdown, so retiring the session
            # must not look like a user cancellation.
            self._retire(reason=reason)
            self._failure = None
            self._archive.clear()
            self._archive_stamps.clear()

    def _set_intent_request(
        self, transfer_id_value: str, request: str, event: str
    ) -> dict[str, Any]:
        intents = self._require_intents()
        try:
            record = intents.set_request(transfer_id_value, request)
        except KeyError as error:
            raise KeyError(f"Unknown transfer: {transfer_id_value}") from error
        except JournalError as error:
            raise RuntimeError(f"Cannot record transfer request: {error}") from error
        self._log_event(event, transfer=transfer_id_value, model_id=record.get("model_id"))
        return record

    def _require_intents(self) -> IntentJournal:
        root = self._model_root or self._configured_root
        if root is None:
            raise RuntimeError("Configure a model directory first")
        if self._intents is None or self._intents.models_root != root:
            self._intents = IntentJournal(root)
        return self._intents

    def _record_intent(self, model_id: str) -> dict[str, Any] | None:
        """Persist what the user asked for before any command file exists."""
        if not model_id:
            return None
        try:
            spec = get_model(model_id)
        except (KeyError, ValueError):
            self._log_event("intent_model_unknown", model_id=model_id, severity="warning")
            return None
        try:
            intents = self._require_intents()
            return intents.record(
                model_id,
                spec,
                license_required=bool(getattr(spec, "restricted", False)),
                license_authorized=self._license_authorized,
                request=REQUESTED,
            )
        except (JournalError, ValueError) as error:
            # Without intent the engine still honours the command's own
            # authorization; it just cannot promise recovery after a restart.
            # `ValueError` covers a catalog entry with nothing pinned in it.
            self._log_event(
                "intent_write_failed", model_id=model_id, message=str(error), severity="warning"
            )
            return None

    def _has_active_job(self) -> bool:
        if self._session is None:
            return False
        if not self._is_running():
            # A dead engine is not writing. Leftover pending claims are stale, and
            # treating them as busy would block a folder change forever.
            self._pending.clear()
            return False
        if any(job.get("status") in ACTIVE_STATUSES for job in self._pending.values()):
            return True
        return any(
            state.get("status") in ACTIVE_STATUSES for state in self._live_states().values()
        )

    def _active_download_job(self, model_id: str) -> str | None:
        if not model_id:
            return None
        candidates = list(self._pending.values()) + list(self._live_states().values())
        for job in candidates:
            if job.get("action") == "download_model" and job.get("model_id") == model_id:
                if job.get("status") in ACTIVE_STATUSES:
                    return str(job.get("job_id"))
        return None

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

    def _engine_environment(self) -> dict[str, str]:
        """Pass user-controlled engine behaviour through the child environment.

        The idle-sleep inhibitor is opt-out: holding `ES_SYSTEM_REQUIRED` only
        while a download thread exists prevents a locked-screen power save from
        pausing a multi-gigabyte transfer, and it is released with the thread.
        """
        environment = dict(os.environ)
        environment["SCULPTR_KEEP_SYSTEM_AWAKE"] = "1" if self._keep_system_awake else "0"
        return environment

    def _new_session(self, root: Path) -> Path:
        session = root / ".sculptr" / "session" / secrets.token_hex(16)
        for name in ("commands", "states", "signals"):
            (session / name).mkdir(parents=True, exist_ok=False)
        return session

    def _start(self) -> None:
        self._require_session()
        self._inhibit_sleep()
        stdout_path = logs_directory(self._model_root) / "engine-stdout.log"
        rotate_if_needed(stdout_path, max_bytes=DEFAULT_LOG_BYTES, backups=DEFAULT_BACKUPS)
        self._log_stream = stdout_path.open("ab", buffering=0)
        options: dict[str, Any] = {
            "cwd": str(self._project_root),
            "stdin": subprocess.DEVNULL,
            "stdout": self._log_stream,
            "stderr": subprocess.STDOUT,
            "close_fds": True,
            "env": self._engine_environment(),
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
        process = self._process
        session = self._session
        exited = process is None or process.poll() is not None
        if process is not None and not exited and session is not None:
            self._request_shutdown(process, session, reason)
        # Close log stream before archiving so temp cleanup can succeed.
        if self._log_stream is not None:
            try:
                self._log_stream.close()
            except Exception:
                pass
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
                    "status": "paused",
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
            "interrupted_transfers": list(record.get("interrupted_transfers") or []),
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

    def _ensure_running(self) -> None:
        if self._is_running():
            return
        code = None if self._process is None else self._process.poll()
        reason = None if self._failure is None else self._failure.get("reason")
        detail = "" if reason is None else f" ({reason})"
        raise RuntimeError(
            f"SculptR engine is not running{detail}; exit code {code}. "
            "Use Recover in the SculptR panel to restart it; saved bytes resume."
        )

    def _is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def _require_session(self) -> None:
        if self._session is None or self._model_root is None:
            raise RuntimeError("Configure a model directory first")

    def _write_heartbeat(self, *, force: bool = False) -> None:
        """Telemetry only: the engine never quits because this file is old."""
        if self._session is None:
            return
        now = time.monotonic()
        if not force and now - self._heartbeat_written_at < BRIDGE_HEARTBEAT_INTERVAL:
            return
        self._heartbeat_written_at = now
        try:
            import json
            path = self._session / "client-heartbeat.json"
            with path.open("w", encoding="utf-8") as stream:
                json.dump(
                    {
                        "schema": SCHEMA_VERSION,
                        "session": self._session.name,
                        "pid": os.getpid(),
                        "updated_at": time.time(),
                    },
                    stream,
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
                # Inside the `with`: after it closes, the handle is dead and a
                # write here would raise ValueError, which an `except OSError`
                # would not catch -- that escaped into the UI poll as a
                # flickering "I/O operation on closed file." banner.
                stream.write("\n")
        except (OSError, ValueError) as error:
            # Telemetry only. A heartbeat write must never abort a poll, a
            # download, or the UI, so both disk faults (OSError) and a
            # mishandled stream (ValueError) are recorded and swallowed.
            self._log_event("heartbeat_failed", message=str(error)[:200])

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

    def _empty_snapshot(self) -> dict[str, Any]:
        """With no session, durable transfer records still drive the panel."""
        root = self._configured_root
        transfers = self.transfer_records()
        return {
            "engine_alive": False,
            "engine_returncode": None,
            "engine_pid": None,
            "engine_heartbeat": {},
            "session": "",
            "jobs": {},
            "models": _models_from_jobs({}, transfers),
            "transfers": transfers,
            "transfers_by_model": {
                str(record["model_id"]): record for record in transfers if record.get("model_id")
            },
            "resources": _normalize_resources({}),
            "storage": {"path": str(root or ""), "used": None, "total": None},
            "log_path": str(logs_directory(root) / "engine.log") if root else "",
            "bridge_log_path": str(logs_directory(root) / "bridge.log") if root else "",
            "jobs_path": str(jobs_directory(root)) if root else "",
            "failure": self._failure,
        }


def _timestamp(state: Mapping[str, Any]) -> float:
    try:
        return float(state.get("updated_at", state.get("created_at", 0.0)) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _valid_job_id(value: str) -> bool:
    return bool(_JOB_ID_PATTERN.match(value))


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


def _models_from_jobs(
    jobs: Mapping[str, Mapping[str, Any]], transfers: list[Mapping[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Model rows from job states, overlaid with durable transfer progress.

    The transfer journal is newer than the job state right after an engine exit —
    and exists when no engine ever ran — so progress from retained bytes must
    survive into the panel rather than showing an empty model list.
    """
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
            "transfer_id": job.get("transfer_id", ""),
            "error_code": job.get("error_code", ""),
        }
        updated[key] = timestamp
    for record in transfers:
        model_id = record.get("model_id")
        if not model_id:
            continue
        key = str(model_id)
        timestamp = _timestamp(record)
        if timestamp < updated.get(key, -1.0):
            continue
        status = _TRANSFER_MODEL_STATUS.get(str(record.get("state")), "")
        if not status:
            continue
        done = int(record.get("completed_bytes") or 0)
        total = int(record.get("total_bytes") or 0)
        retry = record.get("retry") if isinstance(record.get("retry"), Mapping) else {}
        models[key] = {
            "status": status,
            "progress": (done / total) if total > 0 else 0.0,
            "message": str(record.get("message") or ""),
            "detail": str(record.get("message") or ""),
            "job_id": "",
            "bytes_done": done,
            "bytes_total": total,
            "transfer_id": str(record.get("transfer_id") or ""),
            "error_code": str(record.get("error_code") or ""),
            "next_attempt_at": retry.get("next_attempt_at"),
            "intent_request": record.get("intent_request"),
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


_CLIENT = BridgeClient()


def get_client(context: Any = None) -> BridgeClient:
    """Return the shared client. Never configures: a poll must not start a process.

    Use `initialize()` from an explicit user action to bind a models root.
    """
    del context
    return _CLIENT


def settings_from_context(context: Any) -> tuple[str, bool, bool] | None:
    """Read the models root and the remaining user settings from Blender preferences.

    The second element is the license authorization the client will assert. The
    hub stopped offering an acceptance switch, so it stays True.
    """
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
                keep_awake = bool(getattr(values, "keep_awake_while_downloading", True))
                return str(root), True, keep_awake
    return None


def initialize(context: Any = None, *, keep_system_awake: bool | None = None) -> BridgeClient:
    """Explicit one-time initialization from a user action, not from a timer.

    Starts the engine when none exists and recovers it when the old process is
    confirmed gone, so panel drawing never decides process lifecycle.
    """
    settings = settings_from_context(context) if context is not None else None
    client = _CLIENT
    if settings is None:
        root = client.configured_root
        if root is None:
            return client
        if client.engine_pid is None:
            client.recover(reason="initialize")
        return client
    root, authorized, awake_from_preferences = settings
    awake = bool(keep_system_awake) if keep_system_awake is not None else awake_from_preferences
    try:
        client.configure(root, authorized, keep_system_awake=awake)
    except RuntimeError as error:
        # Root change refused while bytes are still being written, or a restart
        # deferred by crash-loop backoff: report it instead of forcing it.
        client._log_event("initialize_deferred", message=str(error)[:200])
        if client.engine_pid is None:
            client.recover(reason="initialize")
    return client


def configure(
    model_root: str | os.PathLike[str],
    license_authorized: bool = True,
    *,
    keep_system_awake: bool = True,
) -> BridgeClient:
    return _CLIENT.configure(model_root, license_authorized, keep_system_awake=keep_system_awake)


def recover(*, reason: str = "user_recovery") -> dict[str, Any]:
    return _CLIENT.recover(reason=reason)


def submit(action: str, payload: Mapping[str, Any] | None = None) -> str:
    return _CLIENT.submit(action, payload)


def cancel(job_id: str) -> None:
    _CLIENT.cancel(job_id)


def pause_transfer(transfer_id_value: str) -> dict[str, Any]:
    return _CLIENT.pause_transfer(transfer_id_value)


def resume_transfer(transfer_id_value: str) -> dict[str, Any]:
    return _CLIENT.resume_transfer(transfer_id_value)


def discard_transfer(transfer_id_value: str) -> dict[str, Any]:
    return _CLIENT.discard_transfer(transfer_id_value)


def snapshot() -> dict[str, Any]:
    return _CLIENT.snapshot()


def stop() -> None:
    # `unregister()` and the interpreter exit hook both land here, and Blender
    # can reach the destructor right after them: `shutdown_once` keeps the
    # teardown net from repeating work an explicit call already finished.
    _CLIENT.shutdown_once("bridge_stop")


# The interpreter exit hook is the last-resort shutdown. It stays because the
# module-level singleton guarantees a destructor fires at teardown, and a
# destructor that runs *during* finalization is where Blender's interpreter has
# already partly gone (`blender.crash.txt`: ACCESS_VIOLATION in `python313.dll`
# reached from `__del__` -> `stop` -> `_log_event` -> `event` -> `_emit`).
# `shutdown_once` is what keeps that teardown net from doing the work a second
# time; the work itself belongs to `unregister()` on quit or `stop()` from an
# operator, and the engine also watches the client PID, so a missed hook can
# stall a transfer but never orphan the models root.
atexit.register(stop)
