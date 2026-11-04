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

"""Provides macro block execution and variable capturing for tmux."""

from concurrent.futures import Future
import inspect
import io
import re
import tempfile
import tokenize
import types
from typing import Any, Dict, List, Optional

import dill
import libtmux  # type: ignore

from .sender import deliver


PASTE_START = "\x1b[200~"
PASTE_END = "\x1b[201~"

_CAPTURE_PENDING_HEADER = "# xontrib-tmux-captured: pending\n"
_CAPTURE_DELIVERED_HEADER = "# xontrib-tmux-captured: delivered\n"

_CONTINUATION_RE = re.compile(
    r"^(?:elif\b|else\s*:|except\b|finally\s*:|case\b|[)\]}])"
)


def _build_history_suppress_preamble(rc_path: Optional[str] = None) -> str:
    """Builds the one-shot history/event suppression preamble for ``rc_src``."""
    return (
        f"{_CAPTURE_PENDING_HEADER}"
        "def _xontrib_tmux_suppress_history():\n"
        "    import builtins\n"
        f"    rc_path = {rc_path!r}\n"
        "    if rc_path is not None:\n"
        "        try:\n"
        "            with open(rc_path, 'r', encoding='utf-8') as f:\n"
        "                content = f.read()\n"
        f"            if not content.startswith({_CAPTURE_PENDING_HEADER!r}):\n"
        "                return\n"
        "            with open(rc_path, 'w', encoding='utf-8') as f:\n"
        f"                f.write({_CAPTURE_DELIVERED_HEADER!r} + content[{len(_CAPTURE_PENDING_HEADER)}:])\n"
        "        except OSError:\n"
        "            return\n"
        "    xsh = getattr(builtins, '__xonsh__', None)\n"
        "    if xsh is None:\n"
        "        return\n"
        "    env = getattr(xsh, 'env', None)\n"
        "    had_rtn = env is not None and 'LAST_RETURN_CODE' in env\n"
        "    prev_rtn = env.get('LAST_RETURN_CODE') if had_rtn else None\n"
        "    hist = getattr(xsh, 'history', None)\n"
        "    if hist is not None:\n"
        "        orig_append = hist.append\n"
        "        def _append_once(cmd, _orig=orig_append):\n"
        "            hist.append = _orig\n"
        "            if env is not None:\n"
        "                if had_rtn:\n"
        "                    env['LAST_RETURN_CODE'] = prev_rtn\n"
        "                else:\n"
        "                    env.pop('LAST_RETURN_CODE', None)\n"
        "        hist.append = _append_once\n"
        "    events = getattr(builtins, 'events', None)\n"
        "    on_post = getattr(events, 'on_postcommand', None)\n"
        "    if on_post is not None and hasattr(on_post, 'fire'):\n"
        "        orig_fire = on_post.fire\n"
        "        def _fire_once(**kwargs):\n"
        "            on_post.fire = orig_fire\n"
        "        on_post.fire = _fire_once\n"
        "    ptk_hist = getattr(\n"
        "        getattr(getattr(xsh, 'shell', None), 'shell', None), 'history', None\n"
        "    )\n"
        "    loaded = getattr(ptk_hist, '_loaded_strings', None)\n"
        "    if loaded and loaded[0].lstrip().startswith('source '):\n"
        "        del loaded[0]\n"
        "_xontrib_tmux_suppress_history()\n"
        "del _xontrib_tmux_suppress_history\n"
    )


def is_pending_capture_cmd(cmd: str) -> bool:
    """Returns True if ``cmd`` sources a pending plugin-generated capture file."""
    parts = cmd.strip().split(maxsplit=1)
    if len(parts) != 2 or parts[0] != "source":
        return False
    path = parts[1].strip()
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.readline() == _CAPTURE_PENDING_HEADER
    except OSError:
        return False


def _has_unclosed_construct(current: List[str]) -> bool:
    """Returns True if ``current`` ends inside an unclosed string, delimiter, or backslash."""
    if not current:
        return False
    if current[-1].rstrip().endswith("\\"):
        return True
    src = "\n".join(current)
    try:
        for _ in tokenize.generate_tokens(io.StringIO(src).readline):
            pass
    except tokenize.TokenError as exc:
        msg = exc.args[0] if exc.args else ""
        if "EOF in multi-line" in msg:
            return True
    except IndentationError:
        pass
    return False


def _continues_block(current: List[str], next_line: str) -> bool:
    """Returns True if ``next_line`` (non-blank) continues ``current``."""
    if _has_unclosed_construct(current):
        return True
    if next_line[0] in (" ", "\t"):
        return True
    if _CONTINUATION_RE.match(next_line.lstrip()):
        return True
    for prev in reversed(current):
        s = prev.strip()
        if not s:
            continue
        if s.endswith("\\"):
            return True
        if s.startswith("@") and not s.startswith(("@(", "@$", "@!")):
            return True
        break
    return False


def _group_statements(raw_lines: List[str]) -> List[List[str]]:
    """Groups lines into top-level single-line or multi-line statements."""
    groups: List[List[str]] = []
    current: List[str] = []
    pending_blanks: List[str] = []

    for line in raw_lines:
        if not line.strip():
            if current and _has_unclosed_construct(current):
                current.append(line)
            elif current:
                pending_blanks.append("")
            else:
                groups.append([""])
            continue

        if current and _continues_block(current, line):
            current.extend(pending_blanks)
            pending_blanks.clear()
            current.append(line)
        else:
            if current:
                groups.append(current)
            for _ in pending_blanks:
                groups.append([""])
            pending_blanks.clear()
            current = [line]

    if current:
        groups.append(current)
    for _ in pending_blanks:
        groups.append([""])

    return groups


class TmuxMacro:
    """Context manager macro for sending code blocks to tmux panes."""

    # make sure the macro_block comes back as a string
    __xonsh_block__ = str
    macro_block: str
    macro_globals: dict[Any, Any]
    macro_locals: dict[Any, Any]

    def __init__(
        self,
        pane: libtmux.Pane,
        captured: Optional[Dict[str, bytes]] = None,
    ):
        self.pane = pane
        self.captured = captured
        # Resolves once the block has been typed into the pane; sending is
        # done in the background so the calling shell never blocks.
        self.future: Optional["Future[Any]"] = None

    def rc_src(self, rc_path: Optional[str] = None) -> Optional[bytes]:
        """Generates script bytes to unpickle captured variables in tmux."""
        if self.captured is None:
            return None

        src = _build_history_suppress_preamble(rc_path) + "import dill\n"
        for var, pickled_val in self.captured.items():
            src += f"{var} = dill.loads({pickled_val})\n"
        src += "del dill\n"
        return src.encode('utf-8')

    def lines(self) -> List[str]:
        """Builds the lines to type into the pane, in order."""
        lines = []
        # Pickle the captured dict and unpickle it on the other side
        if self.captured is not None:
            with tempfile.NamedTemporaryFile(
                delete=False, prefix='captured_', suffix='.xsh'
            ) as rc_file:
                rc_src = self.rc_src(rc_path=rc_file.name)
                assert rc_src is not None
                rc_file.write(rc_src)
            lines.append(f"source {rc_file.name}")

        # strip comments off the macro block
        stripped = re.sub(
            r'[ \t]*#\d+:.*$', '', self.macro_block, flags=re.MULTILINE
        )
        # Wrap single-line statements in paste codes, and wrap multi-line
        # compound blocks across the whole block with a trailing PASTE_END
        # line so the final Enter submits the indented block in xonsh.
        for group in _group_statements(stripped.splitlines()):
            if len(group) == 1:
                lines.append(f"{PASTE_START}{group[0]}{PASTE_END}")
            else:
                lines.append(f"{PASTE_START}{group[0]}")
                lines.extend(group[1:])
                lines.append(PASTE_END)
        return lines

    def __enter__(self):
        # Detect common misuse
        if not hasattr(self, 'macro_block'):
            raise ValueError('TmuxMacro is missing attribute "macro_block". '
                             'If using it as a context manager, ensure you '
                             'are using "with!" rather than "with".')
        # Snapshot the payload now -- __exit__ drops macro_block, and the
        # delivery thread may not have run yet.
        self.future = deliver(self.pane, self.lines())
        return self

    def __exit__(self, exc_type, exc, tb):
        if hasattr(self, 'macro_block'):
            del self.macro_block
        if hasattr(self, 'macro_globals'):
            del self.macro_globals
        if hasattr(self, 'macro_locals'):
            del self.macro_locals


def capture_vars(frame: types.FrameType, names: List[str]) -> Dict[str, bytes]:
    """Captures the values of variables (by name) from the caller's scope.

    Args:
        frame: the caller's frame.
        names: list of variable name strings to look up.

    Returns:
        dict mapping each name to its value in the caller's frame.

    Raises:
        NameError: if a name isn't found in the caller's locals or globals.
    """
    try:
        caller_locals = frame.f_locals
        caller_globals = frame.f_globals
        result = {}
        for name in names:
            if name in caller_locals:
                result[name] = dill.dumps(caller_locals[name])
            elif name in caller_globals:
                result[name] = dill.dumps(caller_globals[name])
            else:
                raise NameError(
                    f"name '{name}' is not defined in caller's scope"
                )
        return result
    finally:
        # avoid reference cycles from holding onto the frame
        del frame


def send(
    pane: libtmux.Pane,
    inline_cmds: Optional[str] = None,
    /,
    capture: Optional[List[str]] = None,
) -> Any:
    """Sends commands to a tmux pane or returns a TmuxMacro context manager.

    Never blocks: keys are typed by a background worker once the pane's shell
    has taken over its tty.

    Returns:
        A :class:`TmuxMacro` when used as a context manager, otherwise a
        future that resolves when ``inline_cmds`` has been typed.
    """
    curr = inspect.currentframe()
    caller_frame = curr.f_back.f_back if curr and curr.f_back else None
    captured = (
        capture_vars(caller_frame, capture)
        if (capture is not None and caller_frame is not None)
        else None
    )
    macro = TmuxMacro(pane, captured)
    if inline_cmds is None:
        # to be used as context manager
        return macro
    else:
        # TODO: handle captured
        return deliver(pane, [inline_cmds])
