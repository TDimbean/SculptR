from __future__ import annotations

import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any


SCHEMA = 1
ALLOWED_ACTIONS = {
    "check_model",
    "download_model",
    "generate",
    "repaint_texture",
    "enhance_detail",
    "clear_cache",
    "shutdown",
}
JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
IMAGE_SUFFIXES = {".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
MESH_SUFFIXES = {".glb", ".gltf", ".obj", ".ply"}


class CancelledError(Exception):
    pass


class CommandError(ValueError):
    pass


def atomic_write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def read_json(path: Path, maximum_bytes: int = 1_048_576) -> dict[str, Any]:
    if path.stat().st_size > maximum_bytes:
        raise ValueError("JSON file is too large")
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object")
    return value


def contained_path(root: Path, relative: str) -> Path:
    candidate = Path(relative)
    if candidate.is_absolute() or not relative or ".." in candidate.parts:
        raise ValueError(f"Unsafe relative path: {relative}")
    resolved_root = root.resolve()
    resolved = (resolved_root / candidate).resolve()
    if not resolved.is_relative_to(resolved_root):
        raise ValueError(f"Path escapes root: {relative}")
    return resolved


def make_state(
    job_id: str,
    action: str,
    status: str,
    phase: str,
    message: str,
    *,
    completed_bytes: int = 0,
    total_bytes: int = 0,
    error_code: str | None = None,
    output: str | None = None,
) -> dict[str, Any]:
    percent = 0
    if total_bytes > 0:
        percent = min(100, max(0, round(completed_bytes * 100 / total_bytes)))
    elif status in {"installed", "completed"}:
        percent = 100
    state: dict[str, Any] = {
        "schema": SCHEMA,
        "job_id": job_id,
        "action": action,
        "status": status,
        "phase": phase,
        "percent": percent,
        "completed_bytes": completed_bytes,
        "total_bytes": total_bytes,
        "message": message,
        "updated_at": time.time(),
    }
    if error_code:
        state["error_code"] = error_code
    if output:
        state["output"] = output
    return state


def validate_command(command: dict[str, Any], expected_session: str) -> dict[str, Any]:
    if command.get("schema") != SCHEMA:
        raise CommandError("Unsupported command schema")
    if command.get("session") != expected_session:
        raise CommandError("Session token does not match")
    job_id = command.get("job_id")
    if not isinstance(job_id, str) or not JOB_ID_PATTERN.fullmatch(job_id):
        raise CommandError("Invalid job ID")
    action = command.get("action")
    if action not in ALLOWED_ACTIONS:
        raise CommandError("Unsupported action")
    if action in {"check_model", "download_model"}:
        if not isinstance(command.get("model_id"), str):
            raise CommandError("Model action requires model_id")
        if action == "download_model" and not isinstance(command.get("license_authorized", False), bool):
            raise CommandError("license_authorized must be boolean")
    if action in {"generate", "repaint_texture", "enhance_detail"}:
        _validate_inference(command, action)
    return command


def _validate_inference(command: dict[str, Any], action: str) -> None:
    inputs = command.get("inputs")
    settings = command.get("settings")
    output_directory = command.get("output_directory")
    if not isinstance(inputs, dict) or not isinstance(settings, dict):
        raise CommandError("Inference requires inputs and settings")
    if not isinstance(output_directory, str) or not Path(output_directory).is_absolute():
        raise CommandError("output_directory must be absolute")
    required = {"front"} if action == "generate" else {"mesh", "front"}
    allowed = {"front", "left", "right", "back"} | ({"mesh"} if action != "generate" else set())
    if set(inputs) - allowed:
        raise CommandError("Unsupported input field")
    for name in required:
        if not inputs.get(name):
            raise CommandError(f"Missing required input: {name}")
    for name, raw_path in inputs.items():
        if raw_path is None:
            continue
        if not isinstance(raw_path, str):
            raise CommandError(f"Invalid input path: {name}")
        path = Path(raw_path)
        suffixes = MESH_SUFFIXES if name == "mesh" else IMAGE_SUFFIXES
        if not path.is_absolute() or not path.is_file() or path.suffix.lower() not in suffixes:
            raise CommandError(f"Invalid input file: {name}")
    if settings.get("quality") not in {"fast", "high", "ultra"}:
        raise CommandError("quality must be fast, high, or ultra")
    faces = settings.get("faces")
    seed = settings.get("seed")
    if not isinstance(faces, int) or isinstance(faces, bool) or not 10_000 <= faces <= 1_000_000:
        raise CommandError("faces must be between 10000 and 1000000")
    if not isinstance(seed, int) or isinstance(seed, bool) or not 0 <= seed <= 2_147_483_647:
        raise CommandError("seed must be between 0 and 2147483647")
    if not isinstance(settings.get("texture"), bool):
        raise CommandError("texture must be boolean")
    if settings.get("texture_size") not in {1024, 2048, 4096}:
        raise CommandError("texture_size must be 1024, 2048, or 4096")
