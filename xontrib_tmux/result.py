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

"""Result and future types returned by ``RemoteXonsh.send()``."""

from collections.abc import Callable, Iterator
from concurrent.futures import CancelledError, Future, InvalidStateError
from concurrent.futures import _base as _cf_base
from dataclasses import dataclass
import sys
import threading
from typing import Any
import weakref

from xonsh.procs.pipelines import CommandPipeline, blocking_property


_STANDARD_OBJECT_DUNDERS = tuple(object.__dir__(object()))
_PUBLIC_RESULT_ATTRS = (
    "commands",
    "end",
    "err",
    "errors",
    "future",
    "lines",
    "out",
    "output",
    "returncode",
    "rtn",
)


@dataclass(frozen=True)
class CommandRecord:
    """Recorded outcome of one command statement inside a sent block.

    Attributes:
        args: Argument list of the command (for a pipeline, its last command).
        rtn: Exit status of the command.
        out: Recorded stdout, or ``None`` if the command was unrecorded.
        err: Recorded stderr, or ``None`` if the command was unrecorded.
    """

    args: list[str]
    rtn: int
    out: str | None
    err: str | None


class PeerFuture(Future):
    """A ``Future`` whose ``cancel()`` can be arbitrated by a remote peer."""

    def __init__(
        self,
        cancel_hook: Callable[[], bool] | None = None,
    ) -> None:
        """Initializes the future with an optional peer cancel hook.

        Args:
            cancel_hook: Zero-argument callable returning ``True`` when the
                remote peer removed the queued block before submission.
        """
        super().__init__()
        self._cancel_hook: Callable[[], bool] | None = cancel_hook
        self._observed: bool = False
        self._active_observers: int = 0
        self._result_finalized: bool = False

    def cancel(self) -> bool:
        """Cancels the future, delegating to ``_cancel_hook`` if set."""
        with self._condition:
            if self._state in (_cf_base.RUNNING, _cf_base.FINISHED):
                return False
            if self._state in (
                _cf_base.CANCELLED,
                _cf_base.CANCELLED_AND_NOTIFIED,
            ):
                return True
            hook = self._cancel_hook

        if hook is not None and not hook():
            return self.cancelled()

        with self._condition:
            if self._state in (_cf_base.RUNNING, _cf_base.FINISHED):
                return False
            if self._state in (
                _cf_base.CANCELLED,
                _cf_base.CANCELLED_AND_NOTIFIED,
            ):
                return True
            self._state = _cf_base.CANCELLED_AND_NOTIFIED
            for waiter in self._waiters:
                waiter.add_cancelled(self)
            self._condition.notify_all()
        self._invoke_callbacks()
        return True

    def set_running_or_notify_cancel(self) -> bool:
        """Handles ``CANCELLED_AND_NOTIFIED`` without raising an error."""
        with self._condition:
            if self._state == _cf_base.CANCELLED_AND_NOTIFIED:
                return False
        return super().set_running_or_notify_cancel()

    def set_exception(self, exception: BaseException | None) -> None:
        """Sets the exception and reports it if the result was already GC'd."""
        super().set_exception(exception)
        with self._condition:
            finalized = self._result_finalized
        if finalized:
            _report_unobserved_failure(self)

    def result(self, timeout: float | None = None) -> Any:
        """Returns the result and marks the future outcome as observed."""
        with self._condition:
            self._active_observers += 1
        try:
            res = super().result(timeout=timeout)
        except TimeoutError:
            with self._condition:
                self._active_observers -= 1
            raise
        except BaseException:
            with self._condition:
                self._observed = True
                self._active_observers -= 1
            raise
        with self._condition:
            self._observed = True
            self._active_observers -= 1
        return res

    def exception(self, timeout: float | None = None) -> BaseException | None:
        """Returns the exception and marks the future outcome as observed."""
        with self._condition:
            self._active_observers += 1
        try:
            exc = super().exception(timeout=timeout)
        except TimeoutError:
            with self._condition:
                self._active_observers -= 1
            raise
        except BaseException:
            with self._condition:
                self._observed = True
                self._active_observers -= 1
            raise
        with self._condition:
            self._observed = True
            self._active_observers -= 1
        return exc

    def _peek_exception(self) -> BaseException | None:
        """Returns the stored exception without blocking or marking read."""
        with self._condition:
            if self._state == _cf_base.FINISHED:
                return self._exception
            return None

    def _peek_result(self) -> Any:
        """Returns the stored result without blocking or marking read."""
        with self._condition:
            if self._state == _cf_base.FINISHED and self._exception is None:
                return self._result
            return None


def _report_unobserved_failure(future: PeerFuture) -> None:
    """Prints unread ``PeerResult`` failures to ``sys.stderr`` on GC."""
    with future._condition:
        future._result_finalized = True
        if (
            future._observed
            or future._active_observers > 0
            or not future.done()
            or future.cancelled()
        ):
            return
        exc = future._exception
    if exc is None or isinstance(exc, CancelledError):
        return
    print(
        f"xontrib-tmux: unread PeerResult failed: {exc!r}",
        file=sys.stderr,
    )


class PeerResult(CommandPipeline):
    """``CommandPipeline`` subclass representing an asynchronous peer block."""

    __hash__ = object.__hash__
    __eq__ = object.__eq__

    def __init__(
        self,
        cancel_hook: Callable[[], bool] | None = None,
    ) -> None:
        """Initializes a pending result without starting a local subprocess.

        Args:
            cancel_hook: Optional peer-arbitrated cancellation callback.
        """
        # Intentionally skips CommandPipeline.__init__: there is no local
        # subprocess or pipe; completion is driven by the remote peer.
        self._future: PeerFuture = PeerFuture(cancel_hook=cancel_hook)
        self._data_lock = threading.RLock()
        self._rtn: int | None = None
        self._records: list[CommandRecord] | None = None
        self._out: str | None = None
        self._err: str | None = None
        self._recorded: bool = True
        self._finalizer = weakref.finalize(
            self, _report_unobserved_failure, self._future
        )

    @property
    def future(self) -> PeerFuture:
        """Non-blocking ``PeerFuture`` for polling, timeouts, and callbacks."""
        return self._future

    def _wait(self) -> int:
        """Waits for completion, marks the result observed, and returns rtn."""
        return int(self._future.result())

    def end(self) -> None:
        """Waits for the remote block to finish and returns ``None``."""
        self._wait()

    @blocking_property
    def rtn(self) -> int:
        """Block exit status; waits for the remote block to finish."""
        return self._wait()

    @blocking_property
    def returncode(self) -> int:
        """Alias for ``rtn``; waits for the remote block to finish."""
        return self._wait()

    @blocking_property
    def out(self) -> str | None:
        """Recorded stdout for the block, or ``None`` when unrecorded."""
        self._wait()
        with self._data_lock:
            return self._out

    @blocking_property
    def output(self) -> str | None:
        """Alias for ``out``; waits for the remote block to finish."""
        return self.out

    @blocking_property
    def err(self) -> str | None:
        """Recorded stderr for the block, or ``None`` when unrecorded."""
        self._wait()
        with self._data_lock:
            return self._err

    @blocking_property
    def errors(self) -> str | None:
        """Alias for ``err``; waits for the remote block to finish."""
        return self.err

    @blocking_property
    def lines(self) -> list[str]:
        """Recorded stdout split into lines (empty list when unrecorded)."""
        out = self.out
        return out.splitlines() if out is not None else []

    @blocking_property
    def commands(self) -> list[CommandRecord]:
        """Per-command records for the block; waits for completion."""
        self._wait()
        with self._data_lock:
            return list(self._records) if self._records is not None else []

    def __bool__(self) -> bool:
        """Returns ``True`` iff the block completed with exit status 0."""
        return self.rtn == 0

    def __iter__(self) -> Iterator[str]:
        """Iterates over ``self.lines`` after waiting for completion."""
        return iter(self.lines)

    def __len__(self) -> int:
        """Raises ``TypeError`` because ``PeerResult`` has no length."""
        raise TypeError("object of type 'PeerResult' has no len()")

    def __str__(self) -> str:
        """Returns the non-blocking representation of the result."""
        return self.__repr__()

    def __dir__(self) -> list[str]:
        """Exposes only ``PeerResult`` API members and ``object`` dunders."""
        return sorted(set(_PUBLIC_RESULT_ATTRS) | set(_STANDARD_OBJECT_DUNDERS))

    def __repr__(self) -> str:
        """Returns a non-blocking summary of the result state."""
        if not self._future.done():
            return "<PeerResult pending>"
        if self._future.cancelled():
            return "<PeerResult cancelled>"
        exc = self._future._peek_exception()
        if exc is not None:
            return f"<PeerResult error={exc!r}>"
        rtn = self._rtn
        if rtn is None:
            rtn = self._future._peek_result()
        return f"<PeerResult rtn={rtn}>"

    def _set_completed(
        self,
        rtn: int,
        commands: list[CommandRecord],
        recorded: bool,
        out: str | None = None,
        err: str | None = None,
    ) -> None:
        """Populates result fields and resolves the underlying future.

        Args:
            rtn: Overall exit status of the block.
            commands: Per-command records observed while running the block.
            recorded: Whether block-level stdout/stderr recording was enabled.
            out: Optional block-level stdout override (when ``recorded`` is
                True). Defaults to concatenating recorded command stdout.
            err: Optional block-level stderr override (when ``recorded`` is
                True). Defaults to concatenating recorded command stderr.
        """
        records = list(commands)
        if recorded:
            resolved_out: str | None = (
                out
                if out is not None
                else "".join(c.out for c in records if c.out is not None)
            )
            resolved_err: str | None = (
                err
                if err is not None
                else "".join(c.err for c in records if c.err is not None)
            )
        else:
            resolved_out = None
            resolved_err = None

        with self._data_lock:
            if self._future.cancelled():
                return
            self._rtn = int(rtn)
            self._records = records
            self._out = resolved_out
            self._err = resolved_err
            self._recorded = recorded

        try:
            self._future.set_result(int(rtn))
        except InvalidStateError:
            if not self._future.cancelled():
                raise

    def _set_exception(self, exc: BaseException) -> None:
        """Marks the result as failed with ``exc`` and wakes waiters."""
        if self._future.cancelled():
            return
        try:
            self._future.set_exception(exc)
        except InvalidStateError:
            if not self._future.cancelled():
                raise


class LastCmdProxy(CommandPipeline):
    """Non-blocking proxy for a remote peer's ``__xonsh__.lastcmd``."""

    __hash__ = object.__hash__
    __eq__ = object.__eq__

    def __init__(self, getter: Callable[[str], Any]) -> None:
        """Initializes the proxy with a remote attribute getter.

        Args:
            getter: Callable taking an attribute name (``"args"``, ``"rtn"``,
                ``"out"``, or ``"err"``) and returning the remote value.
        """
        # Intentionally skips CommandPipeline.__init__: values live on peer.
        self._getter: Callable[[str], Any] = getter

    @blocking_property
    def args(self) -> list[str] | None:
        """Argument list of the last command, or ``None`` if none has run."""
        return self._getter("args")

    @blocking_property
    def rtn(self) -> int | None:
        """Exit status of the last command, or ``None`` if none has run."""
        return self._getter("rtn")

    @blocking_property
    def out(self) -> str | None:
        """Recorded stdout of the last command, or ``None`` if unavailable."""
        return self._getter("out")

    @blocking_property
    def err(self) -> str | None:
        """Recorded stderr of the last command, or ``None`` if unavailable."""
        return self._getter("err")

    def __bool__(self) -> bool:
        """Returns ``True`` iff the last command exited with status 0."""
        return self.rtn == 0

    def __len__(self) -> int:
        """Raises ``TypeError`` because ``LastCmdProxy`` has no length."""
        raise TypeError("object of type 'LastCmdProxy' has no len()")

    def __iter__(self) -> Iterator[str]:
        """Iterates over recorded stdout lines (empty if unavailable)."""
        out = self.out
        return iter(out.splitlines() if out is not None else [])

    def __int__(self) -> int:
        """Returns ``int(self.rtn)`` or raises ``TypeError`` when unset."""
        rtn = self.rtn
        if rtn is None:
            raise TypeError("lastcmd has no exit status yet")
        return int(rtn)

    def __dir__(self) -> list[str]:
        """Exposes only the four public ``lastcmd`` attribute names."""
        return ["args", "err", "out", "rtn"]

    def __repr__(self) -> str:
        """Returns a non-blocking representation of the proxy."""
        return "<LastCmdProxy>"

    def __str__(self) -> str:
        """Returns a non-blocking representation of the proxy."""
        return self.__repr__()
