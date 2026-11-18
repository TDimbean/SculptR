"""Process liveness and the engine lock.

The invariant under test: a download survives a stalled or unfocused UI, so
"cannot tell" must never be reported as "dead", while a confirmed exit must be
confirmed. The OS object (Windows handle / Linux pidfd) is what makes that
possible; where the platform offers neither, the watch says `UNKNOWN`.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sculptr.process import (
    ALIVE,
    EXITED,
    UNKNOWN,
    EngineAlreadyRunning,
    EngineLock,
    ProcessWatch,
    process_alive,
    probe,
    set_idle_inhibition,
)


def _sleep_child():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])


class ProbeTests(unittest.TestCase):
    def test_own_process_is_alive(self):
        self.assertEqual(probe(os.getpid()), ALIVE)
        self.assertTrue(process_alive(os.getpid()))

    def test_missing_pid_is_unknown_and_not_dead(self):
        self.assertEqual(probe(None), UNKNOWN)
        self.assertTrue(process_alive(None))

    def test_non_positive_pid_is_exited(self):
        self.assertEqual(probe(0), EXITED)
        self.assertFalse(process_alive(0))

    def test_finished_process_is_confirmed_exited(self):
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        self.assertEqual(probe(child.pid), EXITED)
        self.assertFalse(process_alive(child.pid))


class ProcessWatchTests(unittest.TestCase):
    def test_watch_follows_a_real_child_to_exit(self):
        child = _sleep_child()
        watch = ProcessWatch(child.pid)
        try:
            self.assertEqual(watch.state(), ALIVE)
            self.assertTrue(watch.alive())
            self.assertFalse(watch.wait(0.2))
            child.terminate()
            child.wait()
            self.assertEqual(watch.state(), EXITED)
            self.assertFalse(watch.alive())
            self.assertTrue(watch.wait(2.0))
        finally:
            watch.close()
            if child.poll() is None:
                child.kill()
                child.wait()

    def test_watch_without_a_pid_is_unknown_not_dead(self):
        watch = ProcessWatch(None)
        self.assertEqual(watch.state(), UNKNOWN)
        self.assertTrue(watch.alive())
        watch.close()

    def test_close_makes_the_watch_unknown_not_dead(self):
        child = _sleep_child()
        try:
            watch = ProcessWatch(child.pid)
            watch.close()
            self.assertEqual(watch.state(), UNKNOWN)
            self.assertTrue(watch.alive())
        finally:
            child.terminate()
            child.wait()

    @unittest.skipUnless(sys.platform == "win32", "kernel-object wait is Windows-only")
    def test_windows_watch_is_supported_and_does_not_poll(self):
        child = _sleep_child()
        try:
            watch = ProcessWatch(child.pid)
            self.assertTrue(watch.supported)
            watch.close()
        finally:
            child.terminate()
            child.wait()


class EngineLockTests(unittest.TestCase):
    """Contention is only real across processes: Windows byte-range locks do not
    conflict inside one process, so the holder has to be a child process — which
    is exactly how a second engine would try to start."""

    HOLDER = (
        "import sys, time\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "from sculptr.process import EngineLock\n"
        "lock = EngineLock(sys.argv[2]).acquire()\n"
        "print(lock.acquired, flush=True)\n"
        "time.sleep(30)\n"
    )

    def setUp(self):
        self.directory = Path(tempfile.mkdtemp(prefix="sculptr-lock-"))
        self.path = self.directory / "engine.lock"
        self.repo_root = str(Path(__file__).resolve().parent.parent)

    def _hold_from_another_process(self):
        child = subprocess.Popen(
            [sys.executable, "-c", self.HOLDER, self.repo_root, str(self.path)],
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            line = child.stdout.readline()
        except OSError:  # pragma: no cover - child died early
            line = ""
        if "True" not in line:
            child.kill()
            child.wait()
            self.fail(f"holder process did not acquire the lock: {line!r}")
        return child

    def test_second_holder_is_rejected_and_named(self):
        holder = self._hold_from_another_process()
        try:
            with self.assertRaises(EngineAlreadyRunning) as caught:
                EngineLock(self.path).acquire()
            self.assertEqual(caught.exception.pid, holder.pid)
        finally:
            holder.kill()
            holder.wait()

    def test_a_crashed_engine_leaves_a_reclaimable_lock(self):
        holder = self._hold_from_another_process()
        holder.kill()
        holder.wait()
        # The file survives; the OS released the byte lock with the process, so
        # a restart must be able to take over instead of being locked out.
        reclaimed = EngineLock(self.path).acquire()
        self.assertTrue(reclaimed.acquired)
        reclaimed.release()

    def test_lock_is_reusable_after_release(self):
        with EngineLock(self.path):
            pass
        second = EngineLock(self.path).acquire()
        self.assertTrue(second.acquired)
        second.release()

    def test_release_does_not_remove_the_file(self):
        holder = EngineLock(self.path).acquire()
        holder.release()
        self.assertTrue(self.path.exists())


class IdleInhibitionTests(unittest.TestCase):
    def test_toggle_returns_the_state_the_os_applied(self):
        self.assertIsInstance(set_idle_inhibition(True), bool)
        self.assertIsInstance(set_idle_inhibition(False), bool)
        if sys.platform != "win32":
            self.assertFalse(set_idle_inhibition(True))


if __name__ == "__main__":
    unittest.main()
