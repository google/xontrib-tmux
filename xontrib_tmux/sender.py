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

"""Delivers keystrokes to tmux panes from background worker threads.

Sends must not block the calling shell: waiting for a freshly spawned pane to
finish booting its shell can take tens of seconds (see :mod:`ready`).  Each
pane gets its own daemon worker thread fed by a FIFO queue, so deliveries to
one pane stay ordered while different panes proceed in parallel, and the
caller gets a :class:`~concurrent.futures.Future` it may ignore or wait on.
"""

import queue
import sys
import threading
import traceback
from concurrent.futures import Future
from typing import Any, Callable, Dict, List, Optional, Tuple

import libtmux  # type: ignore

from .ready import wait_until_ready


_Task = Tuple["Future[Any]", Callable[[], Any]]

_lock = threading.Lock()
_queues: Dict[Any, "queue.Queue[_Task]"] = {}
# Panes already observed with their shell in control of the tty.  Waiting is
# only needed once per pane; afterwards the shell may legitimately put the tty
# back in canonical mode (e.g. while a foreground command reads input) and we
# must not stall on that.
_ready: set = set()


def _pane_key(pane: libtmux.Pane) -> Any:
    """Returns a stable dict key for ``pane``."""
    return getattr(pane, "pane_id", None) or id(pane)


def _pump(work: "queue.Queue[_Task]") -> None:
    """Runs queued tasks in order until the interpreter exits."""
    while True:
        future, task = work.get()
        if not future.set_running_or_notify_cancel():
            continue
        try:
            future.set_result(task())
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            future.set_exception(exc)
            print(
                f"xontrib-tmux: background send failed: {exc!r}",
                file=sys.stderr,
            )
            traceback.print_exc(file=sys.stderr)


def submit(pane: libtmux.Pane, task: Callable[[], Any]) -> "Future[Any]":
    """Queues ``task`` on ``pane``'s worker thread.

    Args:
        pane: pane whose FIFO the task belongs to.
        task: zero-argument callable run on the worker thread.

    Returns:
        A future resolved with the task's return value.
    """
    key = _pane_key(pane)
    with _lock:
        work = _queues.get(key)
        if work is None:
            work = queue.Queue()
            _queues[key] = work
            threading.Thread(
                target=_pump,
                args=(work,),
                name=f"xontrib-tmux-send-{key}",
                daemon=True,
            ).start()
    future: "Future[Any]" = Future()
    work.put((future, task))
    return future


def deliver(
    pane: libtmux.Pane,
    lines: List[str],
    timeout: Optional[float] = None,
) -> "Future[Any]":
    """Sends ``lines`` to ``pane`` in the background, once its shell is up.

    Args:
        pane: destination pane.
        lines: lines to type, each followed by Enter.
        timeout: seconds to wait for the pane's shell; defaults to
            :data:`ready.DEFAULT_TIMEOUT`.

    Returns:
        A future that resolves once every line has been sent.  Callers who
        don't care may drop it; failures are also reported on stderr.
    """

    def _run() -> None:
        key = _pane_key(pane)
        if key not in _ready:
            if wait_until_ready(pane, timeout=timeout):
                _ready.add(key)
        for line in lines:
            pane.send_keys(line, literal=True, enter=True)

    return submit(pane, _run)
