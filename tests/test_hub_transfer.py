"""The HTTP transfer state machine, against a local range-capable fake CDN.

Every case here is one the engine meets in the wild: a CDN that hangs up
mid-body, a proxy that ignores `Range`, a server that answers `416`, a server
that lies about the range it served, and a server that says "wait 2 seconds".
The invariant under test in all of them is the same one the brief demands:
bytes already written and fsynced are never thrown away and never appended to
with unverified data.
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from sculptr.engine import hub
from sculptr.engine.hub import HubClient, RemoteFile, ResumeUnsupported, TransferPaused
from sculptr.transfers import partial_path, staged_path

from tests.support import EventLog, REMOTE_PATH, ServerBehaviour, controller, hooks, serve, spec


def payload_bytes(path: Path) -> bytes:
    return path.read_bytes() if path.is_file() else b""


class DownloadStateMachineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="sculptr-hub-"))
        # ~1 MiB of deterministic, incompressible-looking bytes.
        self.data = bytes(range(251)) * 4099
        self.behaviour = ServerBehaviour(data=self.data)
        self.model_id = "test-model"
        # Small reads, so a pause or a hang-up is observed mid-file the way it
        # would be in a multi-gigabyte transfer rather than only at the end.
        hub.CHUNK = 8192
        self.addCleanup(self.restore_chunk)

    def restore_chunk(self) -> None:
        hub.CHUNK = 1_048_576

    def client(self, endpoint: str, log: EventLog) -> HubClient:
        return HubClient(endpoint, socket_timeout=10.0, log=log)

    def test_clean_transfer_is_verified_and_staged(self) -> None:
        log = EventLog()
        with serve(self.behaviour) as endpoint:
            client = self.client(endpoint, log)
            remote = client.resolve_model(spec(self.data))[0]
            self.assertEqual(remote.size, len(self.data))
            self.assertEqual(remote.sha256, self.behaviour.sha256)
            client.download(self.root, self.model_id, remote, hooks(), controller())
        staged = staged_path(self.root, self.model_id, remote.local_path)
        self.assertTrue(staged.is_file())
        self.assertEqual(staged.stat().st_size, len(self.data))
        self.assertEqual(payload_bytes(staged), self.data)
        # A `.part` that survived verification is not left lying around.
        self.assertFalse(partial_path(self.root, self.model_id, remote.local_path).exists())
        self.assertIsNone(self.behaviour.requests[0]["range"])

    def test_truncated_connection_resumes_from_the_committed_offset(self) -> None:
        """The headline case: a half-written multi-gigabyte file continues where
        it stopped instead of starting over."""
        self.behaviour.abort_after = 200_000
        log = EventLog()
        checkpoints: list[tuple[str, int, str | None]] = []
        with serve(self.behaviour) as endpoint:
            client = self.client(endpoint, log)
            remote = client.resolve_model(spec(self.data))[0]
            client.download(
                self.root, self.model_id, remote, hooks(checkpoints), controller(no_progress_window=10.0)
            )
        self.assertEqual(self.behaviour.aborted, 1)
        self.assertEqual(len(self.behaviour.requests), 2)
        asked = self.behaviour.requests[1]["range"]
        self.assertIsNotNone(asked, "second connection must ask for the saved prefix")
        start = int(asked.split("=", 1)[1].split("-", 1)[0])
        self.assertGreater(start, 0, "resume must not restart from byte zero")
        self.assertIn(start, {offset for _path, offset, _etag in checkpoints})
        self.assertIn("download_resume", log.names())
        staged = staged_path(self.root, self.model_id, remote.local_path)
        self.assertEqual(payload_bytes(staged), self.data)

    def test_server_that_ignores_ranges_forces_an_explained_clean_restart(self) -> None:
        self.behaviour.support_ranges = False
        log = EventLog()
        checkpoints: list[tuple[str, int, str | None]] = []
        with serve(self.behaviour) as endpoint:
            client = self.client(endpoint, log)
            remote = client.resolve_model(spec(self.data))[0]
            payload = partial_path(self.root, self.model_id, remote.local_path)
            payload.parent.mkdir(parents=True, exist_ok=True)
            payload.write_bytes(self.data[:100_000])
            with self.assertRaises(ResumeUnsupported) as caught:
                client.download(
                    self.root,
                    self.model_id,
                    remote,
                    hooks(checkpoints),
                    controller(no_progress_window=10.0),
                    durable_offset=100_000,
                )
            self.assertEqual(caught.exception.offset, 100_000)
            self.assertTrue(payload.is_file(), "saved prefix must be kept, not deleted")
            self.assertEqual(payload.stat().st_size, caught.exception.offset)
            self.assertIn("range_ignored", log.names())
            # The engine now knows ranges are unsupported: restart from zero, logged.
            client.download(
                self.root,
                self.model_id,
                remote,
                hooks(checkpoints),
                controller(no_progress_window=10.0),
                durable_offset=caught.exception.offset,
                resume_supported=False,
            )
            self.assertIn("clean_restart", log.names())
        self.assertEqual(payload_bytes(staged_path(self.root, self.model_id, remote.local_path)), self.data)
        self.assertFalse(payload.exists(), "a restarted payload is promoted, not left half-written")

    def test_a_refused_flush_surfaces_before_the_bytes_are_claimed(self) -> None:
        """`flush`+`fsync` failing is an error, not a silent success: the
        checkpoints already recorded stay the only durable truth, the payload
        keeps whatever it holds, and nothing is promoted or verified."""
        log = EventLog()
        checkpoints: list[tuple[str, int, str | None]] = []
        real_fsync = hub.os.fsync
        calls = {"n": 0}

        def failing_fsync(fileno: int) -> None:
            calls["n"] += 1
            if calls["n"] == 3:
                raise OSError(13, "disk refused the flush")
            real_fsync(fileno)

        hub.os.fsync = failing_fsync
        hub.SYNC_INTERVAL = 8192
        try:
            with serve(self.behaviour) as endpoint:
                client = self.client(endpoint, log)
                remote = client.resolve_model(spec(self.data))[0]
                with self.assertRaises(hub.CheckpointError) as caught:
                    client.download(self.root, self.model_id, remote, hooks(checkpoints), controller())
            self.assertIn("Cannot commit", str(caught.exception))
        finally:
            hub.os.fsync = real_fsync
            hub.SYNC_INTERVAL = 32 * 1_048_576
        # Every fsync that succeeded recorded exactly one checkpoint; the refused
        # one recorded none, so no unflushed byte is ever claimed as durable.
        self.assertEqual(calls["n"], len(checkpoints) + 1)
        self.assertTrue(checkpoints, "bytes were committed before the refusal")
        payload = partial_path(self.root, self.model_id, remote.local_path)
        self.assertTrue(payload.is_file(), "committed bytes are not thrown away")
        self.assertGreaterEqual(payload.stat().st_size, checkpoints[-1][1])
        self.assertFalse(
            staged_path(self.root, self.model_id, remote.local_path).exists(),
            "an unverified payload is never promoted",
        )

    def test_416_never_deletes_saved_bytes(self) -> None:
        self.behaviour.unsatisfiable = True
        log = EventLog()
        with serve(self.behaviour) as endpoint:
            client = self.client(endpoint, log)
            remote = client.resolve_model(spec(self.data))[0]
            payload = partial_path(self.root, self.model_id, remote.local_path)
            payload.parent.mkdir(parents=True, exist_ok=True)
            payload.write_bytes(self.data[:100_000])
            with self.assertRaises(TransferPaused) as caught:
                client.download(
                    self.root,
                    self.model_id,
                    remote,
                    hooks(),
                    controller(no_progress_window=0.5),
                    durable_offset=100_000,
                    etag='"strong-validator"',
                )
            self.assertEqual(caught.exception.reason, "no_progress")
            self.assertTrue(payload.is_file(), "416 must not delete a prefix we already verified")
            self.assertEqual(payload_bytes(payload), self.data[:100_000])
            self.assertIn("range_unsatisfiable", log.names())

    def test_a_range_the_server_did_not_serve_is_rejected(self) -> None:
        """A proxy that answers with the wrong window would corrupt the file by
        appending the wrong bytes. The prefix is left exactly as it was."""
        self.behaviour.correct_content_range = False
        log = EventLog()
        with serve(self.behaviour) as endpoint:
            client = self.client(endpoint, log)
            remote = client.resolve_model(spec(self.data))[0]
            payload = partial_path(self.root, self.model_id, remote.local_path)
            payload.parent.mkdir(parents=True, exist_ok=True)
            payload.write_bytes(self.data[:100_000])
            with self.assertRaises(TransferPaused):
                client.download(
                    self.root,
                    self.model_id,
                    remote,
                    hooks(),
                    controller(no_progress_window=0.5),
                    durable_offset=100_000,
                    etag='"strong-validator"',
                )
            self.assertEqual(payload_bytes(payload), self.data[:100_000])
            rejected = log.fields("range_rejected")
            self.assertTrue(rejected, "the mismatch must be reported, not silently retried forever")
            self.assertEqual(rejected[0]["requested"], 100_000)
            self.assertEqual(rejected[0]["start"], 100_001)

    def test_retry_after_is_honoured_and_not_shortened_by_our_backoff(self) -> None:
        import time

        self.behaviour.fail_first = 1
        self.behaviour.retry_after = "2"
        started = time.monotonic()
        with serve(self.behaviour) as endpoint:
            client = self.client(endpoint, EventLog())
            remote = client.resolve_model(spec(self.data))[0]
            # Our own policy would retry in 10 ms; the server asked for 2 s.
            client.download(self.root, self.model_id, remote, hooks(), controller())
        waited = time.monotonic() - started
        self.assertGreaterEqual(waited, 2.0, f"Retry-After was shortened (waited {waited:.2f}s)")
        self.assertEqual(len(self.behaviour.requests), 2)

    def test_user_pause_keeps_committed_bytes_and_says_why(self) -> None:
        self.behaviour.chunk = 8192
        checkpoints: list[tuple[str, int, str | None]] = []
        seen = {"count": 0}

        def progress(_completed: int) -> None:
            seen["count"] += 1
            if seen["count"] >= 4:
                paused["stop"] = True

        paused = {"stop": False}
        log = EventLog()
        with serve(self.behaviour) as endpoint:
            client = self.client(endpoint, log)
            remote = client.resolve_model(spec(self.data))[0]
            payload = partial_path(self.root, self.model_id, remote.local_path)
            with self.assertRaises(TransferPaused) as caught:
                client.download(
                    self.root,
                    self.model_id,
                    remote,
                    hooks(checkpoints, paused=lambda: paused["stop"], progress=progress),
                    controller(no_progress_window=10.0),
                )
            self.assertEqual(caught.exception.reason, "paused")
            self.assertTrue(payload.is_file())
            # What the journal was told equals what is on disk. No drift.
            self.assertEqual(payload.stat().st_size, checkpoints[-1][1])
            self.assertGreater(checkpoints[-1][1], 0)
            self.assertEqual(payload_bytes(payload), self.data[: checkpoints[-1][1]])
            self.assertEqual(
                payload_bytes(staged_path(self.root, self.model_id, remote.local_path)), b""
            )

    def unprovable_remote(self) -> RemoteFile:
        """A manifest entry with no digest: nothing can prove a prefix belongs
        to this revision."""
        return RemoteFile(
            repo_id="test/repo",
            revision="0" * 40,
            remote_path=REMOTE_PATH,
            local_path=REMOTE_PATH,
            size=len(self.data),
            sha256=None,
            git_sha1=None,
        )

    def test_a_prefix_with_no_journal_and_no_digest_is_quarantined(self) -> None:
        """Bytes nobody can attribute are moved aside, never appended to."""
        remote = self.unprovable_remote()
        payload = partial_path(self.root, self.model_id, remote.local_path)
        payload.parent.mkdir(parents=True, exist_ok=True)
        payload.write_bytes(self.data[:100_000])
        log = EventLog()
        with serve(self.behaviour) as endpoint:
            client = self.client(endpoint, log)
            client.download(
                self.root,
                self.model_id,
                remote,
                hooks(),
                controller(no_progress_window=10.0),
                durable_offset=0,
            )
        self.assertIn("partial_quarantined", log.names())
        self.assertIsNone(
            self.behaviour.requests[0]["range"],
            "unattributable bytes must not be continued with a range request",
        )
        self.assertEqual(payload_bytes(staged_path(self.root, self.model_id, remote.local_path)), self.data)
        quarantined = [
            path for path in payload.parent.iterdir() if ".unverified-" in path.name
        ]
        self.assertEqual(len(quarantined), 1, "the old prefix is kept for inspection, not deleted")
        self.assertEqual(quarantined[0].read_bytes(), self.data[:100_000])

    def test_a_complete_verifiable_payload_is_adopted_without_redownloading(self) -> None:
        """A finished file that matches the pinned digest is reused even when no
        journal claims it: that is the restart-after-reinstall case."""
        remote = RemoteFile(
            repo_id="test/repo",
            revision="0" * 40,
            remote_path=REMOTE_PATH,
            local_path=REMOTE_PATH,
            size=len(self.data),
            sha256=self.behaviour.sha256,
            git_sha1=None,
        )
        payload = partial_path(self.root, self.model_id, remote.local_path)
        payload.parent.mkdir(parents=True, exist_ok=True)
        payload.write_bytes(self.data)
        log = EventLog()
        with serve(self.behaviour) as endpoint:
            client = self.client(endpoint, log)
            client.download(self.root, self.model_id, remote, hooks(), controller())
        self.assertIn("legacy_adopted", log.names())
        self.assertEqual(self.behaviour.requests, [], "no byte was requested for a verified file")
        self.assertEqual(payload_bytes(staged_path(self.root, self.model_id, remote.local_path)), self.data)


if __name__ == "__main__":
    unittest.main()
