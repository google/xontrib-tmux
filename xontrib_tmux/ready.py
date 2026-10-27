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

"""Waits for a freshly created tmux pane's shell to take over its tty.

A brand new pane's pty starts in canonical mode with ECHO enabled, so the
kernel itself echoes anything ``send-keys`` pushes into it -- including the
bracketed paste markers used to defeat shell auto-indentation, which show up
as literal ``^[[200~``/``^[[201~`` noise.  Interactive shells (xonsh via
prompt_toolkit, bash via readline) clear ICANON and ECHO once they start
reading a line, so those flags are a reliable "the shell is up" signal.
"""

import os
import termios
import time
from typing import Optional

import libtmux  # type: ignore


# Shell startup can be slow: xonsh with a heavy ``rc.d`` can take ~19s to
# reach its first prompt.  Waiting happens on a background thread (see
# :mod:`sender`), so budget generously rather than sending early and spraying
# paste markers.  Override with $XONTRIB_TMUX_READY_TIMEOUT.
DEFAULT_TIMEOUT = 120.0
POLL_INTERVAL = 0.02
TIMEOUT_ENV_VAR = "XONTRIB_TMUX_READY_TIMEOUT"


def default_timeout() -> float:
    """Returns the readiness timeout, honoring $XONTRIB_TMUX_READY_TIMEOUT."""
    raw = os.environ.get(TIMEOUT_ENV_VAR)
    if raw is None:
        return DEFAULT_TIMEOUT
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT


def tty_is_raw(tty: str) -> bool:
    """Reports whether ``tty`` has both ICANON and ECHO cleared.

    Args:
        tty: path to the pane's pty, e.g. ``/dev/pts/7``.

    Returns:
        True if the foreground program has put the tty in raw mode (so input
        won't be echoed by the kernel), or if the tty can't be inspected --
        in which case callers shouldn't keep waiting on it.
    """
    try:
        fd = os.open(tty, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY)
    except OSError:
        return True
    try:
        lflag = termios.tcgetattr(fd)[3]
    except termios.error:
        return True
    finally:
        os.close(fd)
    return not lflag & (termios.ICANON | termios.ECHO)


def wait_until_ready(
    pane: libtmux.Pane,
    timeout: Optional[float] = None,
    interval: float = POLL_INTERVAL,
) -> bool:
    """Blocks until ``pane``'s shell owns the tty, so sent keys aren't echoed.

    Args:
        pane: the pane about to receive keys.
        timeout: seconds to wait before giving up; defaults to
            :func:`default_timeout`.
        interval: seconds between polls.

    Returns:
        True once the pane's tty is in raw mode, False if ``timeout`` elapsed
        first or the pane has no tty.
    """
    tty = pane.pane_tty
    if not tty:
        return False
    if timeout is None:
        timeout = default_timeout()
    deadline = time.monotonic() + timeout
    while True:
        if tty_is_raw(tty):
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)
