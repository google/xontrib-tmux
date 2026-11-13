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

"""tmux session, window, and pane helpers for xonsh peers."""

import os
import pathlib
import shlex
import signal
import socket
import sys
import threading
import time
from typing import Any, Optional

import libtmux  # type: ignore
from libtmux.common import (  # type: ignore
    has_gte_version,
    raise_if_stderr,
    session_check_name,
)
from libtmux.constants import (  # type: ignore
    PaneDirection,
    WINDOW_DIRECTION_FLAG_MAP,
    WindowDirection,
)
from libtmux.pane import PANE_DIRECTION_FLAG_MAP  # type: ignore

from .peer import RemoteXonsh
from .protocol import PEER_SOCKET_ENV, default_timeout, new_socket_path

PEER_OPTION = "@xontrib_tmux_peer"

_FORBIDDEN_KWARGS = {
    "shell",
    "window_shell",
    "window_command",
    "window_name",
    "session_name",
}

_XSESS_ALLOWED_KWARGS = frozenset(
    {
        "kill_session",
        "start_directory",
        "x",
        "y",
        "environment",
        "detach_others",
        "no_size",
        "client_flags",
    }
)

_XWIN_ALLOWED_KWARGS = frozenset(
    {
        "start_directory",
        "attach",
        "window_index",
        "environment",
        "direction",
        "target_window",
        "kill_existing",
        "select_existing",
    }
)

_XPANE_ALLOWED_KWARGS = frozenset(
    {
        "start_directory",
        "attach",
        "direction",
        "full_window_split",
        "zoom",
        "size",
        "percentage",
        "environment",
        "empty",
        "style",
        "active_border_style",
        "inactive_border_style",
        "message",
        "keep",
    }
)

_pane_peers: dict[tuple[str, str, str], RemoteXonsh] = {}
_pane_peers_lock = threading.Lock()


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


def _current_server() -> libtmux.Server:
    """Returns the tmux server for $TMUX or the default server if unset."""
    if "TMUX" not in os.environ:
        return libtmux.Server()
    raw = os.environ["TMUX"]
    parts = raw.rsplit(",", 2)
    if (
        len(parts) != 3
        or not parts[0].strip()
        or not parts[1].strip().lstrip("-").isdigit()
        or not parts[2].strip().lstrip("-").isdigit()
    ):
        raise RuntimeError(
            f"Invalid $TMUX={raw!r}; clear $TMUX (e.g. `del $TMUX` or "
            "`unset TMUX`) to use the default tmux server"
        )
    sock_path = parts[0].strip()
    try:
        server = libtmux.Server.from_env()
        alive = bool(server.is_alive())
    except (libtmux.exc.LibTmuxException, ValueError, OSError) as exc:
        raise RuntimeError(
            f"tmux server in $TMUX ({sock_path!r}) is not running; clear "
            "$TMUX (e.g. `del $TMUX` or `unset TMUX`) to use the default "
            "tmux server"
        ) from exc
    if not alive:
        raise RuntimeError(
            f"tmux server in $TMUX ({sock_path!r}) is not running; clear "
            "$TMUX (e.g. `del $TMUX` or `unset TMUX`) to use the default "
            "tmux server"
        )
    return server


def _validate_kwargs(
    fn_name: str,
    kwargs: dict[str, Any],
    forbidden: set[str] | frozenset[str],
    allowed: frozenset[str],
) -> None:
    """Validates caller kwargs against forbidden and allowed option names."""
    bad_forbidden = sorted(set(kwargs) & set(forbidden))
    if bad_forbidden:
        joined = ", ".join(bad_forbidden)
        raise TypeError(f"{fn_name}() does not accept {joined}")
    bad_unknown = sorted(set(kwargs) - allowed)
    if bad_unknown:
        joined = ", ".join(bad_unknown)
        raise TypeError(
            f"{fn_name}() got unexpected keyword argument(s): {joined}"
        )


def _build_peer_command(sock_path: pathlib.Path) -> str:
    """Builds the shell command that launches a peer xonsh inside tmux."""
    return (
        f"env {PEER_SOCKET_ENV}={shlex.quote(str(sock_path))} "
        f"{shlex.quote(sys.executable)} -m xonsh -i"
    )


def _format_peer_note(sock_path: pathlib.Path, start_time: float) -> str:
    """Formats the atomic pane option template stored at peer creation."""
    return f"{sock_path}|{start_time}|#{{pane_pid}}"


def _server_key(server: libtmux.Server) -> str:
    """Returns a stable identifier string for a libtmux Server."""
    return str(
        getattr(server, "socket_path", None)
        or getattr(server, "socket_name", None)
        or "default"
    )


def _is_pid_alive(pid: int) -> bool:
    """Returns True if process ``pid`` exists."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _evict_pane_peers(pane: libtmux.Pane) -> None:
    """Removes any cached ``RemoteXonsh`` entries for ``pane``."""
    srv_key = _server_key(pane.server)
    pid_key = str(pane.pane_id)
    with _pane_peers_lock:
        stale_keys = [
            key
            for key in _pane_peers
            if key[0] == srv_key and key[1] == pid_key
        ]
        for key in stale_keys:
            _pane_peers.pop(key, None)


def _cleanup_dead_peer(
    pane: libtmux.Pane,
    sock_path: pathlib.Path,
) -> None:
    """Removes stale socket files, unsets pane option, and evicts cache."""
    _evict_pane_peers(pane)
    for target in (sock_path, pathlib.Path(f"{sock_path}.err")):
        try:
            target.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass
    try:
        pane.cmd("set-option", "-p", "-u", PEER_OPTION)
    except libtmux.exc.LibTmuxException:
        pass


def _is_socket_listening(sock_path: pathlib.Path) -> bool | None:
    """Probes ``sock_path`` non-blockingly.

    Returns ``True`` if accepting connections, ``False`` on
    ``ConnectionRefusedError``, or ``None`` if the file is missing.
    """
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.setblocking(False)
        sock.connect(str(sock_path))
        return True
    except (BlockingIOError, InterruptedError):
        return True
    except ConnectionRefusedError:
        return False
    except FileNotFoundError:
        return None
    except OSError:
        return False
    finally:
        sock.close()


def get_pane_xonsh(pane: libtmux.Pane) -> RemoteXonsh | None:
    """Returns the ``RemoteXonsh`` for ``pane``, or ``None`` if absent/dead."""
    try:
        res = pane.cmd(
            "display-message",
            "-p",
            f"#{{{PEER_OPTION}}}\t#{{pane_pid}}",
        )
    except libtmux.exc.LibTmuxException:
        _evict_pane_peers(pane)
        return None

    if res.stderr or not res.stdout:
        _evict_pane_peers(pane)
        return None

    line = res.stdout[0]
    opt_val, _, pid_str = line.partition("\t")
    opt_val = opt_val.strip()
    if not opt_val:
        _evict_pane_peers(pane)
        return None

    parts = opt_val.rsplit("|", 2)
    if len(parts) != 3:
        _evict_pane_peers(pane)
        return None

    sock_str, start_str, pid_note_str = parts
    try:
        start_time = float(start_str)
        noted_pid = int(pid_note_str)
    except ValueError:
        _evict_pane_peers(pane)
        return None

    sock_path = pathlib.Path(sock_str)
    err_path = pathlib.Path(f"{sock_path}.err")

    dead = False
    if err_path.exists():
        dead = True
    elif not _is_pid_alive(noted_pid):
        dead = True
    else:
        live_pid_text = pid_str.strip()
        if live_pid_text.isdigit() and int(live_pid_text) != noted_pid:
            dead = True

    if not dead:
        if sock_path.exists():
            listening = _is_socket_listening(sock_path)
            if listening is False:
                dead = True
        else:
            if (
                start_time <= 0
                or (time.time() - start_time) > default_timeout()
            ):
                dead = True

    if dead:
        _cleanup_dead_peer(pane, sock_path)
        return None

    cache_key = (_server_key(pane.server), str(pane.pane_id), sock_str)
    with _pane_peers_lock:
        cached = _pane_peers.get(cache_key)
        if cached is not None:
            if not cached._ctrl.state.killed:
                return cached
            _pane_peers.pop(cache_key, None)

        def _is_alive() -> bool:
            return _is_pid_alive(noted_pid)

        def _on_kill() -> None:
            try:
                os.killpg(noted_pid, signal.SIGKILL)
            except OSError:
                pass
            try:
                os.kill(noted_pid, signal.SIGKILL)
            except OSError:
                pass
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline and _is_pid_alive(noted_pid):
                time.sleep(0.01)

        peer = RemoteXonsh._from_socket(
            sock_path,
            start_time,
            is_alive=_is_alive,
            on_kill=_on_kill,
        )
        _pane_peers[cache_key] = peer
        return peer


def xsess(name: str | None = None, **kwargs: Any) -> libtmux.Session:
    """Creates or returns a tmux session whose active pane runs a peer xonsh.

    Args:
        name: Optional session name. If a session of this name already exists,
            returns it when its active pane holds a peer, or raises
            ``RuntimeError`` otherwise (ignoring ``kwargs``).
        **kwargs: ``libtmux.Server.new_session`` keyword arguments excluding
            ``shell``, ``window_shell``, ``window_command``, ``window_name``,
            ``session_name``, and ``attach``.

    Returns:
        The created or existing ``libtmux.Session``.

    Raises:
        TypeError: When forbidden or unknown keyword arguments are passed.
        RuntimeError: When an existing session's active pane has no peer.
    """
    _validate_kwargs(
        "xsess",
        kwargs,
        _FORBIDDEN_KWARGS | {"attach"},
        _XSESS_ALLOWED_KWARGS,
    )
    server = _current_server()
    if name is not None:
        session_check_name(name)
        if server.has_session(name):
            session = server.sessions.get(session_name=name, default=None)
            if session is not None:
                active = session.active_pane
                if active is not None and get_pane_xonsh(active) is not None:
                    return session
                raise RuntimeError(
                    f"Session {name!r} active pane has no peer xonsh"
                )

    sock_path = new_socket_path("p")
    start_time = time.time()
    peer_cmd = _build_peer_command(sock_path)
    note_fmt = _format_peer_note(sock_path, start_time)

    tmux_args: list[str | int] = ["-d", "-P", "-F#{session_id}"]
    if kwargs.get("detach_others"):
        tmux_args.append("-D")
    if kwargs.get("no_size"):
        tmux_args.append("-X")
    if kwargs.get("client_flags") is not None:
        tmux_args.extend(["-f", kwargs["client_flags"]])
    if name is not None:
        tmux_args.append(f"-s{name}")
    start_dir = kwargs.get("start_directory")
    if start_dir:
        tmux_args.extend(["-c", str(pathlib.Path(start_dir).expanduser())])
    if kwargs.get("x") is not None:
        tmux_args.extend(["-x", kwargs["x"]])
    if kwargs.get("y") is not None:
        tmux_args.extend(["-y", kwargs["y"]])
    environment = kwargs.get("environment")
    if environment:
        for key, val in environment.items():
            tmux_args.append(f"-e{key}={val}")
    tmux_args.extend(
        [
            peer_cmd,
            ";",
            "set-option",
            "-p",
            *(["-t", f"={name}:"] if name is not None else []),
            "-F",
            PEER_OPTION,
            note_fmt,
        ]
    )

    saved_tmux = os.environ.pop("TMUX", None)
    saved_pane = os.environ.pop("TMUX_PANE", None)
    try:
        proc = server.cmd("new-session", *tmux_args)
        raise_if_stderr(proc, "new-session")
        session_id = proc.stdout[0].strip()
    finally:
        if saved_tmux is not None:
            os.environ["TMUX"] = saved_tmux
        if saved_pane is not None:
            os.environ["TMUX_PANE"] = saved_pane

    return libtmux.Session.from_session_id(server=server, session_id=session_id)


def xwin(
    name: str | None = None,
    session: libtmux.Session | None = None,
    **kwargs: Any,
) -> libtmux.Window:
    """Creates a new tmux window running a peer xonsh.

    Args:
        name: Optional window name.
        session: Target ``libtmux.Session``. When ``None``, uses the caller's
            session (requires ``is_inside_tmux()``).
        **kwargs: ``libtmux.Session.new_window`` keyword arguments excluding
            ``shell``, ``window_shell``, ``window_command``, ``window_name``,
            and ``session_name``.

    Returns:
        The newly created ``libtmux.Window``.

    Raises:
        TypeError: When forbidden or unknown keyword arguments are passed.
        libtmux.exc.NotInsideTmux: When ``session`` is ``None`` outside tmux.
    """
    _validate_kwargs("xwin", kwargs, _FORBIDDEN_KWARGS, _XWIN_ALLOWED_KWARGS)
    if session is None:
        if not is_inside_tmux():
            raise libtmux.exc.NotInsideTmux
        session = libtmux.Session.from_env()

    attach = bool(kwargs.get("attach", False))
    sock_path = new_socket_path("p")
    start_time = time.time()
    peer_cmd = _build_peer_command(sock_path)
    note_fmt = _format_peer_note(sock_path, start_time)

    tmux_args: list[str | int] = ["-P", "-F#{window_id}"]
    start_dir = kwargs.get("start_directory")
    if start_dir:
        tmux_args.append(f"-c{pathlib.Path(start_dir).expanduser()}")
    if name is not None and isinstance(name, str):
        tmux_args.extend(["-n", name])
    environment = kwargs.get("environment")
    if environment:
        for key, val in environment.items():
            tmux_args.append(f"-e{key}={val}")
    direction = kwargs.get("direction")
    if direction is not None:
        tmux_args.append(WINDOW_DIRECTION_FLAG_MAP[WindowDirection(direction)])
    if kwargs.get("kill_existing"):
        tmux_args.append("-k")
    if kwargs.get("select_existing"):
        tmux_args.append("-S")

    target_window = kwargs.get("target_window")
    window_index = kwargs.get("window_index", "")
    if target_window:
        target = str(target_window)
    elif window_index is not None:
        target = f"{session.session_id}:{window_index}"
    else:
        target = str(session.session_id)
    tmux_args.extend(["-t", target])
    tmux_args.extend(
        [
            peer_cmd,
            ";",
            "set-option",
            "-p",
            "-F",
            PEER_OPTION,
            note_fmt,
        ]
    )
    if not attach:
        tmux_args.extend([";", "last-window", "-t", str(session.session_id)])

    proc = session.server.cmd("new-window", *tmux_args)
    raise_if_stderr(proc, "new-window")
    window_id = proc.stdout[0].strip()
    return libtmux.Window.from_window_id(
        server=session.server, window_id=window_id
    )


def xpane(
    pane: libtmux.Pane | None = None,
    **kwargs: Any,
) -> libtmux.Pane:
    """Splits a tmux pane with a new pane running a peer xonsh.

    Args:
        pane: Target ``libtmux.Pane`` to split. When ``None``, resolves the
            caller's own pane (requires ``is_inside_tmux()``).
        **kwargs: ``libtmux.Pane.split`` keyword arguments excluding
            ``shell``, ``window_shell``, ``window_command``, ``window_name``,
            and ``session_name``.

    Returns:
        The newly created ``libtmux.Pane``.

    Raises:
        TypeError: When forbidden or unknown keyword arguments are passed.
        ValueError: When both ``size`` and ``percentage`` are passed.
        libtmux.exc.NotInsideTmux: When ``pane`` is ``None`` outside tmux.
    """
    _validate_kwargs("xpane", kwargs, _FORBIDDEN_KWARGS, _XPANE_ALLOWED_KWARGS)
    if pane is None:
        if not is_inside_tmux():
            raise libtmux.exc.NotInsideTmux
        pane = libtmux.Pane.from_env()

    size = kwargs.get("size")
    percentage = kwargs.get("percentage")
    if size is not None and percentage is not None:
        raise ValueError("Cannot specify both size and percentage")

    attach = bool(kwargs.get("attach", False))
    sock_path = new_socket_path("p")
    start_time = time.time()
    peer_cmd = _build_peer_command(sock_path)
    note_fmt = _format_peer_note(sock_path, start_time)

    direction = kwargs.get("direction")
    dir_key = (
        PaneDirection(direction)
        if direction is not None
        else PaneDirection.Below
    )
    tmux_args: list[str | int] = list(PANE_DIRECTION_FLAG_MAP[dir_key])
    if size is not None:
        tmux_args.append(f"-l{size}")
    if percentage is not None:
        tmux_args.append(f"-p{percentage}")
    if kwargs.get("full_window_split"):
        tmux_args.append("-f")
    if kwargs.get("zoom"):
        tmux_args.append("-Z")
    tmux_args.extend(["-P", "-F#{pane_id}"])
    start_dir = kwargs.get("start_directory")
    if start_dir:
        tmux_args.append(f"-c{pathlib.Path(start_dir).expanduser()}")
    environment = kwargs.get("environment")
    if environment:
        for key, val in environment.items():
            tmux_args.append(f"-e{key}={val}")

    if kwargs.get("empty"):
        if has_gte_version("3.7", tmux_bin=pane.server.tmux_bin):
            tmux_args.append("-E")

    styling = {
        "-s": kwargs.get("style"),
        "-S": kwargs.get("active_border_style"),
        "-R": kwargs.get("inactive_border_style"),
        "-m": kwargs.get("message"),
    }
    keep = kwargs.get("keep")
    if keep or any(val is not None for val in styling.values()):
        if has_gte_version("3.7", tmux_bin=pane.server.tmux_bin):
            for flag, val in styling.items():
                if val is not None:
                    tmux_args.extend([flag, val])
            if keep:
                tmux_args.append("-k")

    tmux_args.extend(["-t", str(pane.pane_id)])
    tmux_args.extend(
        [
            peer_cmd,
            ";",
            "set-option",
            "-p",
            "-F",
            PEER_OPTION,
            note_fmt,
        ]
    )
    win_id = getattr(pane, "window_id", None)
    if win_id is None:
        win_obj = getattr(pane, "window", None)
        win_id = getattr(win_obj, "window_id", None)
    if not attach and win_id is not None:
        tmux_args.extend([";", "last-pane", "-t", str(win_id)])

    proc = pane.server.cmd("split-window", *tmux_args)
    if proc.stderr:
        raise libtmux.exc.LibTmuxException(proc.stderr)
    pane_id = proc.stdout[0].strip()
    return libtmux.Pane.from_pane_id(server=pane.server, pane_id=pane_id)


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
