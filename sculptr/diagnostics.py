"""Shared append-only diagnostics log for the UI process and the engine process.

Logs live under `<models root>/.sculptr/logs/`, outside the disposable session
directory, so a crash or a session replacement cannot destroy the evidence that
the next session needs to explain the failure. Each writer owns its own file
(`engine.log`, `bridge.log`): two processes appending one file would race during
rotation.

Line format is one structured event per line, `time level event key=value`.
Exception tracebacks follow the event line as an indented block.
"""

from __future__ import annotations

import os
import re
import threading
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

REDACTED = "[redacted]"

DEFAULT_LOG_BYTES = 5_242_880
DEFAULT_BACKUPS = 3

# Signed download URLs and bearer tokens are the only secrets that can reach a
# log line: Hugging Face redirects carry `X-Amz-Signature`/`X-Amz-Credential`,
# and a token can be echoed back inside an error message.
_SENSITIVE_KEY = r"[A-Za-z0-9_.-]*(?:token|signature|credential|secret|password|apikey|api_key|access_key|authorization|jwt)[A-Za-z0-9_.-]*"
_QUERY_SECRET = re.compile(rf"([?&])({_SENSITIVE_KEY})=([^&\s]*)", re.IGNORECASE)
_AUTH_HEADER = re.compile(r"((?:proxy-)?authorization\s*[:=]\s*)([^,;\s]+)", re.IGNORECASE)
_BEARER = re.compile(r"(bearer\s+)([^\s,;]+)", re.IGNORECASE)


def redact(value: Any) -> str:
    """Mask credentials, tokens, and signed-URL query values in a log value."""
    text = str(value)
    text = _AUTH_HEADER.sub(f"\\1{REDACTED}", text)
    text = _BEARER.sub(f"\\1{REDACTED}", text)
    return _QUERY_SECRET.sub(lambda match: f"{match.group(1)}{match.group(2)}={REDACTED}", text)


def rotate_if_needed(
    path: os.PathLike[str] | str, *, max_bytes: int, backups: int
) -> None:
    """Shift `<name>.1..N` backups so `path` stays below `max_bytes`.

    Shared by the structured logs and the engine's captured stdout, which is an
    append-mode file handle rather than a `DiagnosticsLog`.
    """
    file_path = Path(path)
    try:
        size = file_path.stat().st_size
    except OSError:
        return
    if size + 4096 <= max_bytes:
        return
    for index in range(backups, 0, -1):
        source = file_path.with_name(f"{file_path.name}.{index}")
        if not source.exists():
            continue
        if index >= backups:
            try:
                source.unlink()
            except OSError:
                pass
        else:
            try:
                os.replace(source, file_path.with_name(f"{file_path.name}.{index + 1}"))
            except OSError:
                pass
    try:
        os.replace(file_path, file_path.with_name(f"{file_path.name}.1"))
    except OSError:
        pass


class DiagnosticsLog:
    """Thread-safe structured log with size-bounded retention."""

    def __init__(
        self,
        path: os.PathLike[str] | str,
        *,
        max_bytes: int = DEFAULT_LOG_BYTES,
        backups: int = DEFAULT_BACKUPS,
        mirror_console: bool = True,
    ) -> None:
        self.path = Path(path)
        self.max_bytes = max(1024, int(max_bytes))
        self.backups = max(0, int(backups))
        self.mirror_console = mirror_console
        self._lock = threading.Lock()
        self._identities: dict[str, Any] = {}

    def bind(self, **identities: Any) -> None:
        """Attach session/pid identifiers stamped onto every following line."""
        with self._lock:
            self._identities.update(identities)

    def event(self, kind: str, *, level: str = "info", **fields: Any) -> None:
        self._emit(kind, level, fields, None)

    def exception(self, kind: str, *, fields: Mapping[str, Any] | None = None) -> None:
        """Record an event plus the traceback of the exception being handled."""
        self._emit(kind, "error", fields or {}, traceback.format_exc())

    def _emit(self, kind: str, level: str, fields: Mapping[str, Any], trace: str | None) -> None:
        stamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
        parts = [stamp, level.upper(), str(kind)]
        for name, value in {**self._identities, **fields}.items():
            if value is None:
                continue
            parts.append(f"{name}={_format(value)}")
        line = " ".join(parts)
        if trace:
            line = line + "\n" + "\n".join(f"    {row}" for row in trace.rstrip().splitlines())
        payload = redact(line) + "\n"
        with self._lock:
            self._append(payload)
        if self.mirror_console:
            _write_console(payload)

    def _append(self, payload: str) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._rotate_if_needed()
            with self.path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(payload)
        except OSError:
            pass

    def _rotate_if_needed(self) -> None:
        rotate_if_needed(self.path, max_bytes=self.max_bytes, backups=self.backups)



def _format(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return f"{value:.3f}"
    if isinstance(value, (int, str)):
        return redact(value)
    return redact(repr(value))


def _write_console(payload: str) -> None:
    import sys

    stream = sys.stderr
    try:
        stream.write(payload)
        stream.flush()
    except (OSError, ValueError):
        pass
