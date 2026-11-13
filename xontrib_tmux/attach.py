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

"""Provides attach and switch-client dispatch for tmux sessions."""

from __future__ import annotations

import builtins
import os
import subprocess
from typing import Any

import libtmux  # type: ignore

from .commands import is_inside_tmux


class DifferentTmuxServerError(libtmux.exc.LibTmuxException, RuntimeError):
    """Raised when switching to a session on another tmux server."""


def _server_socket_path(server: libtmux.Server | None) -> str:
    """Resolves the Unix socket path for a libtmux ``Server``."""
    socket_path = getattr(server, "socket_path", None)
    if socket_path:
        return str(socket_path)
    socket_name = getattr(server, "socket_name", None) or "default"
    tmpdir = (os.environ.get("TMUX_TMPDIR") or "/tmp").rstrip("/") or "/tmp"
    return f"{tmpdir}/tmux-{os.getuid()}/{socket_name}"


def _format_stderr(stderr: Any) -> str:
    """Formats tmux stderr output into a readable error message."""
    if isinstance(stderr, (list, tuple)):
        return "; ".join(str(line) for line in stderr)
    return str(stderr)


def _select_switch_client(
    server: Any, caller_session: str | None
) -> str | None:
    """Returns the best client name to switch, or None when none attached."""
    proc = server.cmd(
        "list-clients",
        "-F",
        "#{client_name}\t#{client_session}\t#{client_activity}",
    )
    if getattr(proc, "stderr", None):
        raise libtmux.exc.LibTmuxException(_format_stderr(proc.stderr))

    clients: list[tuple[str, str, int]] = []
    for line in getattr(proc, "stdout", ()) or ():
        parts = line.split("\t")
        if len(parts) < 3 or not parts[0]:
            continue
        try:
            activity = int(parts[2])
        except ValueError:
            activity = 0
        clients.append((parts[0], parts[1], activity))

    if not clients:
        return None
    if caller_session is not None:
        same_sess = [c for c in clients if c[1] == caller_session]
        if same_sess:
            return max(same_sess, key=lambda c: c[2])[0]
    return max(clients, key=lambda c: c[2])[0]


def _resolve_session_id(session: Any) -> Any:
    """Extracts the session identifier from a ``libtmux.Session``."""
    session_id = getattr(session, "session_id", None)
    if session_id is None:
        session_id = getattr(session, "id", None)
    if session_id is None:
        session_id = getattr(session, "session_name", None)
    if session_id is None and hasattr(session, "refresh"):
        try:
            session.refresh()
        except Exception:
            pass
        session_id = (
            getattr(session, "session_id", None)
            or getattr(session, "id", None)
        )
    if session_id is None:
        raise ValueError("Session has no session_id")
    return session_id


def _attach_inside_tmux(
    session: Any,
    target: Any,
    args: tuple[Any, ...],
    exit_: bool | None,
    flags_: list[str] | None,
) -> Any:
    """Switches an active client on the caller's server to ``target``."""
    if exit_ or flags_ or args:
        raise ValueError(
            "exit_ and flags_ are options of tmux attach and cannot be used "
            "inside tmux"
        )

    caller_sock = os.path.realpath(os.environ.get("TMUX", "").split(",")[0])
    target_sock = os.path.realpath(
        _server_socket_path(getattr(session, "server", None))
    )
    if caller_sock != target_sock:
        raise DifferentTmuxServerError(
            f"Cannot switch client from server {caller_sock!r} to session on "
            f"different server {target_sock!r}"
        )

    caller_session: str | None = None
    try:
        caller_pane = libtmux.Pane.from_env()
        caller_session = getattr(caller_pane, "session_name", None)
    except libtmux.exc.LibTmuxException:
        caller_session = None

    client_name = _select_switch_client(session.server, caller_session)
    if client_name is not None:
        proc = session.server.cmd(
            "switch-client", "-c", client_name, "-t", str(target)
        )
    else:
        proc = session.server.cmd("switch-client", "-t", str(target))

    stderr = getattr(proc, "stderr", None)
    returncode = getattr(proc, "returncode", 0)
    if stderr or (isinstance(returncode, int) and returncode != 0):
        msg = _format_stderr(stderr) if stderr else (
            f"tmux switch-client failed with exit status {returncode}"
        )
        raise libtmux.exc.LibTmuxException(msg)
    return session


def _attach_outside_tmux(
    session: Any,
    target: Any,
    args: tuple[Any, ...],
    exit_: bool | None,
    flags_: list[str] | None,
) -> Any:
    """Runs ``tmux attach`` from a bare interactive terminal."""
    server = getattr(session, "server", None)
    tmux_bin = getattr(server, "tmux_bin", None) or "tmux"

    cmd: list[str] = [tmux_bin]
    if server is not None:
        if getattr(server, "socket_path", None):
            cmd.extend(["-S", str(server.socket_path)])
        elif getattr(server, "socket_name", None):
            cmd.extend(["-L", str(server.socket_name)])
        if getattr(server, "config_file", None):
            cmd.extend(["-f", str(server.config_file)])
        if getattr(server, "colors", None):
            if server.colors == 256:
                cmd.append("-2")
            elif server.colors == 88:
                cmd.append("-8")

    cmd.append("attach")
    if exit_:
        cmd.append("-x")
    if flags_:
        cmd.extend(flags_)
    for arg in args:
        if isinstance(arg, str):
            cmd.append(arg)
        elif isinstance(arg, (list, tuple)):
            cmd.extend(str(item) for item in arg)

    if "-t" not in cmd:
        cmd.extend(["-t", str(target)])

    xsh = getattr(builtins, "__xonsh__", None)
    rc = 0
    try:
        if (
            xsh is not None
            and getattr(xsh, "env", None) is not None
            and hasattr(xsh, "subproc_uncaptured")
        ):
            res = xsh.subproc_uncaptured(cmd)
            res_rc = getattr(res, "returncode", None)
            if isinstance(res_rc, int) and not isinstance(res_rc, bool):
                rc = res_rc
            else:
                last_rtn = getattr(getattr(xsh, "lastcmd", None), "rtn", None)
                if isinstance(last_rtn, int) and not isinstance(last_rtn, bool):
                    rc = last_rtn
        else:
            res = subprocess.run(cmd)
            res_rc = getattr(res, "returncode", None)
            if isinstance(res_rc, int) and not isinstance(res_rc, bool):
                rc = res_rc
    except subprocess.CalledProcessError as exc:
        rc = exc.returncode

    if rc != 0:
        raise libtmux.exc.LibTmuxException(
            f"tmux attach failed with exit status {rc}"
        )
    return session


def attach(
    self: Any,
    *args: Any,
    exit_: bool | None = None,
    flags_: list[str] | None = None,
    **kwargs: Any,
) -> Any:
    """Attaches to or switches the active client to the tmux session."""
    session_id = _resolve_session_id(self)
    target = kwargs.get("target", None) or session_id
    if is_inside_tmux():
        return _attach_inside_tmux(self, target, args, exit_, flags_)
    return _attach_outside_tmux(self, target, args, exit_, flags_)
