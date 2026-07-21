"""Bounded subprocess execution for benchmark harnesses.

The benchmark commands start their own worker processes.  ``subprocess.run``
only terminates the direct child on a timeout, which can leave Testenix or
pytest-xdist workers alive.  This module uses a Windows Job Object or an
isolated POSIX process group plus identity-tracked descendant snapshots, and
keeps every cleanup wait bounded.  POSIX descendants which detach before the
first snapshot cannot be given the same kernel-level guarantee as a Job Object.
"""

from __future__ import annotations

import contextlib
import ctypes
import functools
import os
import signal
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_CLEANUP_GRACE_SECONDS = 2.0
_PROCESS_TABLE_TIMEOUT_SECONDS = 1.0
_TRACKER_INTERVAL_SECONDS = 0.02

_ProcessIdentity = tuple[int, int] | str


@functools.lru_cache(maxsize=1)
def _darwin_child_lister() -> Any | None:
    if sys.platform != "darwin":
        return None
    try:
        library = ctypes.CDLL("/usr/lib/libproc.dylib")
        function = library.proc_listchildpids
        function.argtypes = (ctypes.c_int, ctypes.c_void_p, ctypes.c_int)
        function.restype = ctypes.c_int
        return function
    except (AttributeError, OSError):
        return None


@functools.lru_cache(maxsize=1)
def _darwin_identity_probe() -> tuple[Any, type[ctypes.Structure]] | None:
    if sys.platform != "darwin":
        return None
    try:

        class _BsdInfo(ctypes.Structure):
            _fields_ = [
                ("flags", ctypes.c_uint32),
                ("status", ctypes.c_uint32),
                ("xstatus", ctypes.c_uint32),
                ("pid", ctypes.c_uint32),
                ("ppid", ctypes.c_uint32),
                ("uid", ctypes.c_uint32),
                ("gid", ctypes.c_uint32),
                ("ruid", ctypes.c_uint32),
                ("rgid", ctypes.c_uint32),
                ("svuid", ctypes.c_uint32),
                ("svgid", ctypes.c_uint32),
                ("reserved", ctypes.c_uint32),
                ("comm", ctypes.c_char * 16),
                ("name", ctypes.c_char * 32),
                ("nfiles", ctypes.c_uint32),
                ("pgid", ctypes.c_uint32),
                ("pjobc", ctypes.c_uint32),
                ("tty_device", ctypes.c_uint32),
                ("tty_pgid", ctypes.c_uint32),
                ("nice", ctypes.c_int32),
                ("start_seconds", ctypes.c_uint64),
                ("start_microseconds", ctypes.c_uint64),
            ]

        library = ctypes.CDLL("/usr/lib/libproc.dylib")
        function = library.proc_pidinfo
        function.argtypes = (
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int,
        )
        function.restype = ctypes.c_int
        return function, _BsdInfo
    except (AttributeError, OSError):
        return None


def _process_identity(pid: int) -> _ProcessIdentity | None:
    """Return a creation token so a recycled PID is never signalled."""

    if sys.platform.startswith("linux"):
        try:
            stat_line = Path(f"/proc/{pid}/stat").read_text(encoding="ascii")
            fields_after_name = stat_line.rsplit(")", 1)[1].split()
            return fields_after_name[19]
        except (IndexError, OSError):
            return None
    if sys.platform == "darwin":
        probe = _darwin_identity_probe()
        if probe is None:
            return None
        function, structure = probe
        information = structure()
        size = ctypes.sizeof(information)
        if function(pid, 3, 0, ctypes.byref(information), size) != size:
            return None
        return int(information.start_seconds), int(information.start_microseconds)
    try:
        completed = subprocess.run(
            ("ps", "-o", "lstart=", "-p", str(pid)),
            capture_output=True,
            text=True,
            timeout=_PROCESS_TABLE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    token = completed.stdout.strip()
    return token if completed.returncode == 0 and token else None


def _posix_direct_children(pid: int) -> tuple[int, ...]:
    if sys.platform.startswith("linux"):
        children: set[int] = set()
        try:
            task_children = Path(f"/proc/{pid}/task").glob("*/children")
            for child_file in task_children:
                raw = child_file.read_text(encoding="ascii")
                children.update(int(value) for value in raw.split())
        except (OSError, ValueError):
            pass
        return tuple(sorted(children))
    if sys.platform == "darwin":
        function = _darwin_child_lister()
        if function is not None:
            values = (ctypes.c_int * 4096)()
            count = function(pid, values, ctypes.sizeof(values))
            if count > 0:
                return tuple(values[: min(count, len(values))])
            return ()
    # This fallback is used only on less common POSIX hosts. Linux and macOS
    # use cheap native snapshots so tracking does not launch processes while a
    # benchmark is being timed.
    return tuple(_posix_descendants(pid))


class _PosixTreeTracker:
    """Remember descendants before a short-lived leader can orphan them."""

    def __init__(self, root_pid: int) -> None:
        self.root_pid = root_pid
        self._root_identity = _process_identity(root_pid)
        self._identities: dict[int, _ProcessIdentity] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._capture()
        self._thread.start()

    def _capture(self) -> None:
        with self._lock:
            known = {
                pid: identity
                for pid, identity in self._identities.items()
                if _process_identity(pid) == identity
            }
        root_is_current = (
            self._root_identity is not None
            and _process_identity(self.root_pid) == self._root_identity
        )
        pending = [*([self.root_pid] if root_is_current else []), *known]
        visited: set[int] = set()
        discovered: set[int] = set()
        while pending:
            parent = pending.pop()
            if parent in visited:
                continue
            visited.add(parent)
            for child in _posix_direct_children(parent):
                if child <= 0 or child == os.getpid():
                    continue
                if child not in discovered:
                    discovered.add(child)
                    pending.append(child)
        live = dict(known)
        for pid in discovered:
            identity = _process_identity(pid)
            if identity is not None:
                live[pid] = identity
        with self._lock:
            self._identities = live

    def _run(self) -> None:
        while not self._stop.wait(_TRACKER_INTERVAL_SECONDS):
            self._capture()

    def stop(self) -> dict[int, _ProcessIdentity]:
        self._stop.set()
        self._thread.join(timeout=_CLEANUP_GRACE_SECONDS)
        self._capture()
        with self._lock:
            return {
                pid: identity
                for pid, identity in self._identities.items()
                if _process_identity(pid) == identity
            }


@dataclass(slots=True)
class _WindowsJob:
    kernel32: Any
    handle: Any
    closed: bool = False

    def terminate(self, exit_code: int = 1) -> bool:
        if self.closed:
            return True
        try:
            return bool(self.kernel32.TerminateJobObject(self.handle, exit_code))
        except Exception:
            return False

    def close(self) -> bool:
        if self.closed:
            return True
        try:
            closed = bool(self.kernel32.CloseHandle(self.handle))
        except Exception:
            return False
        if closed:
            self.closed = True
        return closed


def _windows_kill_job(process: subprocess.Popen[str]) -> _WindowsJob | None:
    """Attach *process* to a kill-on-close Job Object when Windows permits it."""

    if os.name != "nt":
        return None
    job: _WindowsJob | None = None
    try:  # pragma: no cover - exercised by the Windows CI matrix.
        from ctypes import wintypes

        class _BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_longlong),
                ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class _ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimitInformation),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = (
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        )
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            return None
        job = _WindowsJob(kernel32, handle)
        information = _ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = 0x00002000  # KILL_ON_JOB_CLOSE
        configured = kernel32.SetInformationJobObject(
            handle,
            9,  # JobObjectExtendedLimitInformation
            ctypes.byref(information),
            ctypes.sizeof(information),
        )
        raw_process_handle = vars(process).get("_handle")
        if raw_process_handle is None:
            job.close()
            return None
        process_handle = wintypes.HANDLE(int(raw_process_handle))
        if not configured or not kernel32.AssignProcessToJobObject(handle, process_handle):
            job.close()
            return None
        return job
    except (AttributeError, OSError, TypeError, ValueError):
        if job is not None:
            job.terminate()
            job.close()
        return None


def _resume_windows_process(process: subprocess.Popen[str]) -> None:
    """Resume a CREATE_SUSPENDED process only after it belongs to the Job Object."""

    if os.name != "nt":
        raise OSError("Windows process resume requested on a non-Windows host")
    try:  # pragma: no cover - exercised by the Windows CI matrix.
        from ctypes import wintypes

        raw_process_handle = vars(process).get("_handle")
        if raw_process_handle is None:
            raise OSError("subprocess has no Windows process handle")
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        ntdll.NtResumeProcess.argtypes = (wintypes.HANDLE,)
        ntdll.NtResumeProcess.restype = ctypes.c_long
        status = ntdll.NtResumeProcess(wintypes.HANDLE(int(raw_process_handle)))
        if status != 0:
            raise OSError(f"NtResumeProcess failed with status 0x{status & 0xFFFFFFFF:08x}")
    except (AttributeError, TypeError, ValueError) as error:
        raise OSError(f"cannot resume contained Windows benchmark process: {error}") from error


def _posix_descendants(root_pid: int) -> set[int]:
    """Snapshot descendants, including workers which created their own session."""

    if os.name != "posix":
        return set()
    try:
        completed = subprocess.run(
            ("ps", "-axo", "pid=,ppid="),
            capture_output=True,
            text=True,
            timeout=_PROCESS_TABLE_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if completed.returncode != 0:
        return set()

    children: dict[int, set[int]] = {}
    for line in completed.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        try:
            pid, parent = (int(field) for field in fields)
        except ValueError:
            continue
        children.setdefault(parent, set()).add(pid)

    descendants: set[int] = set()
    pending = list(children.get(root_pid, ()))
    while pending:
        pid = pending.pop()
        if pid in descendants:
            continue
        descendants.add(pid)
        pending.extend(children.get(pid, ()))
    return descendants


def _posix_signal_tree(
    root_pid: int,
    descendants: Mapping[int, _ProcessIdentity],
    signum: int,
    *,
    root_group_owned: bool,
) -> None:
    own_group = os.getpgrp()
    # ``start_new_session=True`` guarantees root_pid is the initial PGID. After
    # the leader is reaped, include that group only when a live, identity-checked
    # descendant still proves ownership; this avoids signalling a recycled PGID.
    groups: set[int] = {root_pid} if root_group_owned else set()
    for pid, identity in descendants.items():
        if _process_identity(pid) != identity:
            continue
        with contextlib.suppress(OSError):
            group = os.getpgid(pid)
            # Revalidate after resolving the PGID so a process which exited in
            # between cannot redirect cleanup at a recycled PID.
            if group != own_group and _process_identity(pid) == identity:
                groups.add(group)
    # Never signal the benchmark driver's own process group, even if a stale
    # PGID was recycled or a platform returned an unexpected group for a PID.
    groups.discard(own_group)
    for group in groups:
        with contextlib.suppress(OSError):
            os.killpg(group, signum)


def _identity_snapshot(pids: set[int]) -> dict[int, _ProcessIdentity]:
    identities: dict[int, _ProcessIdentity] = {}
    for pid in pids:
        identity = _process_identity(pid)
        if identity is not None:
            identities[pid] = identity
    return identities


def _bounded_taskkill(pid: int) -> bool:
    try:  # pragma: no cover - exercised by the Windows CI matrix.
        completed = subprocess.run(
            ("taskkill", "/PID", str(pid), "/T", "/F"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=_CLEANUP_GRACE_SECONDS,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _cleanup_windows_tree(
    process: subprocess.Popen[str],
    windows_job: _WindowsJob | None,
) -> bool:
    if windows_job is not None:
        terminated = windows_job.terminate()
        closed = windows_job.close()
        fallback = False if terminated or closed else _bounded_taskkill(process.pid)
        cleaned = terminated or closed or fallback
    else:
        cleaned = _bounded_taskkill(process.pid)
    with contextlib.suppress(OSError, ValueError):
        process.kill()
    return cleaned


def _bounded_drain(process: subprocess.Popen[str]) -> tuple[str, str]:
    try:
        return process.communicate(timeout=_CLEANUP_GRACE_SECONDS)
    except subprocess.TimeoutExpired as error:
        with contextlib.suppress(OSError, ValueError):
            process.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=_CLEANUP_GRACE_SECONDS)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()
        return _timeout_text(error.output), _timeout_text(error.stderr)


def _timeout_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value


def _terminate_process_tree(
    process: subprocess.Popen[str],
    windows_job: _WindowsJob | None,
    *,
    tracked_pids: Mapping[int, _ProcessIdentity] | None = None,
) -> tuple[str, str]:
    if os.name == "nt":  # pragma: no cover - exercised by the Windows CI matrix.
        cleaned = _cleanup_windows_tree(process, windows_job)
        output = _bounded_drain(process)
        if not cleaned:
            raise RuntimeError("could not verify cleanup of the Windows benchmark process tree")
        return output

    descendants = dict(tracked_pids or {})
    descendants.update(_identity_snapshot(_posix_descendants(process.pid)))
    _posix_signal_tree(
        process.pid,
        descendants,
        signal.SIGTERM,
        root_group_owned=process.returncode is None,
    )
    communication_complete = False
    try:
        stdout, stderr = process.communicate(timeout=_CLEANUP_GRACE_SECONDS)
        communication_complete = True
    except subprocess.TimeoutExpired:
        stdout = stderr = ""
    # A Testenix worker calls setsid(), so killing only the coordinator's
    # process group is insufficient.  Re-snapshot while the leader still
    # exists and signal every captured PID/session before the final drain.
    descendants.update(_identity_snapshot(_posix_descendants(process.pid)))
    _posix_signal_tree(
        process.pid,
        descendants,
        signal.SIGKILL,
        root_group_owned=process.returncode is None,
    )
    if communication_complete:
        return stdout, stderr
    return _bounded_drain(process)


def run_bounded_process(
    command: Sequence[str],
    *,
    cwd: str | Path,
    env: Mapping[str, str],
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Run a command with bounded, cross-platform process-tree cleanup.

    The POSIX process group is always cleaned. Session-detached descendants are
    captured by an immediate snapshot and a lightweight identity-aware poller;
    a process which both detaches and exits its leader before that first capture
    remains an inherent best-effort edge without platform-specific containment.
    """

    options: dict[str, Any]
    if os.name == "nt":
        creation_flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        creation_flags |= getattr(subprocess, "CREATE_SUSPENDED", 0x00000004)
        options = {"creationflags": creation_flags}
    else:
        options = {"start_new_session": True}
    process = subprocess.Popen(
        tuple(command),
        cwd=cwd,
        env=dict(env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        **options,
    )
    windows_job = _windows_kill_job(process)
    try:
        tracker = _PosixTreeTracker(process.pid) if os.name == "posix" else None
    except BaseException:
        _terminate_process_tree(process, windows_job)
        raise
    if os.name == "nt":
        if windows_job is None:
            with contextlib.suppress(OSError, ValueError):
                process.kill()
            _bounded_drain(process)
            raise RuntimeError(
                "cannot place Windows benchmark process in a kill-on-close Job Object"
            )
        try:
            _resume_windows_process(process)
        except OSError as error:
            cleaned = _cleanup_windows_tree(process, windows_job)
            _bounded_drain(process)
            if not cleaned:
                raise RuntimeError(
                    "could not clean up the suspended Windows benchmark process"
                ) from error
            raise
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as error:
        tracked_pids = tracker.stop() if tracker is not None else {}
        stdout, stderr = _terminate_process_tree(
            process,
            windows_job,
            tracked_pids=tracked_pids,
        )
        raise subprocess.TimeoutExpired(
            tuple(command),
            timeout,
            output=stdout,
            stderr=stderr,
        ) from error
    except BaseException:
        tracked_pids = tracker.stop() if tracker is not None else {}
        _terminate_process_tree(
            process,
            windows_job,
            tracked_pids=tracked_pids,
        )
        raise
    else:
        if tracker is not None:
            tracked_pids = tracker.stop()
            _posix_signal_tree(
                process.pid,
                tracked_pids,
                signal.SIGKILL,
                root_group_owned=False,
            )
        elif windows_job is not None:
            if not _cleanup_windows_tree(process, windows_job):
                raise RuntimeError("could not verify cleanup of the Windows benchmark process tree")
    finally:
        if tracker is not None:
            tracker.stop()
        if windows_job is not None:
            windows_job.close()
    return subprocess.CompletedProcess(tuple(command), process.returncode, stdout, stderr)


__all__ = ["run_bounded_process"]
