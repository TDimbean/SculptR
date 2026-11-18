"""Poll-time heartbeat write of the bridge client.

The invariant under test: the heartbeat the UI poll writes is telemetry, so it
can never raise into the poll that calls it. Blender turns an escaping poll
exception into a panel banner, and a throttled write (once per
`BRIDGE_HEARTBEAT_INTERVAL`) makes that banner flicker in and out instead of
staying put. Two shapes are checked: the write lands inside the still-open
handle (the terminating newline is in the file, so the handle was alive when it
was written), and no exception crosses out of `_write_heartbeat`.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from sculptr.bridge import BridgeClient, SCHEMA_VERSION
from sculptr.diagnostics import DiagnosticsLog

SESSION_NAME_LENGTH = 32


class HeartbeatTests(unittest.TestCase):
    def _client_with_session(self, root: str) -> tuple[BridgeClient, Path]:
        client = BridgeClient(root)
        session = Path(root) / ".sculptr" / "session" / ("a" * SESSION_NAME_LENGTH)
        for name in ("commands", "states", "signals"):
            (session / name).mkdir(parents=True, exist_ok=False)
        client._session = session
        return client, session

    def test_heartbeat_is_written_through_the_open_handle(self):
        with tempfile.TemporaryDirectory() as root:
            client, session = self._client_with_session(root)
            client._write_heartbeat(force=True)
            path = session / "client-heartbeat.json"
            self.assertTrue(path.exists())
            text = path.read_text(encoding="utf-8")

        self.assertTrue(text.endswith("\n"))
        document = json.loads(text)
        self.assertEqual(document["schema"], SCHEMA_VERSION)
        self.assertEqual(len(document["session"]), SESSION_NAME_LENGTH)

    def test_heartbeat_never_raises_into_the_poll(self):
        with tempfile.TemporaryDirectory() as root:
            client, session = self._client_with_session(root)
            # A target that cannot be written: a directory where the heartbeat
            # file belongs. Same contract a dead handle must satisfy.
            (session / "client-heartbeat.json").mkdir()
            client._log = DiagnosticsLog(session / "bridge.log")
            client._write_heartbeat(force=True)
            # The client is still usable: the next poll would keep working.
            client._write_heartbeat(force=True)
            logs = (session / "bridge.log").read_text(encoding="utf-8")

        self.assertIn("heartbeat_failed", logs)
        self.assertEqual(logs.count("heartbeat_failed"), 2)


if __name__ == "__main__":
    unittest.main()
