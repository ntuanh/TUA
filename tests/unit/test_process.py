from __future__ import annotations

import os
import signal
import sys
import threading
import time
from pathlib import Path

import pytest

from tua.process import (
    child_env,
    deferred_interrupts,
    process_alive,
    process_identity,
    run_process,
)

PY = sys.executable


def wait_for(path: Path, timeout: float = 10.0) -> str:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if path.exists() and path.read_text():
            return path.read_text()
        time.sleep(0.05)
    raise AssertionError(f"{path} never appeared")


def assert_dead(pid: int, timeout: float = 5.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    raise AssertionError(f"process {pid} is still alive")


def test_success_captures_output_and_stdin(tmp_path: Path) -> None:
    code = "import sys; data = sys.stdin.read(); print(data.upper()); print('err', file=sys.stderr)"
    result = run_process(
        [PY, "-c", code], cwd=tmp_path, env=child_env(), timeout_seconds=10, stdin_data=b"hello"
    )
    assert result.exit_code == 0
    assert result.stdout.strip() == b"HELLO"
    assert result.stderr.strip() == b"err"
    assert not result.timed_out
    assert not result.cancelled


def test_cwd_is_respected(tmp_path: Path) -> None:
    result = run_process(
        [PY, "-c", "import os; print(os.getcwd())"],
        cwd=tmp_path,
        env=child_env(),
        timeout_seconds=10,
    )
    assert Path(result.stdout.decode().strip()) == tmp_path


def test_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    pid_file = tmp_path / "grandchild.pid"
    code = (
        "import subprocess, sys, time\n"
        f"c = subprocess.Popen([{PY!r}, '-c', 'import time; time.sleep(300)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(c.pid))\n"
        "time.sleep(300)\n"
    )
    start = time.monotonic()
    result = run_process(
        [PY, "-c", code], cwd=tmp_path, env=child_env(), timeout_seconds=1.5, grace_seconds=1.0
    )
    assert result.timed_out
    assert time.monotonic() - start < 15
    assert_dead(int(pid_file.read_text()))


def test_sigterm_is_escalated_to_sigkill(tmp_path: Path) -> None:
    code = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)"
    start = time.monotonic()
    result = run_process(
        [PY, "-c", code], cwd=tmp_path, env=child_env(), timeout_seconds=1, grace_seconds=0.5
    )
    assert result.timed_out
    assert result.exit_code == -signal.SIGKILL
    assert time.monotonic() - start < 10


def test_ctrl_c_becomes_cancelled_outcome(tmp_path: Path) -> None:
    started = tmp_path / "started"
    code = f"import time; open({str(started)!r}, 'w').write('1'); time.sleep(300)"

    def interrupt() -> None:
        wait_for(started)
        os.kill(os.getpid(), signal.SIGINT)

    thread = threading.Thread(target=interrupt)
    thread.start()
    result = run_process(
        [PY, "-c", code], cwd=tmp_path, env=child_env(), timeout_seconds=60, grace_seconds=1.0
    )
    thread.join()
    assert result.cancelled
    assert not result.timed_out
    assert result.pid is not None
    assert_dead(result.pid)


def test_background_leftovers_are_stopped_after_normal_exit(tmp_path: Path) -> None:
    pid_file = tmp_path / "bg.pid"
    code = (
        "import subprocess\n"
        f"c = subprocess.Popen([{PY!r}, '-c', 'import time; time.sleep(300)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(c.pid))\n"
    )
    result = run_process([PY, "-c", code], cwd=tmp_path, env=child_env(), timeout_seconds=10)
    assert result.exit_code == 0
    assert_dead(int(pid_file.read_text()))


def test_output_is_bounded(tmp_path: Path) -> None:
    code = "import sys; sys.stdout.write('x' * 500000)"
    result = run_process(
        [PY, "-c", code], cwd=tmp_path, env=child_env(), timeout_seconds=10, max_output_bytes=1000
    )
    assert len(result.stdout) == 1000
    assert result.stdout_truncated


def test_spawn_error_is_reported(tmp_path: Path) -> None:
    result = run_process(
        [str(tmp_path / "missing-binary")], cwd=tmp_path, env=child_env(), timeout_seconds=5
    )
    assert result.spawn_error
    assert result.exit_code is None


def test_failing_callback_does_not_leak_the_child(tmp_path: Path) -> None:
    seen: list[int] = []

    def on_start(pid: int) -> None:
        seen.append(pid)
        raise RuntimeError("database down")

    with pytest.raises(RuntimeError):
        run_process(
            [PY, "-c", "import time; time.sleep(300)"],
            cwd=tmp_path,
            env=child_env(),
            timeout_seconds=60,
            grace_seconds=1.0,
            on_start=on_start,
        )
    assert_dead(seen[0])


def test_child_env_scrubs_parent_session_and_git_location(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_MESSAGING_TOKEN", "x")
    monkeypatch.setenv("GIT_DIR", "/elsewhere")
    monkeypatch.setenv("TUA_KEEP_ME", "1")
    env = child_env({"EXTRA": "y"})
    assert "CLAUDECODE" not in env
    assert "CLAUDE_CODE_MESSAGING_TOKEN" not in env
    assert "GIT_DIR" not in env
    assert env["TUA_KEEP_ME"] == "1"
    assert env["EXTRA"] == "y"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


def test_deferred_interrupts_delay_sigint() -> None:
    reached = []
    with pytest.raises(KeyboardInterrupt), deferred_interrupts():  # noqa: PT012
        os.kill(os.getpid(), signal.SIGINT)
        time.sleep(0.05)
        reached.append(True)
    assert reached == [True]


def test_process_liveness() -> None:
    me = os.getpid()
    assert process_alive(me, process_identity(me), None) is True
    assert process_alive(None, None, None) is False
    assert process_alive(me, "other-boot:1", None) is (process_identity(me) is None)
    assert process_alive(me, None, "some-other-host") is None
    assert process_alive(2**22 + 12345, None, None) is False
