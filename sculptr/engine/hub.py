from __future__ import annotations

import hashlib
import json
import os
import shutil
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable

from ..catalog import FileRule, ModelSpec, RepositorySpec
from .protocol import CancelledError, atomic_write_json, contained_path


Progress = Callable[[str, int, int, str], None]
Cancelled = Callable[[], bool]


class HubError(RuntimeError):
    pass


class LicenseAuthorizationError(HubError):
    pass


@dataclass(frozen=True)
class RemoteFile:
    repo_id: str
    revision: str
    remote_path: str
    local_path: str
    size: int
    sha256: str | None = None
    git_sha1: str | None = None


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
    ):
        self.endpoint = endpoint.rstrip("/")
        if opener is not None:
            self.opener = opener
        elif self.endpoint == "https://huggingface.co":
            hosts = frozenset({"huggingface.co", "hf.co"})
            self.opener = urllib.request.build_opener(_RestrictedRedirectHandler(hosts))
        else:
            self.opener = urllib.request.build_opener()

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
        try:
            with self.opener.open(url, timeout=15) as response:
                raw = response.read(16_777_217)
        except (OSError, urllib.error.URLError) as error:
            raise HubError(f"Cannot read Hugging Face metadata for {repository.repo_id}: {error}") from error
        if len(raw) > 16_777_216:
            raise HubError("Hugging Face metadata response is too large")
        try:
            metadata = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise HubError("Hugging Face metadata response is invalid") from error
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

    def download(
        self,
        remote: RemoteFile,
        destination: Path,
        progress: Callable[[int], None],
        cancelled: Cancelled,
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        existing = destination.stat().st_size if destination.exists() else 0
        if existing > remote.size:
            destination.unlink()
            existing = 0
        if existing == remote.size:
            progress(existing)
            return
        repo = urllib.parse.quote(remote.repo_id, safe="/")
        revision = urllib.parse.quote(remote.revision, safe="")
        path = urllib.parse.quote(remote.remote_path, safe="/")
        url = f"{self.endpoint}/{repo}/resolve/{revision}/{path}?download=true"
        request = urllib.request.Request(url, headers={"Range": f"bytes={existing}-"} if existing else {})
        try:
            response = self.opener.open(request, timeout=15)
        except urllib.error.HTTPError as error:
            if error.code == 416 and existing == remote.size:
                progress(existing)
                return
            raise HubError(f"Download failed for {remote.remote_path}: HTTP {error.code}") from error
        except (OSError, urllib.error.URLError) as error:
            raise HubError(f"Download failed for {remote.remote_path}: {error}") from error
        with response:
            append = existing > 0 and getattr(response, "status", response.getcode()) == 206
            if existing and not append:
                existing = 0
            mode = "ab" if append else "wb"
            with destination.open(mode) as stream:
                completed = existing
                progress(completed)
                while True:
                    if cancelled():
                        raise CancelledError()
                    chunk = response.read(1_048_576)
                    if not chunk:
                        break
                    stream.write(chunk)
                    completed += len(chunk)
                    progress(completed)
                stream.flush()
                os.fsync(stream.fileno())
        if destination.stat().st_size != remote.size:
            raise HubError(f"Incomplete download for {remote.remote_path}")


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
            while chunk := stream.read(1_048_576):
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
        progress("installed", sum(item.size for item in files), sum(item.size for item in files), "Installed")
        return final
    staging_entry = models_root.resolve() / f"{model.model_id}.partial"
    if staging_entry.is_symlink():
        raise HubError(f"Partial model directory cannot be a symlink: {model.model_id}")
    staging = contained_path(models_root, f"{model.model_id}.partial")
    staging.mkdir(parents=True, exist_ok=True)
    total = sum(item.size for item in files)
    completed_before = 0
    for remote in files:
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
