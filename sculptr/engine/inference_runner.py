from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Callable


BACKEND_MODULES = {
    "generate": "sculptr_hunyuan_backend",
    "repaint_texture": "sculptr_hunyuan_backend",
    "enhance_detail": "sculptr_hi3d_backend",
}


def _atomic_write(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    path.parent.mkdir(parents=True, exist_ok=True)
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _write_state(state_path: Path, status: str, phase: str, percent: int, message: str, **extra) -> None:
    value = {
        "schema": 1,
        "status": status,
        "phase": phase,
        "percent": max(0, min(100, int(percent))),
        "message": message,
        "updated_at": time.time(),
        **extra,
    }
    _atomic_write(state_path, value)


def run(request_path: Path, state_path: Path) -> int:
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        action = request["action"]
        module_name = BACKEND_MODULES[action]
        module = importlib.import_module(module_name)
        run_action = getattr(module, "run_action")
        if not callable(run_action):
            raise TypeError(f"{module_name}.run_action is not callable")
    except (OSError, ValueError, KeyError, AttributeError, TypeError, ImportError) as error:
        _write_state(
            state_path,
            "failed",
            "backend",
            0,
            f"Backend unavailable: {error}",
            error_code="backend_unavailable",
        )
        return 2

    def progress(phase: str, percent: int, message: str) -> None:
        _write_state(state_path, "running", phase, percent, message)

    try:
        output = run_action(request, progress)
        output_path = Path(output)
        expected_root = Path(request["output_directory"]).resolve()
        if not output_path.is_file() or output_path.suffix.lower() != ".glb":
            raise RuntimeError("Backend did not produce a GLB file")
        if not output_path.resolve().is_relative_to(expected_root):
            raise RuntimeError("Backend output escaped output directory")
        _write_state(state_path, "completed", "complete", 100, "Completed", output=str(output_path))
        return 0
    except Exception as error:
        _write_state(
            state_path,
            "failed",
            "backend",
            0,
            f"Backend failed: {error}",
            error_code="backend_failed",
        )
        return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True, type=Path)
    parser.add_argument("--state", required=True, type=Path)
    arguments = parser.parse_args()
    return run(arguments.request, arguments.state)


if __name__ == "__main__":
    raise SystemExit(main())
