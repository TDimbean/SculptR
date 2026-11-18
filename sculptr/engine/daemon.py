"""Engine-side transfer scheduler with an outer recovery boundary.

The engine, not Blender, decides when a transfer stops. Blender is a client: its
heartbeat, focus state, and timer cadence are advisory telemetry. A transfer is
paused only when the client process is *confirmed* gone, when the user pauses or
cancels, or when the engine itself is shutting down — never on a missing
heartbeat, and never on a process state we could not determine.

The loop is wrapped so that a transient IPC or disk hiccup cannot end a
multi-gigabyte download: a failed write is retried with backoff and only a
sustained failure stops the engine, and it stops by pausing transfers with their
committed bytes already fsynced.

Progress that the UI reads is telemetry (job states); progress that survives a
crash is the transfer journal in `<models root>/.sculptr/downloads/`. Losing the
first must never lose the second.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from ..catalog import get_model
from ..diagnostics import DiagnosticsLog, redact
from ..process import EXITED, EngineAlreadyRunning, EngineLock, ProcessWatch, set_idle_inhibition
from ..transfers import (
    ACTIVE,
    DISCARDED,
    DISCARDED_STATE,
    INSTALLED,
    IntentJournal,
    JournalError,
    PAUSED,
    PAUSED_STATE,
    QUEUED,
    REQUESTED,
    RESUMABLE_STATES,
    TransferStateJournal,
    VERIFYING,
    WAITING,
    new_attempt_id,
    partial_directory,
    pinned_from_catalog,
    transfer_id,
)
from .hub import (
    DEFAULT_SOCKET_TIMEOUT,
    DiskError,
    DownloadHooks,
    HubClient,
    HubError,
    IntegrityError,
    LicenseAuthorizationError,
    PermanentHTTPError,
    ResumeUnsupported,
    RetryController,
    RetryPolicy,
    TransferPaused,
    check_model,
    download_model,
)
from .protocol import (
    CancelledError,
    CommandError,
    atomic_write_json,
    make_state,
    read_json,
    validate_command,
)
from .system import ResourceSampler, clear_cache


# Advisory only: how old the client heartbeat must be before we log that the UI
# looks stalled. Shutdown depends on the client process, not on this number.
DEFAULT_CLIENT_TIMEOUT = float(os.environ.get("SCULPTR_CLIENT_TIMEOUT", "120"))
DEFAULT_SHUTDOWN_GRACE = float(os.environ.get("SCULPTR_SHUTDOWN_GRACE", "10"))
# Progress-only state writes are coalesced to roughly this interval.
STATE_FLUSH_INTERVAL = float(os.environ.get("SCULPTR_STATE_FLUSH_INTERVAL", "0.35"))
PROGRESS_LOG_INTERVAL = 5.0
ENGINE_HEARTBEAT_INTERVAL = 1.0
INTENT_POLL_INTERVAL = 1.0
# How often the engine re-reads the journal for transfers that must resume without
# a new command: offline waits, backoff windows, and work left by a dead engine.
RESUME_SWEEP_INTERVAL = float(os.environ.get("SCULPTR_RESUME_SWEEP_SECONDS", "30"))
JOB_ARCHIVE_LIMIT = 200
# Downloads are wide and serial-ish: one per model, and a small global cap so one
# 8 GB transfer cannot starve a second request or saturate the link.
MAX_CONCURRENT_DOWNLOADS = min(
    4, max(1, int(os.environ.get("SCULPTR_MAX_CONCURRENT_DOWNLOADS", "2")))
)
# How long the engine tolerates repeated failures writing its own session files
# before it stops. Long enough to ride out a brief disk or antivirus hiccup.
IO_FAILURE_GRACE = float(os.environ.get("SCULPTR_IO_FAILURE_GRACE", "120"))
# A finished check reports `not_downloaded`, so it closes its writer like a
# terminal state instead of looking interrupted at engine shutdown. `paused` is
# terminal for the *job*: the transfer itself stays resumable in the journal.
TERMINAL_STATUSES = frozenset(
    {"cancelled", "completed", "failed", "installed", "not_downloaded", "paused"}
)
PROGRESS_STATUSES = {"check_model": "checking", "download_model": "downloading"}
# Transfer journal states, mapped to the job statuses the UI already renders.
_TRANSFER_STATUS = {
    "queued": "queued",
    ACTIVE: "downloading",
    "retrying": "retrying",
    WAITING: "waiting",
    VERIFYING: "verifying",
    PAUSED_STATE: "paused",
    INSTALLED: "installed",
    "failed": "failed",
    DISCARDED_STATE: "cancelled",
}


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
        """Best-effort telemetry write. The transfer journal holds the truth."""
        try:
            atomic_write_json(self.states_directory / f"{state['job_id']}.json", state)
        except OSError as error:
            # A stalled session write must not cancel a running transfer; the UI
            # can still read durable progress from the transfer journal.
            self.log.event(
                "state_write_failed",
                job=state.get("job_id"),
                status=state.get("status"),
                error=redact(str(error))[:200],
                severity="warning",
            )
        if state.get("status") in TERMINAL_STATUSES:
            self.archive(state)

    def archive(self, state: dict[str, Any]) -> None:
        try:
            atomic_write_json(self.archive_directory / f"{state['job_id']}.json", state)
        except OSError as error:
            self.log.event(
                "state_archive_failed",
                job=state.get("job_id"),
                error=redact(str(error))[:200],
                severity="warning",
            )
            return
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
            "paused",
            "interrupted",
            f"Engine stopped ({reason}); saved bytes are resumable",
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
        if self._command.get("transfer_id"):
            state["transfer_id"] = self._command["transfer_id"]
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
        self.installs_directory = self.models_directory / ".sculptr" / "installs"
        self.journal = StateJournal(self.states_directory, self.archive_directory, self.log)
        # Durable transfer records: the engine is the only writer of these.
        self.transfers = TransferStateJournal(self.models_directory)
        # UI-owned intent records; the engine reads them to honour pause/discard.
        self.intents = IntentJournal(self.models_directory)
        self._stop = threading.Event()
        self._threads: set[threading.Thread] = set()
        self._writers: dict[str, JobStateWriter] = {}
        self._cancelled_jobs: set[str] = set()
        self._pause_flags: dict[str, threading.Event] = {}
        self._job_models: dict[str, str] = {}
        self._records: dict[str, dict[str, Any]] = {}
        self._progress_logged_at: dict[str, float] = {}
        self._active_models: set[str] = set()
        self._lock = threading.Lock()
        self._download_slots = threading.Semaphore(MAX_CONCURRENT_DOWNLOADS)
        self._started_at = time.monotonic()
        self._sampler = ResourceSampler(self.models_directory)
        self._sampler_thread: threading.Thread | None = None
        self._engine_lock: EngineLock | None = None
        self._shutdown_reason = "engine_exit"
        self._stall_reported_at = 0.0
        self._intent_checked_at = 0.0
        self._io_failed_at = 0.0
        self._io_failures = 0
        self._exit_recorded = False
        # A retained process handle (Windows) or pidfd (Linux) gives the engine a
        # client-lifetime answer it can wait on, rather than inferring death from
        # how often the UI writes a file.
        self._watch: ProcessWatch | None = (
            ProcessWatch(client_pid) if client_pid is not None else None
        )

    def run(self, poll_interval: float = 0.1) -> None:
        for directory in (
            self.commands_directory,
            self.states_directory,
            self.signals_directory,
            self.models_directory,
            self.archive_directory,
            self.installs_directory,
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
            client_lifetime_supported=self._watch.supported if self._watch else False,
            http_timeout=self.hub.socket_timeout,
            max_downloads=MAX_CONCURRENT_DOWNLOADS,
        )
        next_heartbeat = 0.0
        next_sweep = 0.0
        try:
            self._start_sampler()
            while not self._stop.is_set():
                try:
                    now = time.monotonic()
                    if now >= next_heartbeat:
                        self._write_heartbeat()
                        next_heartbeat = now + ENGINE_HEARTBEAT_INTERVAL
                    if self._client_gone():
                        break
                    if now >= next_sweep:
                        # Repeated, not just startup: a transfer parked offline or
                        # waiting on backoff must resume while this engine lives,
                        # and a journal fault here is retried on the next sweep
                        # instead of ending the engine before it begins.
                        self._resume_transfers()
                        next_sweep = now + RESUME_SWEEP_INTERVAL
                    self._consume_commands()
                    self._sync_intents()
                    if self._io_failures:
                        # A clean round proves the fault was transient: the grace
                        # window must restart, or a single old failure would age the
                        # engine out minutes later.
                        self._io_failures = 0
                        self._io_failed_at = 0.0
                except (OSError, JournalError) as error:
                    if not self._note_io_failure(error):
                        break
                self._stop.wait(poll_interval)
        except (OSError, JournalError) as error:
            # Sampler or teardown hit storage trouble: pause work, do not cancel it.
            self._note_io_failure(error)
            self._shutdown_reason = "io_unavailable"
        except Exception as error:
            # Anything unexpected still has to leave an exit record behind, or the
            # client sees a silent disappearance instead of a diagnosable crash.
            self.log.exception("engine_crashed", fields={"error": redact(str(error))})
            self._shutdown_reason = "engine_crash"
        finally:
            self._shutdown()

    def stop(self, reason: str = "stop_called") -> None:
        self._shutdown_reason = reason
        self._stop.set()

    def _note_io_failure(self, error: OSError) -> bool:
        """Ride out a transient session/disk failure; stop only if it persists.

        Returns False when the failure is sustained enough that the engine should
        shut down — pausing transfers rather than cancelling them.
        """
        now = time.monotonic()
        self._io_failures += 1
        if self._io_failed_at == 0.0:
            self._io_failed_at = now
        elapsed = now - self._io_failed_at
        delay = min(30.0, 0.5 * (2 ** min(self._io_failures, 6)))
        self.log.event(
            "engine_io_failure",
            error=redact(str(error))[:200],
            consecutive=self._io_failures,
            elapsed=round(elapsed, 1),
            retry_in=round(delay, 1),
            severity="warning",
        )
        if elapsed >= IO_FAILURE_GRACE:
            self.log.event("engine_io_unavailable", elapsed=round(elapsed, 1))
            self._shutdown_reason = "io_unavailable"
            return False
        self._stop.wait(min(delay, 5.0))
        return True

    def _write_heartbeat(self) -> None:
        """Engine liveness for the UI. Failure is telemetry, never fatal."""
        try:
            atomic_write_json(
                self.session_directory / "engine-heartbeat.json",
                {"schema": 1, "session": self.nonce, "updated_at": time.time(), "pid": os.getpid()},
            )
        except OSError as error:
            self.log.event(
                "engine_heartbeat_failed", error=redact(str(error))[:200], severity="warning"
            )

    def _client_gone(self) -> bool:
        """End only on a *confirmed* client exit. Unknown is not dead.

        The UI process can stall (long import, big redraw, locked screen) without
        dying; a stalled UI still owns its downloads and gets them back when it
        resumes. So only `EXITED` from a process we can verify ends the engine.
        """
        if self._watch is None:
            return False
        state = self._watch.state()
        if state == EXITED:
            self._shutdown_reason = "client_gone"
            self.log.event("client_gone", client_pid=self.client_pid)
            return True
        age = self._client_heartbeat_age()
        if age > self.client_timeout and time.monotonic() - self._stall_reported_at >= 30.0:
            self._stall_reported_at = time.monotonic()
            self.log.event(
                "client_stalled",
                heartbeat_age=round(age, 1),
                client_pid=self.client_pid,
                process_state=state,
                action="keeping_engine_alive",
            )
        return False

    def _client_heartbeat_age(self) -> float:
        try:
            return max(0.0, time.time() - (self.session_directory / "client-heartbeat.json").stat().st_mtime)
        except OSError:
            return max(0.0, time.monotonic() - self._started_at)

    def _start_sampler(self) -> None:
        """Resource sampling runs on its own thread, away from the transfer loop.

        `nvidia-smi` and Windows counter reads can block or hit a transient
        sharing violation; neither may delay command handling or a checkpoint.
        """
        thread = threading.Thread(target=self._sample_loop, name="sculptr-resources", daemon=True)
        self._sampler_thread = thread
        thread.start()

    def _sample_loop(self) -> None:
        failures = 0
        while not self._stop.is_set():
            try:
                sample = self._sampler.sample()
                atomic_write_json(self.session_directory / "resources.json", sample)
                failures = 0
                self._stop.wait(1.0)
            except (OSError, JournalError) as error:
                # Transient sharing violation or a removed session directory:
                # back off, report a stale snapshot, keep transferring.
                failures += 1
                delay = min(30.0, 1.0 * (2 ** min(failures, 5)))
                self.log.event(
                    "resources_stale",
                    error=redact(str(error))[:200],
                    consecutive=failures,
                    retry_in=round(delay, 1),
                    severity="warning",
                )
                self._stop.wait(delay)
        if self._sampler_thread is not None:
            self._sampler_thread = None

    def _resume_transfers(self) -> None:
        """Re-open unfinished authorized transfers after a restart, same transfer id.

        Nothing is replayed from the command queue: the journal is the source of
        truth, so a command the UI never re-sends still resumes.
        """
        resumed = 0
        online: bool | None = None
        for record in self.transfers.iter():
            state = record.get("state")
            if state not in RESUMABLE_STATES and state != PAUSED_STATE:
                continue
            identifier = str(record.get("transfer_id") or "")
            if any(item.get("transfer_id") == identifier for item in self._records.values()):
                # Running in this engine already: the journal is current, not stale.
                continue
            intent = self.intents.load(identifier)
            model_id = str(record.get("model_id") or intent.get("model_id") or "")
            if not model_id:
                continue
            if intent.get("request") == DISCARDED:
                # A confirmed discard is the user's final decision and it applies
                # whatever state the transfer is in — including one the user
                # paused earlier. Closing it also removes its payload store.
                self._finish(record, DISCARDED_STATE, phase="discarded", message="Discarded by user")
                self._discard_payload(model_id, record)
                continue
            if state == PAUSED_STATE and intent.get("request") != REQUESTED:
                # A pause is the user's decision. It resumes only when the user
                # asks again, and then from the committed offset — never from zero.
                continue
            retry = record.get("retry") if isinstance(record.get("retry"), dict) else {}
            next_try = retry.get("next_attempt_at")
            if isinstance(next_try, (int, float)) and next_try > time.time():
                # Backoff survives an engine restart. The next sweep picks it up
                # instead of hammering a server that just refused this transfer.
                continue
            if intent.get("request") == PAUSED:
                self._finish(record, PAUSED_STATE, error_code="paused", message="Paused by user")
                continue
            authorization = intent.get("authorization") or record.get("authorization") or {}
            if authorization.get("license_required") and not authorization.get("license_authorized"):
                # Authorized later by the user; the intent keeps the intent paused.
                self._finish(
                    record,
                    PAUSED_STATE,
                    error_code="license_authorization_required",
                    message="License authorization required before resume",
                )
                continue
            if online is None:
                online = self._online_available()
            if not online:
                self._update(record, state=WAITING, error_code="offline", message="Waiting for network")
                continue
            if not self._claim_model(model_id):
                continue
            command = {
                "schema": 1,
                "session": self.nonce,
                "job_id": f"resume-{uuid.uuid4().hex[:12]}",
                "action": "download_model",
                "model_id": model_id,
                "transfer_id": identifier,
                "license_authorized": bool(authorization.get("license_authorized")),
                "resumed_from": state,
            }
            self.log.event(
                "transfer_resume",
                transfer=identifier,
                model_id=model_id,
                from_state=state,
                completed_bytes=record.get("completed_bytes"),
                total_bytes=record.get("total_bytes"),
            )
            self._spawn(command)
            resumed += 1
        if resumed:
            self.log.event("transfers_resumed", count=resumed)

    def _online_available(self) -> bool:
        """Cheap pre-flight so a resume does not start against a dead network.

        Only a reachability failure means offline. Any other fault is treated as
        online: the hub retries with backoff and the no-progress window pauses the
        transfer, which is better than parking every resume on a probe that broke.
        """
        try:
            with socket.create_connection(("huggingface.co", 443), timeout=2.0):
                return True
        except OSError:
            return False
        except Exception as error:
            self.log.event(
                "online_probe_failed",
                error=redact(str(error)),
                severity="warning",
                action="treating_as_online",
            )
            return True

    def _spawn(self, command: dict[str, Any]) -> None:
        self.log.event(
            "job_start",
            job=command["job_id"],
            action=command["action"],
            model_id=command.get("model_id"),
            transfer=command.get("transfer_id"),
        )
        thread = threading.Thread(
            target=self._run_job,
            args=(command,),
            name=f"sculptr-{command['action']}-{command['job_id'][:8]}",
            daemon=True,
        )
        with self._lock:
            self._threads.add(thread)
            self._job_models[str(command["job_id"])] = str(command.get("model_id") or "")
        thread.start()

    def _sync_intents(self) -> None:
        """Honour user pause/discard decisions recorded in the intent journal."""
        now = time.monotonic()
        if now - self._intent_checked_at < INTENT_POLL_INTERVAL:
            return
        self._intent_checked_at = now
        for job_id, record in list(self._records.items()):
            identifier = str(record.get("transfer_id") or "")
            intent = self.intents.load(identifier)
            if not intent:
                continue
            request = intent.get("request")
            if request == PAUSED:
                flag = self._pause_flags.get(job_id)
                if flag is not None and not flag.is_set():
                    flag.set()
                    self.log.event("transfer_pause_requested", transfer=identifier, job=job_id)
            elif request == DISCARDED:
                self._cancelled_jobs.add(job_id)
                self.log.event("transfer_discard_requested", transfer=identifier, job=job_id)
        # A job queued behind a download slot has no journal record yet. It must
        # honour the same decisions, or a pause clicked while queued is ignored
        # until the hub happens to start writing.
        pending = [job_id for job_id in self._pause_flags if job_id not in self._records]
        if not pending:
            return
        for intent in self.intents.iter():
            model_id = str(intent.get("model_id") or "")
            request = intent.get("request")
            if not model_id or request not in (PAUSED, DISCARDED):
                continue
            identifier = str(intent.get("transfer_id") or "")
            for job_id in pending:
                if self._job_models.get(job_id) != model_id:
                    continue
                if request == PAUSED:
                    flag = self._pause_flags.get(job_id)
                    if flag is not None and not flag.is_set():
                        flag.set()
                        self.log.event("transfer_pause_requested", transfer=identifier, job=job_id)
                else:
                    self._cancelled_jobs.add(job_id)
                    self.log.event("transfer_discard_requested", transfer=identifier, job=job_id)

    def _shutdown(self) -> None:
        """Pause work in a recoverable order: evidence first, then join workers."""
        self._stop.set()
        for flag in self._pause_flags.values():
            flag.set()
        interrupted = sorted(self._records)
        # Record what was still in flight *before* draining: a worker that never
        # returns must still leave a resumable record behind.
        self._pause_active(reason=self._shutdown_reason)
        self._write_exit_record(interrupted)
        self._drain_workers()
        # A worker that outlived the drain grace may still hold an in-memory record.
        # Pause it now — before the records are dropped — so nothing is left ACTIVE.
        self._pause_active(reason=self._shutdown_reason)
        self._records.clear()
        self._pause_flags.clear()
        if self._sampler_thread is not None:
            self._sampler_thread.join(timeout=2.0)
            self._sampler_thread = None
        if self._engine_lock is not None:
            if any(thread.is_alive() for thread in self._threads):
                # A worker is still inside a write. Keep the lock until this process
                # exits, which also ends that thread; releasing it early would let a
                # second engine touch the same payload while the first is still busy.
                self.log.event(
                    "engine_lock_held",
                    workers=",".join(t.name for t in self._threads if t.is_alive()),
                    severity="warning",
                )
            else:
                # Release only after every worker has been joined.
                self._engine_lock.release()
                self._engine_lock = None
        if self._watch is not None:
            self._watch.close()
            self._watch = None
        self.log.event("engine_exit", reason=self._shutdown_reason, interrupted=interrupted)

    def _pause_active(self, *, reason: str) -> None:
        """Mark every in-flight transfer paused, keeping its committed offsets."""
        skip = {INSTALLED, DISCARDED_STATE, PAUSED_STATE, "failed", "cancelled"}
        for snapshot in list(self._records.values()):
            identifier = str(snapshot.get("transfer_id") or "")
            if snapshot.get("state") in skip:
                # An already-paused record keeps the reason it was paused for;
                # rewriting it as `engine_stopped` would hide the real cause.
                continue
            try:
                for attempt in range(3):
                    try:
                        # Re-read the durable record: a worker may have committed a
                        # newer checkpoint (or closed the transfer) between the
                        # snapshot above and this write. Writing the snapshot would
                        # rewind recorded bytes; overwriting a closed state would
                        # hide the pause/install/discard reason the worker recorded.
                        record = self.transfers.load(identifier) or snapshot
                        if record.get("state") in skip:
                            break
                        self._update(
                            record,
                            state=PAUSED_STATE,
                            error_code="engine_stopped",
                            message=f"Engine stopped ({reason}); saved bytes are resumable",
                            phase="paused",
                        )
                        break
                    except (OSError, JournalError) as error:
                        if attempt == 2:
                            raise
                        time.sleep(0.2)
            except (OSError, JournalError) as error:
                # An ACTIVE record left behind would look live to the next engine
                # while nothing is writing it, so this failure is an error, not a
                # warning: it needs to be visible in diagnostics.
                self.log.event(
                    "transfer_pause_failed",
                    transfer=identifier,
                    error=redact(str(error)),
                    severity="error",
                )

    def _drain_workers(self) -> None:
        """Let workers commit and exit, join them within a budget, then close writers."""
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

    def _write_exit_record(self, interrupted: list[str]) -> None:
        """Write the exit record *before* draining, so a hung worker still explains itself."""
        record = {
            "schema": 1,
            "session": self.nonce,
            "pid": os.getpid(),
            "reason": self._shutdown_reason,
            "exited_at": time.time(),
            "active_jobs": sorted(self._writers),
            "interrupted_transfers": interrupted,
        }
        try:
            atomic_write_json(self.session_directory / "engine-exit.json", record)
        except OSError:
            self.log.event("exit_record_failed", reason=self._shutdown_reason, severity="warning")
        self._exit_recorded = True

    def _consume_commands(self) -> None:
        try:
            paths = sorted(self.commands_directory.glob("*.json"))
        except OSError as error:
            raise error
        for path in paths:
            try:
                command = validate_command(read_json(path), self.nonce)
            except (OSError, ValueError, json.JSONDecodeError, CommandError) as error:
                self._write_invalid_state(path.stem, str(error))
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            try:
                path.unlink()
            except OSError:
                # A command the UI is still replacing will be read again; leaving
                # it is safer than losing it on a transient sharing violation.
                self.log.event(
                    "command_unlink_failed", job=command["job_id"], severity="warning"
                )
                continue
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
            self._spawn(command)

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
        self._pause_flags.setdefault(job_id, threading.Event())
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
                self._download(command, writer, progress)
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
            if command.get("transfer_id"):
                self._record_cancel(command, writer, job_id)
            else:
                interrupted = self._stop.is_set() and not self._cancel_requested(job_id)
                if interrupted:
                    writer.update(
                        "paused",
                        "interrupted",
                        f"Engine stopped ({self._shutdown_reason}); saved bytes are resumable",
                        error_code="engine_interrupted",
                    )
                    self.log.event(
                        "job_interrupted", job=job_id, action=action, reason=self._shutdown_reason
                    )
                else:
                    writer.update("cancelled", "cancelled", "Cancelled")
                    self.log.event("job_cancelled", job=job_id, action=action)
        except TransferPaused as error:
            # Covers user pause, no-progress window, and a failed checkpoint: all
            # three keep every fsynced byte and wait for an explicit or automatic
            # resume instead of restarting from zero.
            self._record_pause(writer, job_id, error)
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
            code = _failure_code(error)
            writer.update("failed", _failure_phase(error), redact(str(error)), error_code=code)
            self.log.exception(
                "job_failed", fields={"job": job_id, "action": action, "error_code": code}
            )
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
            self._records.pop(job_id, None)
            self._pause_flags.pop(job_id, None)
            self._job_models.pop(job_id, None)

    def _record_cancel(self, command: dict[str, Any], writer: JobStateWriter, job_id: str) -> None:
        """A cancelled download stays resumable: its bytes are kept, its intent waits."""
        record = self._records.get(job_id)
        identifier = command.get("transfer_id")
        intent = self.intents.load(str(identifier)) if identifier else {}
        discarded = intent.get("request") == DISCARDED
        if record is not None:
            try:
                self._finish(
                    record,
                    DISCARDED_STATE if discarded else PAUSED_STATE,
                    error_code="discarded" if discarded else "cancelled",
                    phase="discarded" if discarded else "cancelled",
                    message="Discarded by user"
                    if discarded
                    else "Cancelled; saved bytes kept until you resume or discard",
                )
            except (OSError, JournalError) as error:
                self.log.event(
                    "transfer_close_failed",
                    transfer=identifier,
                    error=redact(str(error))[:200],
                    severity="warning",
                )
        if discarded:
            self._discard_payload(str(command.get("model_id") or ""), record or {})
        writer.update(
            "cancelled",
            "discarded" if discarded else "cancelled",
            "Discarded" if discarded else "Cancelled — saved bytes kept",
            error_code="discarded" if discarded else "cancelled",
        )
        self.log.event(
            "transfer_cancelled",
            job=job_id,
            transfer=identifier,
            discarded=bool(discarded),
        )

    def _record_pause(self, writer: JobStateWriter, job_id: str, error: TransferPaused) -> None:
        record = self._records.get(job_id)
        message = redact(str(error))[:200] or "Transfer paused"
        if record is not None:
            if isinstance(error, ResumeUnsupported) and error.local_path:
                # This server will not honour ranges for this file. Recording that
                # fact makes the next resume restart the file from zero instead of
                # pausing on the same answer a second time.
                try:
                    record = self.transfers.mark_file(
                        record, error.local_path, resume_supported=False
                    )
                    self._records[job_id] = record
                except (OSError, JournalError) as mark_error:
                    self.log.event(
                        "transfer_mark_failed",
                        transfer=record.get("transfer_id"),
                        error=redact(str(mark_error))[:200],
                        severity="warning",
                    )
            try:
                self._finish(
                    record,
                    PAUSED_STATE,
                    error_code=error.reason,
                    phase="paused",
                    message=message,
                )
            except (OSError, JournalError) as write_error:
                self.log.event(
                    "transfer_pause_failed",
                    transfer=record.get("transfer_id"),
                    error=redact(str(write_error))[:200],
                    severity="warning",
                )
        writer.update(
            "paused",
            "paused",
            f"{message} — resume when ready",
            error_code=error.reason,
            completed_bytes=int((record or {}).get("completed_bytes") or 0),
            total_bytes=int((record or {}).get("total_bytes") or 0),
        )
        self.log.event(
            "transfer_paused", job=job_id, reason=error.reason, message=redact(str(error))[:200]
        )

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

    def _download(
        self,
        command: dict[str, Any],
        writer: JobStateWriter,
        progress: Callable[[str, int, int, str], None],
    ) -> None:
        """One transfer for one model: journal open, bounded slots, hub, close state."""
        model = get_model(command["model_id"])
        job_id = command["job_id"]
        identifier = str(command.get("transfer_id") or "") or transfer_id(
            model.model_id, pinned_from_catalog(model)
        )
        intent = self.intents.load(identifier)
        if not command.get("transfer_id"):
            # The UI records intent before it enqueues. A command without an
            # intent is a legacy or hand-written one: proceed with the
            # authorization it carries, and say so in the log.
            self.log.event("intent_missing", transfer=identifier, job=job_id, severity="warning")
        authorization = intent.get("authorization") or {
            "license_required": bool(model.restricted),
            "license_authorized": bool(command.get("license_authorized")),
        }
        if authorization.get("license_required") and not authorization.get("license_authorized"):
            raise LicenseAuthorizationError("License authorization required before download")
        # Bounded global concurrency, released on user cancel so a queued job that
        # the user abandons does not sit waiting for a slot it no longer needs.
        user_cancelled = self._user_cancelled(job_id, command)
        while not self._download_slots.acquire(timeout=0.5):
            if user_cancelled():
                raise CancelledError()
            if self._stop.is_set():
                # Shutdown is not a cancel. A queued job must stop waiting for a
                # slot it will never receive and leave its transfer resumable,
                # instead of consuming the whole drain grace and then being abandoned.
                raise TransferPaused(
                    "engine_stopped",
                    "Engine stopped before this transfer could start; saved bytes are resumable.",
                )
        try:
            manifest = self.hub.resolve_model(model)
            entries = [
                {
                    "repo_id": item.repo_id,
                    "revision": item.revision,
                    "remote_path": item.remote_path,
                    "local_path": item.local_path,
                    "size": item.size,
                    "sha256": item.sha256,
                    "git_sha1": item.git_sha1,
                }
                for item in manifest
            ]
            record = self.transfers.begin(
                {
                    "transfer_id": identifier,
                    "model_id": model.model_id,
                    "authorization": authorization,
                },
                entries,
                attempt_id=new_attempt_id(),
                session_id=self.nonce,
                state=ACTIVE,
            )
            self._records[job_id] = record
            hooks = DownloadHooks(
                progress=self._progress_callback(job_id, progress),
                checkpoint=self._checkpoint_callback(job_id),
                paused=self._pause_callback(job_id),
                state=self._state_callback(job_id, writer),
                cancelled=user_cancelled,
            )
            writer.update(
                "downloading",
                "downloading",
                "Downloading",
                completed_bytes=int(record.get("completed_bytes") or 0),
                total_bytes=int(record.get("total_bytes") or 0),
            )
            download_model(
                self.models_directory,
                model,
                self.hub,
                progress,
                user_cancelled,
                marker_path=self.installs_directory / f"{model.model_id}.json",
                log=self.log,
                hooks=hooks,
                controller=RetryController(),
                file_states=record.get("files") or {},
                model_id=model.model_id,
            )
            # The checkpoint and state callbacks keep `_records[job_id]` current:
            # finishing from the `begin()` snapshot would rewind the offsets and
            # report an installed transfer as if only the resumed prefix existed.
            current = self._records.get(job_id) or record
            self._records[job_id] = self._finish(
                current, INSTALLED, phase="installed", message="Installed"
            )
        finally:
            self._download_slots.release()

    def _progress_callback(self, job_id: str, progress) -> Callable[[int], None]:
        def report(completed: int) -> None:
            record = self._records.get(job_id) or {}
            progress(
                "downloading",
                int(completed),
                int(record.get("total_bytes") or 0),
                f"Downloading {record.get('model_id') or ''}",
            )

        return report

    def _checkpoint_callback(self, job_id: str) -> Callable[[str, int, str | None], None]:
        """The hub has already fsynced these bytes; record the offset durably."""

        def commit(local_path: str, offset: int, etag: str | None) -> None:
            record = self._records.get(job_id)
            if record is None or record.get("state") == DISCARDED_STATE:
                return
            size = int((record.get("files") or {}).get(local_path, {}).get("size") or 0)
            # During shutdown these bytes were still really fsynced, so the offset is
            # recorded. The state stays paused, though: a late checkpoint from a
            # worker the engine already gave up on must not make it look live.
            stopping = self._stop.is_set()
            record = self.transfers.checkpoint(
                record,
                local_path,
                offset,
                etag=etag,
                state=PAUSED_STATE if stopping else ACTIVE,
                phase="paused" if stopping else "downloading",
            )
            if size and offset >= size:
                # Bytes are complete and fsynced: mark them verified before the hub
                # promotes the payload, so a crash mid-promotion is recoverable.
                record = self.transfers.mark_file(record, local_path, verified=True)
            self._records[job_id] = record

        return commit

    def _state_callback(self, job_id: str, writer) -> Any:
        def notice(state: str, error_code: str | None, message: str, retry_at: float | None) -> None:
            record = self._records.get(job_id)
            if record is None:
                return
            controller = record.get("retry") or {}
            retry = dict(controller)
            retry["next_attempt_at"] = retry_at
            retry["last_error_code"] = error_code
            retry["last_error"] = message[:200] if message else retry.get("last_error")
            record = self._update(record, state=state, error_code=error_code, message=message, retry=retry)
            self._records[job_id] = record
            status = _TRANSFER_STATUS.get(state, "downloading")
            writer.update(
                status,
                state,
                _state_message(state, message, retry_at),
                completed_bytes=int(record.get("completed_bytes") or 0),
                total_bytes=int(record.get("total_bytes") or 0),
                error_code=error_code,
            )

        return notice

    def _pause_callback(self, job_id: str) -> Callable[[], bool]:
        """Shutdown and user pause both mean: checkpoint, stop writing, stay resumable."""

        def paused() -> bool:
            flag = self._pause_flags.get(job_id)
            return self._stop.is_set() or (flag is not None and flag.is_set())

        return paused

    def _user_cancelled(self, job_id: str, command: dict[str, Any]) -> Callable[[], bool]:
        """Only a user decision cancels a download; engine stop pauses it instead."""
        signal_path = self.signals_directory / f"{command['job_id']}.cancel"

        def check() -> bool:
            if signal_path.exists():
                self._cancelled_jobs.add(job_id)
            return job_id in self._cancelled_jobs

        return check

    def _update(self, record: dict[str, Any], **changes: Any) -> dict[str, Any]:
        updated = self.transfers.update(record, **changes)
        return updated

    def _finish(
        self, record: dict[str, Any], state: str, *, error_code: str | None = None,
        message: str = "", phase: str = "",
    ) -> dict[str, Any]:
        return self.transfers.finish(record, state, error_code=error_code, message=message, phase=phase)

    def _discard_payload(self, model_id: str, record: dict[str, Any]) -> None:
        """Confirmed discard: remove the payload store for that model, never the install."""
        if not model_id:
            return
        directory = partial_directory(self.models_directory, model_id)
        if not directory.is_dir():
            return
        try:
            for path in sorted(directory.rglob("*"), key=lambda item: len(item.parts), reverse=True):
                if path.is_dir():
                    continue
                path.unlink()
            directory.rmdir()
        except OSError as error:
            self.log.event(
                "payload_discard_failed",
                model_id=model_id,
                error=redact(str(error))[:200],
                severity="warning",
            )
            return
        self.log.event("payload_discarded", model_id=model_id, files=len(record.get("files") or {}))

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
        """Remember a user cancellation so it is not reported as an interruption.

        Inference jobs also stop when the engine stops: their output is worthless
        without a waiting client. Downloads use `_user_cancelled` instead, which
        ignores engine shutdown so the bytes are checkpointed instead of thrown away.
        """
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


def _state_message(state: str, message: str, retry_at: float | None) -> str:
    """Actionable UI text: say what is happening and when the next attempt is."""
    if state == "retrying" and retry_at:
        seconds = max(1, int(round(retry_at - time.time())))
        return f"Retrying in {seconds}s — {message}" if message else f"Retrying in {seconds}s"
    if state == WAITING:
        return message or "Waiting for network"
    return message or state.title()


def _failure_code(error: BaseException) -> str:
    """Distinguish network trouble from storage trouble from integrity failure."""
    if isinstance(error, PermanentHTTPError):
        return f"http_{error.code}"
    if isinstance(error, ResumeUnsupported):
        return "resume_unsupported"
    if isinstance(error, IntegrityError):
        return "integrity_mismatch"
    if isinstance(error, DiskError):
        text = str(error).lower()
        if "free space" in text or "no space" in text:
            return "disk_full"
        return "disk_write"
    if isinstance(error, HubError):
        return "hub_error"
    if isinstance(error, subprocess.SubprocessError):
        return "backend_failed"
    if isinstance(error, OSError):
        return "io_error"
    return "engine_error"


def _failure_phase(error: BaseException) -> str:
    if isinstance(error, ResumeUnsupported):
        return "resume"
    if isinstance(error, DiskError):
        return "storage"
    if isinstance(error, IntegrityError):
        return "verifying"
    if isinstance(error, PermanentHTTPError):
        return "network"
    return "error"
