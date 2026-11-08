from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping


IMAGE_EXTENSIONS = frozenset({".jpeg", ".jpg", ".png", ".webp"})
ACTIVE_STATUSES = frozenset({"checking", "downloading", "queued", "running"})
TERMINAL_STATUSES = frozenset({"cancelled", "completed", "failed", "installed"})
IMAGE_SLOTS = ("main", "left", "right", "back")


def read_field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def format_bytes(size: int | float | None) -> str:
    if size is None:
        return "N/A"
    value = max(0.0, float(size))
    units = ("B", "KB", "MB", "GB", "TB")
    unit = units[0]
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            break
        value /= 1024.0
    digits = 0 if unit == "B" else 1 if value >= 10 else 2
    return f"{value:.{digits}f} {unit}"


def validate_image_path(path: str, require_exists: bool = True) -> tuple[bool, str]:
    if not path:
        return False, "Choose an image"
    candidate = Path(path).expanduser()
    if candidate.suffix.lower() not in IMAGE_EXTENSIONS:
        return False, "Use PNG, JPG, JPEG, or WebP"
    if require_exists and not candidate.is_file():
        return False, "Image file does not exist"
    return True, ""


def choose_drop_slot(paths: Mapping[str, str], last_slot: str = "main") -> str:
    for slot in IMAGE_SLOTS:
        if not paths.get(slot):
            return slot
    return last_slot if last_slot in IMAGE_SLOTS else "main"


def clamp_progress(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number > 1.0:
        number /= 100.0
    return min(1.0, max(0.0, number))


def normalize_status(value: Any) -> str:
    status = str(value or "not_downloaded").strip().lower().replace(" ", "_")
    aliases = {
        "not_found": "not_downloaded",
        "missing": "not_downloaded",
        "downloaded": "installed",
        "in_progress": "running",
        "error": "failed",
        "canceled": "cancelled",
    }
    return aliases.get(status, status)


def status_label(status: Any, progress: Any = 0.0, detail: str = "") -> str:
    normalized = normalize_status(status)
    labels = {
        "not_downloaded": "Not Downloaded",
        "checking": "Checking",
        "downloading": "Downloading",
        "installed": "Installed",
        "queued": "Queued",
        "running": "In Progress",
        "completed": "Completed",
        "cancelled": "Cancelled",
        "failed": "Failed",
    }
    label = labels.get(normalized, normalized.replace("_", " ").title())
    if normalized in {"checking", "downloading", "running"}:
        label = f"{label} ({clamp_progress(progress):.0%})"
    return f"{label}: {detail}" if detail else label


def status_icon(status: Any) -> str:
    return {
        "installed": "CHECKMARK",
        "completed": "CHECKMARK",
        "failed": "ERROR",
        "cancelled": "CANCEL",
        "downloading": "IMPORT",
        "checking": "VIEWZOOM",
        "running": "TIME",
        "queued": "TIME",
    }.get(normalize_status(status), "RADIOBUT_OFF")


def iter_jobs(snapshot: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    jobs = snapshot.get("jobs", {}) if isinstance(snapshot, Mapping) else {}
    if isinstance(jobs, Mapping):
        for job_id, job in jobs.items():
            if isinstance(job, Mapping):
                yield {"job_id": job_id, **job}
    elif isinstance(jobs, list):
        yield from (job for job in jobs if isinstance(job, Mapping))


def find_job(snapshot: Mapping[str, Any], job_id: str) -> dict[str, Any]:
    if not job_id:
        return {}
    for job in iter_jobs(snapshot):
        if str(job.get("job_id", job.get("id", ""))) == job_id:
            return dict(job)
    return {}


def find_model_state(snapshot: Mapping[str, Any], model_id: str) -> dict[str, Any]:
    models = snapshot.get("models", {}) if isinstance(snapshot, Mapping) else {}
    if isinstance(models, Mapping) and isinstance(models.get(model_id), Mapping):
        state = dict(models[model_id])
        state["status"] = normalize_status(state.get("status"))
        return state

    matching = []
    for job in iter_jobs(snapshot):
        payload = job.get("payload", {})
        job_model = job.get("model_id")
        if job_model is None and isinstance(payload, Mapping):
            job_model = payload.get("model_id")
        if job_model == model_id:
            matching.append(job)
    if not matching:
        return {"status": "not_downloaded", "progress": 0.0}
    state = dict(matching[-1])
    state["status"] = normalize_status(state.get("status"))
    return state


def has_active_jobs(snapshot: Mapping[str, Any]) -> bool:
    return any(normalize_status(job.get("status")) in ACTIVE_STATUSES for job in iter_jobs(snapshot))


def resource_metric(snapshot: Mapping[str, Any], name: str) -> tuple[float, str]:
    resources = snapshot.get("resources", {}) if isinstance(snapshot, Mapping) else {}
    metric = resources.get(name, {}) if isinstance(resources, Mapping) else {}
    if not isinstance(metric, Mapping):
        return 0.0, "N/A"

    percent = metric.get("percent")
    if percent is None and metric.get("used") is not None and metric.get("total"):
        percent = float(metric["used"]) / float(metric["total"])
    factor = clamp_progress(percent)
    if name in {"ram", "vram"} and metric.get("used") is not None and metric.get("total") is not None:
        if not metric.get("total"):
            return 0.0, "N/A"
        details = f"{format_bytes(metric['used'])} / {format_bytes(metric['total'])}"
    elif percent is not None:
        details = f"{factor:.0%}"
    else:
        details = "N/A"
    return factor, details


def storage_label(snapshot: Mapping[str, Any]) -> str:
    storage = snapshot.get("storage", {}) if isinstance(snapshot, Mapping) else {}
    if not isinstance(storage, Mapping):
        return "Calculating folder usage..."
    used = storage.get("used", storage.get("folder_bytes"))
    total = storage.get("total", storage.get("disk_total"))
    if used is None or total is None:
        return "Calculating folder usage..."
    return f"{format_bytes(used)} used of {format_bytes(total)} disk"


def result_path(job: Mapping[str, Any]) -> str:
    if not isinstance(job, Mapping):
        return ""
    for result in (job.get("result"), job.get("output")):
        if isinstance(result, str):
            return result
        if isinstance(result, Mapping):
            path = result.get("output", result.get("path", result.get("filepath", "")))
            if path:
                return str(path)
    return ""
