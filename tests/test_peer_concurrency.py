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

"""Multi-process concurrency and serialization tests on private tmux."""

from collections.abc import Iterator
import json
import os
import pathlib
import signal
import subprocess
import sys
import time
from typing import Any

import libtmux  # type: ignore
import pytest

from xontrib_tmux import commands, xsess


def _isolated_tmux_env(tmp_path: pathlib.Path) -> dict[str, str]:
    """Builds an isolated environment safe from the owner's rc/history."""
    env = dict(os.environ)
    home = tmp_path / "home"
    cfg = tmp_path / "xdg_config"
    data = tmp_path / "xdg_data"
    state = tmp_path / "xdg_state"
    syscfg = tmp_path / "sys_config"
    rcdir = tmp_path / "rc_dir"
    for d in (
        home,
        cfg,
        cfg / "xonsh",
        data,
        data / "xonsh",
        state,
        syscfg,
        rcdir,
    ):
        d.mkdir(parents=True, exist_ok=True)
    env["HOME"] = str(home)
    env["XDG_CONFIG_HOME"] = str(cfg)
    env["XDG_DATA_HOME"] = str(data)
    env["XDG_STATE_HOME"] = str(state)
    env["XONSH_CONFIG_DIR"] = str(cfg / "xonsh")
    env["XONSH_DATA_DIR"] = str(data / "xonsh")
    env["XONSH_CACHE_DIR"] = str(tmp_path / "cache")
    env["XONSH_SYS_CONFIG_DIR"] = str(syscfg)
    env["XONSHRC"] = str(tmp_path / "empty_rc.xsh")
    env["XONSHRC_DIR"] = str(rcdir)
    hist_db = str(data / "xonsh" / "xonsh-history.sqlite")
    env["XONSH_HISTORY_FILE"] = hist_db
    env["XONSH_HISTORY_FILENAME"] = hist_db
    env["HISTFILE"] = str(data / "xonsh" / "xonsh_history.json")
    env["PROMPT"] = "$ "
    return env


def _kill_pid(pid: int) -> None:
    """Terminates ``pid`` and its process group best-effort."""
    for fn, target in ((os.killpg, pid), (os.kill, pid)):
        try:
            fn(target, signal.SIGKILL)
        except OSError:
            pass


@pytest.fixture
def conc_tmux(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> Iterator[tuple[libtmux.Server, str, dict[str, str]]]:
    """Starts an isolated private tmux server and cleans up all resources."""
    with commands._pane_peers_lock:
        commands._pane_peers.clear()

    env_map = _isolated_tmux_env(tmp_path)
    for key in (
        "HOME",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_STATE_HOME",
        "XONSH_CONFIG_DIR",
        "XONSH_DATA_DIR",
        "XONSH_SYS_CONFIG_DIR",
        "XONSH_CACHE_DIR",
        "XONSH_HISTORY_FILE",
        "XONSH_HISTORY_FILENAME",
        "HISTFILE",
        "PROMPT",
        "XONSHRC",
        "XONSHRC_DIR",
    ):
        monkeypatch.setenv(key, env_map[key])

    sock_name = f"xtconc_{os.getpid()}_{time.monotonic_ns() % 1000000}"
    srv = libtmux.Server(socket_name=sock_name, config_file="/dev/null")
    host = srv.new_session(
        session_name="host",
        x=160,
        y=30,
        attach=False,
        window_command="/bin/sh",
    )
    sock_path_out = srv.cmd(
        "display-message", "-p", "#{socket_path}"
    ).stdout[0]
    srv_pid = srv.cmd("display-message", "-p", "#{pid}").stdout[0]
    tmux_val = f"{sock_path_out},{srv_pid},0"
    host_pane = host.active_pane
    assert host_pane is not None
    pane_id = str(host_pane.pane_id)

    monkeypatch.setenv("TMUX", tmux_val)
    monkeypatch.setenv("TMUX_PANE", pane_id)
    env_map["TMUX"] = tmux_val
    env_map["TMUX_PANE"] = pane_id

    spawned_procs: list[subprocess.Popen[str]] = []
    monkeypatch.setattr(conc_tmux, "_procs", spawned_procs, raising=False)

    try:
        yield srv, sock_name, env_map
    finally:
        for proc in spawned_procs:
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    pass

        sock_files: list[pathlib.Path] = []
        pids: list[int] = []
        try:
            for pane in srv.panes:
                try:
                    raw = pane.cmd(
                        "display-message",
                        "-p",
                        f"#{{{commands.PEER_OPTION}}}\t#{{pane_pid}}",
                    ).stdout
                except libtmux.exc.LibTmuxException:
                    raw = []
                if raw:
                    opt, _, pid_s = raw[0].partition("\t")
                    if opt and "|" in opt:
                        sock_files.append(pathlib.Path(opt.split("|", 1)[0]))
                    if pid_s.strip().isdigit():
                        pids.append(int(pid_s.strip()))
        except libtmux.exc.LibTmuxException:
            pass

        with commands._pane_peers_lock:
            for peer in commands._pane_peers.values():
                sock_files.append(peer._socket_path)
            commands._pane_peers.clear()

        try:
            srv.kill()
        except libtmux.exc.LibTmuxException:
            pass
        for pid in pids:
            _kill_pid(pid)
        for sf in sock_files:
            sf.unlink(missing_ok=True)
        pathlib.Path(sock_path_out).unlink(missing_ok=True)


def _run_xonsh_senders(
    scripts: list[pathlib.Path],
    env_map: dict[str, str],
    ready_files: list[pathlib.Path],
    go_file: pathlib.Path,
    timeout: float = 45.0,
) -> list[subprocess.CompletedProcess[str]]:
    """Launches xonsh worker scripts, releases barrier, and awaits exit."""
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "xonsh", "--no-rc", str(script)],
            env=env_map,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for script in scripts
    ]
    try:
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            for p in procs:
                if p.poll() is not None and p.returncode != 0:
                    out, err = p.communicate()
                    raise AssertionError(
                        f"Worker exited early rc={p.returncode}: "
                        f"{out!r} {err!r}"
                    )
            if all(rf.exists() for rf in ready_files):
                break
            time.sleep(0.02)
        else:
            missing = [str(rf) for rf in ready_files if not rf.exists()]
            raise TimeoutError(f"Workers did not reach barrier: {missing}")

        go_file.write_text("go\n", encoding="utf-8")

        results: list[subprocess.CompletedProcess[str]] = []
        end_deadline = time.monotonic() + timeout
        for p in procs:
            rem = max(0.1, end_deadline - time.monotonic())
            out, err = p.communicate(timeout=rem)
            results.append(
                subprocess.CompletedProcess(p.args, p.returncode, out, err)
            )
        return results
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                try:
                    p.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    pass


def _read_log_entries(log_path: pathlib.Path) -> list[dict[str, Any]]:
    lines = log_path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_multiprocess_concurrent_blocks_serialized(
    conc_tmux: tuple[libtmux.Server, str, dict[str, str]],
    tmp_path: pathlib.Path,
) -> None:
    """Case 1 & 5: N>=4 senders x M>=5 blocks run whole, isolated, FIFO."""
    _srv, sock_name, env_map = conc_tmux
    target_sess = xsess("conc_target", x=160, y=30)
    target_pane = target_sess.active_pane
    assert target_pane is not None
    assert target_pane.send("echo ready").rtn == 0
    target_pane_id = str(target_pane.pane_id)

    n_senders = 4
    m_blocks = 5
    log_path = tmp_path / "exec_log.jsonl"
    go_file = tmp_path / "go"
    ready_files = [tmp_path / f"ready_{i}" for i in range(n_senders)]
    out_files = [tmp_path / f"sender_{i}.json" for i in range(n_senders)]
    scripts: list[pathlib.Path] = []

    for i in range(n_senders):
        script = tmp_path / f"sender_{i}.xsh"
        body = """
import json, pathlib, time
import libtmux
xontrib load tmux

srv = libtmux.Server(socket_name=__SOCK__, config_file="/dev/null")
pane = srv.panes.get(pane_id=__PANE__)
assert pane is not None
rx = pane.xonsh
assert rx is not None

pathlib.Path(__READY__).write_text("ok", encoding="utf-8")
go = pathlib.Path(__GO__)
while not go.exists():
    time.sleep(0.01)

futures = []
for j in range(__M__):
    bid = f"{__I__}:{j}"
    exp_rtn = (__I__ * __M__ + j) % 4
    block = (
        "import json, pathlib, time\\n"
        f"_lp = pathlib.Path({__LOG__!r})\\n"
        f"_bid = {bid!r}\\n"
        "_w = lambda e: _lp.open('a', encoding='utf-8').write("
        "json.dumps({'ev': e, 'bid': _bid}) + '\\\\n')\\n"
        "_w('begin')\\n"
        "time.sleep(0.02)\\n"
        "_w('mid')\\n"
        "time.sleep(0.02)\\n"
        "_w('end')\\n"
        "print('OUT_' + _bid)\\n"
        f"python3 -c 'import sys; sys.exit({exp_rtn})'"
    )
    futures.append((j, bid, exp_rtn, rx.send(block)))

records = []
for j, bid, exp_rtn, res in futures:
    res.end()
    records.append({
        "j": j,
        "bid": bid,
        "exp_rtn": exp_rtn,
        "rtn": res.rtn,
        "out": res.out,
    })

pathlib.Path(__OUT__).write_text(json.dumps(records), encoding="utf-8")
""".lstrip()
        body = (
            body.replace("__SOCK__", repr(sock_name))
            .replace("__PANE__", repr(target_pane_id))
            .replace("__READY__", repr(str(ready_files[i])))
            .replace("__GO__", repr(str(go_file)))
            .replace("__LOG__", repr(str(log_path)))
            .replace("__OUT__", repr(str(out_files[i])))
            .replace("__M__", str(m_blocks))
            .replace("__I__", str(i))
        )
        script.write_text(body, encoding="utf-8")
        scripts.append(script)

    procs = _run_xonsh_senders(scripts, env_map, ready_files, go_file)
    for proc in procs:
        assert proc.returncode == 0, f"{proc.stdout!r} {proc.stderr!r}"

    entries = _read_log_entries(log_path)
    total_blocks = n_senders * m_blocks
    assert len(entries) == 3 * total_blocks

    # Every block's begin/mid/end must be strictly contiguous with no
    # interleavings, and every block must run once and only once.
    seen_bids: list[str] = []
    for k in range(total_blocks):
        b, m, e = entries[3 * k], entries[3 * k + 1], entries[3 * k + 2]
        assert (b["ev"], m["ev"], e["ev"]) == ("begin", "mid", "end")
        assert b["bid"] == m["bid"] == e["bid"]
        seen_bids.append(b["bid"])

    expected_bids = {
        f"{i}:{j}" for i in range(n_senders) for j in range(m_blocks)
    }
    assert len(seen_bids) == total_blocks
    assert set(seen_bids) == expected_bids

    # Case 5: Per-sender FIFO order preserved in execution log.
    for i in range(n_senders):
        prefix = f"{i}:"
        per_sender = [
            int(bid.split(":")[1])
            for bid in seen_bids
            if bid.startswith(prefix)
        ]
        assert per_sender == list(range(m_blocks))

    # Each sender receives strictly its own result and exit code.
    # Known xonsh 0.24 PopenThread/waitpid race can occasionally overwrite a
    # non-zero subprocess exit code with 0 when recording is enabled.
    waitpid_race_hits = 0
    for i in range(n_senders):
        recs = json.loads(out_files[i].read_text(encoding="utf-8"))
        assert len(recs) == m_blocks
        for rec in recs:
            if rec["exp_rtn"] != 0 and rec["rtn"] == 0:
                waitpid_race_hits += 1
            else:
                assert rec["rtn"] == rec["exp_rtn"], f"Mismatch in {rec!r}"
            assert rec["out"] == f"OUT_{rec['bid']}\n", f"Out in {rec!r}"
    print(
        f"[known xonsh waitpid race] non-zero rtn reported as 0: "
        f"{waitpid_race_hits}/{total_blocks}"
    )


def test_multiprocess_concurrent_captures_isolated_and_restored(
    conc_tmux: tuple[libtmux.Server, str, dict[str, str]],
    tmp_path: pathlib.Path,
) -> None:
    """Case 2: Concurrent capture= frames on same name never overlap/leak."""
    _srv, sock_name, env_map = conc_tmux
    target_sess = xsess("conc_cap", x=160, y=30)
    target_pane = target_sess.active_pane
    assert target_pane is not None
    assert target_pane.send("shared_pre = 'orig'").rtn == 0
    target_pane_id = str(target_pane.pane_id)

    n_senders = 4
    m_blocks = 5
    log_path = tmp_path / "cap_log.jsonl"
    go_file = tmp_path / "go_cap"
    ready_files = [tmp_path / f"cap_ready_{i}" for i in range(n_senders)]
    out_files = [tmp_path / f"cap_out_{i}.json" for i in range(n_senders)]
    scripts: list[pathlib.Path] = []

    for i in range(n_senders):
        script = tmp_path / f"cap_sender_{i}.xsh"
        body = """
import json, pathlib, time
import libtmux
xontrib load tmux

srv = libtmux.Server(socket_name=__SOCK__, config_file="/dev/null")
pane = srv.panes.get(pane_id=__PANE__)
assert pane is not None
rx = pane.xonsh
assert rx is not None

pathlib.Path(__READY__).write_text("ok", encoding="utf-8")
go = pathlib.Path(__GO__)
while not go.exists():
    time.sleep(0.01)

futures = []
for j in range(__M__):
    shared_var = f"v_{__I__}_{j}"
    shared_pre = f"p_{__I__}_{j}"
    bid = f"{__I__}:{j}"
    block = (
        "import json, pathlib, time\\n"
        f"_lp = pathlib.Path({__LOG__!r})\\n"
        f"_bid = {bid!r}\\n"
        "s0, p0 = shared_var, shared_pre\\n"
        "time.sleep(0.02)\\n"
        "shared_var = 'mut_' + _bid\\n"
        "time.sleep(0.02)\\n"
        "s1, p1 = shared_var, shared_pre\\n"
        "_row = {'bid': _bid, 's0': s0, 'p0': p0, 's1': s1, 'p1': p1}\\n"
        "_lp.open('a', encoding='utf-8').write(json.dumps(_row) + '\\\\n')\\n"
        "print(f'{s0}|{p0}|{s1}|{p1}')"
    )
    fut = rx.send(block, capture=["shared_var", "shared_pre"])
    futures.append((j, shared_var, shared_pre, bid, fut))

records = []
for j, sv, sp, bid, res in futures:
    res.end()
    records.append({
        "j": j,
        "sv": sv,
        "sp": sp,
        "bid": bid,
        "rtn": res.rtn,
        "out": res.out,
    })

pathlib.Path(__OUT__).write_text(json.dumps(records), encoding="utf-8")
""".lstrip()
        body = (
            body.replace("__SOCK__", repr(sock_name))
            .replace("__PANE__", repr(target_pane_id))
            .replace("__READY__", repr(str(ready_files[i])))
            .replace("__GO__", repr(str(go_file)))
            .replace("__LOG__", repr(str(log_path)))
            .replace("__OUT__", repr(str(out_files[i])))
            .replace("__M__", str(m_blocks))
            .replace("__I__", str(i))
        )
        script.write_text(body, encoding="utf-8")
        scripts.append(script)

    procs = _run_xonsh_senders(scripts, env_map, ready_files, go_file)
    for proc in procs:
        assert proc.returncode == 0, f"{proc.stdout!r} {proc.stderr!r}"

    entries = _read_log_entries(log_path)
    assert len(entries) == n_senders * m_blocks
    for entry in entries:
        bid = entry["bid"]
        i_s, j_s = bid.split(":")
        assert entry["s0"] == f"v_{i_s}_{j_s}"
        assert entry["p0"] == f"p_{i_s}_{j_s}"
        assert entry["s1"] == f"mut_{bid}"
        assert entry["p1"] == f"p_{i_s}_{j_s}"

    for i in range(n_senders):
        recs = json.loads(out_files[i].read_text(encoding="utf-8"))
        for rec in recs:
            assert rec["rtn"] == 0
            assert (
                rec["out"]
                == f"{rec['sv']}|{rec['sp']}|mut_{rec['bid']}|{rec['sp']}\n"
            )

    rx = target_pane.xonsh
    assert rx is not None
    assert "shared_var" not in rx.ctx
    assert rx.ctx["shared_pre"] == "orig"


def test_env_ctx_ops_wait_between_blocks_never_mid_block(
    conc_tmux: tuple[libtmux.Server, str, dict[str, str]],
    tmp_path: pathlib.Path,
) -> None:
    """Case 3: env/ctx reads & writes are served strictly between blocks."""
    _srv, sock_name, env_map = conc_tmux
    target_sess = xsess("conc_rpc", x=160, y=30)
    target_pane = target_sess.active_pane
    assert target_pane is not None
    assert target_pane.send(
        "__xonsh__.ctx['__block_active'] = None\n"
        "__xonsh__.ctx['__probe_0'] = -1\n"
        "__xonsh__.ctx['__probe_1'] = -1\n"
        "__xonsh__.env['CONC_PROBE_0'] = 'init'\n"
        "__xonsh__.env['CONC_PROBE_1'] = 'init'"
    ).rtn == 0
    target_pane_id = str(target_pane.pane_id)

    n_block_senders = 3
    m_blocks = 5
    go_file = tmp_path / "go_rpc"
    stop_obs = tmp_path / "stop_obs"
    obs_log = tmp_path / "obs_log.jsonl"
    block_log = tmp_path / "block_log.jsonl"

    ready_files: list[pathlib.Path] = []
    scripts: list[pathlib.Path] = []

    for i in range(n_block_senders):
        rf = tmp_path / f"rpc_b_ready_{i}"
        ready_files.append(rf)
        script = tmp_path / f"rpc_b_{i}.xsh"
        body = """
import json, pathlib, time
import libtmux
xontrib load tmux

srv = libtmux.Server(socket_name=__SOCK__, config_file="/dev/null")
pane = srv.panes.get(pane_id=__PANE__)
rx = pane.xonsh
assert rx is not None

pathlib.Path(__READY__).write_text("ok", encoding="utf-8")
go = pathlib.Path(__GO__)
while not go.exists():
    time.sleep(0.01)

futs = []
for j in range(__M__):
    bid = f"{__I__}:{j}"
    block = (
        "import json, pathlib, time\\n"
        f"_bid = {bid!r}\\n"
        "__xonsh__.ctx['__block_active'] = _bid\\n"
        "p0_a = __xonsh__.ctx.get('__probe_0')\\n"
        "p1_a = __xonsh__.ctx.get('__probe_1')\\n"
        "e0_a = __xonsh__.env.get('CONC_PROBE_0')\\n"
        "e1_a = __xonsh__.env.get('CONC_PROBE_1')\\n"
        "time.sleep(0.05)\\n"
        "active_mid = __xonsh__.ctx.get('__block_active')\\n"
        "p0_b = __xonsh__.ctx.get('__probe_0')\\n"
        "p1_b = __xonsh__.ctx.get('__probe_1')\\n"
        "e0_b = __xonsh__.env.get('CONC_PROBE_0')\\n"
        "e1_b = __xonsh__.env.get('CONC_PROBE_1')\\n"
        "__xonsh__.ctx['__block_active'] = None\\n"
        f"_lp = pathlib.Path({__LOG__!r})\\n"
        "_row = {'bid': _bid, 'active_mid': active_mid,\\n"
        "    'p_same': (p0_a, p1_a) == (p0_b, p1_b),\\n"
        "    'e_same': (e0_a, e1_a) == (e0_b, e1_b)}\\n"
        "_lp.open('a', encoding='utf-8').write(json.dumps(_row) + '\\\\n')"
    )
    futs.append(rx.send(block))
    time.sleep(0.03)
for f in futs:
    f.end()
    assert f.rtn == 0
""".lstrip()
        body = (
            body.replace("__SOCK__", repr(sock_name))
            .replace("__PANE__", repr(target_pane_id))
            .replace("__READY__", repr(str(rf)))
            .replace("__GO__", repr(str(go_file)))
            .replace("__LOG__", repr(str(block_log)))
            .replace("__M__", str(m_blocks))
            .replace("__I__", str(i))
        )
        script.write_text(body, encoding="utf-8")
        scripts.append(script)

    # Observer processes read/write ctx & env continuously while blocks run.
    for obs_i in range(2):
        rf = tmp_path / f"rpc_o_ready_{obs_i}"
        ready_files.append(rf)
        script = tmp_path / f"rpc_o_{obs_i}.xsh"
        script.write_text(
            f"""
import json, pathlib, time
import libtmux
xontrib load tmux

srv = libtmux.Server(socket_name={sock_name!r}, config_file="/dev/null")
pane = srv.panes.get(pane_id={target_pane_id!r})
rx = pane.xonsh
assert rx is not None

pathlib.Path({str(rf)!r}).write_text("ok", encoding="utf-8")
go = pathlib.Path({str(go_file)!r})
while not go.exists():
    time.sleep(0.01)

stop = pathlib.Path({str(stop_obs)!r})
k = 0
obs = []
while not stop.exists() or k < 4:
    active_val = rx.ctx["__block_active"]
    probe_val = f"obs_{obs_i}_{{k}}"
    rx.ctx["__probe_{obs_i}"] = probe_val
    rx.env["CONC_PROBE_{obs_i}"] = probe_val
    read_back = rx.env["CONC_PROBE_{obs_i}"]
    obs.append({{"active": active_val, "wrote": probe_val, "read": read_back}})
    k += 1

with pathlib.Path({str(obs_log)!r}).open("a", encoding="utf-8") as f:
    for row in obs:
        f.write(json.dumps(row) + "\\n")
""".lstrip(),
            encoding="utf-8",
        )
        scripts.append(script)

    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "xonsh", "--no-rc", str(s)],
            env=env_map,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for s in scripts
    ]
    try:
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if all(rf.exists() for rf in ready_files):
                break
            time.sleep(0.02)
        else:
            raise TimeoutError("Workers did not reach barrier")

        go_file.write_text("go\n", encoding="utf-8")

        # Wait for all block sender processes first, then signal observers off.
        for p in procs[:n_block_senders]:
            out, err = p.communicate(timeout=45.0)
            assert p.returncode == 0, f"{out!r} {err!r}"

        stop_obs.write_text("stop\n", encoding="utf-8")
        for p in procs[n_block_senders:]:
            out, err = p.communicate(timeout=20.0)
            assert p.returncode == 0, f"{out!r} {err!r}"
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait(timeout=5.0)

    b_rows = _read_log_entries(block_log)
    assert len(b_rows) == n_block_senders * m_blocks
    for row in b_rows:
        assert row["active_mid"] == row["bid"]
        assert row["p_same"] is True
        assert row["e_same"] is True

    o_rows = _read_log_entries(obs_log)
    assert len(o_rows) >= 8
    for row in o_rows:
        assert row["active"] is None
        assert row["wrote"] == row["read"]


def test_hand_typed_command_and_queued_blocks_do_not_interleave(
    conc_tmux: tuple[libtmux.Server, str, dict[str, str]],
    tmp_path: pathlib.Path,
) -> None:
    """Case 4: Hand-typed pane input and queued blocks run whole."""
    _srv, sock_name, env_map = conc_tmux
    target_sess = xsess("conc_typed", x=160, y=30)
    target_pane = target_sess.active_pane
    assert target_pane is not None
    assert target_pane.send("echo ready").rtn == 0
    target_pane_id = str(target_pane.pane_id)

    log_path = tmp_path / "typed_log.jsonl"
    b1_started = tmp_path / "b1_started"
    typed_helper = tmp_path / "typed_helper.py"
    typed_helper.write_text(
        f"""
import json, pathlib, time
lp = pathlib.Path({str(log_path)!r})
with lp.open("a", encoding="utf-8") as f:
    f.write(json.dumps({{"ev": "begin", "id": "typed"}}) + "\\n")
time.sleep(0.08)
with lp.open("a", encoding="utf-8") as f:
    f.write(json.dumps({{"ev": "end", "id": "typed"}}) + "\\n")
""".lstrip(),
        encoding="utf-8",
    )

    # Queue b1, b2, b3 from two independent sender scripts.
    go2 = tmp_path / "go_b2"
    r2 = tmp_path / "r_b2"
    s2 = tmp_path / "sender_b2.xsh"
    s2.write_text(
        """
import json, pathlib, time
import libtmux
xontrib load tmux
srv = libtmux.Server(socket_name=__SOCK__, config_file="/dev/null")
pane = srv.panes.get(pane_id=__PANE__)
rx = pane.xonsh
pathlib.Path(__R2__).write_text("ok", encoding="utf-8")
while not pathlib.Path(__GO2__).exists():
    time.sleep(0.01)
_lp_str = __LOG__
f2 = rx.send(
    "import json, pathlib, time\\n"
    f"_lp = pathlib.Path({_lp_str!r})\\n"
    "_w = lambda e: _lp.open('a').write("
    "json.dumps({'ev': e, 'id': 'b2'}) + '\\\\n')\\n"
    "_w('begin')\\ntime.sleep(0.05)\\n_w('end')"
)
f3 = rx.send(
    "import json, pathlib, time\\n"
    f"_lp = pathlib.Path({_lp_str!r})\\n"
    "_w = lambda e: _lp.open('a').write("
    "json.dumps({'ev': e, 'id': 'b3'}) + '\\\\n')\\n"
    "_w('begin')\\ntime.sleep(0.05)\\n_w('end')"
)
f2.end()
assert f2.rtn == 0
f3.end()
assert f3.rtn == 0
""".lstrip()
        .replace("__SOCK__", repr(sock_name))
        .replace("__PANE__", repr(target_pane_id))
        .replace("__R2__", repr(str(r2)))
        .replace("__GO2__", repr(str(go2)))
        .replace("__LOG__", repr(str(log_path))),
        encoding="utf-8",
    )

    proc2 = subprocess.Popen(
        [sys.executable, "-m", "xonsh", "--no-rc", str(s2)],
        env=env_map,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15.0
        while not r2.exists():
            assert time.monotonic() < deadline
            time.sleep(0.02)

        b1 = target_pane.send(
            "import json, pathlib, time\n"
            f"pathlib.Path({str(b1_started)!r}).write_text('1')\n"
            f"_lp = pathlib.Path({str(log_path)!r})\n"
            "_w = lambda e: _lp.open('a').write("
            "json.dumps({'ev': e, 'id': 'b1'}) + '\\n')\n"
            "_w('begin')\ntime.sleep(0.25)\n_w('end')"
        )
        while not b1_started.exists():
            assert time.monotonic() < deadline
            time.sleep(0.01)

        # Queue b2 & b3 while b1 is still running, and type a command by hand
        # (first partial without Enter, then Enter) into the pane.
        go2.write_text("go\n", encoding="utf-8")
        time.sleep(0.03)
        typed_cmd = f"{sys.executable} {typed_helper}"
        target_pane.send_keys(typed_cmd, enter=False)
        time.sleep(0.05)
        target_pane.send_keys("", enter=True)

        b1.end()
        assert b1.rtn == 0
        out2, err2 = proc2.communicate(timeout=20.0)
        assert proc2.returncode == 0, f"{out2!r} {err2!r}"

        end_deadline = time.monotonic() + 15.0
        entries: list[dict[str, Any]] = []
        while time.monotonic() < end_deadline:
            if log_path.exists():
                entries = _read_log_entries(log_path)
                if len(entries) >= 8:
                    break
            time.sleep(0.02)
        assert len(entries) == 8, f"Expected 8 log entries, got {entries!r}"
    finally:
        if proc2.poll() is None:
            proc2.kill()
            proc2.wait(timeout=5.0)

    seen_ids: list[str] = []
    for k in range(4):
        b_ev, e_ev = entries[2 * k], entries[2 * k + 1]
        assert (b_ev["ev"], e_ev["ev"]) == ("begin", "end")
        assert b_ev["id"] == e_ev["id"]
        seen_ids.append(b_ev["id"])

    assert set(seen_ids) == {"b1", "b2", "b3", "typed"}
    assert seen_ids.index("b2") < seen_ids.index("b3")
