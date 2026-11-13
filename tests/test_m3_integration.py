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

"""Milestone 3 end-to-end integration tests on a private tmux server."""

from collections.abc import Iterator
import os
import pathlib
import re
import signal
import subprocess
import sys
import time
from typing import Any

import libtmux  # type: ignore
import pytest
from xonsh.completers.python import attr_complete

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
def tmux_env(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> Iterator[tuple[libtmux.Server, libtmux.Session, str, str]]:
    """Provides an isolated private tmux server and cleans up everything."""
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

    sock_name = f"xtm3_{os.getpid()}_{time.monotonic_ns() % 1000000}"
    srv = libtmux.Server(socket_name=sock_name, config_file="/dev/null")
    host = srv.new_session(
        session_name="host",
        x=120,
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

    try:
        yield srv, host, tmux_val, pane_id
    finally:
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
            pathlib.Path(f"{sf}.err").unlink(missing_ok=True)
        pathlib.Path(sock_path_out).unlink(missing_ok=True)


def test_xsess_preexisting_session_and_existing_checks(
    tmux_env: tuple[libtmux.Server, libtmux.Session, str, str],
) -> None:
    """xsess() targets new pane even when host session already exists."""
    srv, host, _tmux_val, _pane_id = tmux_env
    host_pane = host.active_pane
    assert host_pane is not None
    assert host_pane.xonsh is None

    # Create named and unnamed sessions right away and send while booting.
    s_named = xsess("work")
    s_unnamed = xsess()
    assert s_named.session_name == "work"
    assert s_named.active_pane is not None
    assert s_unnamed.active_pane is not None
    assert host_pane.xonsh is None

    r1 = s_named.send("echo named-ready")
    r2 = s_unnamed.send("echo unnamed-ready")
    assert r1.rtn == 0
    assert r1.out == "named-ready\n"
    assert r2.rtn == 0
    assert r2.out == "unnamed-ready\n"

    # Calling xsess("work") again returns existing session & cached peer.
    s_again = xsess("work")
    assert s_again.session_id == s_named.session_id
    assert s_again.active_pane is not None
    assert s_again.active_pane.xonsh is s_named.active_pane.xonsh

    # Calling xsess("host") on plain sh pane raises RuntimeError.
    with pytest.raises(RuntimeError, match="active pane has no peer xonsh"):
        xsess("host")

    # Splitting with a plain shell focused makes work.active_pane lack a peer.
    plain_pane = s_named.active_window.split(shell="/bin/sh", attach=True)
    assert plain_pane.xonsh is None
    with pytest.raises(RuntimeError, match="Target pane has no peer xonsh"):
        s_named.send("echo nope")
    with pytest.raises(RuntimeError, match="active pane has no peer xonsh"):
        xsess("work")


def test_xwin_xpane_focus_and_pane_moves(
    tmux_env: tuple[libtmux.Server, libtmux.Session, str, str],
) -> None:
    """xwin/xpane preserve caller focus, capture vars, and track pane moves."""
    srv, _host, _tmux_val, _pane_id = tmux_env
    ops = xsess("ops")
    p0 = ops.active_pane
    assert p0 is not None
    assert p0.send("echo boot").rtn == 0

    # Inside peer `ops`, call xwin() and xpane() with no target (inside tmux).
    r_create = ops.send(
        "w = xwin('logs')\n"
        "p = xpane()\n"
        "print(w.window_id, p.pane_id)\n"
        "assert w.send('echo in-win').out == 'in-win\\n'\n"
        "assert p.send('echo in-pane').out == 'in-pane\\n'\n"
    )
    assert r_create.rtn == 0, r_create.err
    win_id, split_pane_id = r_create.out.strip().split()

    # Neither xwin() nor xpane() stole focus from ops's active window/pane.
    ops.refresh()
    assert ops.active_window.window_id == p0.window_id
    assert ops.active_window.active_pane.pane_id == p0.pane_id

    # Move the split pane with break-pane, swap-pane, join-pane.
    moved = srv.panes.get(pane_id=split_pane_id)
    assert moved is not None
    initial_rx = moved.xonsh
    assert initial_rx is not None

    # Tab completion on Pane and RemoteXonsh stays non-blocking while busy.
    busy = moved.send("sleep 0.6\necho after-busy")
    t0 = time.monotonic()
    p_comps = attr_complete("moved.", locals(), lambda o, a: o.startswith(a))
    dt = time.monotonic() - t0
    assert dt < 0.2
    assert "moved.xonsh" in p_comps
    assert busy.out == "after-busy\n"

    srv.cmd("break-pane", "-d", "-s", split_pane_id)
    after_break = srv.panes.get(pane_id=split_pane_id)
    assert after_break is not None
    assert after_break.xonsh is initial_rx
    assert after_break.send("echo after-break").out == "after-break\n"

    win_logs = srv.windows.get(window_id=win_id)
    assert win_logs is not None
    logs_pane = win_logs.active_pane
    assert logs_pane is not None
    srv.cmd("join-pane", "-d", "-s", split_pane_id, "-t", logs_pane.pane_id)
    srv.cmd("swap-pane", "-d", "-s", split_pane_id, "-t", logs_pane.pane_id)
    after_swap = srv.panes.get(pane_id=split_pane_id)
    assert after_swap is not None
    assert after_swap.xonsh is initial_rx
    assert after_swap.send("echo after-swap").out == "after-swap\n"


def test_pane_peer_exit_kill_and_starter_outlive(
    tmux_env: tuple[libtmux.Server, libtmux.Session, str, str],
) -> None:
    """pane.xonsh cleans up after exit/kill and outlives its starter."""
    srv, _host, tmux_val, pane_id = tmux_env

    # 1. Peer created by a separate starter shell outlives that starter.
    starter_code = (
        "xontrib load tmux\n"
        "s = xsess('survivor')\n"
        "assert s.send('echo born').out == 'born\\n'\n"
    )
    env = dict(os.environ)
    env["TMUX"] = tmux_val
    env["TMUX_PANE"] = pane_id
    proc = subprocess.run(
        [sys.executable, "-m", "xonsh", "--no-rc", "-c", starter_code],
        env=env,
        capture_output=True,
        text=True,
        timeout=30.0,
    )
    assert proc.returncode == 0, proc.stderr

    surv = srv.sessions.get(session_name="survivor")
    assert surv is not None
    assert surv.send("echo still-alive").out == "still-alive\n"

    # 2. Peer exiting cleanly removes socket & clears pane.xonsh.
    surv_pane = surv.active_pane
    assert surv_pane is not None
    surv_rx = surv_pane.xonsh
    assert surv_rx is not None
    surv_sock = surv_rx._socket_path
    try:
        surv_pane.send("exit").end()
    except RuntimeError:
        pass
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline and surv_pane.xonsh is not None:
        time.sleep(0.05)
    assert surv_pane.xonsh is None
    assert not surv_sock.exists()

    # 3. Calling pane.xonsh.kill() terminates peer and removes socket.
    s2 = xsess("killme")
    p2 = s2.active_pane
    assert p2 is not None
    assert p2.send("echo ok").rtn == 0
    rx2 = p2.xonsh
    assert rx2 is not None
    sock2 = rx2._socket_path
    rx2.kill()
    assert p2.xonsh is None
    assert not sock2.exists()


def test_hidden_peer_outside_tmux_and_window_renaming(
    tmux_env: tuple[libtmux.Server, libtmux.Session, str, str],
) -> None:
    """Hidden peers stay outside tmux; visible panes rename per block."""
    _srv, _host, _tmux_val, _pane_id = tmux_env
    vis = xsess("vis")
    win = vis.active_window
    assert vis.send("echo ready").rtn == 0
    win.refresh()
    assert win.window_name == "xonsh"

    # Multi-line sent block renames window once to first non-comment command.
    slow = vis.send("# block comment\nsleep 0.8\necho done\n")
    rename_deadline = time.monotonic() + 3.0
    seen_name = ""
    while time.monotonic() < rename_deadline:
        win.refresh()
        if win.window_name != "xonsh":
            seen_name = str(win.window_name)
            break
        time.sleep(0.02)
    assert seen_name == "sleep"
    assert slow.rtn == 0
    win.refresh()
    assert win.window_name == "xonsh"

    # Sent block with capture= names after first command, not '# captured:'.
    delay = "0.6"
    cap_slow = vis.send("sleep @(delay)\necho cap-ok", capture=["delay"])
    cap_deadline = time.monotonic() + 3.0
    cap_seen = ""
    while time.monotonic() < cap_deadline:
        win.refresh()
        if win.window_name != "xonsh":
            cap_seen = str(win.window_name)
            break
        time.sleep(0.02)
    assert cap_seen == "sleep"
    assert cap_slow.rtn == 0
    win.refresh()
    assert win.window_name == "xonsh"

    # Hand-typed entry with the same command produces the same window name.
    vis_pane = vis.active_pane
    assert vis_pane is not None
    vis_pane.send_keys("sleep 0.6", enter=True)
    hand_deadline = time.monotonic() + 3.0
    hand_seen = ""
    while time.monotonic() < hand_deadline:
        win.refresh()
        if win.window_name != "xonsh":
            hand_seen = str(win.window_name)
            break
        time.sleep(0.02)
    assert hand_seen == seen_name == "sleep"
    back_deadline = time.monotonic() + 3.0
    while time.monotonic() < back_deadline:
        win.refresh()
        if win.window_name == "xonsh":
            break
        time.sleep(0.05)
    assert win.window_name == "xonsh"

    # A hidden RemoteXonsh() started inside vis inherits TMUX/TMUX_PANE,
    # yet reports is_inside_tmux() == False, raises NotInsideTmux on
    # xwin()/xpane(), and does NOT rename vis's window.
    check_block = (
        "import libtmux\n"
        "from xontrib_tmux.commands import is_inside_tmux, curtmuxw\n"
        "with RemoteXonsh() as h:\n"
        "    r = h.send(\n"
        "        'import libtmux\\n'\n"
        "        'from xontrib_tmux.commands import ('\n"
        "        'is_inside_tmux, curtmuxw)\\n'\n"
        "        'assert is_inside_tmux() is False\\n'\n"
        "        'assert curtmuxw() is None\\n'\n"
        "        'for fn in (xwin, xpane):\\n'\n"
        "        '    try:\\n'\n"
        "        '        fn()\\n'\n"
        "        '        raise AssertionError(\"want NotInsideTmux\")\\n'\n"
        "        '    except libtmux.exc.NotInsideTmux:\\n'\n"
        "        '        pass\\n'\n"
        "        'sleep 0.4\\n'\n"
        "    )\n"
        "    assert r.rtn == 0, r.err\n"
    )
    hidden_run = vis.send(check_block)
    time.sleep(0.25)
    win.refresh()
    assert win.window_name != "sleep"
    assert hidden_run.rtn == 0, hidden_run.err
    win.refresh()
    assert win.window_name == "xonsh"

    # $XONTRIB_TMUX_RENAME_WINDOW = False leaves the window name untouched.
    assert vis.send("$XONTRIB_TMUX_RENAME_WINDOW = False").rtn == 0
    win.rename_window("custom")
    win.refresh()
    assert win.window_name == "custom"
    opt_out = vis.send("sleep 0.4")
    time.sleep(0.2)
    win.refresh()
    assert win.window_name == "custom"
    assert opt_out.rtn == 0, opt_out.err
    win.refresh()
    assert win.window_name == "custom"


def test_nested_triage_dashboard_workflow(
    tmux_env: tuple[libtmux.Server, libtmux.Session, str, str],
) -> None:
    """Runs the proposal's nested with! dashboard workflow end to end."""
    srv, _host, tmux_val, pane_id = tmux_env
    script = (
        "xontrib load tmux\n"
        "import time\n"
        "issue = 99\n"
        "dash = xsess(f'gh-{issue}', x=160, y=30)\n"
        "with! dash.send(capture=['issue']) as outer:\n"
        "    with! xpane().send(capture=['issue']) as sub:\n"
        "        echo @(f'agent diagnosing issue {issue}')\n"
        "    echo @(f'viewing issue {issue}')\n"
        "outer.end()\n"
        "deadline = time.monotonic() + 30.0\n"
        "while time.monotonic() < deadline:\n"
        "    if len(dash.active_window.panes) >= 2:\n"
        "        _p0, _p1 = dash.active_window.panes\n"
        "        if _p1.xonsh is not None:\n"
        "            if _p1.xonsh.lastcmd.out is not None:\n"
        "                break\n"
        "    time.sleep(0.1)\n"
        "else:\n"
        "    raise AssertionError('Timed out waiting for nested dashboard')\n"
        "p0, p1 = dash.active_window.panes\n"
        "assert outer.rtn == 0, outer.err\n"
        "assert p0.xonsh.lastcmd.out == 'viewing issue 99\\n'\n"
        "assert p1.xonsh.lastcmd.out == 'agent diagnosing issue 99\\n'\n"
    )
    env = dict(os.environ)
    env["TMUX"] = tmux_val
    env["TMUX_PANE"] = pane_id
    proc = subprocess.run(
        [sys.executable, "-m", "xonsh", "--no-rc", "-c", script],
        env=env,
        capture_output=True,
        text=True,
        timeout=60.0,
    )
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr}"

    dash = srv.sessions.get(session_name="gh-99")
    assert dash is not None
    assert len(dash.active_window.panes) == 2
    p0, p1 = dash.active_window.panes
    assert "# captured: issue=99" in p0.text(scrollback=True)
    assert "# captured: issue=99" in p1.text(scrollback=True)
    assert p0.send("echo p0-ok").out == "p0-ok\n"
    assert p1.send("echo p1-ok").out == "p1-ok\n"


def test_scoped_capture_header_in_pane_text_and_history(
    tmux_env: tuple[libtmux.Server, libtmux.Session, str, str],
) -> None:
    """Verifies scoped capture= and # captured: in pane.text and history."""
    _srv, _host, _tmux_val, _pane_id = tmux_env
    main = xsess("cap_main", x=160, y=30)
    pane = main.active_pane
    assert pane is not None
    peer = pane.xonsh
    assert peer is not None

    peer.ctx["x"] = "before"
    x = 42
    issue = "gh-17"
    cfg = {f"k{i}": i for i in range(12)}
    r = pane.send(
        "print(f'in:{x}:{issue}:{len(cfg)}')\n"
        "echo cmd-ok\n"
        "x = 999\n"
        "saved_issue = issue\n",
        capture=["x", "issue", "cfg"],
    )
    assert r.rtn == 0, r.err
    assert r.out == "in:42:gh-17:12\ncmd-ok\n"
    assert r.err == ""
    assert [c.args for c in r.commands] == [["echo", "cmd-ok"]]

    assert peer.ctx["x"] == "before"
    assert "issue" not in peer.ctx
    assert "cfg" not in peer.ctx
    assert peer.ctx["saved_issue"] == "gh-17"

    expected_header = "# captured: x=42, issue='gh-17', cfg=<dict, 12 items>"
    expected_restored = "# restored: x='before'; unset: issue, cfg"
    pane_text = pane.text(scrollback=True)
    assert expected_header in pane_text
    assert expected_restored in pane_text
    lines = pane_text.splitlines()
    hdr_idx = next(i for i, ln in enumerate(lines) if expected_header in ln)
    out1_idx = lines.index("in:42:gh-17:12", hdr_idx)
    out2_idx = lines.index("cmd-ok", out1_idx)
    rest_idx = lines.index(expected_restored, out2_idx)
    assert hdr_idx < out1_idx < out2_idx < rest_idx
    assert rest_idx == len(lines) - 2
    assert lines[-1].endswith(("$", "@"))

    # Verify failing command, uncaught exception, interrupt, and plain block.
    r_fail = pane.send("false\n", capture=["x"])
    assert r_fail.rtn == 1
    assert "# restored:" not in (r_fail.out or "")
    assert "# restored: x='before'" in pane.text(scrollback=True).splitlines()

    r_exc = pane.send("1 / 0\n", capture=["issue"])
    assert r_exc.rtn == 1
    assert "# unset:" not in (r_exc.out or "")
    assert "# unset:" not in (r_exc.err or "")
    assert "# unset: issue" in pane.text(scrollback=True).splitlines()

    before_int = pane.text(scrollback=True).count("# restored:")
    r_int = pane.send("echo BEFORE_INT\nsleep 3\n", capture=["x"])
    time.sleep(0.5)
    pane.send_keys("C-c", literal=False)
    assert r_int.rtn != 0
    assert r_int.out == "BEFORE_INT\n"
    assert "# restored:" not in r_int.out
    assert pane.text(scrollback=True).count("# restored:") == before_int + 1

    before_plain = before_int + 1
    r_plain = pane.send("print('PLAIN_OK')\n")
    assert r_plain.rtn == 0
    assert r_plain.out == "PLAIN_OK\n"
    assert pane.text(scrollback=True).count("# restored:") == before_plain

    hist_res = pane.send(
        "items = [it['inp'] for it in __xonsh__.history.items()]\n"
        "matches = [s for s in items if 'saved_issue = issue' in s]\n"
        "assert matches, 'missing history entry'\n"
        "assert not any('# restored:' in s or '# unset:' in s for s in items)\n"
        "print(matches[-1].splitlines()[0])\n"
    )
    assert hist_res.rtn == 0, hist_res.err
    assert hist_res.out.strip() == expected_header


def test_xsess_dead_or_malformed_tmux_raises(
    tmux_env: tuple[libtmux.Server, libtmux.Session, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """xsess() raises RuntimeError recommending clearing TMUX on bad $TMUX."""
    _srv, _host, _tmux_val, _pane_id = tmux_env

    monkeypatch.setenv("TMUX", "/tmp/nonexistent_xtm4_sock,999999,0")
    with pytest.raises(RuntimeError, match="TMUX"):
        xsess("dead-should-fail")

    monkeypatch.setenv("TMUX", "malformed-tmux-value")
    with pytest.raises(RuntimeError, match="TMUX"):
        xsess("bad-should-fail")


def test_live_pane_text_wrapped_ansi_scrollback_and_closed(
    tmux_env: tuple[libtmux.Server, libtmux.Session, str, str],
) -> None:
    """Pane.text() joins wraps, strips trailing space/ANSI, checks closed."""
    _srv, _host, _tmux_val, _pane_id = tmux_env
    sess = xsess("text_sess", x=60, y=15)
    pane = sess.active_pane
    assert pane is not None

    r = pane.send(
        "python3 -c \"for i in range(40): "
        "print(f'\\x1b[31mline-{i:02d}\\x1b[0m "
        "https://example.com/very/long/path/{i}   ')\""
    )
    assert r.rtn == 0, r.err

    visible = pane.text()
    full = pane.text(scrollback=True)
    last10 = pane.text(scrollback=10)

    assert "\x1b" not in full
    assert not visible.endswith("\n")
    assert not full.endswith("\n")
    assert not any(line.endswith(" ") for line in full.splitlines())
    assert len(visible.splitlines()) < len(full.splitlines())
    assert len(visible.splitlines()) < len(last10.splitlines())
    urls = re.findall(r"https?://\S+", full)
    assert urls[-1] == "https://example.com/very/long/path/39"

    with pytest.raises(ValueError, match="non-negative"):
        pane.text(scrollback=-1)

    assert pane.xonsh is not None
    pane.xonsh.kill()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if pane.xonsh is None:
            break
        time.sleep(0.05)
    with pytest.raises(libtmux.exc.LibTmuxException):
        pane.text()


def test_live_session_attach_inside_tmux_dispatch(
    tmux_env: tuple[libtmux.Server, libtmux.Session, str, str],
) -> None:
    """Inside tmux, attach() rejects exit_, other server, and no client."""
    _srv, _host, _tmux_val, _pane_id = tmux_env
    driver = xsess("att_driver")
    res = driver.send(
        "import libtmux\n"
        "from xontrib_tmux.attach import DifferentTmuxServerError\n"
        "from xontrib_tmux.commands import is_inside_tmux\n"
        "assert is_inside_tmux() is True\n"
        "work = xsess('att_work')\n"
        "try:\n"
        "    work.attach(exit_=True)\n"
        "    raise AssertionError('expected ValueError')\n"
        "except ValueError as exc:\n"
        "    assert 'exit_' in str(exc)\n"
        "other_srv = libtmux.Server(socket_name='xtm4_other_srv')\n"
        "other_sess = libtmux.Session(server=other_srv, session_id='$0')\n"
        "try:\n"
        "    other_sess.attach()\n"
        "    raise AssertionError('expected DifferentTmuxServerError')\n"
        "except DifferentTmuxServerError as exc:\n"
        "    assert 'xtm4_other_srv' in str(exc)\n"
        "try:\n"
        "    work.attach()\n"
        "    raise AssertionError('expected LibTmuxException')\n"
        "except libtmux.exc.LibTmuxException:\n"
        "    pass\n"
        "print('ATTACH_CHECKS_OK')\n"
    )
    assert res.rtn == 0, res.err
    assert res.out == "ATTACH_CHECKS_OK\n"
