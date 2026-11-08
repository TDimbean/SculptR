"""Process and file-IPC bridge between Blender and SculptR's engine."""

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
    """Own one isolated engine process and its nonce-scoped session."""

    def __init__(self, project_root: str | os.PathLike[str] | None = None) -> None:
        self._project_root = Path(project_root or Path(__file__).resolve().parents[1]).resolve()
        self._lock = threading.RLock()
        self._process: subprocess.Popen[bytes] | None = None
        self._log_stream: Any = None
        self._model_root: Path | None = None
        self._session: Path | None = None
        self._license_authorized = False
        self._pending: dict[str, dict[str, Any]] = {}

    @property
    def model_root(self) -> Path | None:
        return self._model_root

    @property
    def session(self) -> Path | None:
        return self._session

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

            self.stop()
            if root.exists() and not root.is_dir():
                raise NotADirectoryError(root)
            root.mkdir(parents=True, exist_ok=True)
            self._model_root = root
            self._session = root / ".sculptr" / "session" / secrets.token_hex(16)
            for name in ("commands", "states", "signals"):
                (self._session / name).mkdir(parents=True, exist_ok=False)
            self._pending.clear()
            self._start()
            self._write_heartbeat()
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
                **_job_identity(values),
            }
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
            self._write_heartbeat()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            if self._session is None:
                return _empty_snapshot()
            if self._is_running():
                self._write_heartbeat()

            jobs = dict(self._pending)
            for path in sorted((self._session / "states").glob("*.json")):
                state = _read_json(path)
                job_id = str(state.get("job_id") or path.stem)
                if not _valid_job_id(job_id):
                    continue
                jobs[job_id] = _normalize_job(job_id, state, jobs.get(job_id))
                if jobs[job_id]["status"] not in {"queued", "checking", "downloading", "running"}:
                    self._pending.pop(job_id, None)

            resources_document = _read_json(self._session / "resources.json")
            resources = _normalize_resources(resources_document)
            storage = _normalize_storage(resources_document.get("storage")) or {
                "path": str(self._model_root),
                "used": None,
                "total": None,
            }
            process = self._process
            return {
                "engine_alive": self._is_running(),
                "engine_returncode": None if process is None else process.poll(),
                "engine_heartbeat": _read_json(self._session / "engine-heartbeat.json"),
                "session": str(self._session),
                "jobs": jobs,
                "models": _models_from_jobs(jobs),
                "resources": resources,
                "storage": storage,
            }

    def stop(self) -> None:
        with self._lock:
            process = self._process
            session = self._session
            if process is not None and process.poll() is None and session is not None:
                job_id = uuid.uuid4().hex
                try:
                    _atomic_json(
                        session / "commands" / f"{job_id}.json",
                        self._base_command(job_id, "shutdown"),
                    )
                    process.wait(timeout=2.0)
                except (OSError, subprocess.TimeoutExpired):
                    process.terminate()
                    try:
                        process.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2.0)

            if self._log_stream is not None:
                self._log_stream.close()
            self._log_stream = None
            self._process = None
            self._pending.clear()
            if session is not None:
                self._remove_session(session)
            self._session = None
            self._model_root = None

    def _base_command(self, job_id: str, action: str) -> dict[str, Any]:
        self._require_session()
        return {
            "schema": SCHEMA_VERSION,
            "session": self._session.name,
            "job_id": job_id,
            "action": action,
            "created_at": time.time(),
        }

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
        ]

    def _start(self) -> None:
        self._require_session()
        self._log_stream = (self._session / "engine.log").open("ab", buffering=0)
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
        except Exception:
            self._log_stream.close()
            self._log_stream = None
            raise

    def _ensure_running(self) -> None:
        if self._is_running():
            return
        code = None if self._process is None else self._process.poll()
        raise RuntimeError(f"SculptR engine is not running (exit code {code})")

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

    def _remove_session(self, session: Path) -> None:
        if self._model_root is None:
            return
        parent = (self._model_root / ".sculptr" / "session").resolve()
        resolved = session.resolve()
        if resolved.parent == parent and len(resolved.name) == 32:
            shutil.rmtree(resolved, ignore_errors=True)


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
        timestamp = float(job.get("updated_at", job.get("created_at", 0.0)) or 0.0)
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
        "engine_heartbeat": {},
        "session": "",
        "jobs": {},
        "models": {},
        "resources": _normalize_resources({}),
        "storage": {"path": "", "used": None, "total": None},
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
