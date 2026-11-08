from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from ..catalog import get_model
from .hub import (
    HubClient,
    HubError,
    LicenseAuthorizationError,
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


class RuntimeNotInstalledError(RuntimeError):
    pass


class EngineDaemon:
    def __init__(
        self,
        session_directory: Path,
        models_directory: Path,
        nonce: str,
        *,
        hub: HubClient | None = None,
        client_timeout: float = 10.0,
    ):
        self.session_directory = session_directory.resolve()
        self.models_directory = models_directory.resolve()
        self.nonce = nonce
        self.hub = hub or HubClient()
        self.client_timeout = client_timeout
        self.commands_directory = self.session_directory / "commands"
        self.states_directory = self.session_directory / "states"
        self.signals_directory = self.session_directory / "signals"
        self._stop = threading.Event()
        self._threads: set[threading.Thread] = set()
        self._active_models: set[str] = set()
        self._lock = threading.Lock()
        self._started_at = time.monotonic()
        self._sampler = ResourceSampler(self.models_directory)

    def run(self, poll_interval: float = 0.1) -> None:
        for directory in (
            self.commands_directory,
            self.states_directory,
            self.signals_directory,
            self.models_directory,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        next_resources = 0.0
        while not self._stop.is_set():
            self._write_heartbeat()
            now = time.monotonic()
            if now >= next_resources:
                atomic_write_json(self.session_directory / "resources.json", self._sampler.sample())
                next_resources = now + 1.0
            self._consume_commands()
            if self._client_stale():
                self._stop.set()
                break
            self._stop.wait(poll_interval)
        for thread in tuple(self._threads):
            thread.join(timeout=2)

    def stop(self) -> None:
        self._stop.set()

    def _write_heartbeat(self) -> None:
        atomic_write_json(
            self.session_directory / "engine-heartbeat.json",
            {"schema": 1, "session": self.nonce, "updated_at": time.time(), "pid": os.getpid()},
        )

    def _client_stale(self) -> bool:
        heartbeat = self.session_directory / "client-heartbeat.json"
        if not heartbeat.exists():
            return time.monotonic() - self._started_at > self.client_timeout
        try:
            return time.time() - heartbeat.stat().st_mtime > self.client_timeout
        except OSError:
            return False

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
                self._stop.set()
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
            thread = threading.Thread(target=self._run_job, args=(command,), daemon=True)
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
        try:
            action = command["action"]
            if action == "check_model":
                self._check(command)
            elif action == "download_model":
                self._download(command)
            elif action == "clear_cache":
                with self._lock:
                    busy = any(thread is not threading.current_thread() for thread in self._threads)
                if busy:
                    self._write_state(
                        command,
                        "failed",
                        "busy",
                        "Cache is in use by another job",
                        error_code="cache_busy",
                    )
                    return
                self._write_state(command, "running", "clearing", "Clearing cache")
                clear_cache(self.models_directory)
                self._write_state(command, "completed", "complete", "Cache cleared")
            else:
                self._inference(command)
        except CancelledError:
            self._write_state(command, "cancelled", "cancelled", "Cancelled")
        except LicenseAuthorizationError as error:
            self._write_state(
                command,
                "failed",
                "license",
                str(error),
                error_code="license_authorization_required",
            )
        except RuntimeNotInstalledError:
            self._write_state(
                command,
                "failed",
                "runtime",
                "Runtime not installed",
                error_code="runtime_not_installed",
            )
        except (HubError, ValueError, OSError, subprocess.SubprocessError) as error:
            self._write_state(command, "failed", "error", str(error), error_code="engine_error")
        finally:
            model_id = command.get("model_id")
            with self._lock:
                if model_id:
                    self._active_models.discard(model_id)
                self._threads.discard(threading.current_thread())

    def _check(self, command: dict[str, Any]) -> None:
        model = get_model(command["model_id"])
        directory = self.models_directory / model.model_id
        if not directory.exists():
            self._write_state(command, "not_downloaded", "complete", "Not Downloaded")
            return
        self._write_state(command, "checking", "metadata", "Reading pinned manifest")
        files = self.hub.resolve_model(model)

        def progress(phase: str, completed: int, total: int, message: str) -> None:
            self._write_state(
                command,
                "checking",
                phase,
                message,
                completed_bytes=completed,
                total_bytes=total,
            )

        installed, failing_path = check_model(directory, files, progress, self._cancelled(command))
        if installed:
            total = sum(item.size for item in files)
            self._write_state(
                command,
                "installed",
                "complete",
                "Installed",
                completed_bytes=total,
                total_bytes=total,
            )
        else:
            self._write_state(
                command,
                "not_downloaded",
                "complete",
                f"Missing or invalid: {failing_path}",
            )

    def _download(self, command: dict[str, Any]) -> None:
        model = get_model(command["model_id"])

        def progress(phase: str, completed: int, total: int, message: str) -> None:
            status = "installed" if phase == "installed" else "downloading"
            self._write_state(
                command,
                status,
                phase,
                message,
                completed_bytes=completed,
                total_bytes=total,
            )

        download_model(
            self.models_directory,
            model,
            self.hub,
            command.get("license_authorized", False),
            progress,
            self._cancelled(command),
        )

    def _inference(self, command: dict[str, Any]) -> None:
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
        self._write_state(command, "running", "starting", "Starting isolated runtime")
        cancelled = self._cancelled(command)
        last_update = 0.0
        while process.poll() is None:
            if cancelled():
                self._terminate(process)
                raise CancelledError()
            if runtime_state.exists() and runtime_state.stat().st_mtime > last_update:
                last_update = runtime_state.stat().st_mtime
                self._forward_runtime_state(command, runtime_state)
            time.sleep(0.2)
        if runtime_state.exists():
            state = read_json(runtime_state)
            self._forward_runtime_state(command, runtime_state)
            if state.get("status") == "completed" and process.returncode == 0:
                return
            message = state.get("message") if isinstance(state.get("message"), str) else "Backend failed"
            code = state.get("error_code") if isinstance(state.get("error_code"), str) else "backend_failed"
            self._write_state(command, "failed", "backend", message, error_code=code)
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

    def _forward_runtime_state(self, command: dict[str, Any], path: Path) -> None:
        state = read_json(path)
        status = state.get("status")
        if status not in {"running", "completed", "failed"}:
            return
        percent = state.get("percent", 0)
        if not isinstance(percent, int) or isinstance(percent, bool):
            percent = 0
        message = state.get("message") if isinstance(state.get("message"), str) else status.title()
        phase = state.get("phase") if isinstance(state.get("phase"), str) else "backend"
        self._write_state(
            command,
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

    def _cancelled(self, command: dict[str, Any]):
        signal_path = self.signals_directory / f"{command['job_id']}.cancel"
        return lambda: self._stop.is_set() or signal_path.exists()

    def _write_invalid_state(self, job_id: str, message: str) -> None:
        if not job_id or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for character in job_id):
            job_id = "invalid-command"
        state = make_state(job_id, "invalid", "failed", "validation", message, error_code="invalid_command")
        atomic_write_json(self.states_directory / f"{job_id}.json", state)

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
        atomic_write_json(self.states_directory / f"{command['job_id']}.json", state)
