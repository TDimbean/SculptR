"""Durable download intent and transfer state, independent of engine sessions.

Why this exists: the UI process deletes the engine session directory whenever it
replaces an engine, so anything stored inside a session dies with it. A multi-GB
transfer must survive that, an engine crash, and a Blender restart, so intent and
progress live in their own journal under `<models root>/.sculptr/downloads/`.

Two directories, one writer each, so the two processes never race on a record:

* `intent/<transfer_id>.json` — written only by the UI process. Records what the
  user asked for (and whether they paused or discarded it) *before* the command
  is acknowledged, plus the pinned manifest identity.
* `state/<transfer_id>.json` — written only by the engine process. Records the
  attempt, retry information, and the durable byte offsets that were fsynced.

A logical transfer id is derived from the model id plus the pinned manifest, so a
restart reuses the same transfer with a new attempt id. If the pinned manifest
changes, the id changes: different bytes are a different intent, not a resume.

Records never contain credentials or signed redirect URLs; ETags are stored only
when they are strong validators for the pinned representation.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping

from .process import replace_atomic

SCHEMA = 1

REQUESTED = "requested"
PAUSED = "paused"
DISCARDED = "discarded"
INTENT_REQUESTS = frozenset({REQUESTED, PAUSED, DISCARDED})

QUEUED = "queued"
ACTIVE = "active"
RETRYING = "retrying"
WAITING = "waiting"
PAUSED_STATE = "paused"
VERIFYING = "verifying"
INSTALLED = "installed"
FAILED = "failed"
DISCARDED_STATE = "discarded"
# States the engine may resume on its own after a restart.
RESUMABLE_STATES = frozenset({QUEUED, ACTIVE, RETRYING, WAITING, VERIFYING})
# States the UI renders as "still working" rather than "finished".
BUSY_STATES = frozenset({QUEUED, ACTIVE, RETRYING, WAITING, VERIFYING})
TERMINAL_STATES = frozenset({PAUSED_STATE, INSTALLED, FAILED, DISCARDED_STATE})


class JournalError(OSError):
    """A durable write failed. Callers must pause rather than keep transferring."""


def download_root(models_root: os.PathLike[str] | str) -> Path:
    return Path(models_root) / ".sculptr" / "downloads"


def intent_directory(models_root: os.PathLike[str] | str) -> Path:
    return download_root(models_root) / "intent"


def state_directory(models_root: os.PathLike[str] | str) -> Path:
    return download_root(models_root) / "state"


def _safe_model_id(model_id: str) -> str:
    """Model ids reach here from command files, so they are validated, not trusted.

    A payload directory named from an unsanitized id could be written outside the
    models root, and an empty id would name the root itself.
    """
    text = str(model_id or "")
    if not text or text != Path(text).name or Path(text).is_absolute() or text in {".", ".."}:
        raise ValueError(f"Unsafe model id: {text[:80]!r}")
    return text


def partial_directory(models_root: os.PathLike[str] | str, model_id: str) -> Path:
    return Path(models_root) / f"{_safe_model_id(model_id)}.partial"


def partial_path(models_root: os.PathLike[str] | str, model_id: str, local_path: str) -> Path:
    """Payload store keeps the manifest's directory structure: two `config.yaml`
    files from different repositories must not share one `.part`."""
    return (
        partial_directory(models_root, model_id)
        / _relative(local_path)
        / f"{PurePosixPath(local_path).name}.part"
    )


def staged_path(models_root: os.PathLike[str] | str, model_id: str, local_path: str) -> Path:
    return (
        partial_directory(models_root, model_id)
        / _relative(local_path)
        / f"{PurePosixPath(local_path).name}.staged"
    )


def _relative(local_path: str) -> PurePosixPath:
    """Directory part of a manifest path, refusing anything that escapes the store."""
    relative = PurePosixPath(local_path)
    if relative.is_absolute() or any(part == ".." for part in relative.parts[:-1]):
        raise ValueError(f"Unsafe manifest path: {local_path}")
    return relative.parent if len(relative.parts) > 1 else PurePosixPath(".")


def new_attempt_id() -> str:
    return f"a{int(time.time())}-{secrets.token_hex(4)}"


def transfer_id(model_id: str, pinned: Iterable[Mapping[str, Any]]) -> str:
    """Stable logical id: model id + pinned manifest, not the session or job id."""
    digest = hashlib.sha256(_canonical({"model_id": model_id, "pinned": list(pinned)}).encode())
    return f"t-{digest.hexdigest()[:32]}"


def manifest_fingerprint(manifest: Iterable[Mapping[str, Any]]) -> str:
    return hashlib.sha256(_canonical(list(manifest)).encode()).hexdigest()[:32]


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _field(value: Any, name: str, default: Any = None) -> Any:
    """Read a catalog field from either a dataclass or a mapping."""
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def pinned_from_catalog(spec: Any) -> list[dict[str, Any]]:
    """Pinned manifest identity from the catalog, resolved without the network.

    `spec` is a `catalog.ModelSpec` or any mapping with `repositories`; only
    identity fields are kept, because the UI must record intent before it has
    contacted the hub.
    """
    repositories = _field(spec, "repositories", ()) or ()
    pinned: list[dict[str, Any]] = []
    for repository in repositories:
        files = [
            {"path": _field(rule, "path", ""), "sha256": _field(rule, "sha256")}
            for rule in (_field(repository, "files", ()) or ())
        ]
        pinned.append(
            {
                "repo_id": str(_field(repository, "repo_id", "") or ""),
                "revision": str(_field(repository, "revision", "") or ""),
                "target_prefix": str(_field(repository, "target_prefix", "") or ""),
                "include": list(_field(repository, "include", ("*",)) or ()),
                "exclude": list(_field(repository, "exclude", ()) or ()),
                "files": files,
            }
        )
    return pinned


def _write(path: Path, record: Mapping[str, Any]) -> None:
    """Atomic, fsync'd replace. Errors propagate: a silent failure loses progress."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(record, stream, ensure_ascii=False, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        replace_atomic(temporary_path, path)
    except OSError as error:
        raise JournalError(f"Cannot persist {path.name}: {error}") from error
    finally:
        try:
            temporary_path.unlink()
        except OSError:
            pass


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


_TRANSFER_ID_RE = re.compile(r"^t-[0-9a-f]{32}$")


def _safe_transfer_id(value: Any) -> str:
    """Journal file names come from callers, so they are validated, not trusted.

    Anything else is a malformed or hostile identifier: accepting it would let a
    caller write a journal file outside the journal directory.
    """
    text = str(value if value is not None else "")
    if not _TRANSFER_ID_RE.fullmatch(text):
        raise ValueError(f"Malformed transfer id: {text[:80]!r}")
    return text


def intent_path(models_root: os.PathLike[str] | str, transfer_id_value: str) -> Path:
    return intent_directory(models_root) / f"{_safe_transfer_id(transfer_id_value)}.json"


def state_path(models_root: os.PathLike[str] | str, transfer_id_value: str) -> Path:
    return state_directory(models_root) / f"{_safe_transfer_id(transfer_id_value)}.json"


def _journal_identifier(value: Any) -> str:
    """A record we cannot name cannot be written: report it as a journal error."""
    try:
        return _safe_transfer_id(value)
    except ValueError as error:
        raise JournalError(str(error)) from error


class IntentJournal:
    """UI-side record of what the user asked for. Survives engine replacement."""

    def __init__(self, models_root: os.PathLike[str] | str) -> None:
        self.models_root = Path(models_root)

    def record(
        self,
        model_id: str,
        spec: Any,
        *,
        license_required: bool,
        license_authorized: bool,
        request: str = REQUESTED,
    ) -> dict[str, Any]:
        pinned = pinned_from_catalog(spec)
        if not pinned:
            raise ValueError(f"Catalog entry for {model_id} has no pinned repositories")
        identifier = transfer_id(model_id, pinned)
        existing = self.load(identifier)
        now = time.time()
        record = {
            "schema": SCHEMA,
            "transfer_id": identifier,
            "model_id": model_id,
            "request": request if request in INTENT_REQUESTS else REQUESTED,
            "pinned": pinned,
            "manifest_fingerprint": manifest_fingerprint(pinned),
            "authorization": {
                "license_required": bool(license_required),
                "license_authorized": bool(license_authorized),
            },
            "client_pid": os.getpid(),
            "created_at": existing.get("created_at") or now,
            "updated_at": now,
        }
        _write(intent_path(self.models_root, identifier), record)
        return record

    def load(self, identifier: str) -> dict[str, Any]:
        try:
            path = intent_path(self.models_root, identifier)
        except ValueError:
            # A bad identifier in a record is data, not a crash: report it as absent.
            return {}
        record = _read(path)
        if record.get("schema") != SCHEMA or not record.get("transfer_id"):
            return {}
        return record

    def set_request(self, identifier: str, request: str) -> dict[str, Any]:
        record = self.load(identifier)
        if not record:
            raise KeyError(identifier)
        if request not in INTENT_REQUESTS:
            raise ValueError(f"Unknown intent request: {request}")
        record["request"] = request
        record["updated_at"] = time.time()
        _write(intent_path(self.models_root, identifier), record)
        return record

    def authorize(self, identifier: str, license_authorized: bool) -> dict[str, Any]:
        record = self.load(identifier)
        if not record:
            raise KeyError(identifier)
        record.setdefault("authorization", {})["license_authorized"] = bool(license_authorized)
        record["updated_at"] = time.time()
        _write(intent_path(self.models_root, identifier), record)
        return record

    def iter(self) -> list[dict[str, Any]]:
        directory = intent_directory(self.models_root)
        records: list[dict[str, Any]] = []
        try:
            paths = sorted(directory.glob("t-*.json"))
        except OSError:
            return records
        for path in paths:
            record = self.load(path.stem)
            if record:
                records.append(record)
        return records


class TransferStateJournal:
    """Engine-side record of progress. The only writer of `state/`."""

    def __init__(self, models_root: os.PathLike[str] | str) -> None:
        self.models_root = Path(models_root)

    def load(self, identifier: str) -> dict[str, Any]:
        try:
            path = state_path(self.models_root, identifier)
        except ValueError:
            return {}
        record = _read(path)
        if record.get("schema") != SCHEMA or not record.get("transfer_id"):
            return {}
        return record

    def iter(self) -> list[dict[str, Any]]:
        directory = state_directory(self.models_root)
        records: list[dict[str, Any]] = []
        try:
            paths = sorted(directory.glob("t-*.json"))
        except OSError:
            return records
        for path in paths:
            record = self.load(path.stem)
            if record:
                records.append(record)
        return records

    def begin(
        self,
        intent: Mapping[str, Any],
        manifest: Iterable[Mapping[str, Any]],
        *,
        attempt_id: str,
        session_id: str,
        state: str = QUEUED,
    ) -> dict[str, Any]:
        """Open (or re-open) one attempt for an existing logical transfer."""
        identifier = _journal_identifier(intent.get("transfer_id"))
        existing = self.load(identifier)
        now = time.time()
        entries = list(manifest)
        files = {
            str(entry["local_path"]): {
                "offset": int(existing.get("files", {}).get(str(entry["local_path"]), {}).get("offset", 0)),
                "size": int(entry.get("size") or 0),
                "verified": bool(
                    existing.get("files", {}).get(str(entry["local_path"]), {}).get("verified", False)
                ),
                "staged": bool(
                    existing.get("files", {}).get(str(entry["local_path"]), {}).get("staged", False)
                ),
                "installed": bool(
                    existing.get("files", {}).get(str(entry["local_path"]), {}).get("installed", False)
                ),
                "etag": existing.get("files", {}).get(str(entry["local_path"]), {}).get("etag"),
                "checkpoint_at": existing.get("files", {}).get(str(entry["local_path"]), {}).get(
                    "checkpoint_at"
                ),
            }
            for entry in entries
        }
        retry = existing.get("retry") if isinstance(existing.get("retry"), Mapping) else {}
        record = {
            "schema": SCHEMA,
            "transfer_id": identifier,
            "model_id": str(intent.get("model_id") or ""),
            "attempt_id": attempt_id,
            "session_id": session_id,
            "state": state,
            "phase": "queued",
            "message": "Transfer queued",
            "error_code": None,
            "manifest": entries,
            "manifest_fingerprint": manifest_fingerprint(entries),
            "authorization": dict(intent.get("authorization") or {}),
            "files": files,
            "total_bytes": sum(int(entry.get("size") or 0) for entry in entries),
            "completed_bytes": sum(entry["offset"] for entry in files.values()),
            "retry": {
                "consecutive_failures": int(retry.get("consecutive_failures") or 0),
                "last_error": retry.get("last_error"),
                "last_error_code": retry.get("last_error_code"),
                "next_attempt_at": retry.get("next_attempt_at"),
                "no_progress_since": retry.get("no_progress_since") or now,
                "recovered_bytes": int(retry.get("recovered_bytes") or 0),
            },
            "created_at": existing.get("created_at") or now,
            "updated_at": now,
            "finished_at": None,
        }
        _write(state_path(self.models_root, identifier), record)
        return record

    def update(self, record: Mapping[str, Any], **changes: Any) -> dict[str, Any]:
        """Merge changes and persist. Raises JournalError when the write fails.

        A state-only update never rewinds recorded progress: a worker may have
        committed a newer checkpoint between the moment the caller copied this record
        and the moment this write lands, and the replace is atomic, so the caller's
        copy could otherwise overwrite bytes that are already fsynced on disk. Those
        offsets are merged against the newest durable record. Writes that carry
        `files=` (a checkpoint) stay authoritative, because only a checkpoint knows
        that a payload was legitimately restarted from zero.
        """
        updated = dict(record)
        updated.update(changes)
        updated["updated_at"] = time.time()
        if "files" not in changes and "completed_bytes" not in changes:
            updated = self._advance_from_durable(updated)
        _write(state_path(self.models_root, _journal_identifier(record.get("transfer_id"))), updated)
        return updated

    def _advance_from_durable(self, updated: dict[str, Any]) -> dict[str, Any]:
        """Take the newest durable byte offsets, so a state write cannot undo them."""
        durable = self.load(_journal_identifier(updated.get("transfer_id")))
        if not durable:
            return updated
        files = {path: dict(entry) for path, entry in (updated.get("files") or {}).items()}
        changed = False
        for path, entry in (durable.get("files") or {}).items():
            target = files.setdefault(path, {})
            if int(entry.get("offset") or 0) > int(target.get("offset") or 0):
                for key in ("offset", "checkpoint_at", "etag"):
                    if entry.get(key) is not None:
                        target[key] = entry[key]
                changed = True
            for flag in ("verified", "staged", "installed"):
                if entry.get(flag):
                    target[flag] = True
        if changed:
            updated["files"] = files
            updated["completed_bytes"] = sum(
                int(entry.get("offset") or 0) for entry in files.values()
            )
        return updated

    def checkpoint(
        self, record: Mapping[str, Any], local_path: str, offset: int, *, etag: str | None = None,
        **changes: Any,
    ) -> dict[str, Any]:
        """Record a durable offset that the caller already fsynced."""
        files = {path: dict(values) for path, values in (record.get("files") or {}).items()}
        entry = files.setdefault(local_path, {})
        entry["offset"] = int(offset)
        entry["checkpoint_at"] = time.time()
        if etag is not None:
            entry["etag"] = etag
        completed = sum(int(values.get("offset") or 0) for values in files.values())
        return self.update(record, files=files, completed_bytes=completed, **changes)

    def mark_file(self, record: Mapping[str, Any], local_path: str, **changes: Any) -> dict[str, Any]:
        files = {path: dict(values) for path, values in (record.get("files") or {}).items()}
        entry = files.setdefault(local_path, {})
        entry.update(changes)
        completed = sum(int(values.get("offset") or 0) for values in files.values())
        return self.update(record, files=files, completed_bytes=completed)

    def finish(self, record: Mapping[str, Any], state: str, *, error_code: str | None = None,
               message: str = "", phase: str = "") -> dict[str, Any]:
        changes: dict[str, Any] = {"state": state, "message": message, "error_code": error_code}
        if phase:
            changes["phase"] = phase
        if state in TERMINAL_STATES:
            changes["finished_at"] = time.time()
        return self.update(record, **changes)


def merged_records(models_root: os.PathLike[str] | str) -> list[dict[str, Any]]:
    """Intent joined with engine state, for UI display when no engine is running."""
    states = TransferStateJournal(models_root).iter()
    by_id: dict[str, dict[str, Any]] = {}
    for record in states:
        identifier = str(record.get("transfer_id") or "")
        if _TRANSFER_ID_RE.fullmatch(identifier):
            by_id[identifier] = record
    records: list[dict[str, Any]] = []
    for intent in IntentJournal(models_root).iter():
        identifier = str(intent.get("transfer_id") or "")
        if not _TRANSFER_ID_RE.fullmatch(identifier):
            # A journal file the UI cannot name is skipped, not fatal: the panel
            # still shows every transfer it can verify.
            continue
        state = by_id.pop(identifier, None)
        record = {
            "transfer_id": identifier,
            "model_id": str(intent.get("model_id") or ""),
            "intent_request": intent.get("request"),
            "authorization": intent.get("authorization") or {},
            "pinned": intent.get("pinned") or [],
        }
        if state:
            record.update(
                {
                    "attempt_id": state.get("attempt_id"),
                    "state": state.get("state"),
                    "phase": state.get("phase"),
                    "message": state.get("message"),
                    "error_code": state.get("error_code"),
                    "files": state.get("files") or {},
                    "completed_bytes": state.get("completed_bytes") or 0,
                    "total_bytes": state.get("total_bytes") or 0,
                    "retry": state.get("retry") or {},
                    "updated_at": state.get("updated_at"),
                }
            )
        else:
            record.update(
                {
                    "state": QUEUED if intent.get("request") == REQUESTED else intent.get("request"),
                    "completed_bytes": 0,
                    "total_bytes": 0,
                    "files": {},
                    "retry": {},
                    "updated_at": intent.get("updated_at"),
                }
            )
        records.append(record)
    # A transfer whose intent file was removed still deserves to be visible.
    for state in by_id.values():
        records.append(
            {
                "transfer_id": state["transfer_id"],
                "model_id": state.get("model_id"),
                "intent_request": None,
                "state": state.get("state"),
                "phase": state.get("phase"),
                "message": state.get("message"),
                "error_code": state.get("error_code"),
                "files": state.get("files") or {},
                "completed_bytes": state.get("completed_bytes") or 0,
                "total_bytes": state.get("total_bytes") or 0,
                "retry": state.get("retry") or {},
                "updated_at": state.get("updated_at"),
            }
        )
    records.sort(key=lambda record: float(record.get("updated_at") or 0.0))
    return records


def model_record(
    models_root: os.PathLike[str] | str, model_id: str, transfer_id_value: str | None = None
) -> dict[str, Any]:
    """The transfer for one model: exact id first, then the newest matching intent."""
    records = merged_records(models_root)
    if transfer_id_value:
        for record in records:
            if record.get("transfer_id") == transfer_id_value:
                return record
    matches = [record for record in records if record.get("model_id") == model_id]
    return matches[-1] if matches else {}


def reconcile_offset(disk_size: int, durable_offset: int) -> tuple[int, str | None]:
    """Where to resume, given what is on disk and what was committed.

    Bytes past the durable checkpoint are uncommitted and are truncated; that tail
    is the only data ever discarded. A disk shorter than the checkpoint says the
    volume changed under us, so the smaller length wins and the caller logs it.
    """
    if disk_size > durable_offset:
        return int(durable_offset), "truncated_uncommitted_tail"
    if disk_size < durable_offset:
        return int(disk_size), "checkpoint_ahead_of_disk"
    return int(disk_size), None


def payload_identity(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Identity a `.part` prefix is bound to; size alone never proves a prefix."""
    return {
        "repo_id": entry.get("repo_id"),
        "revision": entry.get("revision"),
        "remote_path": entry.get("remote_path"),
        "local_path": entry.get("local_path"),
        "size": int(entry.get("size") or 0),
        "sha256": entry.get("sha256"),
        "git_sha1": entry.get("git_sha1"),
    }


def has_trustworthy_digest(entry: Mapping[str, Any]) -> bool:
    return bool(entry.get("sha256") or entry.get("git_sha1"))
