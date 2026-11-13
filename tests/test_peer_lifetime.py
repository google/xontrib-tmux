# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Lifetime and subprocess-cleanup regression tests for ``RemoteXonsh``."""

from collections.abc import Iterator
import gc
import os
import pathlib
import pty
import select
import signal
import subprocess
import sys
import time
import pytest

from xontrib_tmux import RemoteXonsh


_START_TIMEOUT_S = 10.0
_DROP_EXIT_TIMEOUT_S = 3.0
_SHELL_EXIT_TIMEOUT_S = 5.0
_SIGKILL_EXIT_TIMEOUT_S = 5.0
_PTY_PROMPT_TIMEOUT_S = 10.0


def _proc_children(parent_pid: int) -> list[int]:
    """Returns live non-zombie direct child PIDs of ``parent_pid``."""
    target = str(parent_pid)
    children: list[int] = []
    for entry in os.scandir("/proc"):
        if not entry.name.isdigit():
            continue
        try:
            stat = pathlib.Path(entry.path, "stat").read_text(
                encoding="utf-8",
                errors="replace",
            )
        except OSError:
            continue
        rparen = stat.rfind(")")
        if rparen < 0:
            continue
        fields = stat[rparen + 2 :].split()
        if len(fields) >= 2 and fields[1] == target and fields[0] != "Z":
            children.append(int(entry.name))
    return sorted(children)


def _proc_tree(root_pid: int) -> list[int]:
    """Returns live descendant PIDs of ``root_pid`` (breadth-first)."""
    descendants: list[int] = []
    queue = _proc_children(root_pid)
    seen: set[int] = set()
    while queue:
        pid = queue.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        descendants.append(pid)
        queue.extend(_proc_children(pid))
    return descendants


def _is_pid_alive(pid: int) -> bool:
    """Returns True if ``pid`` exists and is not a zombie."""
    try:
        stat = pathlib.Path(f"/proc/{pid}/stat").read_text(
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError:
        return False
    except OSError:
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
    rparen = stat.rfind(")")
    if rparen < 0:
        return True
    fields = stat[rparen + 2 :].split()
    return not (fields and fields[0] == "Z")


def _wait_pids_gone(pids: list[int], timeout: float) -> list[int]:
    """Waits up to ``timeout`` seconds for ``pids`` to exit."""
    deadline = time.monotonic() + timeout
    remaining = [pid for pid in pids if _is_pid_alive(pid)]
    while remaining and time.monotonic() < deadline:
        time.sleep(0.02)
        remaining = [pid for pid in remaining if _is_pid_alive(pid)]
    return remaining


def _kill_pid_tree(pids: list[int]) -> None:
    """Best-effort cleanup of ``pids`` and their process groups."""
    for pid in reversed(pids):
        try:
            pgid = os.getpgid(pid)
            if pgid != os.getpgrp():
                os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _clean_env() -> dict[str, str]:
    """Returns a copy of ``os.environ`` without ``TMUX`` or ``TMUX_PANE``."""
    env = os.environ.copy()
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    return env


@pytest.fixture
def _isolate_no_tmux_gc_off(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[set[int]]:
    """Unsets tmux env vars, disables cyclic GC, and cleans leftover PIDs."""
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    baseline = set(_proc_tree(os.getpid()))
    tracked: set[int] = set()
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        yield tracked
    finally:
        if was_enabled:
            gc.enable()
        gc.collect()
        leaked = (set(_proc_tree(os.getpid())) - baseline) | tracked
        alive = [pid for pid in leaked if _is_pid_alive(pid)]
        if alive:
            _kill_pid_tree(alive)


def _read_pty_until(
    master_fd: int, marker: bytes, timeout: float
) -> bytes:
    """Reads from ``master_fd`` until ``marker`` appears or ``timeout``."""
    deadline = time.monotonic() + timeout
    buf = b""
    while time.monotonic() < deadline:
        remaining = max(0.01, min(0.05, deadline - time.monotonic()))
        readable, _, _ = select.select([master_fd], [], [], remaining)
        if not readable:
            continue
        try:
            chunk = os.read(master_fd, 65536)
        except OSError:
            break
        if not chunk:
            break
        buf += chunk
        if marker in buf:
            break
    return buf


def _is_xonsh_peer_pid(pid: int) -> bool:
    """Returns True if ``pid`` is a ``-m xonsh`` peer process."""
    try:
        cmdline = pathlib.Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return b"-m\x00xonsh" in cmdline


def _wait_for_new_children(
    parent_pid: int, baseline: set[int], count: int, timeout: float
) -> list[int]:
    """Waits up to ``timeout`` for ``count`` new peer children."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = [
            pid
            for pid in sorted(set(_proc_children(parent_pid)) - baseline)
            if _is_xonsh_peer_pid(pid)
        ]
        if len(found) >= count:
            return found
        time.sleep(0.02)
    return [
        pid
        for pid in sorted(set(_proc_children(parent_pid)) - baseline)
        if _is_xonsh_peer_pid(pid)
    ]


def test_unbound_send_exits_within_bound_without_gc(
    _isolate_no_tmux_gc_off: set[int],
) -> None:
    """Case (a): ``RemoteXonsh().send('echo x').out`` exits within 3.0s.

    Runs with cyclic GC disabled so cleanup cannot rely on ``gc.collect()``.
    """
    baseline = set(_proc_children(os.getpid()))

    def _run() -> str | None:
        return RemoteXonsh().send("echo x").out

    out = _run()
    assert out == "x\n"

    spawned = sorted(set(_proc_children(os.getpid())) - baseline)
    _isolate_no_tmux_gc_off.update(spawned)
    remaining = _wait_pids_gone(spawned, _DROP_EXIT_TIMEOUT_S)
    assert not remaining, (
        f"Unbound send() peer(s) {remaining} survived >"
        f"{_DROP_EXIT_TIMEOUT_S}s without gc.collect()"
    )


def test_del_bound_peer_exits_within_bound_without_gc(
    _isolate_no_tmux_gc_off: set[int],
) -> None:
    """Case (b): ``w = RemoteXonsh(); del w`` exits within 3.0s."""
    baseline = set(_proc_children(os.getpid()))
    w = RemoteXonsh()
    spawned = _wait_for_new_children(
        os.getpid(), baseline, 1, _START_TIMEOUT_S
    )
    assert spawned, "Expected hidden peer subprocess to start"
    _isolate_no_tmux_gc_off.update(spawned)
    _isolate_no_tmux_gc_off.update(_proc_tree(spawned[0]))

    del w
    remaining = _wait_pids_gone(spawned, _DROP_EXIT_TIMEOUT_S)
    assert not remaining, (
        f"Deleted peer(s) {remaining} survived >{_DROP_EXIT_TIMEOUT_S}s"
    )


def test_bare_unbound_peer_exits_within_bound_without_gc(
    _isolate_no_tmux_gc_off: set[int],
) -> None:
    """Case (c): Bare unused ``RemoteXonsh()`` exits within 3.0s."""
    baseline = set(_proc_children(os.getpid()))

    def _spawn_and_drop() -> None:
        RemoteXonsh()

    _spawn_and_drop()
    spawned = sorted(set(_proc_children(os.getpid())) - baseline)
    _isolate_no_tmux_gc_off.update(spawned)
    remaining = _wait_pids_gone(spawned, _DROP_EXIT_TIMEOUT_S)
    assert not remaining, (
        f"Bare unbound peer(s) {remaining} survived >{_DROP_EXIT_TIMEOUT_S}s"
    )


def test_unbound_peer_waits_for_in_flight_block_then_exits(
    _isolate_no_tmux_gc_off: set[int],
    tmp_path: pathlib.Path,
) -> None:
    """Case (d): Unbound peer stays alive mid-block, then exits within 3.0s."""
    baseline = set(_proc_children(os.getpid()))
    res = RemoteXonsh().send("sleep 0.5\necho done-d")
    spawned = _wait_for_new_children(
        os.getpid(), baseline, 1, _START_TIMEOUT_S
    )
    assert len(spawned) == 1, f"Expected 1 peer child, got {spawned}"
    _isolate_no_tmux_gc_off.update(spawned)

    # Wait briefly to confirm the peer stays alive while the block runs.
    time.sleep(0.15)
    assert not res.future.done()
    assert _is_pid_alive(spawned[0])

    # Wait for future completion WITHOUT dropping `res`; the controller
    # should still drop and clean up the peer once the result is filled in.
    assert res.rtn == 0
    assert res.out == "done-d\n"
    children_after = _proc_tree(spawned[0])
    _isolate_no_tmux_gc_off.update(children_after)

    remaining = _wait_pids_gone(spawned + children_after, _DROP_EXIT_TIMEOUT_S)
    assert not remaining, (
        f"In-flight peer/children {remaining} survived >"
        f"{_DROP_EXIT_TIMEOUT_S}s after block completed"
    )

    # Also test completely unbound fire-and-forget (neither peer nor result
    # bound in caller): must stay alive until the block finishes, then exit.
    baseline_2 = set(_proc_children(os.getpid()))
    started = tmp_path / "ff_started"
    finished = tmp_path / "ff_finished"

    def _fire_and_forget() -> None:
        RemoteXonsh().send(
            "import pathlib, time\n"
            f"pathlib.Path({str(started)!r}).write_text('up', "
            "encoding='utf-8')\n"
            "time.sleep(0.6)\n"
            f"pathlib.Path({str(finished)!r}).write_text('done', "
            "encoding='utf-8')"
        )

    _fire_and_forget()
    spawned_2 = _wait_for_new_children(
        os.getpid(), baseline_2, 1, _START_TIMEOUT_S
    )
    assert len(spawned_2) == 1, f"Expected 1 peer child, got {spawned_2}"
    _isolate_no_tmux_gc_off.update(spawned_2)

    # Start the exit clock only once the peer is up and running the block.
    start_deadline = time.monotonic() + _START_TIMEOUT_S
    while not started.exists() and time.monotonic() < start_deadline:
        time.sleep(0.02)
    assert started.exists(), "Fire-and-forget peer did not start block"
    time.sleep(0.15)
    assert _is_pid_alive(spawned_2[0])

    done_deadline = time.monotonic() + _START_TIMEOUT_S
    while not finished.exists() and time.monotonic() < done_deadline:
        time.sleep(0.02)
    assert finished.exists(), "Fire-and-forget block did not finish"

    remaining_2 = _wait_pids_gone(spawned_2, _DROP_EXIT_TIMEOUT_S)
    assert not remaining_2, (
        f"Fire-and-forget peer {remaining_2} survived >"
        f"{_DROP_EXIT_TIMEOUT_S}s after block completed"
    )


def test_parent_normal_exit_cleans_up_peers_and_running_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case (e): Parent normal exit cleans live & mid-block peers within 5s."""
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    script = (
        "import os, pathlib, time\n"
        "xontrib load tmux\n"
        "w1 = RemoteXonsh()\n"
        "w2 = RemoteXonsh()\n"
        "w1.send('echo ready-e').end()\n"
        "w2.send('echo ready-e2').end()\n"
        "r2 = w2.send('sleep 30')\n"
        "mypid = str(os.getpid())\n"
        "def _descendant_running():\n"
        "    children = set()\n"
        "    for entry in os.scandir('/proc'):\n"
        "        if not entry.name.isdigit():\n"
        "            continue\n"
        "        try:\n"
        "            st = pathlib.Path(entry.path, 'stat').read_text()\n"
        "            f = st[st.rfind(')') + 2:].split()\n"
        "            if len(f) >= 2 and f[1] == mypid:\n"
        "                children.add(entry.name)\n"
        "        except OSError:\n"
        "            pass\n"
        "    for entry in os.scandir('/proc'):\n"
        "        if not entry.name.isdigit():\n"
        "            continue\n"
        "        try:\n"
        "            st = pathlib.Path(entry.path, 'stat').read_text()\n"
        "            f = st[st.rfind(')') + 2:].split()\n"
        "            if len(f) >= 2 and f[1] in children:\n"
        "                return True\n"
        "        except OSError:\n"
        "            pass\n"
        "    return False\n"
        "deadline = time.monotonic() + 8.0\n"
        "while time.monotonic() < deadline and not _descendant_running():\n"
        "    time.sleep(0.05)\n"
        "print('PARENT_EXITING', flush=True)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "xonsh", "--no-rc", "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_clean_env(),
        text=True,
    )
    tracked_pids: list[int] = []
    try:
        assert proc.stdout is not None
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            tracked_pids = sorted(set(tracked_pids) | set(_proc_tree(proc.pid)))
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        out, err = proc.communicate(timeout=_SHELL_EXIT_TIMEOUT_S)
        assert proc.returncode == 0, f"stderr={err!r}"
        assert "PARENT_EXITING" in out
        remaining = _wait_pids_gone(tracked_pids, _SHELL_EXIT_TIMEOUT_S)
        assert not remaining, (
            f"Peers/children {remaining} survived >{_SHELL_EXIT_TIMEOUT_S}s "
            "after starting shell exited normally"
        )
    finally:
        tracked_pids = sorted(set(tracked_pids) | set(_proc_tree(proc.pid)))
        if proc.poll() is None:
            _kill_pid_tree([proc.pid])
        _kill_pid_tree(tracked_pids)


def test_parent_sigkill_cleans_up_peer_and_running_sleep(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case (f): SIGKILL on starting shell terminates peer & sleep in 5s."""
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    script = (
        "import time\n"
        "xontrib load tmux\n"
        "w = RemoteXonsh()\n"
        "r = w.send('sleep 30')\n"
        "print('SENT', flush=True)\n"
        "time.sleep(30)\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "xonsh", "--no-rc", "-c", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_clean_env(),
        text=True,
    )
    tracked_pids: list[int] = []
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        assert "SENT" in line

        # Wait for both the peer xonsh and its child `sleep 30`.
        deadline = time.monotonic() + _START_TIMEOUT_S
        peer_pid: int | None = None
        sleep_pid: int | None = None
        while time.monotonic() < deadline:
            children = _proc_children(proc.pid)
            if children:
                peer_pid = children[0]
                grandchildren = _proc_children(peer_pid)
                if grandchildren:
                    sleep_pid = grandchildren[0]
                    break
            time.sleep(0.05)
        assert peer_pid is not None, "Hidden peer did not start"
        assert sleep_pid is not None, "Peer sleep 30 child did not start"
        tracked_pids = [peer_pid, sleep_pid]

        os.kill(proc.pid, signal.SIGKILL)
        proc.wait(timeout=_SIGKILL_EXIT_TIMEOUT_S)

        remaining = _wait_pids_gone(tracked_pids, _SIGKILL_EXIT_TIMEOUT_S)
        assert not remaining, (
            f"Peer/sleep {remaining} survived >{_SIGKILL_EXIT_TIMEOUT_S}s "
            "after parent SIGKILL"
        )
    finally:
        tracked_pids = sorted(set(tracked_pids) | set(_proc_tree(proc.pid)))
        if proc.poll() is None:
            _kill_pid_tree([proc.pid])
        _kill_pid_tree(tracked_pids)


def test_interactive_prompt_last_underscore_and_exit_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Case (g): Interactive pty prompt cleans ``_`` replacement & on exit.

    A bare ``RemoteXonsh()`` typed at an interactive xonsh prompt stays
    alive while bound by ``_``, exits within 3.0s once ``_`` is replaced
    by another expression, and a second bound peer exits within 5.0s when
    the interactive shell exits.
    """
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    env = _clean_env()
    env["TERM"] = "xterm-256color"
    env["PROMPT"] = "> "

    master_fd, slave_fd = pty.openpty()
    proc: subprocess.Popen[bytes] | None = None
    tracked_pids: list[int] = []
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "xonsh", "-i", "--no-rc"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            preexec_fn=os.setsid,
            close_fds=True,
        )
        os.close(slave_fd)
        slave_fd = -1

        def _send_line(line: str, marker: bytes) -> None:
            os.write(master_fd, (line + "\n").encode("utf-8"))
            got = _read_pty_until(master_fd, marker, _PTY_PROMPT_TIMEOUT_S)
            assert marker in got, f"Timed out waiting for {marker!r} in {got!r}"

        # Wait for the initial prompt by running a simple echo marker.
        time.sleep(0.5)
        _send_line(
            "$PROMPT = '> '; print('__PROMPT_READY__')",
            b"__PROMPT_READY__",
        )
        _send_line(
            "xontrib load tmux; print('__LOADED__')",
            b"__LOADED__",
        )

        # Bare RemoteXonsh() is retained by `_` on the interactive prompt.
        baseline = set(_proc_children(proc.pid))
        os.write(master_fd, b"RemoteXonsh()\n")
        peers_1 = _wait_for_new_children(
            proc.pid, baseline, 1, _START_TIMEOUT_S
        )
        assert len(peers_1) == 1, f"Expected 1 interactive peer, got {peers_1}"
        tracked_pids.extend(peers_1)
        _read_pty_until(master_fd, b"RemoteXonsh hidden", _PTY_PROMPT_TIMEOUT_S)
        assert _is_pid_alive(peers_1[0])

        # Replacing `_` with `40 + 2` must drop the bare peer without exit.
        _send_line("40 + 2", b"42")
        remaining_1 = _wait_pids_gone(peers_1, _DROP_EXIT_TIMEOUT_S)
        assert not remaining_1, (
            f"Interactive bare peer {remaining_1} survived >"
            f"{_DROP_EXIT_TIMEOUT_S}s after `_` was replaced"
        )

        # A still-bound peer on the interactive prompt must exit on shell exit.
        baseline_2 = set(_proc_children(proc.pid))
        _send_line(
            "w_keep = RemoteXonsh(); print('__BOUND_STARTED__')",
            b"__BOUND_STARTED__",
        )
        peers_2 = _wait_for_new_children(
            proc.pid, baseline_2, 1, _START_TIMEOUT_S
        )
        assert len(peers_2) == 1, f"Expected bound peer, got {peers_2}"
        tracked_pids.extend(peers_2)

        os.write(master_fd, b"exit\n")
        proc.wait(timeout=_SHELL_EXIT_TIMEOUT_S)
        remaining_2 = _wait_pids_gone(peers_2, _SHELL_EXIT_TIMEOUT_S)
        assert not remaining_2, (
            f"Interactive bound peer {remaining_2} survived >"
            f"{_SHELL_EXIT_TIMEOUT_S}s after shell exit"
        )
    finally:
        if slave_fd >= 0:
            try:
                os.close(slave_fd)
            except OSError:
                pass
        try:
            os.close(master_fd)
        except OSError:
            pass
        if proc is not None:
            tracked_pids = sorted(
                set(tracked_pids) | set(_proc_tree(proc.pid))
            )
            if proc.poll() is None:
                _kill_pid_tree([proc.pid])
        _kill_pid_tree(tracked_pids)
