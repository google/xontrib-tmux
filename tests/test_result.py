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

"""Hermetic unit tests for ``xontrib_tmux.result``."""

import asyncio
import dataclasses
import gc
import threading
import time
from typing import Any

import pytest
from xonsh.completers.python import attr_complete
from xonsh.procs.pipelines import CommandPipeline, blocking_property

from xontrib_tmux.result import (
    CommandRecord,
    PeerFuture,
    PeerResult,
)


def test_command_record_frozen() -> None:
    rec = CommandRecord(args=["echo", "hi"], rtn=0, out="hi\n", err="")
    assert rec.args == ["echo", "hi"]
    assert rec.rtn == 0
    assert rec.out == "hi\n"
    assert rec.err == ""
    with pytest.raises(dataclasses.FrozenInstanceError):
        rec.rtn = 1  # type: ignore[misc]


def test_peer_result_is_command_pipeline_with_blocking_properties() -> None:
    run = PeerResult()
    assert isinstance(run, CommandPipeline)
    assert not hasattr(run, "procs")
    assert not hasattr(run, "spec")

    for name in (
        "rtn",
        "returncode",
        "out",
        "output",
        "err",
        "errors",
        "lines",
        "commands",
    ):
        assert isinstance(getattr(PeerResult, name), blocking_property), name


@pytest.mark.parametrize(
    "reader",
    [
        lambda r: r.rtn,
        lambda r: r.returncode,
        lambda r: r.out,
        lambda r: r.output,
        lambda r: r.err,
        lambda r: r.errors,
        lambda r: r.lines,
        lambda r: r.commands,
        lambda r: r.end(),
        lambda r: bool(r),
        lambda r: list(r),
    ],
)
def test_waiting_members_block_until_completed(reader: Any) -> None:
    run = PeerResult()
    gate = threading.Event()
    result_holder: list[Any] = []
    err_holder: list[BaseException] = []

    def worker() -> None:
        try:
            result_holder.append(reader(run))
        except BaseException as exc:  # noqa: BLE001
            err_holder.append(exc)
        finally:
            gate.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    time.sleep(0.03)
    assert not gate.is_set()

    run._set_completed(
        0,
        [CommandRecord(args=["echo", "ok"], rtn=0, out="ok\n", err="")],
        recorded=True,
    )
    assert gate.wait(timeout=2.0)
    t.join(timeout=2.0)
    assert not err_holder
    assert len(result_holder) == 1


def test_recorded_and_foreground_command_aggregation() -> None:
    run = PeerResult()
    cmds = [
        CommandRecord(args=["echo", "one"], rtn=0, out="one\n", err=""),
        CommandRecord(args=["vim"], rtn=0, out=None, err=None),
        CommandRecord(args=["true"], rtn=0, out="", err=""),
        CommandRecord(args=["cat"], rtn=0, out="two\n", err="warn\n"),
    ]
    run._set_completed(0, cmds, recorded=True)

    assert bool(run) is True
    assert run.rtn == 0
    assert run.returncode == 0
    assert run.out == "one\ntwo\n"
    assert run.output == "one\ntwo\n"
    assert run.err == "warn\n"
    assert run.errors == "warn\n"
    assert run.lines == ["one", "two"]
    assert list(run) == ["one", "two"]
    assert str(run) == "<PeerResult rtn=0>"

    got_cmds = run.commands
    assert len(got_cmds) == 4
    assert got_cmds[1].out is None and got_cmds[1].err is None
    assert got_cmds[2].out == "" and got_cmds[2].err == ""
    got_cmds.pop()
    assert len(run.commands) == 4


def test_recorded_only_foreground_commands() -> None:
    run = PeerResult()
    run._set_completed(
        0,
        [CommandRecord(args=["vim"], rtn=0, out=None, err=None)],
        recorded=True,
    )
    assert run.out == ""
    assert run.err == ""
    assert run.lines == []


def test_unrecorded_result() -> None:
    run = PeerResult()
    run._set_completed(
        2,
        [CommandRecord(args=["false"], rtn=2, out=None, err=None)],
        recorded=False,
    )
    assert bool(run) is False
    assert run.rtn == 2
    assert run.returncode == 2
    assert run.out is None
    assert run.output is None
    assert run.err is None
    assert run.errors is None
    assert run.lines == []
    assert list(run) == []
    assert str(run) == "<PeerResult rtn=2>"
    assert run.commands[0].out is None
    assert run.commands[0].err is None


def test_repr_and_dir_and_tab_completion_never_block(
    xonsh_execer: Any,
) -> None:
    del xonsh_execer
    run = PeerResult()

    t0 = time.monotonic()
    assert repr(run) == "<PeerResult pending>"
    assert str(run) == "<PeerResult pending>"
    with pytest.raises(TypeError):
        _ = len(run)
    comps = attr_complete("run.", {"run": run}, lambda o, a: o.startswith(a))
    dt = time.monotonic() - t0

    assert dt < 0.05
    stripped = {c.rstrip("(") for c in comps}
    assert {
        "run.commands",
        "run.end",
        "run.err",
        "run.errors",
        "run.future",
        "run.lines",
        "run.out",
        "run.output",
        "run.returncode",
        "run.rtn",
    } <= stripped
    assert "run.pipestatus" not in stripped
    assert "run.pid" not in stripped
    assert "pipestatus" not in dir(run)

    run._set_completed(0, [], recorded=True)
    assert repr(run) == "<PeerResult rtn=0>"

    failed = PeerResult()
    failed._set_exception(RuntimeError("boom"))
    assert "RuntimeError('boom')" in repr(failed)
    _ = failed.future.exception()

    cancelled = PeerResult()
    assert cancelled.future.cancel() is True
    assert repr(cancelled) == "<PeerResult cancelled>"


def test_exception_propagates_to_waiters() -> None:
    run = PeerResult()
    run._set_exception(RuntimeError("peer died"))

    for accessor in (
        lambda: run.rtn,
        lambda: run.out,
        lambda: run.err,
        lambda: run.lines,
        lambda: run.commands,
        lambda: run.end(),
        lambda: bool(run),
        lambda: list(run),
    ):
        with pytest.raises(RuntimeError, match="peer died"):
            accessor()


def test_unread_failed_result_prints_to_stderr_on_gc(capsys: Any) -> None:
    run = PeerResult()
    run._set_exception(RuntimeError("startup failed"))
    del run
    gc.collect()

    captured = capsys.readouterr()
    assert "startup failed" in captured.err


def test_timeout_before_unread_failure_still_prints_on_gc(capsys: Any) -> None:
    run = PeerResult()
    with pytest.raises(TimeoutError):
        run.future.result(timeout=0.005)
    run._set_exception(RuntimeError("late failure"))
    del run
    gc.collect()

    captured = capsys.readouterr()
    assert "late failure" in captured.err


@pytest.mark.parametrize(
    "observer",
    [
        lambda r: r.rtn,
        lambda r: r.out,
        lambda r: r.end(),
        lambda r: bool(r),
        lambda r: r.future.result(),
        lambda r: r.future.exception(),
    ],
)
def test_read_failed_result_does_not_print_on_gc(
    capsys: Any, observer: Any
) -> None:
    run = PeerResult()
    run._set_exception(RuntimeError("observed failure"))
    try:
        observer(run)
    except RuntimeError:
        pass
    del run
    gc.collect()

    captured = capsys.readouterr()
    assert captured.err == ""


def test_asyncio_wrap_future_with_peer_result() -> None:
    run = PeerResult()

    async def main() -> int:
        wrapped = asyncio.wrap_future(run.future)
        run._set_completed(7, [], recorded=True)
        return await wrapped

    assert asyncio.run(main()) == 7


def test_pending_hash_and_eq_do_not_block_or_raise() -> None:
    r1 = PeerResult()
    r2 = PeerResult()

    assert {r1, r2} == {r1, r2}
    items = [r1, r2]
    items.remove(r2)
    assert items == [r1]
    assert (r1 == r1) is True
    assert (r1 == r2) is False
    assert (r1 == None) is False  # noqa: E711


def test_str_does_not_mark_unread_failure_observed(capsys: Any) -> None:
    run = PeerResult()
    run._set_exception(RuntimeError("unread via str"))
    assert "unread via str" in str(run)
    del run
    gc.collect()

    captured = capsys.readouterr()
    assert "unread via str" in captured.err


def test_dropped_result_while_waiting_on_future_does_not_print_on_gc(
    capsys: Any,
) -> None:
    def make_pending() -> PeerFuture:
        r = PeerResult()
        return r.future

    fut = make_pending()
    gc.collect()
    t = threading.Timer(
        0.03,
        lambda: fut.set_exception(RuntimeError("observed via future")),
    )
    t.start()
    with pytest.raises(RuntimeError, match="observed via future"):
        fut.result(timeout=2.0)
    t.join(timeout=1.0)
    gc.collect()
    captured = capsys.readouterr()
    assert captured.err == ""


def test_gc_before_set_exception_reports_later_failure(capsys: Any) -> None:
    def make_pending() -> PeerFuture:
        r = PeerResult()
        return r.future

    fut = make_pending()
    gc.collect()
    assert capsys.readouterr().err == ""

    fut.set_exception(RuntimeError("failed after gc"))
    captured = capsys.readouterr()
    assert "failed after gc" in captured.err


def test_set_completed_optional_block_out_err_override() -> None:
    run = PeerResult()
    run._set_completed(
        0,
        [CommandRecord(args=["echo", "a"], rtn=0, out="a\n", err="")],
        recorded=True,
        out="a\nb\n",
        err="py-err\n",
    )
    assert run.out == "a\nb\n"
    assert run.err == "py-err\n"
    assert [c.out for c in run.commands] == ["a\n"]
