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

"""Provides attach functionality for tmux sessions."""

import builtins
import subprocess
from typing import Any, List, Optional

import libtmux  # type: ignore


def attach(
    self: Any,
    *args: Any,
    exit_: Optional[bool] = None,
    flags_: Optional[List[str]] = None,
    **kwargs: Any,
) -> Any:
    """Attaches the interactive xonsh terminal to the tmux session."""
    session_id = getattr(self, "session_id", None)
    if session_id is None:
        session_id = getattr(self, "id", None)
    if session_id is None:
        session_id = getattr(self, "session_name", None)
    if session_id is None and hasattr(self, "refresh"):
        try:
            self.refresh()
        except Exception:
            pass
        session_id = getattr(self, "session_id", None) or getattr(self, "id", None)
    if session_id is None:
        raise ValueError("Session has no session_id")

    server = getattr(self, "server", None)
    tmux_bin = getattr(server, "tmux_bin", None) or "tmux"

    cmd: List[str] = [tmux_bin]
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
            cmd.extend(str(a) for a in arg)

    target = kwargs.get("target", None) or session_id
    if "-t" not in cmd:
        cmd.extend(["-t", str(target)])

    xsh = getattr(builtins, "__xonsh__", None)
    if (
        xsh is not None
        and getattr(xsh, "env", None) is not None
        and hasattr(xsh, "subproc_uncaptured")
    ):
        xsh.subproc_uncaptured(cmd)
    else:
        subprocess.run(cmd)

    return self
