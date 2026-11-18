from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from .transfers import (
    ACTIVE,
    DISCARDED_STATE,
    FAILED,
    INSTALLED,
    PAUSED_STATE,
    QUEUED,
    RESUMABLE_STATES,
    RETRYING,
    VERIFYING,
    WAITING,
)

IMAGE_EXTENSIONS = frozenset({".jpeg", ".jpg", ".png", ".webp"})
# The transfer journal vocabulary. `paused` belongs here: the user has not
# finished with the transfer, so its rows still show progress, Resume is offered,
# and the engine keeps the committed bytes. What the paused state does NOT do is
# claim a writer: nothing is writing, so the gate that blocks a models-folder
# change reads `IN_PROGRESS_STATUSES` instead — the bridge already treats a
# paused job as terminal (`ACTIVE_STATUSES` there omits `paused`).
ACTIVE_STATUSES = frozenset(
    {
        "checking",
        "downloading",
        "queued",
        "running",
        "retrying",
        "waiting",
        "verifying",
        "paused",
    }
)
# Subset that is making progress right now: progress bars, Cancel buttons.
IN_PROGRESS_STATUSES = ACTIVE_STATUSES - {"paused"}
# States the user reads as finished. `paused` is absent on purpose — it resumes.
TERMINAL_STATUSES = frozenset(
    {"cancelled", "completed", "discarded", "failed", "installed", "not_downloaded"}
)
IMAGE_SLOTS = ("main", "left", "right", "back")

STATE_LABELS = {
    "not_downloaded": "Not Downloaded",
    "checking": "Checking",
    "queued": "Queued",
    "downloading": "Downloading",
    "retrying": "Retrying",
    "waiting": "Waiting for network",
    "paused": "Paused — resume when ready",
    "verifying": "Verifying",
    "installed": "Installed",
    "running": "In Progress",
    "completed": "Completed",
    "cancelled": "Cancelled",
    "discarded": "Discarded",
    "failed": "Failed",
    "invalid": "Invalid request",
}

STATE_ICONS = {
    "not_downloaded": "RADIOBUT_OFF",
    "checking": "VIEWZOOM",
    "queued": "TIME",
    "downloading": "IMPORT",
    "retrying": "FILE_REFRESH",
    "waiting": "LOOP_BACK",
    "paused": "PAUSE",
    "verifying": "FILE_TICK",
    "installed": "CHECKMARK",
    "running": "TIME",
    "completed": "CHECKMARK",
    "cancelled": "CANCEL",
    "discarded": "TRASH",
    "failed": "ERROR",
    "invalid": "ERROR",
}

# Plain-language text for the failure codes the engine writes into the journal,
# so a stalled download says what to do instead of naming an internal reason.
FAILURE_MESSAGES = {
    "http_400": "The server rejected this download. Re-check the model, then Resume.",
    "http_401": (
        "The download link expired or the repository refused the request. "
        "Retry or Resume; a gated repository needs access granted upstream."
    ),
    "http_403": (
        "The repository refused this download. Its license and access rules apply "
        "upstream; this add-on has nothing to accept."
    ),
    "http_404": "This file is no longer in the repository. Saved bytes are kept; re-check the model.",
    "http_405": "The server does not accept this request. Saved bytes are kept.",
    "http_410": "This file was removed from the repository. Saved bytes are kept.",
    "http_413": "The file is too large for the server to serve in one request.",
    "http_451": "The file is unavailable for legal reasons. Review the model's license.",
    "license_authorization_required": (
        "Authorization was missing on an older transfer record. Press Resume; "
        "it states the authorization and saved bytes continue."
    ),
    "disk_full": "Not enough free disk space. Free space and press Resume; saved bytes stay.",
    "disk_write": "The models folder could not be written. Check it is writable and press Resume.",
    "integrity_mismatch": "Saved bytes did not match the pinned checksum, so this file restarts from zero.",
    "digest_mismatch": "Saved bytes did not match the pinned checksum, so this file restarts from zero.",
    "resume_unsupported": "This server does not support partial downloads; the next attempt restarts this file from zero.",
    "no_progress": "No progress for a while. Bytes are saved; press Resume when the network recovers.",
    "size_mismatch": "The saved offset is beyond the reported file size; resume re-reads the pinned manifest.",
    "offline": "No network connection. Bytes are saved and the engine retries by itself.",
    "dns": "The server could not be resolved. Bytes are saved and the engine retries by itself.",
    "connect": "The server could not be reached. Bytes are saved and the engine retries by itself.",
    "network": "The connection dropped mid-download. Bytes are saved and the engine retries by itself.",
    "checkpoint_failed": "Progress could not be saved, so the transfer paused to avoid losing bytes.",
    "engine_interrupted": "The engine stopped mid-download. Saved bytes resume on the next engine.",
    "engine_stopped": "The engine stopped mid-download. Saved bytes resume on the next engine.",
    "engine_error": "The engine hit an internal error. Saved bytes are kept; press Recover.",
    "hub_error": "The model repository returned an unexpected error. Saved bytes are kept.",
    "backend_failed": "The inference backend failed. Saved model bytes are unaffected.",
    "runtime_not_installed": "The inference runtime is not installed.",
    "io_error": "A disk or network read failed. Saved bytes are kept; press Resume.",
    "model_busy": "This model is already being downloaded.",
    "cache_busy": "A cache cleanup is already running.",
    "invalid_command": "The engine rejected this request. Try again after Recover.",
    "paused": "Paused. Saved bytes are kept.",
    "cancelled": "Cancelled. Saved bytes stay on disk.",
    "discarded": "Discarded. Downloaded bytes were deleted.",
}


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
        # Journal states that the UI renders with the job vocabulary.
        ACTIVE: "downloading",
        INSTALLED: "installed",
        FAILED: "failed",
        QUEUED: "queued",
        RETRYING: "retrying",
        WAITING: "waiting",
        VERIFYING: "verifying",
        PAUSED_STATE: "paused",
        DISCARDED_STATE: "discarded",
        "requested": "queued",
        "interrupted": "paused",
        "stopped": "paused",
    }
    return aliases.get(status, status)


def retry_countdown(retry: Any = None, *, now: float | None = None) -> int | None:
    """Seconds until the next attempt, or None when no retry is scheduled."""
    value = retry.get("next_attempt_at") if isinstance(retry, Mapping) else retry
    try:
        epoch = float(value)
    except (TypeError, ValueError):
        return None
    if epoch <= 0.0:
        return None
    return max(0, int(round(epoch - (time.time() if now is None else now))))


def failure_message(error_code: Any, detail: str = "") -> str:
    """Plain-language reason for a journal `error_code`, falling back to its text."""
    code = str(error_code or "").strip().lower()
    message = FAILURE_MESSAGES.get(code, "")
    if message:
        return message
    return detail or (code.replace("_", " ").title() if code else "")


def status_label(status: Any, progress: Any = 0.0, detail: str = "", retry: Any = None) -> str:
    normalized = normalize_status(status)
    label = STATE_LABELS.get(normalized, normalized.replace("_", " ").title())
    if normalized == "retrying":
        seconds = retry_countdown(retry)
        if seconds is not None:
            label = f"Retrying in {seconds}s"
    elif normalized in IN_PROGRESS_STATUSES:
        label = f"{label} ({clamp_progress(progress):.0%})"
    return f"{label}: {detail}" if detail else label


def status_icon(status: Any) -> str:
    return STATE_ICONS.get(normalize_status(status), "RADIOBUT_OFF")


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
        transfer = transfer_for_model(snapshot, model_id)
        if transfer:
            return {
                "status": normalize_status(transfer.get("state")),
                "progress": transfer_progress(transfer)[0],
                "message": str(transfer.get("message") or ""),
                "transfer_id": str(transfer.get("transfer_id") or ""),
                "bytes_done": int(transfer.get("completed_bytes") or 0),
                "bytes_total": int(transfer.get("total_bytes") or 0),
                "error_code": str(transfer.get("error_code") or ""),
            }
        return {"status": "not_downloaded", "progress": 0.0}
    state = dict(matching[-1])
    state["status"] = normalize_status(state.get("status"))
    return state


def iter_transfers(snapshot: Mapping[str, Any]) -> Iterable[Mapping[str, Any]]:
    """Durable transfer records: readable with no engine and no session."""
    transfers = snapshot.get("transfers", []) if isinstance(snapshot, Mapping) else []
    if isinstance(transfers, Mapping):
        yield from (record for record in transfers.values() if isinstance(record, Mapping))
    elif isinstance(transfers, list):
        yield from (record for record in transfers if isinstance(record, Mapping))


def find_transfer(snapshot: Mapping[str, Any], transfer_id_value: str) -> dict[str, Any]:
    if not transfer_id_value:
        return {}
    for record in iter_transfers(snapshot):
        if str(record.get("transfer_id", "")) == transfer_id_value:
            return dict(record)
    return {}


def transfer_for_model(snapshot: Mapping[str, Any], model_id: str) -> dict[str, Any]:
    """The newest transfer for a model; journals are already sorted oldest first."""
    if not model_id:
        return {}
    by_model = snapshot.get("transfers_by_model") if isinstance(snapshot, Mapping) else None
    if isinstance(by_model, Mapping) and isinstance(by_model.get(model_id), Mapping):
        return dict(by_model[model_id])
    matches = [
        dict(record) for record in iter_transfers(snapshot) if record.get("model_id") == model_id
    ]
    return matches[-1] if matches else {}


def transfer_progress(record: Mapping[str, Any]) -> tuple[float, int, int]:
    """(fraction, saved bytes, total bytes) from the durable checkpoint."""
    try:
        done = int(read_field(record, "completed_bytes", 0) or 0)
    except (TypeError, ValueError):
        done = 0
    try:
        total = int(read_field(record, "total_bytes", 0) or 0)
    except (TypeError, ValueError):
        total = 0
    return (min(1.0, max(0.0, done / total)) if total > 0 else 0.0), done, total


def attempt_count(record: Mapping[str, Any]) -> int:
    """One-based attempt number: the journal counts consecutive failures."""
    retry = read_field(record, "retry", {})
    if not isinstance(retry, Mapping):
        return 1
    try:
        failures = int(retry.get("consecutive_failures") or 0)
    except (TypeError, ValueError):
        failures = 0
    return max(1, failures + 1)


def saved_bytes_label(record: Mapping[str, Any]) -> str:
    """What a resume would reuse, so the user knows nothing was thrown away."""
    _, done, total = transfer_progress(record)
    if total > 0:
        return f"Saved {format_bytes(done)} of {format_bytes(total)}"
    if done > 0:
        return f"Saved {format_bytes(done)}"
    return "No bytes saved yet"


def transfer_busy(record: Mapping[str, Any]) -> bool:
    """The engine may still be writing bytes for this transfer."""
    state = str(read_field(record, "state", "") or "")
    return state in RESUMABLE_STATES or normalize_status(state) in IN_PROGRESS_STATUSES


def has_active_jobs(snapshot: Mapping[str, Any]) -> bool:
    """True while work is still making progress or bytes are still owed.

    `IN_PROGRESS_STATUSES`, not `ACTIVE_STATUSES`: a paused download stopped
    by the user writes nothing, so it must not hold the models folder hostage
    forever — that is what made the Folder panel report "Download running"
    with nothing running and grey the change button with no way out. The
    engine side agrees: `BridgeClient._has_active_job` reads its own set,
    which omits `paused`, and `transfer_busy` already excludes it.
    """
    if any(
        normalize_status(job.get("status")) in IN_PROGRESS_STATUSES for job in iter_jobs(snapshot)
    ):
        return True
    return any(transfer_busy(record) for record in iter_transfers(snapshot))


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
