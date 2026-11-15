"""Process liveness and single-engine locking.

Both mechanisms exist to answer one question the heartbeat alone cannot answer:
is the peer process actually gone, or is its thread just busy? A Blender main
thread stall makes the heartbeat file stale while the Blender process is very
much alive, and the daemon used to treat the two cases identically by quitting
in the middle of a download.

Windows-only details are reached through `ctypes`, matching `system.py`; no
third-party dependency is introduced (Constitution Article X).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5


def process_alive(pid: int | None) -> bool:
    """True when `pid` still names a live process.

    A permission denial means the process exists but cannot be opened, which
    counts as alive. A pid of 0/None or an unknown pid counts as dead. PID reuse
    can only produce a false "alive", which is the safe direction: it keeps a
    download running instead of killing it.
    """
    if pid is None:
        return False
    try:
        candidate = int(pid)
    except (TypeError, ValueError):
        return False
    if candidate <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, candidate)
        if handle:
            try:
                code = ctypes.c_ulong()
                if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return code.value == _STILL_ACTIVE
                return True
            finally:
                kernel32.CloseHandle(handle)
        return int(kernel32.GetLastError()) == _ERROR_ACCESS_DENIED
    try:
        os.kill(candidate, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class EngineAlreadyRunning(RuntimeError):
    def __init__(self, pid: int) -> None:
        super().__init__(
            f"SculptR engine already owns this models directory (pid {pid}); "
            "refusing to start a second engine that would write the same checkpoints"
        )
        self.pid = pid


class EngineLock:
    """Advisory exclusive lock held for the lifetime of one engine process.

    The OS releases the lock when the handle closes, including on a hard crash,
    so no stale-lock recovery is needed. The file records the holder pid only so
    a rejected start can name the process to kill.
    """

    def __init__(self, path: os.PathLike[str] | str) -> None:
        self.path = Path(path)
        self._stream = None

    def acquire(self) -> "EngineLock":
        if self._stream is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # "a+" never truncates: a competing holder keeps its content readable.
        stream = self.path.open("a+", encoding="utf-8")
        try:
            _lock_file(stream.fileno())
        except OSError:
            holder = _holder_pid(stream)
            stream.close()
            raise EngineAlreadyRunning(holder) from None
        stream.seek(0)
        stream.truncate()
        stream.write(f"{os.getpid()}\n")
        stream.flush()
        self._stream = stream
        return self

    def release(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            _unlock_file(stream.fileno())
        except OSError:
            pass
        try:
            stream.close()
        except OSError:
            pass

    def __enter__(self) -> "EngineLock":
        return self.acquire()

    def __exit__(self, *_: object) -> None:
        self.release()


def _lock_file(descriptor: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        # 1 byte at offset 0 is enough: the whole file is a mutex, not a range map.
        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(descriptor: int) -> None:
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


def _holder_pid(stream) -> int:
    try:
        stream.seek(0)
        return int((stream.read(24) or "0").strip() or 0)
    except (OSError, ValueError):
        return 0
