"""Process liveness, process-lifetime watching, and single-engine locking.

The engine has to answer one question before it may stop a download: did the
client process actually go away, or is its main thread merely busy? Blender only
runs `bpy.app.timers` when the main thread is free, so a heartbeat file can go
stale while Blender is alive and minimized. A file timestamp cannot tell those
apart; an OS process handle can.

Three states, not two:

* `ALIVE`   — the OS says the process is still running.
* `EXITED`  — the OS says the process finished (or the pid was reused).
* `UNKNOWN` — the OS refused to answer (access denied, unsupported platform).

`UNKNOWN` is never treated as death. A blocked, minimized, or unfocused client
is an attached client while it is alive.

Windows details use `ctypes` with explicit signatures: an unsigned `restype`
truncates a 64-bit `HANDLE` to 32 bits, which breaks the retained handle, so
handles are `c_void_p` (Constitution Article X: no third-party dependency).
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ALIVE = "alive"
EXITED = "exited"
UNKNOWN = "unknown"

_STILL_ACTIVE = 259
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
_WAIT_FAILED = 0xFFFFFFFF
_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_PARAMETER = 87

# `pidfd_open` syscall numbers by architecture; the glibc wrapper is preferred
# when the loader exports it, these are the fallback for older glibc.
_SYS_PIDFD_OPEN = {
    "x86_64": 434,
    "amd64": 434,
    "aarch64": 283,
    "arm64": 283,
    "i386": 355,
    "i686": 355,
}


def _kernel32():
    import ctypes

    library = ctypes.windll.kernel32
    if getattr(library, "_sculptr_typed", False):
        return library
    library.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_bool, ctypes.c_ulong]
    # HANDLE is pointer-sized: c_ulong would truncate it on 64-bit Windows.
    library.OpenProcess.restype = ctypes.c_void_p
    library.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    library.WaitForSingleObject.restype = ctypes.c_ulong
    library.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    library.GetExitCodeProcess.restype = ctypes.c_bool
    library.CloseHandle.argtypes = [ctypes.c_void_p]
    library.CloseHandle.restype = ctypes.c_bool
    library.GetLastError.argtypes = []
    library.GetLastError.restype = ctypes.c_ulong
    library._sculptr_typed = True
    return library


def _open_process_handle(pid: int):
    """Retained Windows handle as an integer, or `(None, last_error)` when refused.

    The handle stays a plain int here and is wrapped in `c_void_p` exactly once,
    at the call site. Wrapping it twice makes ctypes raise, which would turn
    every Windows probe into a crash instead of an answer.
    """
    handle = _kernel32().OpenProcess(
        _PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid
    )
    if handle:
        return int(handle), 0
    return None, int(_kernel32().GetLastError())


def _close_handle(handle) -> None:
    import ctypes

    if handle is None:
        return
    try:
        _kernel32().CloseHandle(ctypes.c_void_p(handle))
    except OSError:
        pass


def _win_state(pid: int) -> str:
    """One-shot Windows state from a handle wait, never from an exit code.

    `GetExitCodeProcess` reports 259 both for "still running" and for a process
    that exited with code 259, so it is ambiguous; the wait is exact.
    """
    import ctypes

    handle, error = _open_process_handle(pid)
    if handle is None:
        if error == _ERROR_ACCESS_DENIED:
            return UNKNOWN
        if error == _ERROR_INVALID_PARAMETER:
            return EXITED
        return UNKNOWN
    try:
        kernel32 = _kernel32()
        result = int(kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 0))
        if result == _WAIT_OBJECT_0:
            return EXITED
        if result == _WAIT_TIMEOUT:
            return ALIVE
        return UNKNOWN
    finally:
        _close_handle(handle)


def _posix_state(pid: int) -> str:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return EXITED
    except PermissionError:
        return ALIVE
    except OSError:
        # EINTR/EINVAL and friends say nothing about liveness.
        return UNKNOWN
    return ALIVE


def _start_identity(pid: int) -> str | None:
    """Kernel start time, used to notice pid reuse where no handle exists."""
    if not sys.platform.startswith("linux"):
        return None
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii", errors="replace").split()
    except (OSError, ValueError, IndexError):
        return None
    try:
        return fields[21]
    except IndexError:
        return None


def probe(pid: int | None) -> str:
    """Answer `ALIVE` / `EXITED` / `UNKNOWN` for one pid without keeping state."""
    if pid is None:
        return UNKNOWN
    try:
        candidate = int(pid)
    except (TypeError, ValueError):
        return UNKNOWN
    if candidate <= 0:
        return EXITED
    if sys.platform == "win32":
        return _win_state(candidate)
    if sys.platform.startswith("linux"):
        return _posix_state(candidate)
    state = _posix_state(candidate)
    # Without /proc a permission error is not proof of life either.
    return ALIVE if state == ALIVE else UNKNOWN


def process_alive(pid: int | None) -> bool:
    """True unless the OS confirmed the process finished.

    `UNKNOWN` counts as alive: the safe direction keeps a download running
    instead of destroying verified bytes on an unanswered question.
    """
    return probe(pid) != EXITED


class ProcessWatch:
    """Lifetime monitor for one process, independent of any heartbeat file.

    On Windows the handle is retained, so pid reuse cannot fool it and
    `wait()` blocks on the kernel object instead of polling. On Linux a
    `pidfd` is used when available, otherwise pid plus kernel start identity,
    which detects reuse. Elsewhere the watch degrades to `UNKNOWN`, and callers
    must not treat `UNKNOWN` as death.
    """

    def __init__(self, pid: int | None) -> None:
        self.pid = None if pid is None else int(pid)
        self._handle = None
        self._pidfd: int | None = None
        self._start: str | None = None
        self._closed = False
        if self.pid is None or self.pid <= 0:
            return
        if sys.platform == "win32":
            handle, _ = _open_process_handle(self.pid)
            self._handle = handle
            return
        if sys.platform.startswith("linux"):
            descriptor = _pidfd_open(self.pid)
            if descriptor is not None:
                self._pidfd = descriptor
            else:
                self._start = _start_identity(self.pid)
                if self._start is None:
                    # No /proc entry right now: either already gone or unreadable.
                    self._start = None
            return

    @property
    def supported(self) -> bool:
        """False when this platform cannot distinguish death from ignorance."""
        return self._handle is not None or self._pidfd is not None or self._start is not None

    def state(self) -> str:
        if self.pid is None:
            return UNKNOWN
        if self._closed:
            return UNKNOWN
        if self._handle is not None:
            import ctypes

            kernel32 = _kernel32()
            result = int(kernel32.WaitForSingleObject(ctypes.c_void_p(self._handle), 0))
            if result == _WAIT_OBJECT_0:
                return EXITED
            if result == _WAIT_TIMEOUT:
                return ALIVE
            return UNKNOWN
        if self._pidfd is not None:
            return _pidfd_state(self._pidfd)
        if self._start is not None:
            current = _start_identity(self.pid)
            if current is None:
                return EXITED
            return ALIVE if current == self._start else EXITED
        return probe(self.pid)

    def alive(self) -> bool:
        """True unless the OS confirmed this process finished.

        `UNKNOWN` is not death: a stalled or unfocused UI still owns its
        downloads, so the safe direction is to keep the engine alive.
        """
        return self.state() != EXITED

    def wait(self, timeout: float | None = None) -> bool:
        """Block until the process is confirmed exited; False on timeout."""
        if self.state() == EXITED:
            return True
        if self._handle is not None:
            import ctypes

            kernel32 = _kernel32()
            milliseconds = (
                0xFFFFFFFF if timeout is None else max(0, int(round(timeout * 1000)))
            )
            result = int(
                kernel32.WaitForSingleObject(ctypes.c_void_p(self._handle), milliseconds)
            )
            return result == _WAIT_OBJECT_0
        if self._pidfd is not None:
            return _pidfd_wait(self._pidfd, timeout)
        # Polling fallback: coarse, used only where no kernel object exists.
        import time

        deadline_monotonic = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while self.state() == ALIVE:
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                return False
            time.sleep(0.25)
        return self.state() == EXITED

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._handle is not None:
            _close_handle(self._handle)
            self._handle = None
        if self._pidfd is not None:
            try:
                os.close(self._pidfd)
            except OSError:
                pass
            self._pidfd = None

    def __enter__(self) -> "ProcessWatch":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


def _pidfd_open(pid: int) -> int | None:
    import ctypes

    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError:
        return None
    try:
        if hasattr(libc, "pidfd_open"):
            libc.pidfd_open.argtypes = [ctypes.c_int, ctypes.c_uint]
            libc.pidfd_open.restype = ctypes.c_int
            descriptor = int(libc.pidfd_open(pid, 0))
        else:
            architecture = (getattr(sys, "machine", "") or "").lower()
            number = _SYS_PIDFD_OPEN.get(architecture)
            if number is None:
                return None
            libc.syscall.restype = ctypes.c_long
            descriptor = int(libc.syscall(number, pid, 0))
    except (OSError, ValueError, TypeError):
        return None
    if descriptor < 0:
        return None
    return descriptor


def _pidfd_state(descriptor: int) -> str:
    import select

    try:
        events = select.poll()
        events.register(descriptor, select.POLLIN)
        return EXITED if events.poll(0) else ALIVE
    except (OSError, ValueError):
        return UNKNOWN


def _pidfd_wait(descriptor: int, timeout: float | None) -> bool:
    import select

    try:
        events = select.poll()
        events.register(descriptor, select.POLLIN)
        milliseconds = -1 if timeout is None else max(0, int(round(timeout * 1000)))
        return bool(events.poll(milliseconds))
    except (OSError, ValueError):
        return False


_REPLACE_TRIES = 10
_REPLACE_WAIT = 0.02


def replace_atomic(temporary: os.PathLike[str] | str, destination: os.PathLike[str] | str) -> None:
    """Rename a finished temp file onto its target, tolerating Windows readers.

    On Windows a rename fails with `ERROR_ACCESS_DENIED` while any other handle has
    the destination open — even a read-only one, because Python opens files without
    `FILE_SHARE_DELETE`. A download journal must not lose a checkpoint just because
    the UI was reading the same record, so the rename is retried briefly and only
    then reported. Anything else (a real permission problem) raises immediately.
    """
    last: PermissionError | None = None
    for attempt in range(_REPLACE_TRIES):
        try:
            os.replace(temporary, destination)
            return
        except PermissionError as error:
            last = error
            time.sleep(_REPLACE_WAIT * (attempt + 1))
    assert last is not None
    raise last


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
    so there is no stale-lock recovery to get wrong. The holder pid is written to
    a sidecar file that is *not* locked: on Windows a locked byte range also
    refuses reads from other processes, so reporting the pid from the lock file
    itself would always yield 0 and the rejected start could name nobody.

    Release deliberately does not unlink: a session or lock must never disappear
    while a worker might still write to it.
    """

    def __init__(self, path: os.PathLike[str] | str) -> None:
        self.path = Path(path)
        self.pid_path = Path(f"{self.path}.pid")
        self._stream = None

    def acquire(self) -> "EngineLock":
        if self._stream is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # "a+" never truncates: a competing holder keeps its content readable.
        stream = self.path.open("a+", encoding="utf-8")
        try:
            _lock_file(stream)
        except OSError:
            holder = _holder_pid(self.pid_path)
            stream.close()
            raise EngineAlreadyRunning(holder) from None
        stream.seek(0)
        stream.truncate()
        stream.write(f"{os.getpid()}\n")
        stream.flush()
        os.fsync(stream.fileno())
        self._stream = stream
        self._record_holder()
        return self

    def _record_holder(self) -> None:
        """Publish the holder pid where a loser can read it without touching the lock.

        Informational only: the byte lock is the authority, so a failure here must
        not stop an engine that is otherwise free to run.
        """
        try:
            temporary = Path(f"{self.pid_path}.tmp")
            with temporary.open("w", encoding="utf-8") as stream:
                stream.write(f"{os.getpid()}\n")
                stream.flush()
                os.fsync(stream.fileno())
            replace_atomic(temporary, self.pid_path)
        except OSError:
            pass

    def release(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is None:
            return
        try:
            _unlock_file(stream)
        except OSError:
            pass
        try:
            stream.close()
        except OSError:
            pass

    @property
    def acquired(self) -> bool:
        """Whether this lock holds an active stream."""
        return self._stream is not None

    @property
    def _acquired(self) -> bool:
        """Alias for acquired, used by tests that check the internal flag."""
        return self.acquired

    def __enter__(self) -> "EngineLock":
        return self.acquire()

    def __exit__(self, *_: object) -> None:
        self.release()


def _lock_file(stream) -> None:
    # msvcrt.locking locks at the *current* file position. The file is opened
    # "a+", so the position is end-of-file and differs per holder: two processes
    # would lock different bytes and both believe they own the engine. Lock one
    # byte at offset 0 in every process instead.
    stream.seek(0)
    stream.flush()
    descriptor = stream.fileno()
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock_file(stream) -> None:
    stream.seek(0)
    descriptor = stream.fileno()
    if sys.platform == "win32":
        import msvcrt

        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor, fcntl.LOCK_UN)


def _holder_pid(path: Path) -> int:
    """The pid the last holder published. 0 when nothing was readable."""
    try:
        return int(path.read_text(encoding="utf-8").strip() or 0)
    except (OSError, ValueError):
        return 0


# Windows idle timer: a locked screen or an idle power plan can put the machine to
# sleep in the middle of a multi-gigabyte download. `ES_SYSTEM_REQUIRED` is a
# per-thread request, so a download thread that dies releases it automatically;
# callers still release explicitly on pause, finish, and failure.
_ES_CONTINUOUS = 0x80000000
_ES_SYSTEM_REQUIRED = 0x00000001


def set_idle_inhibition(enabled: bool) -> bool:
    """Ask Windows not to sleep while a transfer runs. Non-Windows: no-op.

    Returns the state the OS actually applied, so a caller can log when the
    request is unsupported instead of claiming protection it does not have.
    """
    if sys.platform != "win32":
        return False
    import ctypes

    try:
        library = ctypes.windll.kernel32
        setter = library.SetThreadExecutionState
        setter.argtypes = [ctypes.c_uint]
        setter.restype = ctypes.c_uint
        flags = _ES_CONTINUOUS | (_ES_SYSTEM_REQUIRED if enabled else 0)
        return bool(setter(flags))
    except (AttributeError, OSError, ValueError):
        return False

