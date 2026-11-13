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

"""Wire protocol, socket security, and syntax checking for peer xonsh shells."""

import base64
import builtins
from collections.abc import Sequence
import itertools
import json
import os
import pathlib
import re
import socket
import stat
import struct
import threading
import types
from typing import Any, IO


PROTOCOL_VERSION: int = 1
PEER_SOCKET_ENV: str = "_XONTRIB_TMUX_PEER_SOCKET"
HIDDEN_PEER_ENV: str = "_XONTRIB_TMUX_HIDDEN"
TIMEOUT_ENV_VAR: str = "XONTRIB_TMUX_READY_TIMEOUT"
DEFAULT_TIMEOUT: float = 120.0

_MACRO_COMMENT_RE = re.compile(r"[ \t]*#\d+:.*$", re.MULTILINE)
_MAX_SOCKET_DIR_BYTES = 80
_MAX_SOCKET_PATH_BYTES = 100

_counter_lock = threading.Lock()
_socket_counter = itertools.count()

_syntax_lock = threading.Lock()
_syntax_execer: Any = None
_tls = threading.local()


def default_timeout() -> float:
    """Returns the peer readiness timeout, honoring $XONTRIB_TMUX_READY_TIMEOUT.

    Checks ``builtins.__xonsh__.env`` first, then ``os.environ``.

    Returns:
        Timeout in seconds, defaulting to ``DEFAULT_TIMEOUT`` when unset or
        unparseable.
    """
    raw: Any = None
    xsh = getattr(builtins, "__xonsh__", None)
    env = getattr(xsh, "env", None)
    if env is not None and TIMEOUT_ENV_VAR in env:
        raw = env.get(TIMEOUT_ENV_VAR)
    if raw is None:
        raw = os.environ.get(TIMEOUT_ENV_VAR)
    if raw is None:
        return DEFAULT_TIMEOUT
    try:
        return float(raw)
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT


def _verify_dir(path: pathlib.Path) -> pathlib.Path:
    """Creates ``path`` with 0o700 mode and checks ownership/permissions."""
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
    except FileExistsError as exc:
        raise PermissionError(
            f"Socket directory {path} is not a real directory"
        ) from exc
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        raise PermissionError(
            f"Socket directory {path} is not a real directory"
        )
    uid = os.getuid()
    if st.st_uid != uid:
        raise PermissionError(
            f"Socket directory {path} is owned by uid {st.st_uid}, "
            f"expected {uid}"
        )
    if (st.st_mode & 0o077) != 0:
        raise PermissionError(
            f"Socket directory {path} has unsafe mode "
            f"{oct(stat.S_IMODE(st.st_mode))}; expected 0o700"
        )
    return path


def socket_dir() -> pathlib.Path:
    """Returns a verified owner-only directory for peer Unix sockets.

    Uses ``$XDG_RUNTIME_DIR/xontrib-tmux`` when ``$XDG_RUNTIME_DIR`` is set and
    the resulting path is shorter than 80 UTF-8 bytes; otherwise falls back to
    ``/tmp/xontrib-tmux-<uid>``.

    Returns:
        Path to the verified ``0o700`` socket directory.

    Raises:
        PermissionError: If the directory is a symlink/non-directory, is owned
            by another user, or grants group/other permissions.
    """
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        candidate = pathlib.Path(xdg) / "xontrib-tmux"
        if len(str(candidate).encode("utf-8")) < _MAX_SOCKET_DIR_BYTES:
            return _verify_dir(candidate)
    return _verify_dir(pathlib.Path(f"/tmp/xontrib-tmux-{os.getuid()}"))


def new_socket_path(prefix: str = "p") -> pathlib.Path:
    """Allocates a unique Unix socket path under 100 UTF-8 bytes.

    Args:
        prefix: Filename prefix for the socket.

    Returns:
        A unique ``.sock`` path inside a verified owner-only directory.

    Raises:
        PermissionError: If the socket directory fails ownership/mode checks.
        ValueError: If the resulting path cannot fit under 100 UTF-8 bytes.
    """
    sdir = socket_dir()
    pid = os.getpid()
    with _counter_lock:
        seq = next(_socket_counter)
    name = f"{prefix}_{pid}_{seq}.sock"
    path = sdir / name
    if len(str(path).encode("utf-8")) >= _MAX_SOCKET_PATH_BYTES:
        fallback = _verify_dir(
            pathlib.Path(f"/tmp/xontrib-tmux-{os.getuid()}")
        )
        path = fallback / name
    if len(str(path).encode("utf-8")) >= _MAX_SOCKET_PATH_BYTES:
        raise ValueError(
            f"Socket path {path} exceeds {_MAX_SOCKET_PATH_BYTES - 1} bytes"
        )
    return path


def verify_peer_uid(conn: socket.socket) -> int:
    """Verifies via ``SO_PEERCRED`` that the connected peer has our UID.

    Args:
        conn: Connected ``AF_UNIX`` stream socket.

    Returns:
        The connected peer's PID.

    Raises:
        PermissionError: If the peer's UID does not match ``os.getuid()``.
    """
    cred_size = struct.calcsize("3i")
    creds = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, cred_size)
    pid, uid, _gid = struct.unpack("3i", creds)
    expected_uid = os.getuid()
    if uid != expected_uid:
        raise PermissionError(
            f"Peer uid {uid} does not match expected uid {expected_uid}"
        )
    return pid


def strip_macro_comments(block: str) -> str:
    """Strips xonsh macro line comments (``#<n>:...``) from ``block``."""
    return _MACRO_COMMENT_RE.sub("", block)


def _patch_execer_tls(execer_mod: Any) -> None:
    """Routes ``xonsh.execer``'s helper lexers through ``_tls.lexer``.

    ``xonsh.execer.Execer._parse_ctx_free`` calls ``ends_with_colon_token``
    and ``balanced_parens`` without passing ``lexer=self.parser.lexer``, so
    they otherwise fall back to ``XSH.execer.parser.lexer`` and collide with
    an active main-thread shell parser.
    """
    if getattr(execer_mod, "_xontrib_tmux_tls_patched", False):
        return
    orig_ends = execer_mod.ends_with_colon_token
    orig_bal = execer_mod.balanced_parens

    def _ends_with_colon_token(line: str, lexer: Any = None) -> bool:
        if lexer is None:
            lexer = getattr(_tls, "lexer", None)
        return orig_ends(line, lexer=lexer)

    def _balanced_parens(
        line: str, mincol: int = 0, maxcol: int | None = None, lexer: Any = None
    ) -> bool:
        if lexer is None:
            lexer = getattr(_tls, "lexer", None)
        return orig_bal(line, mincol=mincol, maxcol=maxcol, lexer=lexer)

    execer_mod.ends_with_colon_token = _ends_with_colon_token
    execer_mod.balanced_parens = _balanced_parens
    execer_mod._xontrib_tmux_tls_patched = True


def check_block_syntax(block: str) -> str:
    """Validates xonsh syntax and non-emptiness for a command block.

    Uses a dedicated ``Execer`` guarded by a module-level lock so concurrent
    callers (for example ``future.add_done_callback`` threads) cannot corrupt
    parser state or collide with ``XSH.execer``.

    Args:
        block: Raw xonsh/Python source text.

    Returns:
        The macro-comment-stripped block normalized to end with ``\\n``.

    Raises:
        TypeError: If ``block`` is not a ``str``.
        SyntaxError: If parsing fails or the block is incomplete.
        ValueError: If the block contains no statements (empty or comments
            only).
    """
    global _syntax_execer
    if not isinstance(block, str):
        raise TypeError(f"Expected str block, got {type(block).__name__}")
    cleaned = strip_macro_comments(block)
    normalized = cleaned if cleaned.endswith("\n") else cleaned + "\n"
    with _syntax_lock:
        if _syntax_execer is None:
            from .parser_patch import patch_xonsh_parser

            patch_xonsh_parser()
            import xonsh.execer

            _patch_execer_tls(xonsh.execer)
            _syntax_execer = xonsh.execer.Execer(filename="<peer-check>")
        _tls.lexer = _syntax_execer.parser.lexer
        try:
            tree = _syntax_execer.parse(normalized, ctx={})
        finally:
            _tls.lexer = None
    if tree is None or not getattr(tree, "body", None):
        raise ValueError("Block contains no statements")
    return normalized


def send_json(sock: socket.socket, msg: dict[str, Any]) -> None:
    """Sends a newline-delimited JSON message, stamping ``PROTOCOL_VERSION``.

    Args:
        sock: Connected stream socket.
        msg: JSON-serializable message mapping.
    """
    payload = dict(msg)
    if "version" not in payload:
        payload["version"] = PROTOCOL_VERSION
    encoded = (json.dumps(payload, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    sock.sendall(encoded)


def recv_json(f: IO[str]) -> dict[str, Any] | None:
    """Reads one newline-delimited JSON message and checks protocol version.

    Args:
        f: Text stream wrapped around a socket.

    Returns:
        The parsed JSON dict, or ``None`` on EOF.

    Raises:
        RuntimeError: If the payload is malformed JSON, not a dict, or its
            ``version`` does not match ``PROTOCOL_VERSION``.
    """
    line = f.readline()
    if not line:
        return None
    try:
        msg = json.loads(line)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Malformed JSON protocol message: {exc}") from exc
    if not isinstance(msg, dict):
        raise RuntimeError(
            f"Protocol message must be a JSON object, got {type(msg).__name__}"
        )
    version = msg.get("version")
    if version != PROTOCOL_VERSION:
        raise RuntimeError(
            f"Protocol version mismatch: expected {PROTOCOL_VERSION}, "
            f"got {version!r}"
        )
    return msg


def encode_value(val: Any) -> str:
    """Serializes ``val`` with ``dill`` and encodes as an ASCII base64 string.

    Args:
        val: Arbitrary picklable Python value.

    Returns:
        Base64-encoded ASCII string of the ``dill`` payload.
    """
    import dill

    return base64.b64encode(dill.dumps(val)).decode("ascii")


def decode_value(b64: str) -> Any:
    """Decodes a base64 ``dill`` payload produced by :func:`encode_value`.

    Args:
        b64: Base64-encoded ASCII string.

    Returns:
        The unpickled Python value.
    """
    import dill

    return dill.loads(base64.b64decode(b64.encode("ascii")))


def capture_vars(
    frame: types.FrameType | None,
    names: Sequence[str],
) -> dict[str, str]:
    """Looks up ``names`` in ``frame`` and encodes them for JSON transport.

    Resolves each name in ``frame.f_locals`` and then ``frame.f_globals``,
    raising ``NameError`` if missing and propagating pickling errors from
    ``dill.dumps`` immediately. Always deletes ``frame`` in ``finally``.

    Args:
        frame: Caller execution frame, or ``None`` if unavailable.
        names: Sequence of variable names to capture.

    Returns:
        Mapping from variable name to base64-encoded ``dill`` payload.

    Raises:
        TypeError: If ``names`` is not a sequence of strings.
        NameError: If any requested variable name is not in scope.
    """
    encoded: dict[str, str] = {}
    try:
        if isinstance(names, (str, bytes)):
            raise TypeError(
                "capture must be a sequence of variable names, not str or bytes"
            )
        if not isinstance(names, Sequence):
            raise TypeError("capture must be a sequence of variable names")
        if frame is None:
            if names:
                raise NameError(f"name {names[0]!r} is not defined")
            return encoded
        locs = frame.f_locals
        globs = frame.f_globals
        for name in names:
            if not isinstance(name, str):
                raise TypeError(
                    f"capture variable name must be str, "
                    f"got {type(name).__name__}"
                )
            if name in locs:
                val = locs[name]
            elif name in globs:
                val = globs[name]
            else:
                raise NameError(f"name {name!r} is not defined")
            encoded[name] = encode_value(val)
        return encoded
    finally:
        del frame


def decode_captured(encoded: dict[str, str]) -> dict[str, Any]:
    """Decodes a ``{name: b64}`` mapping produced by :func:`capture_vars`.

    Args:
        encoded: Mapping from variable name to base64-encoded ``dill`` payload.

    Returns:
        Mapping from variable name to deserialized Python object.
    """
    return {name: decode_value(b64) for name, b64 in encoded.items()}
