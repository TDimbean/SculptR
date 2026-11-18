"""Once-only shutdown, so a destructor cannot repeat it at interpreter teardown.

The invariant under test: retiring a session is work that must happen at most
once per session, and never a second time from a destructor. Blender clears its
modules at quit, which drops the last reference to the bridge singleton and runs
`__del__` inside an interpreter that is already finalizing; `%TEMP%\blender.crash.txt`
recorded `EXCEPTION_ACCESS_VIOLATION` in `python313.dll` on exactly that path
(`__del__` -> `stop` -> `_log_event` -> `event` -> `_emit`, faulting at the
`datetime.now()` stamp of the second shutdown log). Five shapes are checked:
the destructor still retires a live session when nothing else did, an explicit
call marks the session retired so the destructor adds nothing, the module
exit hook -- the shape Blender itself uses -- is likewise a no-op once the
session is gone, the destructor does its one round of work without escaping,
and a session reopened by `configure()` can be retired again.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from sculptr.bridge import BridgeClient, logs_directory
from sculptr.diagnostics import DiagnosticsLog

SESSION_NAME_LENGTH = 32


class ShutdownTests(unittest.TestCase):
    def _client_with_session(self, root: str) -> tuple[BridgeClient, Path, Path]:
        client = BridgeClient(root)
        client._model_root = Path(root)
        session = Path(root) / ".sculptr" / "session" / ("a" * SESSION_NAME_LENGTH)
        for name in ("commands", "states", "signals"):
            (session / name).mkdir(parents=True, exist_ok=False)
        client._session = session
        # Logs live outside the session directory: retirement deletes the
        # session, and the record of the shutdown has to survive it.
        log_path = logs_directory(root) / "bridge.log"
        client._log = DiagnosticsLog(log_path)
        return client, session, log_path

    def test_the_net_retires_a_session_nothing_else_retired(self):
        with tempfile.TemporaryDirectory() as root:
            client, session, log_path = self._client_with_session(root)
            client.shutdown_once("__del__")
            lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line]
            retired = not session.exists()
            self.assertTrue(client._shutdown_done)
            self.assertIsNone(client._session)

        self.assertEqual(len(lines), 1)
        self.assertIn("bridge_stop", lines[0])
        self.assertIn("__del__", lines[0])
        self.assertTrue(retired)

    def test_an_explicit_shutdown_leaves_the_destructor_with_nothing_to_do(self):
        with tempfile.TemporaryDirectory() as root:
            client, session, log_path = self._client_with_session(root)
            client.stop("user_stop")
            client.shutdown_once("__del__")
            lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line]
            retired = not session.exists()

        self.assertEqual(len(lines), 1)
        self.assertIn("user_stop", lines[0])
        self.assertNotIn("__del__", lines[0])
        self.assertTrue(retired)

    def test_the_exit_hook_then_the_destructor_retire_once(self):
        with tempfile.TemporaryDirectory() as root:
            client, session, log_path = self._client_with_session(root)
            # The two paths Blender reaches at quit, in the order it reaches
            # them: the registered exit hook first, then the destructor while
            # the interpreter is being finalized.
            client.shutdown_once("bridge_stop")
            client.shutdown_once("__del__")
            lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line]
            retired = not session.exists()

        self.assertEqual(len(lines), 1)
        self.assertNotIn("__del__", lines[0])
        self.assertTrue(retired)

    def test_the_destructor_never_raises_out_of_the_teardown(self):
        with tempfile.TemporaryDirectory() as root:
            client, session, log_path = self._client_with_session(root)
            del client
            lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line]
            retired = not session.exists()

        self.assertEqual(len(lines), 1)
        self.assertIn("bridge_stop", lines[0])
        self.assertIn("__del__", lines[0])
        self.assertTrue(retired)

    def test_a_reopened_session_may_be_retired_again(self):
        with tempfile.TemporaryDirectory() as root:
            client, session, log_path = self._client_with_session(root)
            client.stop("user_stop")
            # `configure()` clears the flag when it opens a new session, and it
            # is `configure()` that restores `_model_root` the retirement needs
            # (`_retire` drops both the session and the model root).
            client._shutdown_done = False
            client._model_root = Path(root)
            client._session = session
            for name in ("commands", "states", "signals"):
                (session / name).mkdir(parents=True, exist_ok=True)
            client.shutdown_once("engine_exit")
            lines = [line for line in log_path.read_text(encoding="utf-8").splitlines() if line]
            retired = not session.exists()

        self.assertEqual(len(lines), 2)
        self.assertIn("engine_exit", lines[1])
        self.assertTrue(retired)


if __name__ == "__main__":
    unittest.main()
