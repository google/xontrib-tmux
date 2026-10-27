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

import shutil
from typing import Optional

import libtmux  # type: ignore


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
    """Returns the current tmux session from environment if available."""
    try:
        return libtmux.Session.from_env()
    except libtmux.exc.NotInsideTmux:
        return None


def curtmuxw() -> Optional[libtmux.Window]:
    """Returns the current tmux window from environment if available."""
    try:
        return libtmux.Window.from_env()
    except libtmux.exc.NotInsideTmux:
        return None


def curtmuxp() -> Optional[libtmux.Pane]:
    """Returns the current tmux pane from environment if available."""
    try:
        return libtmux.Pane.from_env()
    except libtmux.exc.NotInsideTmux:
        return None
