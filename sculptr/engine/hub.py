from __future__ import annotations

import email.utils
import hashlib
import http.client
import json
import os
import random
import shutil
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable

from ..catalog import FileRule, ModelSpec, RepositorySpec
from ..diagnostics import DiagnosticsLog, redact
from .protocol import CancelledError, atomic_write_json, contained_path


Progress = Callable[[str, int, int, str], None]
Cancelled = Callable[[], bool]
FileProgress = Callable[[int], None]

# A stalled LFS read is the normal way a multi-gigabyte checkpoint dies, so the
# socket timeout is forgiving and adjustable without editing this file.
DEFAULT_SOCKET_TIMEOUT = float(os.environ.get("SCULPTR_HTTP_TIMEOUT", "60"))
DEFAULT_ATTEMPTS = int(os.environ.get("SCULPTR_HTTP_RETRIES", "5"))
CHUNK = 1_048_576
METADATA_LIMIT = 16_777_216
# Bytes between forced fsyncs: an abrupt kill then costs at most this much
# re-downloaded data, instead of a sync for every chunk.
SYNC_INTERVAL = 32 * CHUNK
RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504, 507})
NETWORK_ERRORS = (
    TimeoutError,
    socket.timeout,
    ConnectionError,
    OSError,
    http.client.HTTPException,
    urllib.error.URLError,
)


class HubError(RuntimeError):
    pass


class LicenseAuthorizationError(HubError):
    pass


class HubHTTPError(HubError):
    """An HTTP status that ended the request: permanent, or retries exhausted."""

    def __init__(self, code: int, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


@dataclass(frozen=True)
class RemoteFile:
    repo_id: str
    revision: str
    remote_path: str
    local_path: str
    size: int
    sha256: str | None = None
    git_sha1: str | None = None


@dataclass(frozen=True)
class RetryPolicy:
    """Bounded exponential backoff with jitter; `Retry-After` wins when present."""

    max_attempts: int = DEFAULT_ATTEMPTS
    base_delay: float = 1.0
    max_delay: float = 30.0
    jitter: float = 0.25

    def delay(self, attempt: int, retry_after: float | None = None) -> float:
        if retry_after is not None:
            return min(self.max_delay, max(0.0, retry_after))
        delay = min(self.max_delay, self.base_delay * (2 ** max(0, attempt - 1)))
        return delay + random.random() * self.jitter * delay


def _retry_after(headers: Any) -> float | None:
    raw = headers.get("Retry-After") if headers is not None else None
    if not raw:
        return None
    value = str(raw).strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        deadline = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if deadline is None:
        return None
    if deadline.tzinfo is None:
        deadline = deadline.replace(tzinfo=timezone.utc)
    return max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds())


class _RestrictedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, allowed_hosts: frozenset[str]):
        self.allowed_hosts = allowed_hosts

    def redirect_request(self, request, file_pointer, code, message, headers, new_url):
        parsed = urllib.parse.urlparse(new_url)
        host = parsed.hostname or ""
        if parsed.scheme != "https" or not any(
            host == allowed or host.endswith(f".{allowed}") for allowed in self.allowed_hosts
        ):
            raise HubError(f"Blocked download redirect host: {host}")
        return super().redirect_request(request, file_pointer, code, message, headers, new_url)


class HubClient:
    def __init__(
        self,
        endpoint: str = "https://huggingface.co",
        *,
        opener: urllib.request.OpenerDirector | None = None,
        socket_timeout: float = DEFAULT_SOCKET_TIMEOUT,
        retry: RetryPolicy | None = None,
        log: DiagnosticsLog | None = None,
    ):
        self.endpoint = endpoint.rstrip("/")
        self.socket_timeout = max(1.0, float(socket_timeout))
        self.retry = retry or RetryPolicy()
        self.log = log
        if opener is not None:
            self.opener = opener
        elif self.endpoint == "https://huggingface.co":
            hosts = frozenset({"huggingface.co", "hf.co"})
            self.opener = urllib.request.build_opener(_RestrictedRedirectHandler(hosts))
        else:
            self.opener = urllib.request.build_opener()

    def log_event(self, kind: str, **fields: Any) -> None:
        if self.log is not None:
            self.log.event(kind, **fields)

    def resolve_model(self, model: ModelSpec) -> tuple[RemoteFile, ...]:
        files: list[RemoteFile] = []
        for repository in model.repositories:
            metadata = self._metadata(repository)
            siblings = metadata.get("siblings")
            if not isinstance(siblings, list):
                raise HubError(f"Invalid Hugging Face metadata for {repository.repo_id}")
            available = frozenset(
                sibling.get("rfilename") for sibling in siblings if isinstance(sibling, dict)
            )
            required = {rule.path: rule for rule in repository.files}
            selected_paths: set[str] = set()
            for sibling in siblings:
                if not isinstance(sibling, dict) or not isinstance(sibling.get("rfilename"), str):
                    continue
                remote_path = sibling["rfilename"]
                if not repository.accepts(remote_path, available):
                    continue
                selected_paths.add(remote_path)
                files.append(self._remote_file(repository, sibling, required.get(remote_path)))
            missing = set(required) - selected_paths
            if missing:
                raise HubError(
                    f"Pinned repository {repository.repo_id} lacks required files: {', '.join(sorted(missing))}"
                )
        if not files:
            raise HubError(f"Pinned manifest for {model.name} selected no files")
        local_paths = [item.local_path for item in files]
        if len(local_paths) != len(set(local_paths)):
            raise HubError(f"Pinned manifest for {model.name} contains duplicate target paths")
        return tuple(files)

    def _metadata(self, repository: RepositorySpec) -> dict:
        repo = urllib.parse.quote(repository.repo_id, safe="/")
        revision = urllib.parse.quote(repository.revision, safe="")
        url = f"{self.endpoint}/api/models/{repo}/revision/{revision}?blobs=true"
        request = urllib.request.Request(
            url, headers={"Accept": "application/json", "User-Agent": "SculptR/0.1"}
        )
        try:
            with self._open(request, what=f"metadata for {repository.repo_id}") as response:
                raw = response.read(METADATA_LIMIT + 1)
        except HubError:
            raise
        except NETWORK_ERRORS as error:
            raise HubError(
                redact(f"Cannot read Hugging Face metadata for {repository.repo_id}: {error}")
            ) from error
        if len(raw) > METADATA_LIMIT:
            raise HubError("Hugging Face metadata response is too large")
        try:
            metadata = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HubError("Hugging Face metadata response is invalid") from error
        if not isinstance(metadata, dict):
            raise HubError("Hugging Face metadata response is invalid")
        sha = metadata.get("sha")
        if not isinstance(sha, str) or not sha.startswith(repository.revision):
            raise HubError(f"Pinned revision mismatch for {repository.repo_id}")
        return metadata

    @staticmethod
    def _remote_file(
        repository: RepositorySpec,
        sibling: dict,
        rule: FileRule | None,
    ) -> RemoteFile:
        remote_path = sibling["rfilename"]
        lfs = sibling.get("lfs") if isinstance(sibling.get("lfs"), dict) else {}
        size = lfs.get("size", sibling.get("size"))
        if not isinstance(size, int) or size < 0:
            raise HubError(f"Missing size for {repository.repo_id}/{remote_path}")
        metadata_sha256 = lfs.get("sha256")
        expected_sha256 = rule.sha256 if rule and rule.sha256 else metadata_sha256
        if expected_sha256 is not None and (
            not isinstance(expected_sha256, str) or len(expected_sha256) != 64
        ):
            raise HubError(f"Invalid SHA-256 for {repository.repo_id}/{remote_path}")
        if rule and rule.sha256 and metadata_sha256 and rule.sha256 != metadata_sha256:
            raise HubError(f"Catalog SHA-256 mismatch for {repository.repo_id}/{remote_path}")
        local_path = PurePosixPath(repository.target_prefix, remote_path).as_posix().lstrip("/")
        return RemoteFile(
            repo_id=repository.repo_id,
            revision=repository.revision,
            remote_path=remote_path,
            local_path=local_path,
            size=size,
            sha256=expected_sha256,
            git_sha1=sibling.get("blobId") if not expected_sha256 else None,
        )

    def _open(
        self,
        request: urllib.request.Request,
        *,
        what: str,
        cancelled: Cancelled | None = None,
    ) -> Any:
        """Open a response, retrying transient failures within the policy.

        Authentication, permission, and unknown-revision statuses raise at once:
        retrying those only hides a manifest or licence problem and burns time.
        """
        attempt = 0
        while True:
            if cancelled is not None and cancelled():
                raise CancelledError()
            try:
                return self.opener.open(request, timeout=self.socket_timeout)
            except urllib.error.HTTPError as error:
                retry_after = _retry_after(error.headers)
                message = redact(
                    f"{what}: HTTP {error.code} for {request.full_url}: {error.reason or ''}".strip()
                )
                _discard(error)
                if error.code in RETRYABLE_STATUSES and attempt < self.retry.max_attempts:
                    attempt += 1
                    self.log_event(
                        "http_retry",
                        what=what,
                        attempt=attempt,
                        status=error.code,
                        retry_after=retry_after,
                    )
                    _sleep(self.retry.delay(attempt, retry_after), cancelled)
                    continue
                raise HubHTTPError(error.code, message, retry_after=retry_after) from error
            except HubError:
                raise
            except NETWORK_ERRORS as error:
                if attempt >= self.retry.max_attempts:
                    raise HubError(
                        redact(f"{what}: no response after {attempt + 1} attempts: {error}")
                    ) from error
                attempt += 1
                delay = self.retry.delay(attempt)
                self.log_event("http_retry", what=what, attempt=attempt, error=redact(str(error)))
                _sleep(delay, cancelled)

    def download(
        self,
        remote: RemoteFile,
        destination: Path,
        progress: FileProgress,
        cancelled: Cancelled,
    ) -> None:
        """Stream one file into `destination`, resuming from its existing bytes.

        The append point is decided from the response, never from the request: a
        server that ignores `Range` answers 200 with the whole body, and appending
        that would duplicate bytes, so the partial file is discarded and the
        transfer restarts from zero. Bytes already written stay on disk for the
        next attempt whether the transfer failed, was cancelled, or was interrupted.
        """
        destination.parent.mkdir(parents=True, exist_ok=True)
        repo = urllib.parse.quote(remote.repo_id, safe="/")
        revision = urllib.parse.quote(remote.revision, safe="")
        path = urllib.parse.quote(remote.remote_path, safe="/")
        url = f"{self.endpoint}/{repo}/resolve/{revision}/{path}?download=true"
        attempt = 0
        range_resets = 0
        while True:
            if cancelled():
                raise CancelledError()
            existing = _existing_size(destination, remote.size)
            if existing >= remote.size:
                progress(remote.size)
                return
            headers = {"Accept-Encoding": "identity", "User-Agent": "SculptR/0.1"}
            if existing:
                headers["Range"] = f"bytes={existing}-"
            request = urllib.request.Request(url, headers=headers)
            response = None
            stream = None
            try:
                response = self._open(request, what=remote.remote_path, cancelled=cancelled)
                existing = self._append_point(response, destination, existing, remote)
                stream = destination.open("ab" if existing else "wb")
                progress(existing)
                completed = _transfer(response, stream, existing, remote, progress, cancelled)
                _sync(stream)
                stream.close()
                stream = None
            except CancelledError:
                _close_quietly(stream, response)
                self.log_event(
                    "download_cancelled",
                    path=remote.local_path,
                    offset=_existing_size(destination, remote.size),
                    size=remote.size,
                )
                raise
            except HubHTTPError as error:
                _close_quietly(stream, response)
                if error.code == 416 and range_resets < self.retry.max_attempts and existing:
                    # The server will not serve our offset: start over cleanly.
                    range_resets += 1
                    self.log_event(
                        "range_reset", path=remote.local_path, offset=existing, status=error.code
                    )
                    destination.unlink(missing_ok=True)
                    continue
                raise
            except HubError:
                _close_quietly(stream, response)
                raise
            except NETWORK_ERRORS as error:
                _close_quietly(stream, response)
                offset = _existing_size(destination, remote.size)
                if attempt >= self.retry.max_attempts:
                    self.log_event(
                        "download_failed",
                        path=remote.local_path,
                        offset=offset,
                        size=remote.size,
                        attempts=attempt + 1,
                        error=redact(str(error)),
                    )
                    raise HubError(
                        redact(
                            f"Download failed for {remote.remote_path} after {attempt + 1} attempts: {error}"
                        )
                    ) from error
                attempt += 1
                delay = self.retry.delay(attempt)
                self.log_event(
                    "download_retry",
                    path=remote.local_path,
                    offset=offset,
                    size=remote.size,
                    attempt=attempt,
                    delay=delay,
                    error=redact(str(error)),
                )
                _sleep(delay, cancelled)
                continue
            size = destination.stat().st_size
            if size != remote.size:
                raise HubError(
                    f"Incomplete download for {remote.remote_path}: {size} of {remote.size} bytes"
                )
            self.log_event(
                "download_complete",
                path=remote.local_path,
                bytes=size,
                attempts=attempt + 1,
                verified=bool(remote.sha256 or remote.git_sha1),
            )
            return

    def _append_point(
        self,
        response: Any,
        destination: Path,
        existing: int,
        remote: RemoteFile,
    ) -> int:
        status = getattr(response, "status", None)
        if status is None:
            status = response.getcode()
        if status == 200:
            if existing:
                self.log_event(
                    "range_ignored",
                    path=remote.local_path,
                    offset=existing,
                    reason="server answered 200 to a range request",
                )
                destination.unlink(missing_ok=True)
            return 0
        if status != 206:
            raise HubError(f"Download failed for {remote.remote_path}: HTTP {status}")
        start, end = _content_range(response.headers.get("Content-Range"), remote.size)
        if start is None or start != existing or (end is not None and end != remote.size - 1):
            self.log_event(
                "range_mismatch",
                path=remote.local_path,
                requested=existing,
                start=start,
                end=end,
                size=remote.size,
            )
            destination.unlink(missing_ok=True)
            return 0
        self.log_event("download_resume", path=remote.local_path, offset=existing, size=remote.size)
        return existing


def check_model(
    directory: Path,
    files: Iterable[RemoteFile],
    progress: Progress,
    cancelled: Cancelled,
) -> tuple[bool, str]:
    manifest = tuple(files)
    total = sum(item.size for item in manifest)
    completed = 0
    for remote in manifest:
        if cancelled():
            raise CancelledError()
        try:
            path = contained_path(directory, remote.local_path)
        except ValueError as error:
            raise HubError(str(error)) from error
        if not path.is_file() or path.is_symlink() or path.stat().st_size != remote.size:
            return False, remote.local_path
        sha256 = hashlib.sha256() if remote.sha256 else None
        sha1 = hashlib.sha1(f"blob {remote.size}\0".encode()) if remote.git_sha1 else None
        with path.open("rb") as stream:
            while chunk := stream.read(CHUNK):
                if cancelled():
                    raise CancelledError()
                if sha256:
                    sha256.update(chunk)
                if sha1:
                    sha1.update(chunk)
                completed += len(chunk)
                progress("checking", completed, total, f"Checking {remote.local_path}")
        if sha256 and sha256.hexdigest() != remote.sha256:
            return False, remote.local_path
        if sha1 and sha1.hexdigest() != remote.git_sha1:
            return False, remote.local_path
    return True, ""


def download_model(
    models_root: Path,
    model: ModelSpec,
    hub: HubClient,
    license_authorized: bool,
    progress: Progress,
    cancelled: Cancelled,
) -> Path:
    if model.restricted and not license_authorized:
        raise LicenseAuthorizationError(model.license_notice or "License authorization is required")
    files = hub.resolve_model(model)
    final_entry = models_root.resolve() / model.model_id
    if final_entry.is_symlink():
        raise HubError(f"Model directory cannot be a symlink: {model.model_id}")
    final = contained_path(models_root, model.model_id)
    installed, _ = check_model(final, files, progress, cancelled) if final.exists() else (False, "")
    if installed:
        total = sum(item.size for item in files)
        progress("installed", total, total, "Installed")
        return final
    staging_entry = models_root.resolve() / f"{model.model_id}.partial"
    if staging_entry.is_symlink():
        raise HubError(f"Partial model directory cannot be a symlink: {model.model_id}")
    staging = contained_path(models_root, f"{model.model_id}.partial")
    staging.mkdir(parents=True, exist_ok=True)
    total = sum(item.size for item in files)
    completed_before = 0
    for remote in files:
        if cancelled():
            raise CancelledError()
        target = contained_path(staging, remote.local_path)
        partial = target.with_name(f"{target.name}.part")
        if target.is_file() and not target.is_symlink() and target.stat().st_size == remote.size:
            valid, _ = check_model(staging, (remote,), lambda *_: None, cancelled)
            if valid:
                completed_before += remote.size
                progress("downloading", completed_before, total, f"Ready {remote.local_path}")
                continue
            target.unlink()
        if partial.exists() and partial.is_symlink():
            partial.unlink()

        def report(current: int, base: int = completed_before, name: str = remote.local_path) -> None:
            progress("downloading", base + current, total, f"Downloading {name}")

        hub.download(remote, partial, report, cancelled)
        os.replace(partial, target)
        valid, _ = check_model(staging, (remote,), lambda *_: None, cancelled)
        if not valid:
            target.unlink(missing_ok=True)
            raise HubError(f"Integrity check failed for {remote.local_path}")
        completed_before += remote.size
    valid, failing_path = check_model(staging, files, progress, cancelled)
    if not valid:
        raise HubError(f"Integrity check failed for {failing_path}")
    marker = {
        "schema": 1,
        "model_id": model.model_id,
        "files": [asdict(item) for item in files],
    }
    atomic_write_json(staging / ".sculptr-model.json", marker)
    if final.exists():
        for remote in files:
            source = contained_path(staging, remote.local_path)
            destination = contained_path(final, remote.local_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(source, destination)
        atomic_write_json(final / ".sculptr-model.json", marker)
        shutil.rmtree(staging)
    else:
        os.replace(staging, final)
    progress("installed", total, total, "Installed")
    return final


def _transfer(
    response: Any,
    stream: Any,
    completed: int,
    remote: RemoteFile,
    progress: FileProgress,
    cancelled: Cancelled,
) -> int:
    """Copy the body into an open stream, fsyncing periodically, not per chunk."""
    synced = completed
    while completed < remote.size:
        if cancelled():
            raise CancelledError()
        chunk = response.read(CHUNK)
        if not chunk:
            break
        stream.write(chunk)
        completed += len(chunk)
        progress(completed)
        if completed - synced >= SYNC_INTERVAL:
            _sync(stream)
            synced = completed
    _sync(stream)
    return completed


def _sync(stream: Any) -> None:
    try:
        stream.flush()
        os.fsync(stream.fileno())
    except (OSError, ValueError):
        pass


def _close_quietly(stream: Any, response: Any) -> None:
    """Flush what was written and release the socket; partial bytes stay on disk."""
    if stream is not None:
        _sync(stream)
        try:
            stream.close()
        except (OSError, ValueError):
            pass
    if response is not None:
        try:
            response.close()
        except OSError:
            pass


def _existing_size(path: Path, expected_size: int) -> int:
    try:
        size = path.stat().st_size
    except OSError:
        return 0
    if size > expected_size:
        # More bytes than the manifest promises cannot be a valid prefix.
        path.unlink(missing_ok=True)
        return 0
    return size


def _content_range(value: Any, expected_size: int) -> tuple[int | None, int | None]:
    if not value:
        return None, None
    parts = str(value).split()
    if len(parts) != 2 or not parts[0].startswith("bytes "):
        return None, None
    span, _, total = parts[1].partition("/")
    start_text, _, end_text = span.partition("-")
    try:
        start = int(start_text)
        end = int(end_text)
    except ValueError:
        return None, None
    if total != "*" and total.isdigit() and int(total) != expected_size:
        return None, None
    return start, end


def _discard(error: urllib.error.HTTPError) -> None:
    """Drain a little of the error body so a retry can reuse the connection."""
    try:
        error.read(256)
    except (OSError, http.client.HTTPException, ValueError):
        pass
    try:
        error.close()
    except (OSError, http.client.HTTPException):
        pass


def _sleep(delay: float, cancelled: Cancelled | None) -> None:
    """Wait out a backoff while staying responsive to cancellation."""
    deadline = time.monotonic() + max(0.0, float(delay))
    while True:
        if cancelled is not None and cancelled():
            raise CancelledError()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(0.2, remaining))
