"""Safe subprocess execution: argument lists only, timeouts, cancellation, bounded output.

Long-running children (the provider CLI and eval commands) start in their own session so
TUA can signal the whole process group. On timeout or Ctrl+C TUA sends SIGTERM to the
group, waits a bounded grace period, then sends SIGKILL. The group leader is not reaped
until its group has been signalled, so the group ID cannot be reused in between.
"""

from __future__ import annotations

import contextlib
import os
import signal
import socket
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType

DEFAULT_GRACE_SECONDS = 10.0
POLL_INTERVAL = 0.1

# Variables that tie a process to a *parent* Claude Code session (set when TUA itself runs
# inside one). A task must be an independent session, and some of these carry IPC tokens.
PARENT_SESSION_VARS = frozenset(
    {
        "CLAUDECODE",
        "CLAUDE_CODE_ENTRYPOINT",
        "CLAUDE_CODE_SESSION_ID",
        "CLAUDE_CODE_CHILD_SESSION",
        "CLAUDE_CODE_SESSION_ATTENDED",
        "CLAUDE_CODE_MESSAGING_SOCKET",
        "CLAUDE_CODE_MESSAGING_TOKEN",
        "CLAUDE_CODE_SSE_PORT",
        "CLAUDE_CODE_EXECPATH",
        "CLAUDE_PID",
        "CLAUDE_AGENT_SDK_VERSION",
    }
)
# Variables that would redirect Git away from the intended repository or worktree.
GIT_LOCATION_VARS = frozenset(
    {
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_NAMESPACE",
        "GIT_PREFIX",
    }
)


def child_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Environment for child processes.

    The parent environment is inherited because provider authentication, proxies, and
    locale legitimately live there, but parent-session and Git-location variables are
    removed. TUA never logs or stores this mapping.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in PARENT_SESSION_VARS and k not in GIT_LOCATION_VARS
    }
    env["GIT_TERMINAL_PROMPT"] = "0"
    if extra:
        env.update(extra)
    return env


# ------------------------------------------------------------------- process identity


def _boot_id() -> str | None:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return None


def process_identity(pid: int) -> str | None:
    """A reuse-resistant identity for ``pid`` (boot ID + start time) on Linux, else None."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # Field 2 (comm) may contain spaces and parentheses; split after the last ')'.
    fields = stat[stat.rfind(")") + 2 :].split()
    if len(fields) < 20:
        return None
    return f"{_boot_id()}:{fields[19]}"


def pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def hostname() -> str:
    return socket.gethostname()


def process_alive(pid: int | None, identity: str | None, host: str | None) -> bool | None:
    """True/False when liveness can be determined, None when it cannot (other host)."""
    if pid is None:
        return False
    if host is not None and host != hostname():
        return None
    if not pid_exists(pid):
        return False
    if identity is not None:
        current = process_identity(pid)
        if current is not None and current != identity:
            return False
    return True


# ----------------------------------------------------------------------- cancellation


class CancelRequested(KeyboardInterrupt):
    """Raised in the main thread when TUA receives SIGTERM or SIGHUP."""


_received_signal: int | None = None


def last_signal_name() -> str:
    if _received_signal is None:
        return "SIGINT"
    return signal.Signals(_received_signal).name


@contextlib.contextmanager
def cancellation_signals() -> Iterator[None]:
    """Turn SIGTERM/SIGHUP into :class:`CancelRequested` (Ctrl+C already raises)."""
    global _received_signal
    _received_signal = None
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def handler(signum: int, frame: FrameType | None) -> None:
        global _received_signal
        _received_signal = signum
        raise CancelRequested(signal.Signals(signum).name)

    previous = {}
    for sig in (signal.SIGTERM, signal.SIGHUP):
        previous[sig] = signal.signal(sig, handler)
    try:
        yield
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)


@contextlib.contextmanager
def deferred_interrupts() -> Iterator[None]:
    """Delay SIGINT/SIGTERM/SIGHUP during a short critical section, then re-raise."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    pending: list[int] = []

    def handler(signum: int, frame: FrameType | None) -> None:
        pending.append(signum)

    sigs = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
    previous = {sig: signal.signal(sig, handler) for sig in sigs}
    try:
        yield
    finally:
        for sig, old in previous.items():
            signal.signal(sig, old)
    if pending:
        global _received_signal
        _received_signal = pending[0]
        if pending[0] == signal.SIGINT:
            raise KeyboardInterrupt
        raise CancelRequested(signal.Signals(pending[0]).name)


# ---------------------------------------------------------------------------- runner


@dataclass
class _Capture:
    limit: int
    data: bytearray = field(default_factory=bytearray)
    truncated: bool = False

    def pump(self, stream: object) -> None:
        read = getattr(stream, "read")  # noqa: B009 - stream is a binary pipe
        while True:
            chunk = read(65536)
            if not chunk:
                return
            room = self.limit - len(self.data)
            if room > 0:
                self.data.extend(chunk[:room])
            if len(chunk) > room:
                self.truncated = True


@dataclass(frozen=True)
class ProcessOutcome:
    exit_code: int | None
    stdout: bytes
    stderr: bytes
    stdout_truncated: bool
    stderr_truncated: bool
    timed_out: bool
    cancelled: bool
    duration_seconds: float
    pid: int | None
    spawn_error: str | None = None


def run_process(
    argv: list[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout_seconds: float,
    stdin_data: bytes | None = None,
    max_output_bytes: int = 16 * 1024 * 1024,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
    on_start: Callable[[int], None] | None = None,
) -> ProcessOutcome:
    """Run ``argv`` (never through a shell) with a deadline and cooperative cancellation.

    ``KeyboardInterrupt`` (Ctrl+C) and :class:`CancelRequested` are converted into a
    ``cancelled`` outcome after the process group has been terminated.
    """
    started = time.monotonic()
    posix = os.name == "posix"
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.PIPE if stdin_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=posix,
            close_fds=True,
        )
    except OSError as exc:
        return ProcessOutcome(
            exit_code=None,
            stdout=b"",
            stderr=b"",
            stdout_truncated=False,
            stderr_truncated=False,
            timed_out=False,
            cancelled=False,
            duration_seconds=time.monotonic() - started,
            pid=None,
            spawn_error=f"{type(exc).__name__}: {exc.strerror or exc}",
        )

    out = _Capture(max_output_bytes)
    err = _Capture(max(1024 * 1024, max_output_bytes // 8))
    threads = [
        threading.Thread(target=out.pump, args=(proc.stdout,), daemon=True),
        threading.Thread(target=err.pump, args=(proc.stderr,), daemon=True),
    ]
    if stdin_data is not None:
        threads.append(threading.Thread(target=_feed, args=(proc, stdin_data), daemon=True))
    for thread in threads:
        thread.start()

    timed_out = cancelled = False
    deadline = started + timeout_seconds
    try:
        if on_start is not None:
            on_start(proc.pid)
        while not _exited(proc):
            if time.monotonic() >= deadline:
                timed_out = True
                break
            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        cancelled = True
    except BaseException:
        # Never leave a child running because TUA itself failed.
        _stop_group(proc, graceful=True, grace_seconds=grace_seconds)
        proc.wait()
        raise

    try:
        _stop_group(proc, graceful=timed_out or cancelled, grace_seconds=grace_seconds)
    except KeyboardInterrupt:
        cancelled = True
        _signal_group(proc.pid, signal.SIGKILL)
    exit_code = proc.wait()
    for thread in threads:
        thread.join(timeout=5.0)
    for pipe in (proc.stdout, proc.stderr):
        if pipe is not None:
            with contextlib.suppress(OSError):
                pipe.close()
    return ProcessOutcome(
        exit_code=exit_code,
        stdout=bytes(out.data),
        stderr=bytes(err.data),
        stdout_truncated=out.truncated,
        stderr_truncated=err.truncated,
        timed_out=timed_out,
        cancelled=cancelled,
        duration_seconds=time.monotonic() - started,
        pid=proc.pid,
    )


def _feed(proc: subprocess.Popen[bytes], data: bytes) -> None:
    stdin = proc.stdin
    if stdin is None:
        return
    with contextlib.suppress(BrokenPipeError, OSError):
        stdin.write(data)
    with contextlib.suppress(BrokenPipeError, OSError):
        stdin.close()


def _exited(proc: subprocess.Popen[bytes]) -> bool:
    """Has the child exited? Where ``waitid`` exists this does *not* reap it (WNOWAIT)."""
    if not hasattr(os, "waitid"):
        return proc.poll() is not None
    try:
        info = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except ChildProcessError:
        return True
    return info is not None


def _signal_group(pgid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pgid, sig)


def _stop_group(proc: subprocess.Popen[bytes], *, graceful: bool, grace_seconds: float) -> None:
    """Terminate the child's process group, escalating to SIGKILL after the grace period."""
    if os.name != "posix":
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=grace_seconds)
            except subprocess.TimeoutExpired:
                proc.kill()
        return
    pgid = proc.pid  # start_new_session=True makes the child its own group leader
    if graceful:
        _signal_group(pgid, signal.SIGTERM)
        end = time.monotonic() + grace_seconds
        try:
            while not _exited(proc) and time.monotonic() < end:
                time.sleep(POLL_INTERVAL)
        except KeyboardInterrupt:
            pass  # a second Ctrl+C skips the rest of the grace period
    else:
        # Normal exit: ask leftover background processes in the group to stop.
        _signal_group(pgid, signal.SIGTERM)
        time.sleep(POLL_INTERVAL)
    # The leader is still unreaped here, so the group ID cannot have been reused.
    _signal_group(pgid, signal.SIGKILL)
