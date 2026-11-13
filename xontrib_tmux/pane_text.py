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

"""Clean plain-text pane capture helper for ``libtmux.Pane.text()``."""

import libtmux  # type: ignore


def pane_text(self: libtmux.Pane, scrollback: bool | int = False) -> str:
    """Captures clean plain text from a tmux pane.

    Joins wrapped rows (``capture-pane -p -J``), omits ANSI colour codes,
    strips trailing whitespace from each line, drops trailing blank padding
    rows, and returns the result with no trailing newline.

    Args:
        self: Target ``libtmux.Pane`` instance.
        scrollback: Controls how much history to include. ``False`` or ``0``
            captures only the visible display; ``True`` captures the full
            scrollback history (``-S -``); a positive integer ``N`` captures
            the last ``N`` scrollback lines plus the visible display
            (``-S -N``).

    Returns:
        Captured pane contents with wrapped lines joined and trailing per-line
        whitespace and trailing blank rows removed.

    Raises:
        TypeError: If ``scrollback`` is not a ``bool`` or ``int``.
        ValueError: If ``scrollback`` is negative.
        libtmux.exc.LibTmuxException: If capturing fails or the pane is closed.
    """
    start_arg: str | None
    if isinstance(scrollback, bool):
        start_arg = "-" if scrollback else None
    elif isinstance(scrollback, int):
        if scrollback < 0:
            raise ValueError("scrollback must be non-negative")
        start_arg = None if scrollback == 0 else f"-{scrollback}"
    else:
        raise TypeError("scrollback must be a bool or int")

    cmd = ["capture-pane", "-p", "-J"]
    if start_arg is not None:
        cmd.extend(["-S", start_arg])

    proc = self.cmd(*cmd)
    if proc.stderr:
        err_msg = (
            "\n".join(proc.stderr)
            if isinstance(proc.stderr, (list, tuple))
            else str(proc.stderr)
        )
        raise libtmux.exc.LibTmuxException(err_msg)
    if getattr(proc, "returncode", 0) not in (0, None):
        raise libtmux.exc.LibTmuxException(
            f"capture-pane failed with exit status {proc.returncode}"
        )

    stdout = proc.stdout or []
    if not stdout and self.pane_id not in {
        p.pane_id for p in self.server.panes
    }:
        raise libtmux.exc.LibTmuxException(f"Pane {self.pane_id} is closed")

    lines = [line.rstrip() for line in stdout]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)
