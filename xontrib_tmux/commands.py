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

"""Helper functions for interacting with tmux from xonsh."""

import os
import shutil
from typing import Optional

import libtmux  # type: ignore


def _controlling_tty_rdev() -> int:
    """Returns the device number of this process's controlling terminal.

    Reads field 7 (``tty_nr``) after the closing ``)`` of ``comm`` in
    ``/proc/self/stat``, falling back to ``os.ttyname`` on stdio descriptors.
    Returns ``0`` when the process has no controlling terminal.
    """
    try:
        with open(
            "/proc/self/stat", "r", encoding="ascii", errors="replace"
        ) as f:
            raw = f.read()
        idx = raw.rfind(")")
        if idx != -1:
            fields = raw[idx + 1 :].split()
            if len(fields) >= 5:
                return int(fields[4])
    except (OSError, ValueError, IndexError):
        pass

    for fd in (0, 1, 2):
        try:
            tty = os.ttyname(fd)
            return os.stat(tty).st_rdev
        except OSError:
            continue
    return 0


def is_inside_tmux() -> bool:
    """Returns True only when this process's controlling tty is $TMUX_PANE."""
    if not os.environ.get("TMUX") or not os.environ.get("TMUX_PANE"):
        return False
    rdev = _controlling_tty_rdev()
    if rdev == 0:
        return False
    pane_id = os.environ["TMUX_PANE"]
    try:
        server = libtmux.Server.from_env()
        res = server.cmd(
            "display-message", "-p", "-t", pane_id, "#{pane_tty}"
        )
        if res.stderr or not res.stdout:
            return False
        pane_tty = res.stdout[0].strip()
        if not pane_tty:
            return False
        return os.stat(pane_tty).st_rdev == rdev
    except (OSError, libtmux.exc.LibTmuxException, ValueError):
        return False


def _which_xonsh() -> str:
    """Finds the path to the xonsh executable in PATH."""
    path = shutil.which("xonsh")
    if path is None:
        raise FileNotFoundError("xonsh not found in PATH")
    return path


def tmuxs(
    name: Optional[str] = None, existing: bool = True
) -> libtmux.Session:
    """Creates a new tmux session running xonsh.

    Args:
        name: Name for the session. If None, tmux picks one.
        existing: Whether to accept a session that already goes by ``name``.
            If True, that session is returned as-is; if False, a session
            already using ``name`` is an error.

    Returns:
        The session named ``name``, created if it did not already exist.

    Raises:
        libtmux.exc.TmuxSessionExists: ``existing`` is False and a session
            named ``name`` already exists.
    """
    server = libtmux.Server()
    if existing and name is not None:
        session = server.sessions.get(session_name=name, default=None)
        if session is not None:
            return session
    return server.new_session(
        session_name=name, window_command=_which_xonsh()
    )


def tmuxw(name: Optional[str] = None) -> libtmux.Window:
    """Creates a new tmux window in the current session running xonsh."""
    return libtmux.Session.from_env().new_window(
        window_name=name, window_shell=_which_xonsh()
    )


def tmuxp(*args, **kwargs) -> libtmux.Pane:
    """Creates a new pane in the current window running xonsh."""
    return libtmux.Window.from_env().split(
        *args, shell=_which_xonsh(), **kwargs
    )


def curtmuxs() -> Optional[libtmux.Session]:
    """Returns the current tmux session when inside tmux, else None."""
    if not is_inside_tmux():
        return None
    try:
        return libtmux.Session.from_env()
    except libtmux.exc.LibTmuxException:
        return None


def curtmuxw() -> Optional[libtmux.Window]:
    """Returns the current tmux window when inside tmux, else None."""
    if not is_inside_tmux():
        return None
    try:
        return libtmux.Window.from_env()
    except libtmux.exc.LibTmuxException:
        return None


def curtmuxp() -> Optional[libtmux.Pane]:
    """Returns the current tmux pane when inside tmux, else None."""
    if not is_inside_tmux():
        return None
    try:
        return libtmux.Pane.from_env()
    except libtmux.exc.LibTmuxException:
        return None
