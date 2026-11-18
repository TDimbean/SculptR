"""Test scaffolding: a local HTTP server that behaves like a range-capable CDN.

Nothing in the suite reaches the network beyond 127.0.0.1, and nothing imports
`bpy`: the engine modules are exercised directly, with `HubClient` pointed at
this server. `EngineDaemon` is driven in-process with an injected hub.
"""

from __future__ import annotations

import hashlib
import http.server
import json
import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

REPO = "test/repo"
REVISION = "0" * 40
REMOTE_PATH = "weights.bin"
ETAG = '"strong-validator"'


@dataclass
class ServerBehaviour:
    """Knobs for the fake CDN. `requests` records what the client asked for."""

    data: bytes
    support_ranges: bool = True
    correct_content_range: bool = True
    unsatisfiable: bool = False
    fail_first: int = 0
    retry_after: str | None = None
    abort_after: int = 0
    chunk: int = 65_536
    requests: list[dict[str, Any]] = field(default_factory=list)
    aborted: int = 0

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def _handler_class(behaviour: ServerBehaviour):
    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args: Any) -> None:  # silence the test output
            pass

        def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
            path = self.path.split("?", 1)[0]
            if path.startswith("/api/models/"):
                self._metadata()
                return
            if "/resolve/" not in path:
                self.send_error(404)
                return
            self._blob()

        def _metadata(self) -> None:
            payload = json.dumps(
                {
                    "siblings": [
                        {
                            "rfilename": REMOTE_PATH,
                            "size": len(behaviour.data),
                            "blobId": hashlib.sha1(behaviour.data).hexdigest(),
                        }
                    ]
                }
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _blob(self) -> None:
            range_header = self.headers.get("Range")
            behaviour.requests.append(
                {"range": range_header, "if_range": self.headers.get("If-Range")}
            )
            if behaviour.fail_first > 0:
                behaviour.fail_first -= 1
                body = b"temporarily unavailable"
                self.send_response(500)
                if behaviour.retry_after is not None:
                    self.send_header("Retry-After", behaviour.retry_after)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if behaviour.unsatisfiable:
                self.send_response(416)
                self.send_header("Content-Range", f"files */{len(behaviour.data)}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            start = 0
            ranged = bool(range_header) and behaviour.support_ranges
            if ranged:
                try:
                    start = int(range_header.split("=", 1)[1].split("-", 1)[0])
                except (IndexError, ValueError):
                    start = 0
            body = behaviour.data[start:]
            if ranged:
                self.send_response(206)
                end = start + len(body) - 1
                declared_start = start if behaviour.correct_content_range else start + 1
                self.send_header(
                    "Content-Range", f"bytes {declared_start}-{end}/{len(behaviour.data)}"
                )
            else:
                # A server that ignores `Range` answers 200 with the whole body.
                self.send_response(200)
            self.send_header("ETag", ETAG)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            written = 0
            for index in range(0, len(body), behaviour.chunk):
                piece = body[index : index + behaviour.chunk]
                if behaviour.abort_after and written + len(piece) > behaviour.abort_after:
                    # Lie about Content-Length and hang up: the client sees a
                    # truncated body, which is how a real CDN dies mid-transfer.
                    piece = behaviour.data[start + written : start + behaviour.abort_after]
                    behaviour.abort_after = 0
                    behaviour.aborted += 1
                    self.wfile.write(piece)
                    self.close_connection = True
                    return
                self.wfile.write(piece)
                written += len(piece)

    return Handler


@contextmanager
def serve(behaviour: ServerBehaviour) -> Iterator[str]:
    """Run the fake CDN on an ephemeral port and yield its endpoint URL."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _handler_class(behaviour))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


def spec(data: bytes, *, model_id: str = "test-model", target_prefix: str = ""):
    """A catalog spec whose single pinned file carries the digest of `data`."""
    from sculptr.catalog import FileRule, ModelSpec, RepositorySpec

    repository = RepositorySpec(
        repo_id=REPO,
        revision=REVISION,
        target_prefix=target_prefix,
        include=(REMOTE_PATH,),
        files=(FileRule(REMOTE_PATH, hashlib.sha256(data).hexdigest()),),
    )
    return ModelSpec(
        model_id=model_id,
        name="Test Model",
        display_size=f"{len(data)} bytes",
        role="test",
        repositories=(repository,),
        restricted=False,
    )


def controller(*, no_progress_window: float = 0.5):
    """A fast retry budget so a test exercises the state machine, not the clock."""
    from sculptr.engine.hub import RetryController, RetryPolicy

    return RetryController(
        policy=RetryPolicy(base_delay=0.01, max_delay=0.05, jitter=0.0),
        no_progress_window=no_progress_window,
    )


class EventLog:
    """Captures `log.event(kind, **fields)` so a test can assert on what was said."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def event(self, kind: str, **fields: Any) -> None:
        self.events.append((kind, fields))

    def exception(self, kind: str, **fields: Any) -> None:
        self.event(kind, **fields)

    def names(self) -> list[str]:
        return [kind for kind, _ in self.events]

    def fields(self, kind: str) -> list[dict[str, Any]]:
        return [fields for name, fields in self.events if name == kind]


def hooks(
    checkpoints: list[tuple[str, int, str | None]] | None = None,
    paused=lambda: False,
    cancelled=lambda: False,
    progress=lambda _completed: None,
):
    from sculptr.engine.hub import DownloadHooks

    def checkpoint(local_path: str, offset: int, etag: str | None) -> None:
        if checkpoints is not None:
            checkpoints.append((local_path, offset, etag))

    return DownloadHooks(
        progress=progress,
        checkpoint=checkpoint,
        paused=paused,
        state=lambda *args: None,
        cancelled=cancelled,
    )
