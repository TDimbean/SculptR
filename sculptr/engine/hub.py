"""Pinned-manifest HTTP transfer with a resumable, validated state machine.

Two rules dominate this module.

1. Only committed bytes count. A chunk is written, `flush`+`fsync`'d, and only
   then is its offset recorded in the durable journal. Recovery truncates the
   uncommitted tail and resumes from the committed offset, so a crash costs at
   most one checkpoint of work and can never duplicate bytes.
2. A prefix is trusted only against the pinned manifest identity. A `206` is
   accepted only when the whole `Content-Range` matches the requested offset and
   the manifest size; a `200` answer to a range request never appends, and a
   `416` never deletes the saved prefix. Restarting from byte zero is an
   explained exception, not normal recovery.

One `RetryController` per transfer owns backoff, `Retry-After`, the no-progress
window, and the CDN-authorization budget. Nothing else retries.
"""

from __future__ import annotations

import email.utils
import errno
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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Mapping

from ..catalog import FileRule, ModelSpec, RepositorySpec
from ..diagnostics import DiagnosticsLog, redact
from ..transfers import (
    has_trustworthy_digest,
    partial_path,
    reconcile_offset,
    staged_path,
)
from .protocol import CancelledError, atomic_write_json, contained_path


Progress = Callable[[str, int, int, str], None]
Cancelled = Callable[[], bool]
FileProgress = Callable[[int], None]
Checkpoint = Callable[[str, int, str | None], None]
StateNotice = Callable[[str, str | None, str, float | None], None]

# A stalled LFS read is the normal way a multi-gigabyte checkpoint dies, so the
# socket timeout is forgiving and adjustable without editing this file.
DEFAULT_SOCKET_TIMEOUT = float(os.environ.get("SCULPTR_HTTP_TIMEOUT", "60"))
# Wall-clock budget for repeated failures before a transfer pauses resumably
# instead of looping forever. Long offline periods are waiting, not data loss.
NO_PROGRESS_WINDOW = float(os.environ.get("SCULPTR_NO_PROGRESS_WINDOW", "900"))
CHUNK = 1_048_576
METADATA_LIMIT = 16_777_216
# Bytes between forced fsyncs: an abrupt kill then costs at most this much
# re-downloaded data, instead of a sync for every chunk.
SYNC_INTERVAL = 32 * CHUNK
# Also force a checkpoint when time, not bytes, is what has passed.
SYNC_INTERVAL_SECONDS = float(os.environ.get("SCULPTR_SYNC_INTERVAL_SECONDS", "5"))
# Slack kept free in addition to the remaining bytes, for staging and rename.
SPACE_SLACK = 16 * CHUNK
RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504, 507})
# 401/403 usually means an expiring signed CDN URL: bounded fresh resolution is
# allowed, forwarding credentials to another host is not.
AUTH_STATUSES = frozenset({401, 403})
PERMANENT_STATUSES = frozenset({400, 404, 405, 410, 413, 451})
NETWORK_ERRORS = (
    TimeoutError,
    socket.timeout,
    ConnectionError,
    OSError,
    http.client.HTTPException,
    urllib.error.URLError,
)
_DISK_ERRNOS = frozenset(
    {errno.ENOSPC, errno.EDQUOT, errno.EACCES, errno.EPERM, errno.EFBIG, errno.EROFS}
)


class HubError(RuntimeError):
    pass


class LicenseAuthorizationError(HubError):
    pass


class TransferPaused(HubError):
    """Resumable stop: user pause, no-progress window, or durable-write failure."""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason


class CheckpointError(TransferPaused):
    """A flush/fsync or journal write failed; bytes are not committed, so we stop."""

    def __init__(self, message: str) -> None:
        super().__init__("checkpoint_failed", message)


class DiskError(HubError):
    """Storage problem (full, permission, read-only): needs user intervention."""


class ResumeUnsupported(TransferPaused):
    """The server answered 200 to a range request: append would corrupt the file.

    Resumable rather than fatal. The saved prefix is kept untouched and the
    journal marks this file as un-resumable, so the next attempt re-fetches from
    byte zero without a range request instead of pausing forever. Restarting from
    zero is therefore an explained exception, not normal recovery.
    """

    def __init__(self, message: str, *, offset: int, local_path: str) -> None:
        super().__init__("resume_unsupported", message)
        self.offset = offset
        self.local_path = local_path


class PermanentHTTPError(HubError):
    """An HTTP status that cannot be recovered automatically."""

    def __init__(self, code: int, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


class IntegrityError(HubError):
    """The completed payload does not match the pinned digest."""


class ConnectionInterrupted(HubError):
    """A body ended early (EOF, reset, timeout). Committed bytes stay; retry resumes.

    `committed` is the offset that was written *and* fsynced before this was
    raised. The caller resumes the next connection from it; without it a retry
    would start over from zero and throw away bytes nobody asked to lose.
    """

    def __init__(self, code: str, message: str, *, committed: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.committed = committed


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

    base_delay: float = 1.0
    max_delay: float = 60.0
    jitter: float = 0.3

    def delay(self, attempt: int, retry_after: float | None = None) -> float:
        if retry_after is not None:
            # Honour the server's wait exactly: shortening it re-requests too soon.
            return max(0.0, float(retry_after))
        delay = min(self.max_delay, self.base_delay * (2 ** max(0, attempt - 1)))
        return delay + random.random() * self.jitter * delay


@dataclass
class RetryController:
    """The single retry budget for one transfer.

    Forward progress resets the consecutive-failure count; a persistent failure
    pauses the transfer resumably once the no-progress window is spent, keeping
    every committed byte.
    """

    policy: RetryPolicy = field(default_factory=RetryPolicy)
    no_progress_window: float = NO_PROGRESS_WINDOW
    attempts: int = 0
    consecutive_failures: int = 0
    stalled_since: float = field(default_factory=time.monotonic)
    last_error: str | None = None
    last_error_code: str | None = None
    next_attempt_at: float | None = None
    auth_retries: int = 0
    auth_budget: int = 2

    def note_progress(self) -> None:
        self.attempts += 1
        self.consecutive_failures = 0
        self.stalled_since = time.monotonic()
        self.last_error = None
        self.last_error_code = None

    def note_failure(self, code: str, message: str, retry_after: float | None = None) -> float:
        """Record a failure and return how long to wait before the next attempt."""
        self.consecutive_failures += 1
        self.attempts += 1
        self.last_error = redact(message)[:400]
        self.last_error_code = code
        delay = self.policy.delay(self.consecutive_failures, retry_after)
        self.next_attempt_at = time.time() + delay
        return delay

    def out_of_time(self) -> bool:
        return (time.monotonic() - self.stalled_since) >= self.no_progress_window

    def allow_fresh_authorization(self) -> bool:
        if self.auth_retries >= self.auth_budget:
            return False
        self.auth_retries += 1
        return True


@dataclass(frozen=True)
class DownloadHooks:
    """Callbacks the engine supplies so the hub stays free of journal knowledge."""

    progress: FileProgress
    checkpoint: Checkpoint
    paused: Callable[[], bool]
    state: StateNotice
    cancelled: Cancelled


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
        # Every hop is a fresh request to an allowlisted host; credentials are
        # never forwarded to a CDN that was not asked for them.
        request.headers.pop("Authorization", None)
        request.headers.pop("authorization", None)
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
        # Metadata has no prefix to protect, so its own short budget is fine here;
        # transfers use `RetryController` and nothing retries inside `_open`.
        for attempt in range(1, 4):
            try:
                with self._open(request, what=f"metadata for {repository.repo_id}") as response:
                    raw = response.read(METADATA_LIMIT)
                return json.loads(raw.decode("utf-8"))
            except NETWORK_ERRORS as error:
                if attempt >= 3:
                    raise HubError(
                        redact(f"Cannot read metadata for {repository.repo_id}: {error}")
                    ) from error
                _sleep(self.retry.delay(attempt), None)
            except urllib.error.HTTPError as error:
                _discard(error)
                if error.code in RETRYABLE_STATUSES and attempt < 3:
                    _sleep(self.retry.delay(attempt, _retry_after(error.headers)), None)
                    continue
                raise HubError(
                    f"Cannot read metadata for {repository.repo_id}: HTTP {error.code}"
                ) from error
        raise HubError(f"Cannot read metadata for {repository.repo_id}")

    @staticmethod
    def _remote_file(
        repository: RepositorySpec, sibling: dict, rule: FileRule | None
    ) -> RemoteFile:
        remote_path = sibling["rfilename"]
        size = sibling.get("size")
        if not isinstance(size, int) or size < 0:
            raise HubError(f"Missing size for {remote_path} in {repository.repo_id}")
        prefix = repository.target_prefix.strip("/")
        relative = PurePosixPath(remote_path)
        local_path = relative.as_posix() if not prefix else f"{prefix}/{relative.as_posix()}"
        blob_id = sibling.get("blobId")
        declared = rule.sha256 if rule is not None else None
        quoted = urllib.parse.quote(remote_path, safe="/")
        return RemoteFile(
            repo_id=repository.repo_id,
            revision=repository.revision,
            remote_path=remote_path,
            local_path=local_path,
            size=size,
            sha256=declared,
            git_sha1=blob_id if isinstance(blob_id, str) and not declared else None,
        )

    def _open(
        self, request: urllib.request.Request, *, what: str, timeout: float | None = None
    ):
        request.add_header("User-Agent", request.get_header("User-Agent") or "SculptR/0.1")
        request.add_header("Accept-Encoding", "identity")
        effective = self.socket_timeout if timeout is None else max(1.0, float(timeout))
        return self.opener.open(request, timeout=effective)

    def download(
        self,
        directory: Path,
        model_id: str,
        remote: RemoteFile,
        hooks: DownloadHooks,
        controller: RetryController,
        *,
        durable_offset: int = 0,
        etag: str | None = None,
        total_bytes: int = 0,
        completed_bytes: int = 0,
        retry_allowed: bool = True,
        resume_supported: bool = True,
    ) -> None:
        """Transfer one file into its payload store, promoting it when verified.

        `durable_offset`/`etag` describe what the journal already committed. A
        prefix without a trustworthy digest is never resumed: it cannot be
        proven to belong to this revision.
        """
        destination = contained_path(directory, remote.local_path)
        payload = partial_path(directory, model_id, remote.local_path)
        staged = staged_path(directory, model_id, remote.local_path)
        if staged.is_file() and not staged.is_symlink() and staged.stat().st_size == remote.size:
            self.log_event("stage_reuse", path=remote.local_path, offset=remote.size)
            return
        offset = self._prepare_payload(remote, payload, durable_offset)
        if offset and not resume_supported:
            # This server answered 200 to a range request before, so it cannot
            # resume. Rewriting from byte zero is the only correct continuation:
            # `_stream` truncates at the offset it is given, so stale bytes are
            # never appended to. This is an exceptional, explained restart.
            self.log_event(
                "clean_restart",
                path=remote.local_path,
                offset=offset,
                reason="server does not support byte ranges",
            )
            offset = 0
        payload.parent.mkdir(parents=True, exist_ok=True)
        while offset < remote.size:
            self._guard(hooks)
            self._require_space(payload.parent, remote.size - offset)
            response = self._connect(remote, offset, etag, hooks, controller)
            if response is None:
                continue
            try:
                offset, etag = self._accept_response(
                    response,
                    remote,
                    payload,
                    offset,
                    hooks,
                    controller,
                    total_bytes=total_bytes,
                    completed_bytes=completed_bytes,
                )
            except ConnectionInterrupted as error:
                # Bytes past the last checkpoint were committed before this was
                # raised, so the next connection resumes from a trustworthy offset.
                if error.committed is not None and error.committed > offset:
                    offset = error.committed
                self._wait(None, remote, offset, hooks, controller, error.code, str(error))
                continue
            finally:
                _close_quietly(response)
            if offset >= remote.size:
                break
        self._finish_payload(remote, payload, staged, model_id, hooks, retry_allowed=retry_allowed)

    def _prepare_payload(
        self, remote: RemoteFile, payload: Path, durable_offset: int
    ) -> int:
        """Reconcile the payload file with its committed checkpoint."""
        if not payload.exists() or payload.is_symlink():
            return 0
        disk_size = payload.stat().st_size
        if disk_size == 0:
            return 0
        if durable_offset <= 0:
            # No journal identity for these bytes: legacy or abandoned payload.
            if not has_trustworthy_digest(
                {"sha256": remote.sha256, "git_sha1": remote.git_sha1}
            ) or disk_size != remote.size:
                self._quarantine(payload, remote, reason="no_journal_identity")
                return 0
            if self._digest_matches(payload, remote):
                # Full-size payload matching the pinned digest is adoptable even
                # without a journal record; it goes through verification below.
                self.log_event("legacy_adopted", path=remote.local_path, offset=disk_size)
                return disk_size
            self._quarantine(payload, remote, reason="digest_mismatch")
            return 0
        offset, note = reconcile_offset(disk_size, durable_offset)
        if note:
            self.log_event(
                "checkpoint_reconciled",
                path=remote.local_path,
                note=note,
                disk=disk_size,
                committed=durable_offset,
                offset=offset,
            )
        return offset

    def _quarantine(self, payload: Path, remote: RemoteFile, *, reason: str) -> None:
        """Move an unverifiable prefix aside instead of appending to it."""
        size = payload.stat().st_size if payload.exists() else 0
        destination = payload.with_name(f"{payload.name}.unverified-{int(time.time())}")
        try:
            payload.rename(destination)
        except OSError as error:
            raise DiskError(f"Cannot quarantine {remote.local_path}: {error}") from error
        self.log_event(
            "partial_quarantined",
            path=remote.local_path,
            reason=reason,
            bytes=size,
            moved_to=destination.name,
        )

    def _connect(
        self,
        remote: RemoteFile,
        offset: int,
        etag: str | None,
        hooks: DownloadHooks,
        controller: RetryController,
    ):
        """Open one connection for the committed offset, or return None to retry."""
        url = (
            f"{self.endpoint}/{urllib.parse.quote(remote.repo_id, safe='/')}/resolve/"
            f"{urllib.parse.quote(remote.revision, safe='')}/{urllib.parse.quote(remote.remote_path, safe='/')}"
            "?download=true"
        )
        headers = {"Accept-Encoding": "identity"}
        if offset > 0:
            headers["Range"] = f"bytes={offset}-"
            # `If-Range` guards against a representation change: a mismatched
            # validator makes the server send 200, which we refuse to append to.
            if etag and etag.startswith("W/") is False:
                headers["If-Range"] = etag
        request = urllib.request.Request(url, headers=headers)
        try:
            response = self._open(request, what=remote.remote_path)
        except urllib.error.HTTPError as error:
            return self._http_failure(error, remote, offset, hooks, controller)
        except NETWORK_ERRORS as error:
            return self._network_failure(error, remote, offset, hooks, controller)
        status = _status(response)
        if status in (200, 206):
            encoding = (response.headers.get("Content-Encoding") or "").strip().lower()
            if encoding and encoding != "identity":
                self.log_event(
                    "encoding_rejected", path=remote.local_path, encoding=encoding, status=status
                )
                _close_quietly(response)
                self._wait(_retry_after(response.headers), remote, offset, hooks, controller,
                           "encoding_unsupported", f"Content-Encoding {encoding}")
                return None
            return response
        if status == 416:
            return self._unsatisfiable(response, remote, offset, hooks, controller)
        return self._unexpected_status(response, remote, offset, hooks, controller)

    def _http_failure(
        self,
        error: urllib.error.HTTPError,
        remote: RemoteFile,
        offset: int,
        hooks: DownloadHooks,
        controller: RetryController,
    ):
        _discard(error)
        code = error.code
        retry_after = _retry_after(error.headers)
        if code == 416:
            # `urllib` raises for every non-2xx status, so this is the normal
            # route for `416`. It must never fall through to a permanent failure:
            # an unsatisfiable range says nothing about the saved prefix.
            return self._unsatisfiable(error, remote, offset, hooks, controller)
        if code in AUTH_STATUSES and offset > 0 and controller.allow_fresh_authorization():
            # Expired signed CDN URL: re-resolve the canonical pinned URL once
            # the backoff expires, without carrying the old signed URL forward.
            self.log_event(
                "cdn_authorization_expired",
                path=remote.local_path,
                status=code,
                offset=offset,
                fresh_attempts=controller.auth_retries,
            )
            self._wait(retry_after, remote, offset, hooks, controller, "cdn_expired",
                       f"HTTP {code} on resume")
            return None
        if code in RETRYABLE_STATUSES:
            self._wait(retry_after, remote, offset, hooks, controller, f"http_{code}",
                       f"HTTP {code}")
            return None
        raise PermanentHTTPError(code, f"Download failed for {remote.remote_path}: HTTP {code}",
                                 retry_after=retry_after)

    def _network_failure(
        self,
        error: Exception,
        remote: RemoteFile,
        offset: int,
        hooks: DownloadHooks,
        controller: RetryController,
    ):
        self._wait(None, remote, offset, hooks, controller, _network_code(error), str(error))
        return None

    def _wait(
        self,
        retry_after: float | None,
        remote: RemoteFile,
        offset: int,
        hooks: DownloadHooks,
        controller: RetryController,
        code: str,
        message: str,
    ) -> None:
        delay = controller.note_failure(code, message, retry_after)
        if controller.out_of_time():
            self.log_event(
                "no_progress_window",
                path=remote.local_path,
                offset=offset,
                window=controller.no_progress_window,
                last_error_code=controller.last_error_code,
            )
            hooks.state("paused", "no_progress", _INTERRUPT_MESSAGE, None)
            raise TransferPaused("no_progress", _INTERRUPT_MESSAGE)
        waiting = code in {"dns", "connect", "network"} or controller.consecutive_failures >= 3
        hooks.state(
            "waiting" if waiting else "retrying",
            controller.last_error_code,
            redact(message)[:200],
            controller.next_attempt_at,
        )
        self.log_event(
            "download_retry",
            path=remote.local_path,
            offset=offset,
            size=remote.size,
            attempt=controller.consecutive_failures,
            delay=delay,
            reason=code,
            error=redact(message)[:200],
            next_attempt_at=controller.next_attempt_at,
        )
        _sleep(delay, hooks.cancelled)

    def _unsatisfiable(
        self,
        response: Any,
        remote: RemoteFile,
        offset: int,
        hooks: DownloadHooks,
        controller: RetryController,
    ):
        """`416`: never unlink. Decide from offset, returned total, and manifest."""
        total = _content_range_total(response.headers.get("Content-Range"))
        self.log_event(
            "range_unsatisfiable",
            path=remote.local_path,
            offset=offset,
            total=total,
            size=remote.size,
        )
        _close_quietly(response)
        if offset >= remote.size:
            # Already complete per the manifest; the caller's size check decides.
            return None
        if total is not None and offset > total:
            raise TransferPaused(
                "size_mismatch",
                f"Saved offset {offset} is beyond the reported size {total} for "
                f"{remote.remote_path}. Prefix kept; resume re-reads the pinned manifest.",
            )
        self._wait(None, remote, offset, hooks, controller, "range_unsatisfiable", "HTTP 416")
        return None

    def _unexpected_status(
        self,
        response: Any,
        remote: RemoteFile,
        offset: int,
        hooks: DownloadHooks,
        controller: RetryController,
    ):
        status = _status(response)
        retry_after = _retry_after(response.headers)
        _close_quietly(response)
        if status in RETRYABLE_STATUSES:
            self._wait(retry_after, remote, offset, hooks, controller, f"http_{status}",
                       f"HTTP {status}")
            return None
        raise PermanentHTTPError(status, f"Download failed for {remote.remote_path}: HTTP {status}",
                                 retry_after=retry_after)

    def _accept_response(
        self,
        response: Any,
        remote: RemoteFile,
        payload: Path,
        offset: int,
        hooks: DownloadHooks,
        controller: RetryController,
        *,
        total_bytes: int,
        completed_bytes: int,
    ) -> tuple[int, str | None]:
        status = _status(response)
        etag = _strong_etag(response.headers.get("ETag"))
        if status == 200:
            if offset > 0:
                # Range ignored, or the representation changed. Appending this
                # whole body to the saved prefix would corrupt the file.
                self.log_event(
                    "range_ignored",
                    path=remote.local_path,
                    offset=offset,
                    size=remote.size,
                    reason="server answered 200 to a range request",
                )
                raise ResumeUnsupported(
                    f"{remote.remote_path}: server ignored the range request. Saved prefix "
                    f"({offset} bytes) kept; the next resume re-downloads this file from zero.",
                    offset=offset,
                    local_path=remote.local_path,
                )
            declared = _declared_length(response.headers)
            if declared is not None and remote.size and declared != remote.size:
                raise IntegrityError(
                    f"{remote.remote_path}: server advertised {declared} bytes, manifest says {remote.size}"
                )
            expected = remote.size
            written = self._stream(
                response, payload, 0, expected if remote.size else None, hooks, controller,
                remote, total_bytes, completed_bytes,
            )
            if remote.size and written != remote.size:
                raise _truncated(remote, written, remote.size, committed=written)
            controller.note_progress()
            return written, etag
        start, end, total = _parse_content_range(response.headers.get("Content-Range"))
        if start is None or start != offset:
            self.log_event(
                "range_rejected",
                path=remote.local_path,
                requested=offset,
                start=start,
                end=end,
                total=total,
                size=remote.size,
            )
            # The saved prefix stays exactly as it is.
            raise _range_error(remote, offset, start, end)
        if total is not None and remote.size and total != remote.size:
            self.log_event(
                "range_total_mismatch",
                path=remote.local_path,
                total=total,
                size=remote.size,
                offset=offset,
            )
            raise _range_error(remote, offset, start, end)
        if end is not None and remote.size and end > remote.size - 1:
            self.log_event(
                "range_beyond_manifest",
                path=remote.local_path,
                end=end,
                size=remote.size,
                offset=offset,
            )
            raise _range_error(remote, offset, start, end)
        expected = (end + 1 - start) if end is not None else (remote.size - offset)
        self.log_event(
            "download_resume",
            path=remote.local_path,
            offset=offset,
            end=end,
            size=remote.size,
            status=206,
        )
        written = self._stream(
            response, payload, offset, expected, hooks, controller, remote,
            total_bytes, completed_bytes,
        )
        if end is not None and written != expected:
            raise _truncated(
                remote, offset + written, offset + expected, committed=offset + written
            )
        controller.note_progress()
        return offset + written, etag

    def _stream(
        self,
        response: Any,
        payload: Path,
        offset: int,
        expected: int | None,
        hooks: DownloadHooks,
        controller: RetryController,
        remote: RemoteFile,
        total_bytes: int,
        completed_bytes: int,
    ) -> int:
        """Write from `offset`, fsync before recording each durable checkpoint."""
        written = 0
        next_sync = SYNC_INTERVAL
        last_sync = time.monotonic()
        with payload.open("r+b" if payload.exists() else "wb") as stream:
            # The uncommitted tail is not trustworthy: drop it before writing so a
            # reconnect cannot duplicate bytes.
            stream.seek(offset)
            stream.truncate()
            stream.flush()
            while expected is None or written < expected:
                if hooks.cancelled():
                    raise CancelledError()
                if hooks.paused():
                    self._commit(
                        stream,
                        payload,
                        offset + written,
                        remote,
                        hooks,
                        etag=_strong_etag(response.headers.get("ETag")),
                    )
                    raise TransferPaused("paused", "Paused by user; committed bytes kept")
                remaining = None if expected is None else expected - written
                request = CHUNK if remaining is None else min(CHUNK, remaining)
                try:
                    data = response.read(request)
                except http.client.IncompleteRead as error:
                    partial = len(error.partial or b"")
                    self._commit(stream, payload, offset + written, remote, hooks, etag=None)
                    raise _truncated(
                        remote,
                        offset + written + partial,
                        offset + written + request,
                        committed=offset + written,
                    ) from error
                except NETWORK_ERRORS as error:
                    self._commit(stream, payload, offset + written, remote, hooks, etag=None)
                    raise _truncated(
                        remote,
                        offset + written,
                        offset + written + request,
                        committed=offset + written,
                    ) from error
                if not data:
                    break
                try:
                    stream.write(data)
                except OSError as error:
                    raise _disk_error(error, remote) from error
                written += len(data)
                hooks.progress(completed_bytes + offset + written)
                if written >= next_sync or (time.monotonic() - last_sync) >= SYNC_INTERVAL_SECONDS:
                    self._commit(
                        stream, payload, offset + written, remote, hooks,
                        etag=_strong_etag(response.headers.get("ETag")),
                        total_bytes=total_bytes,
                        completed_bytes=completed_bytes,
                        downloaded=offset + written,
                    )
                    next_sync = written + SYNC_INTERVAL
                    last_sync = time.monotonic()
            self._commit(
                stream, payload, offset + written, remote, hooks,
                etag=_strong_etag(response.headers.get("ETag")),
                total_bytes=total_bytes,
                completed_bytes=completed_bytes,
                downloaded=offset + written,
            )
        return written

    def _commit(
        self,
        stream,
        payload: Path,
        offset: int,
        remote: RemoteFile,
        hooks: DownloadHooks,
        *,
        etag: str | None,
        **notice: Any,
    ) -> None:
        """Checkpoint bytes first, then the durable offset. Errors propagate."""
        try:
            stream.flush()
            os.fsync(stream.fileno())
        except OSError as error:
            raise CheckpointError(
                f"Cannot commit {remote.local_path} at {offset} bytes: {error}"
            ) from error
        try:
            hooks.checkpoint(remote.local_path, offset, etag)
        except OSError as error:
            raise CheckpointError(
                f"Cannot record checkpoint for {remote.local_path} at {offset} bytes: {error}"
            ) from error
        if notice:
            self.log_event("download_progress", path=remote.local_path, offset=offset, **notice)

    def _finish_payload(
        self,
        remote: RemoteFile,
        payload: Path,
        staged: Path,
        model_id: str,
        hooks: DownloadHooks,
        *,
        retry_allowed: bool,
    ) -> None:
        """Verify, then promote to a staged file. Promotion is recoverable."""
        size = payload.stat().st_size if payload.exists() else 0
        if size != remote.size:
            raise HubError(
                f"Incomplete download for {remote.remote_path}: {size} of {remote.size} bytes"
            )
        hooks.state("verifying", None, f"Verifying {PurePosixPath(remote.local_path).name}", None)
        if has_trustworthy_digest({"sha256": remote.sha256, "git_sha1": remote.git_sha1}):
            if not self._digest_matches(payload, remote):
                if not retry_allowed:
                    raise IntegrityError(
                        f"{remote.remote_path} does not match its pinned digest"
                    )
                # Proven wrong bytes: quarantine, then a clean restart from zero.
                self._quarantine(payload, remote, reason="integrity_mismatch")
                self.log_event("integrity_retry", path=remote.local_path, size=remote.size)
                raise _IntegrityRestart(remote.local_path)
        staged.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(payload, staged)
        except OSError as error:
            raise DiskError(f"Cannot promote {remote.local_path}: {error}") from error
        self.log_event(
            "download_complete",
            path=remote.local_path,
            bytes=remote.size,
            verified=bool(remote.sha256 or remote.git_sha1),
        )

    def _digest_matches(self, path: Path, remote: RemoteFile) -> bool:
        return file_digest(path, remote)

    def _require_space(self, directory: Path, remaining: int) -> None:
        try:
            free = shutil.disk_usage(directory).free
        except OSError:
            return
        if free < remaining + SPACE_SLACK:
            raise DiskError(
                f"Not enough free space for {remaining} more bytes in {directory} "
                f"({free} free)"
            )

    def _guard(self, hooks: DownloadHooks) -> None:
        if hooks.cancelled():
            raise CancelledError()
        if hooks.paused():
            raise TransferPaused("paused", "Paused by user; committed bytes kept")


class _IntegrityRestart(TransferPaused):
    """Verified payload quarantined; the caller restarts the transfer from zero."""

    def __init__(self, local_path: str) -> None:
        super().__init__("integrity_restart", f"{local_path}: quarantined, re-downloading from zero")


def check_model(
    directory: Path,
    files: Iterable[RemoteFile],
    progress: Progress,
    cancelled: Cancelled,
    *,
    proven: Iterable[str] = (),
) -> tuple[bool, str]:
    """Confirm every manifest entry is present and byte-exact.

    `proven` lists paths already hashed against the pinned digest during this
    same run. Re-hashing a multi-gigabyte file twice only burns I/O, so those
    entries are checked by size alone; everything else is hashed.
    """
    manifest = tuple(files)
    proven_paths = frozenset(proven)
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
        if remote.local_path in proven_paths:
            completed += remote.size
            continue
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
                progress("verifying", completed, total, f"Verifying {remote.local_path}")
        if sha256 and sha256.hexdigest() != remote.sha256.lower():
            return False, remote.local_path
        if sha1 and sha1.hexdigest() != (remote.git_sha1 or "").lower():
            return False, remote.local_path
    return True, ""


def download_model(
    directory: Path,
    model: ModelSpec,
    client: HubClient,
    progress: Progress,
    cancelled: Cancelled,
    *,
    marker_path: Path | None = None,
    allow_download: bool = True,
    log: DiagnosticsLog | None = None,
    hooks: DownloadHooks | None = None,
    controller: RetryController | None = None,
    file_states: Mapping[str, Mapping[str, Any]] | None = None,
    model_id: str | None = None,
) -> tuple[bool, str]:
    """Download a pinned model, install only when every file verifies.

    Payloads live in `<model_id>.partial/`, are promoted to `.staged` after
    verification, and only then move to their final paths. The install marker is
    written last, so an interrupted install is never visible to inference.
    """
    identifier = model_id or model.model_id
    manifest = client.resolve_model(model)
    total = sum(item.size for item in manifest)
    if not allow_download:
        message = "License authorization required before download"
        raise LicenseAuthorizationError(message)
    present, missing = check_model(directory, manifest, progress, cancelled)
    if present:
        _write_marker(marker_path, manifest, identifier)
        progress("installed", total, total, "All files installed")
        return True, ""
    if hooks is None or controller is None:
        raise HubError("Download requires transfer hooks and a retry controller")
    states = file_states or {}
    # Paths proven byte-exact against the pinned digest during this run. They are
    # not hashed again during install or the final check.
    verified: set[str] = set()
    for remote in manifest:
        if cancelled():
            raise CancelledError()
        state = states.get(remote.local_path) or {}
        name = PurePosixPath(remote.local_path).name
        final = contained_path(directory, remote.local_path)
        staged = staged_path(directory, identifier, remote.local_path)
        base = sum(item.size for item in manifest if item.local_path != remote.local_path)
        if _final_matches(final, remote):
            client.log_event("file_reuse", path=remote.local_path, offset=remote.size)
            verified.add(remote.local_path)
            continue
        if staged.is_file() and not staged.is_symlink() and file_digest(staged, remote):
            verified.add(remote.local_path)
        else:
            try:
                client.download(
                    directory,
                    identifier,
                    remote,
                    hooks,
                    controller,
                    durable_offset=int(state.get("offset") or 0),
                    etag=state.get("etag"),
                    total_bytes=total,
                    completed_bytes=base,
                    resume_supported=bool(state.get("resume_supported", True)),
                )
            except _IntegrityRestart:
                # Reuse no prefix: the payload was proven wrong and is quarantined.
                client.download(
                    directory,
                    identifier,
                    remote,
                    hooks,
                    controller,
                    durable_offset=0,
                    etag=None,
                    total_bytes=total,
                    completed_bytes=base,
                    retry_allowed=False,
                    resume_supported=True,
                )
            verified.add(remote.local_path)
        progress(
            "staging",
            base + remote.size,
            total,
            f"Verified {name}",
        )
    _install_staged(directory, identifier, manifest, cancelled, progress, total, verified)
    complete, missing = check_model(directory, manifest, progress, cancelled, proven=verified)
    if not complete:
        raise HubError(f"Install incomplete for {model.name}: missing {missing}")
    _write_marker(marker_path, manifest, identifier)
    progress("installed", total, total, "All files installed")
    if log is not None:
        log.event("model_installed", model=identifier, files=len(manifest), bytes=total)
    return True, ""


def _install_staged(
    directory: Path,
    model_id: str,
    manifest: tuple[RemoteFile, ...],
    cancelled: Cancelled,
    progress: Progress,
    total: int,
    proven: Iterable[str],
) -> None:
    """Move verified staged files to their final paths; incomplete installs stay absent.

    `proven` are paths whose bytes were digested against the pinned manifest during
    this run, which is what makes promotion safe without a second multi-gigabyte
    hashing pass.
    """
    proven_paths = frozenset(proven)
    done = 0
    for remote in manifest:
        if cancelled():
            raise CancelledError()
        final = contained_path(directory, remote.local_path)
        staged = staged_path(directory, model_id, remote.local_path)
        if staged.is_file() and not staged.is_symlink():
            final.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(staged, final)
            except OSError as error:
                raise DiskError(f"Cannot install {remote.local_path}: {error}") from error
            done += remote.size
            progress("installing", done, total, f"Installing {PurePosixPath(remote.local_path).name}")
            continue
        if remote.local_path in proven_paths and final.is_file() and not final.is_symlink():
            # Already in place and proven in this run: there is nothing to move.
            done += remote.size
            continue
        raise HubError(f"{remote.local_path} is not staged; re-download it")


def _final_matches(path: Path, remote: RemoteFile) -> bool:
    """Reuse a verified final file instead of fetching it again."""
    if not path.is_file() or path.is_symlink() or path.stat().st_size != remote.size:
        return False
    if not has_trustworthy_digest({"sha256": remote.sha256, "git_sha1": remote.git_sha1}):
        # Without a digest, size is the only available evidence: still verify by
        # re-downloading rather than trusting an unproven file.
        return False
    return file_digest(path, remote)


def file_digest(path: Path, remote: RemoteFile) -> bool:
    """Hash a file and compare it to the pinned digest (SHA-256 or Git blob SHA-1)."""
    expected = remote.sha256 or remote.git_sha1
    if not expected:
        return False
    if remote.sha256:
        digest = hashlib.sha256()
    else:
        digest = hashlib.sha1(f"blob {remote.size}\0".encode())
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest() == expected.lower()


def _write_marker(marker_path: Path | None, manifest: tuple[RemoteFile, ...], model_id: str) -> None:
    if marker_path is None:
        return
    atomic_write_json(
        marker_path,
        {
            "schema": 1,
            "model_id": model_id,
            "files": [
                {
                    "path": item.local_path,
                    "size": item.size,
                    "sha256": item.sha256,
                    "git_sha1": item.git_sha1,
                }
                for item in manifest
            ],
        },
    )


def _status(response: Any) -> int:
    status = getattr(response, "status", None)
    if status is None:
        status = response.getcode()
    return int(status or 0)


def _strong_etag(value: str | None) -> str | None:
    """Only strong validators are usable for `If-Range`."""
    if not value:
        return None
    etag = value.strip()
    if not etag or etag == "*":
        return None
    if etag.startswith("W/"):
        return None
    if etag.startswith('"') and etag.endswith('"') and len(etag) > 2:
        return etag
    return f'"{etag}"'


def _declared_length(headers: Any) -> int | None:
    raw = headers.get("Content-Length") if headers is not None else None
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _parse_content_range(value: str | None) -> tuple[int | None, int | None, int | None]:
    """Parse `bytes <start>-<end>/<total>` completely; None start means reject."""
    if not value:
        return None, None, None
    parts = value.strip().split(" ", 1)
    if len(parts) != 2 or parts[0].lower() != "bytes":
        return None, None, None
    span, _, total_text = parts[1].partition("/")
    start_text, _, end_text = span.partition("-")
    try:
        start = int(start_text)
        end = int(end_text)
    except ValueError:
        return None, None, None
    if start < 0 or end < start:
        return None, None, None
    total = None
    if total_text and total_text != "*":
        try:
            total = int(total_text)
        except ValueError:
            return None, None, None
        if total <= end:
            return None, None, None
    return start, end, total


def _content_range_total(value: str | None) -> int | None:
    _, _, total = _parse_content_range(value)
    if total is not None:
        return total
    # `416` bodies use `bytes */<total>`, which the span parser rejects.
    if not value:
        return None
    _, _, total_text = value.strip().partition("/")
    try:
        return int(total_text)
    except ValueError:
        return None


def _range_error(remote: RemoteFile, offset: int, start: int | None, end: int | None) -> HubError:
    # Recoverable, not fatal: the prefix is untouched and the next connection asks
    # for the same committed offset again. A server that keeps answering with a
    # wrong range ends in the no-progress pause, not in a failed download.
    return ConnectionInterrupted(
        "range_mismatch",
        f"{remote.remote_path}: server returned a range that does not match the request "
        f"(asked for offset {offset}, got {start}-{end}). Saved prefix kept.",
        committed=offset,
    )


def _truncated(
    remote: RemoteFile,
    received: int,
    expected: int,
    *,
    committed: int | None = None,
) -> ConnectionInterrupted:
    return ConnectionInterrupted(
        "truncated_body",
        f"{remote.remote_path}: connection ended at {received} of {expected} expected bytes",
        committed=committed,
    )


def _disk_error(error: OSError, remote: RemoteFile) -> DiskError:
    return DiskError(f"{remote.local_path}: write failed with {error}")


def _network_code(error: Exception) -> str:
    name = error.__class__.__name__
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, socket.gaierror):
        return "dns"
    if isinstance(error, ConnectionError):
        return "connect"
    return name.lower()


_INTERRUPT_MESSAGE = (
    "No forward progress within the retry window; committed bytes kept, resumes on network recovery"
)


_INTERRUPTED = _INTERRUPT_MESSAGE


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


def _close_quietly(*targets: Any) -> None:
    for target in targets:
        if target is None:
            continue
        try:
            target.close()
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
