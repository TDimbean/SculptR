"""Engine recovery: a transfer outlives the engine that started it.

These tests are the acceptance criterion for the brief. The engine is a worker,
not the scheduler: it dies, is replaced by a new session with a new nonce, and
the download still continues from the bytes it already committed. Everything here
runs against a local fake CDN, so no real network is involved and no Blender is
required.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path

from sculptr.diagnostics import DiagnosticsLog
from sculptr.engine import daemon as daemon_module
from sculptr.engine import hub as hub_module
from sculptr.engine.daemon import EngineDaemon
from sculptr.engine.hub import HubClient
from sculptr.engine.protocol import atomic_write_json
from sculptr.process import EngineAlreadyRunning, EngineLock
from sculptr.transfers import (
    DISCARDED,
    DISCARDED_STATE,
    INSTALLED,
    PAUSED,
    PAUSED_STATE,
    REQUESTED,
    IntentJournal,
    TransferStateJournal,
    merged_records,
    partial_directory,
    partial_path,
)

from tests import support

MODEL_ID = "test-model"
HOLDER = (
    "import sys, time\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from sculptr.process import EngineLock\n"
    "lock = EngineLock(sys.argv[2]).acquire()\n"
    "print(lock.acquired, flush=True)\n"
    "time.sleep(30)\n"
)


def wait_for(predicate, timeout=40.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class EngineRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="sculptr-engine-"))
        self.models = self.root / "models"
        self.models.mkdir()
        self.repo_root = str(Path(__file__).resolve().parent.parent)
        self.data = bytes(range(197)) * 2000  # ~390 KB
        self.behaviour = support.ServerBehaviour(data=self.data, abort_after=40_000)
        self.intents = IntentJournal(self.models)
        self.states = TransferStateJournal(self.models)
        self.spec = support.spec(self.data, model_id=MODEL_ID)
        self.daemons: list[EngineDaemon] = []
        self.threads: list[threading.Thread] = []
        self.restore_chunk = hub_module.CHUNK
        hub_module.CHUNK = 8192
        hub_module.SYNC_INTERVAL = 8192
        self.addCleanup(self._restore_chunk)
        self.patched_get_model = daemon_module.get_model
        daemon_module.get_model = lambda model_id: self.spec
        self.addCleanup(lambda: setattr(daemon_module, "get_model", self.patched_get_model))
        self.intent = self.intents.record(
            MODEL_ID, self.spec, license_required=False, license_authorized=True
        )
        self.identifier = self.intent["transfer_id"]

    def _restore_chunk(self):
        hub_module.CHUNK = self.restore_chunk
        hub_module.SYNC_INTERVAL = 32 * self.restore_chunk

    def _start(self, session: Path, endpoint: str) -> EngineDaemon:
        nonce = uuid.uuid4().hex
        log = DiagnosticsLog(self.models / ".sculptr" / "logs" / f"engine-{nonce[:8]}.log")
        daemon = EngineDaemon(
            session,
            self.models,
            nonce,
            hub=HubClient(endpoint, socket_timeout=10.0, log=log),
            client_pid=os.getpid(),
            client_timeout=600.0,
            shutdown_grace=1.0,
            log=log,
        )
        # The fake CDN is local; a real outbound probe would say "offline" here.
        daemon._online_available = lambda: True
        thread = threading.Thread(target=daemon.run, daemon=True, kwargs={"poll_interval": 0.05})
        thread.start()
        self.daemons.append(daemon)
        self.threads.append(thread)
        return daemon

    def _enqueue(self, daemon: EngineDaemon) -> None:
        daemon.commands_directory.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            daemon.commands_directory / "dl-1.json",
            {
                "schema": 1,
                "session": daemon.nonce,
                "job_id": "dl-1",
                "action": "download_model",
                "model_id": MODEL_ID,
                "transfer_id": self.identifier,
                "license_authorized": True,
            },
        )

    def _stop(self, daemon: EngineDaemon) -> None:
        daemon.stop("test_finished")
        for thread in self.threads:
            thread.join(timeout=15.0)
            self.assertFalse(thread.is_alive(), "engine thread must exit after stop()")
        self.threads.clear()

    def _payload(self) -> Path:
        return partial_path(self.models, MODEL_ID, support.REMOTE_PATH)

    def _drive_to_pause(self, daemon: EngineDaemon) -> int:
        """Start the transfer, let bytes land, then pause like the user would."""
        self._enqueue(daemon)
        payload = self._payload()
        self.assertTrue(
            wait_for(lambda: payload.is_file() and payload.stat().st_size > 0, timeout=30.0),
            "the transfer must write bytes to the payload store",
        )
        self.intents.set_request(self.identifier, PAUSED)
        self.assertTrue(
            wait_for(
                lambda: self.states.load(self.identifier).get("state") == PAUSED_STATE,
                timeout=30.0,
            ),
            f"a paused intent must park the transfer: {self.states.load(self.identifier)}",
        )
        return int(self.states.load(self.identifier)["files"][support.REMOTE_PATH]["offset"])

    def test_a_user_pause_parks_the_transfer_with_committed_bytes(self):
        with support.serve(self.behaviour) as endpoint:
            daemon = self._start(self.root / "session-a", endpoint)
            committed = self._drive_to_pause(daemon)
            self._stop(daemon)
        record = self.states.load(self.identifier)
        self.assertEqual(record["state"], PAUSED_STATE)
        self.assertEqual(record["error_code"], "paused")
        self.assertGreater(committed, 0, "a pause must not report zero progress")
        payload = self._payload()
        self.assertTrue(payload.is_file(), "saved bytes stay on disk after a pause")
        self.assertEqual(payload.stat().st_size, committed)

    def test_a_replacement_engine_resumes_from_saved_bytes_and_installs(self):
        with support.serve(self.behaviour) as endpoint:
            daemon = self._start(self.root / "session-a", endpoint)
            committed = self._drive_to_pause(daemon)
            self._stop(daemon)
        first_attempt = self.states.load(self.identifier)["attempt_id"]
        # The user asks again. The intent is the only thing that changed.
        self.intents.set_request(self.identifier, REQUESTED)
        clean = support.ServerBehaviour(data=self.data)
        with support.serve(clean) as endpoint:
            second = self._start(self.root / "session-b", endpoint)
            self.assertTrue(
                wait_for(
                    lambda: self.states.load(self.identifier).get("state") == INSTALLED,
                    timeout=40.0,
                ),
                f"the transfer must install after resume: {self.states.load(self.identifier)}",
            )
            self._stop(second)
        self.assertTrue(clean.requests, "the replacement engine must contact the server")
        self.assertEqual(
            clean.requests[0]["range"],
            f"bytes={committed}-",
            "a resume must ask for the saved prefix, not restart from byte zero",
        )
        self.assertEqual(clean.requests[0]["if_range"], support.ETAG, "the validator must be re-offered")
        self.assertEqual(len(clean.requests), 1, "a clean server needs one connection to finish")
        record = self.states.load(self.identifier)
        self.assertEqual(record["transfer_id"], self.identifier, "one logical transfer")
        self.assertNotEqual(record["attempt_id"], first_attempt, "a new attempt id per engine")
        self.assertEqual(record["completed_bytes"], len(self.data))
        installed = self.models / support.REMOTE_PATH
        self.assertTrue(installed.is_file())
        self.assertEqual(installed.stat().st_size, len(self.data))
        self.assertEqual(installed.read_bytes(), self.data)
        self.assertTrue((self.models / ".sculptr" / "installs" / f"{MODEL_ID}.json").is_file())
        self.assertFalse(self._payload().exists(), "the payload is consumed on install")

    def test_a_discarded_intent_is_not_resumed_and_its_bytes_are_removed(self):
        with support.serve(self.behaviour) as endpoint:
            daemon = self._start(self.root / "session-a", endpoint)
            self._drive_to_pause(daemon)
            self._stop(daemon)
        self.assertTrue(self._payload().is_file())
        self.intents.set_request(self.identifier, DISCARDED)
        clean = support.ServerBehaviour(data=self.data)
        with support.serve(clean) as endpoint:
            second = self._start(self.root / "session-b", endpoint)
            self.assertTrue(
                wait_for(
                    lambda: self.states.load(self.identifier).get("state") == DISCARDED_STATE,
                    timeout=20.0,
                ),
                "a discarded transfer must be closed, not resumed",
            )
            self._stop(second)
        self.assertEqual(clean.requests, [], "a discarded transfer must not contact the server")
        self.assertFalse(partial_directory(self.models, MODEL_ID).exists(), "discard removes the bytes")

    def test_the_ui_sees_the_transfer_without_an_running_engine(self):
        with support.serve(self.behaviour) as endpoint:
            daemon = self._start(self.root / "session-a", endpoint)
            committed = self._drive_to_pause(daemon)
            self._stop(daemon)
        records = merged_records(self.models)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["intent_request"], PAUSED)
        self.assertEqual(records[0]["state"], PAUSED_STATE)
        self.assertEqual(records[0]["completed_bytes"], committed)

    def test_a_second_engine_on_the_same_models_root_is_refused(self):
        lock_path = self.models / ".sculptr" / "engine.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        holder = subprocess.Popen(
            [sys.executable, "-c", HOLDER, self.repo_root, str(lock_path)],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            line = holder.stdout.readline()
            if "True" not in line:
                self.fail(f"holder process did not acquire the lock: {line!r}")
            daemon = EngineDaemon(
                self.root / "session-c",
                self.models,
                uuid.uuid4().hex,
                hub=HubClient("http://127.0.0.1:1"),
                client_pid=os.getpid(),
            )
            with self.assertRaises(EngineAlreadyRunning) as caught:
                daemon.run()
            self.assertEqual(caught.exception.pid, holder.pid, "the message must name the running engine")
        finally:
            holder.kill()
            holder.wait()

    def test_quitting_the_application_parks_the_transfer_with_bytes_intact(self):
        """Blender closing is not a cancel: the bytes stay and the state is resumable."""
        behaviour = support.ServerBehaviour(data=self.data, abort_after=40_000, fail_first=1)
        with support.serve(behaviour) as endpoint:
            daemon = self._start(self.root / "session-d", endpoint)
            self._enqueue(daemon)
            payload = self._payload()
            self.assertTrue(
                wait_for(lambda: payload.is_file() and payload.stat().st_size > 0, timeout=30.0),
                "the transfer must write bytes before the quit lands",
            )
            committed = payload.stat().st_size
            daemon.stop("user_quit")
            self.assertTrue(
                wait_for(
                    lambda: self.states.load(self.identifier).get("state") in (PAUSED_STATE, INSTALLED),
                    timeout=20.0,
                ),
                f"shutdown must leave the transfer resumable: {self.states.load(self.identifier)}",
            )
            for thread in self.threads:
                thread.join(timeout=15.0)
            self.threads.clear()
        record = self.states.load(self.identifier)
        self.assertNotEqual(record["state"], "failed", "a quit must not fail the download")
        self.assertNotEqual(record.get("error_code"), "cancelled")
        if record["state"] == PAUSED_STATE:
            self.assertGreaterEqual(record["files"][support.REMOTE_PATH]["offset"], committed)
            self.assertTrue(payload.is_file(), "saved bytes stay on disk after a quit")


if __name__ == "__main__":
    unittest.main()
