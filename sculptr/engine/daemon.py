from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ..catalog import get_model
from ..diagnostics import DiagnosticsLog, redact
from .hub import DEFAULT_SOCKET_TIMEOUT, HubClient, HubError, LicenseAuthorizationError, RetryPolicy, check_model, download_model
from ..process import EngineAlreadyRunning, EngineLock, process_alive
from .protocol import (
    CancelledError,
    CommandError,
    atomic_write_json,
    make_state,
    read_json,
    validate_command,
)
from .system import ResourceSampler, clear_cache


# A Blender main-thread stall used to read as a dead client after 10 s and killed
# running downloads. The window is now generous and, more importantly, advisory:
# `_client_stale` checks whether the client process is alive before ending a job.
DEFAULT_CLIENT_TIMEOUT = float(os.environ.get("SCULPTR_CLIENT_TIMEOUT", "120"))
DEFAULT_SHUTDOWN_GRACE = float(os.environ.get("SCULPTR_SHUTDOWN_GRACE", "10"))
# Progress-only state writes are coalesced to roughly this interval.
STATE_FLUSH_INTERVAL = float(os.environ.get("SCULPTR_STATE_FLUSH_INTERVAL", "0.35"))
PROGRESS_LOG_INTERVAL = 5.0
ENGINE_HEARTBEAT_INTERVAL = 1.0
JOB_ARCHIVE_LIMIT = 200
# A finished check reports `not_downloaded`, so it closes its writer like a
# terminal state instead of looking interrupted at engine shutdown.
TERMINAL_STATUSES = frozenset(
    {"cancelled", "completed", "failed", "installed", "not_downloaded"}
)
PROGRESS_STATUSES = {"check_model": "checking", "download_model": "downloading"}


class RuntimeNotInstalledError(RuntimeError):
    pass


class StateJournal:
    """Persists job state and keeps terminal state past the session directory.

    The UI process deletes the session directory when it replaces an engine, and
    that used to take every job state with it. Terminal states are mirrored under
    `<models root>/.sculptr/jobs/`, which survives and is readable by the UI and
    by the next engine.
    """

    def __init__(
        self,
        states_directory: Path,
        archive_directory: Path,
        log: DiagnosticsLog,
        *,
        flush_interval: float = STATE_FLUSH_INTERVAL,
    ) -> None:
        self.states_directory = states_directory
        self.archive_directory = archive_directory
        self.log = log
        self.flush_interval = max(0.0, float(flush_interval))

    def persist(self, state: dict[str, Any]) -> None:
        atomic_write_json(self.states_directory / f"{state['job_id']}.json", state)
        if state.get("status") in TERMINAL_STATUSES:
            self.archive(state)

    def archive(self, state: dict[str, Any]) -> None:
        atomic_write_json(self.archive_directory / f"{state['job_id']}.json", state)
        self._prune()

    def _prune(self) -> None:
        try:
            entries = sorted(
                self.archive_directory.glob("*.json"),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return
        for stale in entries[JOB_ARCHIVE_LIMIT:]:
            try:
                stale.unlink()
            except OSError:
                pass


class JobStateWriter:
    """One job's view of the journal: coalesce progress, force transitions."""

    def __init__(
        self,
        command: dict[str, Any],
        journal: StateJournal,
        on_terminal: Callable[[], None],
    ) -> None:
        self._command = command
        self._journal = journal
        self._on_terminal = on_terminal
        self._lock = threading.Lock()
        self._pending: dict[str, Any] | None = None
        self._next_write = 0.0
        self._transition_key: tuple[Any, ...] | None = None
        self._terminal = False

    def update(
        self,
        status: str,
        phase: str,
        message: str,
        *,
        completed_bytes: int = 0,
        total_bytes: int = 0,
        error_code: str | None = None,
        output: str | None = None,
    ) -> dict[str, Any]:
        """Write at once for a status/phase transition or terminal state, else coalesce."""
        state = self._state(status, phase, message, completed_bytes, total_bytes, error_code, output)
        transition = (status, phase, error_code, output)
        with self._lock:
            if self._terminal:
                return state
            now = time.monotonic()
            if transition != self._transition_key or status in TERMINAL_STATUSES or now >= self._next_write:
                self._write_locked(state, transition)
                self._next_write = now + self._journal.flush_interval
            else:
                # Only the newest coalesced snapshot matters; older ones are noise.
                self._pending = state
            if status in TERMINAL_STATUSES:
                self._terminal = True
                self._pending = None
        if status in TERMINAL_STATUSES:
            self._on_terminal()
        return state

    def flush(self) -> None:
        """Persist the newest coalesced snapshot, if one is outstanding."""
        with self._lock:
            if self._pending is not None and not self._terminal:
                state = self._pending
                self._write_locked(
                    state,
                    (state["status"], state["phase"], state.get("error_code"), state.get("output")),
                )
                self._pending = None

    def interrupt(self, reason: str) -> None:
        """Close a job the engine could not finish before it stopped."""
        self.update(
            "failed",
            "interrupted",
            f"Engine stopped ({reason}); partial download preserved for retry",
            error_code="engine_interrupted",
        )

    def _state(
        self,
        status: str,
        phase: str,
        message: str,
        completed_bytes: int,
        total_bytes: int,
        error_code: str | None,
        output: str | None,
    ) -> dict[str, Any]:
        state = make_state(
            self._command["job_id"],
            self._command["action"],
            status,
            phase,
            message,
            completed_bytes=completed_bytes,
            total_bytes=total_bytes,
            error_code=error_code,
            output=output,
        )
        if self._command.get("model_id"):
            state["model_id"] = self._command["model_id"]
        return state

    def _write_locked(self, state: dict[str, Any], transition: tuple[Any, ...]) -> None:
        self._transition_key = transition
        self._journal.persist(state)


class EngineDaemon:
    def __init__(
        self,
        session_directory: Path,
        models_directory: Path,
        nonce: str,
        *,
        hub: HubClient | None = None,
        client_timeout: float | None = None,
        client_pid: int | None = None,
        shutdown_grace: float | None = None,
        log: DiagnosticsLog | None = None,
    ) -> None:
        self.session_directory = session_directory.resolve()
        self.models_directory = models_directory.resolve()
        self.nonce = nonce
        self.log = log or DiagnosticsLog(self.models_directory / ".sculptr" / "logs" / "engine.log")
        self.hub = hub or HubClient(
            socket_timeout=DEFAULT_SOCKET_TIMEOUT, retry=RetryPolicy(), log=self.log
        )
        self.client_timeout = (
            float(client_timeout) if client_timeout is not None else DEFAULT_CLIENT_TIMEOUT
        )
        self.client_pid = client_pid
        self.shutdown_grace = (
            float(shutdown_grace) if shutdown_grace is not None else DEFAULT_SHUTDOWN_GRACE
        )
        self.commands_directory = self.session_directory / "commands"
        self.states_directory = self.session_directory / "states"
        self.signals_directory = self.session_directory / "signals"
        self.archive_directory = self.models_directory / ".sculptr" / "jobs"
        self.journal = StateJournal(self.states_directory, self.archive_directory, self.log)
        self._stop = threading.Event()
        self._threads: set[threading.Thread] = set()
        self._writers: dict[str, JobStateWriter] = {}
        self._cancelled_jobs: set[str] = set()
        self._progress_logged_at: dict[str, float] = {}
        self._active_models: set[str] = set()
        self._lock = threading.Lock()
        self._started_at = time.monotonic()
        self._sampler = ResourceSampler(self.models_directory)
        self._engine_lock: EngineLock | None = None
        self._shutdown_reason = "engine_exit"
        self._stall_reported_at = 0.0

    def run(self, poll_interval: float = 0.1) -> None:
        for directory in (
            self.commands_directory,
            self.states_directory,
            self.signals_directory,
            self.models_directory,
            self.archive_directory,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        self.log.bind(session=self.nonce, pid=os.getpid())
        try:
            self._engine_lock = EngineLock(self.models_directory / ".sculptr" / "engine.lock").acquire()
        except EngineAlreadyRunning as error:
            self.log.event(
                "engine_already_running", holder=error.pid, models=str(self.models_directory)
            )
            raise
        self.log.event(
            "engine_start",
            models=str(self.models_directory),
            session=str(self.session_directory),
            client_pid=self.client_pid,
            client_timeout=self.client_timeout,
            http_timeout=self.hub.socket_timeout,
            http_attempts=self.hub.retry.max_attempts,
        )
        next_resources = 0.0
        next_heartbeat = 0.0
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                if now >= next_heartbeat:
                    self._write_heartbeat()
                    next_heartbeat = now + ENGINE_HEARTBEAT_INTERVAL
                if now >= next_resources:
                    atomic_write_json(self.session_directory / "resources.json", self._sampler.sample())
                    next_resources = now + 1.0
                self._consume_commands()
                if self._client_stale():
                    break
                self._stop.wait(poll_interval)
        finally:
            self._drain_workers()
            self._write_exit_record()
            if self._engine_lock is not None:
                self._engine_lock.release()
                self._engine_lock = None
            self.log.event("engine_exit", reason=self._shutdown_reason)

    def stop(self, reason: str = "stop_called") -> None:
        self._shutdown_reason = reason
        self._stop.set()

    def _write_heartbeat(self) -> None:
        atomic_write_json(
            self.session_directory / "engine-heartbeat.json",
            {"schema": 1, "session": self.nonce, "updated_at": time.time(), "pid": os.getpid()},
        )

    def _client_stale(self) -> bool:
        """End only on a client that is gone, never on one that is merely busy.

        `client-heartbeat.json` is written by the UI process, and Blender runs its
        timers only while the main thread is free. A long import or redraw can age
        that file past the timeout while the client is still alive, so the pid the
        bridge recorded decides whether running jobs are kept.
        """
        heartbeat = self.session_directory / "client-heartbeat.json"
        try:
            age = max(0.0, time.time() - heartbeat.stat().st_mtime)
            observed = True
        except OSError:
            age = max(0.0, time.monotonic() - self._started_at)
            observed = False
        if age <= self.client_timeout:
            return False
        if process_alive(self.client_pid):
            now = time.monotonic()
            if now - self._stall_reported_at >= 30.0:
                self._stall_reported_at = now
                self.log.event(
                    "client_stalled",
                    heartbeat_age=age,
                    heartbeat_present=observed,
                    client_pid=self.client_pid,
                    timeout=self.client_timeout,
                    action="keeping_engine_alive",
                )
            return False
        self._shutdown_reason = "client_gone"
        self.log.event(
            "client_gone",
            heartbeat_age=age,
            heartbeat_present=observed,
            client_pid=self.client_pid,
            timeout=self.client_timeout,
        )
        return True

    def _drain_workers(self) -> None:
        """Cancel running jobs, let them persist state, and join them within a budget."""
        self._stop.set()
        deadline = time.monotonic() + self.shutdown_grace
        for thread in tuple(self._threads):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            thread.join(timeout=min(max(0.0, remaining), 5.0))
        abandoned = sorted(thread.name for thread in self._threads if thread.is_alive())
        if abandoned:
            self.log.event(
                "workers_abandoned", workers=",".join(abandoned), grace=self.shutdown_grace
            )
        for job_id, writer in list(self._writers.items()):
            writer.interrupt(self._shutdown_reason)
            self._writers.pop(job_id, None)

    def _write_exit_record(self) -> None:
        record = {
            "schema": 1,
            "session": self.nonce,
            "pid": os.getpid(),
            "reason": self._shutdown_reason,
            "exited_at": time.time(),
            "active_jobs": sorted(self._writers),
        }
        try:
            atomic_write_json(self.session_directory / "engine-exit.json", record)
        except OSError:
            self.log.event("exit_record_failed", reason=self._shutdown_reason)

    def _consume_commands(self) -> None:
        for path in sorted(self.commands_directory.glob("*.json")):
            try:
                command = validate_command(read_json(path), self.nonce)
            except (OSError, ValueError, json.JSONDecodeError, CommandError) as error:
                self._write_invalid_state(path.stem, str(error))
                path.unlink(missing_ok=True)
                continue
            path.unlink(missing_ok=True)
            if command["action"] == "shutdown":
                self._write_state(command, "completed", "shutdown", "Engine stopped")
                self.stop(command.get("reason") or "shutdown_command")
                return
            model_id = command.get("model_id")
            if model_id and not self._claim_model(model_id):
                self._write_state(
                    command,
                    "failed",
                    "busy",
                    "Model already has an active job",
                    error_code="model_busy",
                )
                continue
            self.log.event(
                "job_start",
                job=command["job_id"],
                action=command["action"],
                model_id=model_id,
            )
            thread = threading.Thread(
                target=self._run_job,
                args=(command,),
                name=f"sculptr-{command['action']}-{command['job_id'][:8]}",
                daemon=True,
            )
            with self._lock:
                self._threads.add(thread)
            thread.start()

    def _claim_model(self, model_id: str) -> bool:
        with self._lock:
            if model_id in self._active_models:
                return False
            self._active_models.add(model_id)
            return True

    def _run_job(self, command: dict[str, Any]) -> None:
        job_id = command["job_id"]
        writer = JobStateWriter(command, self.journal, lambda: self._writers.pop(job_id, None))
        self._writers[job_id] = writer
        action = command["action"]
        running_status = PROGRESS_STATUSES.get(action, "running")
        cancelled = self._cancelled(command, job_id)

        def progress(phase: str, completed: int, total: int, message: str) -> None:
            status = "installed" if phase == "installed" else running_status
            writer.update(
                status,
                phase,
                message,
                completed_bytes=completed,
                total_bytes=total,
            )
            self._log_progress(job_id, action, phase, completed, total, message)

        try:
            if action == "check_model":
                self._check(command, writer, progress, cancelled)
            elif action == "download_model":
                self._download(command, progress, cancelled)
            elif action == "clear_cache":
                with self._lock:
                    busy = any(thread is not threading.current_thread() for thread in self._threads)
                if busy:
                    writer.update(
                        "failed",
                        "busy",
                        "Cache is in use by another job",
                        error_code="cache_busy",
                    )
                    return
                writer.update("running", "clearing", "Clearing cache")
                clear_cache(self.models_directory)
                writer.update("completed", "complete", "Cache cleared")
            else:
                self._inference(command, writer, cancelled)
        except CancelledError:
            interrupted = self._stop.is_set() and not self._cancel_requested(job_id)
            if interrupted:
                writer.update(
                    "failed",
                    "interrupted",
                    f"Engine stopped ({self._shutdown_reason}); partial download preserved for retry",
                    error_code="engine_interrupted",
                )
                self.log.event(
                    "job_interrupted",
                    job=job_id,
                    action=action,
                    reason=self._shutdown_reason,
                )
            else:
                writer.update("cancelled", "cancelled", "Cancelled")
                self.log.event("job_cancelled", job=job_id, action=action)
        except LicenseAuthorizationError as error:
            writer.update(
                "failed",
                "license",
                str(error),
                error_code="license_authorization_required",
            )
            self.log.exception("job_license", fields={"job": job_id, "action": action})
        except RuntimeNotInstalledError:
            writer.update("failed", "runtime", "Runtime not installed", error_code="runtime_not_installed")
            self.log.exception("job_runtime", fields={"job": job_id, "action": action})
        except (HubError, ValueError, OSError, subprocess.SubprocessError) as error:
            writer.update("failed", "error", redact(str(error)), error_code="engine_error")
            self.log.exception("job_failed", fields={"job": job_id, "action": action})
        except Exception as error:  # A worker bug must never drop a job state silently.
            writer.update(
                "failed",
                "error",
                redact(f"Unexpected {error.__class__.__name__}: {error}"),
                error_code="engine_error",
            )
            self.log.exception("job_crashed", fields={"job": job_id, "action": action})
        finally:
            writer.flush()
            model_id = command.get("model_id")
            with self._lock:
                if model_id:
                    self._active_models.discard(model_id)
                self._threads.discard(threading.current_thread())

    def _log_progress(
        self, job_id: str, action: str, phase: str, completed: int, total: int, message: str
    ) -> None:
        now = time.monotonic()
        stamps = self._progress_logged_at
        if now - stamps.get(job_id, 0.0) < PROGRESS_LOG_INTERVAL:
            return
        stamps[job_id] = now
        self.log.event(
            "job_progress",
            job=job_id,
            action=action,
            phase=phase,
            completed_bytes=completed,
            total_bytes=total,
            message=redact(message),
        )

    def _check(
        self,
        command: dict[str, Any],
        writer: JobStateWriter,
        progress,
        cancelled: Callable[[], bool],
    ) -> None:
        model = get_model(command["model_id"])
        directory = self.models_directory / model.model_id
        if not directory.exists():
            writer.update("not_downloaded", "complete", "Not Downloaded")
            return
        writer.update("checking", "metadata", "Reading pinned manifest")
        files = self.hub.resolve_model(model)
        installed, failing_path = check_model(directory, files, progress, cancelled)
        if installed:
            total = sum(item.size for item in files)
            writer.update(
                "installed", "complete", "Installed", completed_bytes=total, total_bytes=total
            )
        else:
            writer.update(
                "not_downloaded",
                "complete",
                f"Missing or invalid: {failing_path}",
            )

    def _download(self, command: dict[str, Any], progress, cancelled: Callable[[], bool]) -> None:
        download_model(
            self.models_directory,
            get_model(command["model_id"]),
            self.hub,
            command.get("license_authorized", False),
            progress,
            cancelled,
        )

    def _inference(self, command: dict[str, Any], writer: JobStateWriter, cancelled: Callable[[], bool]) -> None:
        action = command["action"]
        outputs = (self.models_directory / ".sculptr" / "outputs").resolve()
        output = Path(command["output_directory"]).resolve()
        if output != outputs and outputs not in output.parents:
            raise ValueError("output_directory must be inside the SculptR outputs directory")
        profile = "hi3d" if action == "enhance_detail" else "hunyuan"
        runtime = self.models_directory / ".sculptr" / "runtime" / profile
        interpreter = runtime / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not interpreter.is_file():
            raise RuntimeNotInstalledError()
        job_directory = self.models_directory / ".sculptr" / "cache" / "jobs" / command["job_id"]
        job_directory.mkdir(parents=True, exist_ok=True)
        request_path = job_directory / "request.json"
        runtime_state = job_directory / "runtime-state.json"
        request = {
            **command,
            "model_directory": str(self.models_directory),
        }
        atomic_write_json(request_path, request)
        runner = Path(__file__).with_name("inference_runner.py")
        flags = 0
        options: dict[str, Any] = {"start_new_session": True}
        if os.name == "nt":
            flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
            options = {}
        environment = self._runtime_environment()
        self.log.event("inference_start", job=command["job_id"], action=action, runtime=str(runtime))
        process = subprocess.Popen(
            [
                str(interpreter),
                "-I",
                "-s",
                "-u",
                str(runner),
                "--request",
                str(request_path),
                "--state",
                str(runtime_state),
            ],
            cwd=job_directory,
            env=environment,
            creationflags=flags,
            shell=False,
            **options,
        )
        writer.update("running", "starting", "Starting isolated runtime")
        last_update = 0.0
        while process.poll() is None:
            if cancelled():
                self._terminate(process)
                raise CancelledError()
            if runtime_state.exists() and runtime_state.stat().st_mtime > last_update:
                last_update = runtime_state.stat().st_mtime
                self._forward_runtime_state(command, writer, runtime_state)
            time.sleep(0.2)
        if runtime_state.exists():
            state = read_json(runtime_state)
            self._forward_runtime_state(command, writer, runtime_state)
            if state.get("status") == "completed" and process.returncode == 0:
                self.log.event("inference_completed", job=command["job_id"], action=action)
                return
            message = state.get("message") if isinstance(state.get("message"), str) else "Backend failed"
            code = state.get("error_code") if isinstance(state.get("error_code"), str) else "backend_failed"
            writer.update("failed", "backend", message, error_code=code)
            self.log.event(
                "inference_failed",
                job=command["job_id"],
                action=action,
                code=code,
                returncode=process.returncode,
                message=redact(message),
            )
            return
        raise subprocess.SubprocessError(f"Runtime exited with code {process.returncode}")

    @staticmethod
    def _runtime_environment() -> dict[str, str]:
        allowed = {
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "CUDA_PATH",
            "CUDA_HOME",
            "LD_LIBRARY_PATH",
            "DYLD_LIBRARY_PATH",
            "HOME",
            "USERPROFILE",
            "TEMP",
            "TMP",
            "TMPDIR",
            "NVIDIA_VISIBLE_DEVICES",
            "CUDA_VISIBLE_DEVICES",
        }
        environment = {key: value for key, value in os.environ.items() if key in allowed}
        environment.update({"PYTHONNOUSERSITE": "1", "PYTHONUTF8": "1"})
        return environment

    def _forward_runtime_state(
        self, command: dict[str, Any], writer: JobStateWriter, path: Path
    ) -> None:
        state = read_json(path)
        status = state.get("status")
        if status not in {"running", "completed", "failed"}:
            return
        percent = state.get("percent", 0)
        if not isinstance(percent, int) or isinstance(percent, bool):
            percent = 0
        message = state.get("message") if isinstance(state.get("message"), str) else status.title()
        phase = state.get("phase") if isinstance(state.get("phase"), str) else "backend"
        writer.update(
            status,
            phase,
            message,
            completed_bytes=max(0, min(100, percent)),
            total_bytes=100,
            error_code=state.get("error_code") if isinstance(state.get("error_code"), str) else None,
            output=state.get("output") if isinstance(state.get("output"), str) else None,
        )

    @staticmethod
    def _terminate(process: subprocess.Popen) -> None:
        if process.poll() is not None:
            return
        if os.name == "nt":
            process.terminate()
        else:
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                process.kill()
            else:
                os.killpg(process.pid, signal.SIGKILL)

    def _cancelled(self, command: dict[str, Any], job_id: str) -> Callable[[], bool]:
        """Remember a user cancellation so it is not reported as an interruption."""
        signal_path = self.signals_directory / f"{command['job_id']}.cancel"

        def check() -> bool:
            if signal_path.exists():
                self._cancelled_jobs.add(job_id)
            return self._stop.is_set() or job_id in self._cancelled_jobs

        return check

    def _cancel_requested(self, job_id: str) -> bool:
        return job_id in self._cancelled_jobs

    def _write_invalid_state(self, job_id: str, message: str) -> None:
        if not job_id or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
            for character in job_id
        ):
            job_id = "invalid-command"
        state = make_state(
            job_id, "invalid", "failed", "validation", redact(message), error_code="invalid_command"
        )
        self.journal.persist(state)
        self.log.event("command_rejected", job=job_id, message=redact(message))

    def _write_state(
        self,
        command: dict[str, Any],
        status: str,
        phase: str,
        message: str,
        *,
        completed_bytes: int = 0,
        total_bytes: int = 0,
        error_code: str | None = None,
        output: str | None = None,
    ) -> None:
        """Immediate write for states that must never be coalesced (rejections, shutdown)."""
        state = make_state(
            command["job_id"],
            command["action"],
            status,
            phase,
            message,
            completed_bytes=completed_bytes,
            total_bytes=total_bytes,
            error_code=error_code,
            output=output,
        )
        if command.get("model_id"):
            state["model_id"] = command["model_id"]
        self.journal.persist(state)
        self.log.event(
            "job_state",
            job=command["job_id"],
            status=status,
            phase=phase,
            error_code=error_code,
            message=redact(message),
        )
