"""The busy gate reflects work the user can still act on, not stale bookkeeping.

Two gates must agree: the Folder panel greys the change-folder button from
`ui_helpers.has_active_jobs`, and `BridgeClient.has_busy_work` is what the
operator refuses on. The reported failure was both lying at once -- a transfer
the user had paused or discarded kept reporting "Download running" forever, so
the button stayed grey and the message told the user to pause a download that
cannot run. The state journal is written only by the engine, so a session that
died mid-chunk never corrects its own record; the intent journal is the last
thing the user said, and it decides. A transfer the user never stopped still
holds the folder, because its bytes belong there.

The job side of the same rule already existed (`_has_active_job` clears pending
claims for a dead engine); this is the transfer side.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sculptr.bridge import BridgeClient
from sculptr.transfers import (
    ACTIVE,
    DISCARDED,
    DISCARDED_STATE,
    PAUSED,
    PAUSED_STATE,
    REQUESTED,
    IntentJournal,
    TransferStateJournal,
)

from tests.support import spec


class BusyGateTests(unittest.TestCase):
    def _stopped_transfer(self, root: str, request: str) -> None:
        """A transfer the engine was writing when its session died."""
        intents = IntentJournal(root)
        intent = intents.record(
            "test-model", spec(b"x"), license_required=False, license_authorized=True
        )
        states = TransferStateJournal(root)
        record = states.begin(
            intent, [{"local_path": "w.bin", "size": 10}], attempt_id="a", session_id="s"
        )
        states.update(record, state=ACTIVE, phase="downloading", message="Writing bytes")
        if request != REQUESTED:
            intents.set_request(intent["transfer_id"], request)

    def _client_without_engine(self, root: str) -> BridgeClient:
        client = BridgeClient(root)
        client._model_root = Path(root)
        client._configured_root = Path(root)
        client._session = None
        # No process is attached, so `_is_running` reports a dead engine.
        return client

    def test_a_paused_transfer_with_no_engine_leaves_the_folder_open(self):
        with tempfile.TemporaryDirectory() as root:
            self._stopped_transfer(root, PAUSED)
            client = self._client_without_engine(root)
            records = client.transfer_records()
            self.assertEqual([record["state"] for record in records], [PAUSED_STATE])
            self.assertFalse(client.has_busy_work())

    def test_a_discarded_transfer_with_no_engine_leaves_the_folder_open(self):
        with tempfile.TemporaryDirectory() as root:
            self._stopped_transfer(root, DISCARDED)
            client = self._client_without_engine(root)
            self.assertEqual(
                [record["state"] for record in client.transfer_records()],
                [DISCARDED_STATE],
            )
            self.assertFalse(client.has_busy_work())

    def test_bytes_the_user_still_wants_keep_the_folder_locked(self):
        with tempfile.TemporaryDirectory() as root:
            self._stopped_transfer(root, REQUESTED)
            client = self._client_without_engine(root)
            self.assertEqual([record["state"] for record in client.transfer_records()], [ACTIVE])
            self.assertTrue(client.has_busy_work())


if __name__ == "__main__":
    unittest.main()
