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

"""PEP 701 with! parser patch and interpreter-start peer bootstrap.

In Python 3.12+ (PEP 701), f-strings are tokenized into ``FSTRING_START``,
``FSTRING_MIDDLE``, and ``FSTRING_END`` tokens rather than a single ``STRING``
token.  xonsh's ``_attach_nodedent_base_rules`` excludes those three tokens
from the ``nodedent`` rule used by ``with!`` macro suites (``rawsuite``),
causing any ``f"..."`` inside a multiline ``with!`` block to raise
``SyntaxError``.

Because ``xonsh`` parses ``rc.xsh`` during startup *before* autoloading
xontribs, this module also provides a lightweight ``sys.meta_path`` import
hook installed via ``xontrib_tmux.pth`` so ``xonsh.parser_table`` is patched
as soon as it is imported, and bootstraps peer xonsh processes when
``_XONTRIB_TMUX_PEER_SOCKET`` is present in ``os.environ``.
"""

import atexit
import builtins
from importlib.abc import Loader, MetaPathFinder
from importlib.machinery import ModuleSpec
import io
import os
import sys
import tokenize
from types import ModuleType
from typing import Any, Callable, Optional, Sequence

from .protocol import HIDDEN_PEER_ENV, PEER_SOCKET_ENV


_FSTRING_TOKENS = ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END")
_NODEDENT_LHS = ("nodedent", "any_dedent_tok", "any_dedent_toks")

_peer_socket_path: str | None = None
_is_hidden_peer: bool = False
_peer_server_started: bool = False
_startup_atexit_registered: bool = False
_peer_events_attached: bool = False
_pre_rc_handler: Callable[..., Any] | None = None
_post_rc_handler: Callable[..., Any] | None = None
_post_init_handler: Callable[..., Any] | None = None
_orig_rc_runner: Callable[..., Any] | None = None


def _capture_peer_env() -> None:
    """Pops peer startup variables from ``os.environ`` into module state."""
    global _peer_socket_path, _is_hidden_peer, _peer_server_started
    raw_sock = os.environ.pop(PEER_SOCKET_ENV, None)
    raw_hidden = os.environ.pop(HIDDEN_PEER_ENV, None)
    if raw_sock:
        _peer_socket_path = raw_sock
        _is_hidden_peer = raw_hidden is not None and raw_hidden.lower() in (
            "1",
            "true",
            "yes",
        )
        _peer_server_started = False
        _ensure_startup_atexit()
        if _is_hidden_peer and os.environ.get("TERM", "") in ("", "dumb"):
            os.environ["TERM"] = "xterm-256color"


def mark_peer_server_started() -> None:
    """Disarms the startup-failure ``atexit`` sentinel."""
    global _peer_server_started
    _peer_server_started = True


def write_startup_error(socket_path: str, message: str) -> None:
    """Writes ``<socket_path>.err`` with a UTF-8 startup error message."""
    err_path = f"{socket_path}.err"
    try:
        with open(err_path, "w", encoding="utf-8") as f:
            f.write(message)
    except OSError:
        pass


def _on_startup_exit() -> None:
    """Writes a startup error sentinel if the peer exited before serving."""
    if _peer_socket_path is None or _peer_server_started:
        return
    err_path = f"{_peer_socket_path}.err"
    if not os.path.exists(err_path):
        write_startup_error(
            _peer_socket_path,
            "Peer xonsh exited before starting peer server",
        )


def _ensure_startup_atexit() -> None:
    """Registers the startup-failure ``atexit`` sentinel once."""
    global _startup_atexit_registered
    if not _startup_atexit_registered:
        atexit.register(_on_startup_exit)
        _startup_atexit_registered = True


def _attach_peer_events() -> None:
    """Attaches xonsh startup events to configure and start the peer server."""
    global _peer_events_attached, _pre_rc_handler, _post_rc_handler
    global _post_init_handler
    if _peer_events_attached or _peer_socket_path is None:
        return
    try:
        from xonsh.events import events
    except ImportError:
        return

    @events.on_pre_rc
    def _peer_on_pre_rc(**kwargs: Any) -> None:
        global _orig_rc_runner
        if _peer_socket_path is None:
            return
        import xonsh.environ

        if _orig_rc_runner is None:
            _orig_rc_runner = xonsh.environ.run_script_with_cache
            orig = _orig_rc_runner

            def _rc_runner(*args: Any, **kw: Any) -> Any:
                exc_info = orig(*args, **kw)
                if (
                    exc_info is not None
                    and exc_info[0] is not None
                    and issubclass(exc_info[0], SystemExit)
                ):
                    if _peer_socket_path is not None:
                        write_startup_error(
                            _peer_socket_path,
                            f"Peer exited during rc: {exc_info[1]}",
                        )
                    raise exc_info[1]
                return exc_info

            xonsh.environ.run_script_with_cache = _rc_runner

    @events.on_post_rc
    def _peer_on_post_rc(**kwargs: Any) -> None:
        global _orig_rc_runner
        if _orig_rc_runner is not None:
            import xonsh.environ

            xonsh.environ.run_script_with_cache = _orig_rc_runner
            _orig_rc_runner = None
        if _is_hidden_peer:
            xsh = getattr(builtins, "__xonsh__", None)
            env = getattr(xsh, "env", None)
            if env is not None:
                env["XONSH_HISTORY_BACKEND"] = "dummy"
                if env.get("TERM") in (None, "", "dumb"):
                    env["TERM"] = "xterm-256color"

    @events.on_post_init
    def _peer_on_post_init(**kwargs: Any) -> None:
        if _peer_socket_path is None:
            return
        xsh = getattr(builtins, "__xonsh__", None)
        try:
            import xontrib_tmux
            from . import peer_server

            ctx = getattr(xsh, "ctx", None)
            if ctx is None or (
                "RemoteXonsh" not in ctx and "tmuxw" not in ctx
            ):
                xontrib_tmux._load_xontrib_(xsh)
            peer_server.start_peer_server(
                xsh, _peer_socket_path, is_hidden=_is_hidden_peer
            )
        except Exception as exc:
            if not _peer_server_started:
                write_startup_error(_peer_socket_path, str(exc))
            raise

    _pre_rc_handler = _peer_on_pre_rc
    _post_rc_handler = _peer_on_post_rc
    _post_init_handler = _peer_on_post_init
    _peer_events_attached = True


def _is_autoload_disabled() -> bool:
    """Returns True if xontrib autoloading is disabled for ``tmux``."""
    xsh = getattr(builtins, "__xonsh__", None)
    env = getattr(xsh, "env", None)
    if env is not None and "XONTRIBS_AUTOLOAD_DISABLED" in env:
        disabled = env.get("XONTRIBS_AUTOLOAD_DISABLED")
        if disabled is True:
            return True
        if (
            isinstance(disabled, (list, tuple, set, frozenset))
            and "tmux" in disabled
        ):
            return True
    raw = os.environ.get("XONTRIBS_AUTOLOAD_DISABLED", "")
    if raw.lower() in ("1", "true", "yes"):
        return True
    if "tmux" in [item.strip() for item in raw.split(",")]:
        return True
    return False


def _find_unterminated_fstring(src: str) -> Optional[str]:
    """Returns an error message if ``src`` has an unterminated f-string."""
    try:
        for _ in tokenize.generate_tokens(io.StringIO(src).readline):
            pass
    except tokenize.TokenError as exc:
        msg = exc.args[0] if exc.args else ""
        if "f-string" in msg:
            return msg
    except IndentationError:
        pass
    return None


def _patch_base_parser() -> None:
    """Patches ``BaseParser`` for regenerated tables and f-string checks."""
    base_mod = sys.modules.get("xonsh.parsers.base")
    if base_mod is None:
        try:
            import xonsh.parsers.base as base_mod  # type: ignore
        except ImportError:
            return
    base_parser = getattr(base_mod, "BaseParser", None)
    if base_parser is None or getattr(
        base_parser, "_xontrib_tmux_patched", False
    ):
        return

    def _attach_nodedent_base_rules(self: Any) -> None:
        toks = set(self.tokens)
        toks.discard("DEDENT")
        toks.discard("INDENT")
        toks.discard("ENDMARKER")
        ts = "\n       | ".join(sorted(toks))
        self.p_nodedent_base.__func__.__doc__ = "nodedent : " + ts + "\n"

    orig_rawsuite = base_parser.p_rawsuite_indent

    def p_rawsuite_indent(self: Any, p: Any) -> None:
        orig_rawsuite(self, p)
        block_val = getattr(p[0], "value", None)
        if isinstance(block_val, str):
            err = _find_unterminated_fstring(block_val)
            if err is not None:
                self._set_error(err, None)
                raise SyntaxError(err)

    p_rawsuite_indent.__doc__ = orig_rawsuite.__doc__
    base_parser._attach_nodedent_base_rules = _attach_nodedent_base_rules
    base_parser.p_rawsuite_indent = p_rawsuite_indent
    base_parser._xontrib_tmux_patched = True

    xsh = getattr(builtins, "__xonsh__", None)
    parser_obj = getattr(getattr(xsh, "execer", None), "parser", None)
    lr_parser = getattr(parser_obj, "parser", None)
    for prod in getattr(lr_parser, "productions", None) or ():
        if getattr(prod, "name", None) == "rawsuite" and getattr(
            getattr(prod, "func", None), "__name__", None
        ) == "p_rawsuite_indent":
            prod.func = p_rawsuite_indent.__get__(
                parser_obj, type(parser_obj)
            )


def patch_parser_table(parser_table: Any) -> None:
    """Adds FSTRING token transitions to ``nodedent`` states."""
    if getattr(parser_table, "_xontrib_tmux_fstring_patched", False):
        return

    lr_action = getattr(parser_table, "_lr_action", None)
    lr_goto = getattr(parser_table, "_lr_goto", None)
    lr_productions = getattr(parser_table, "_lr_productions", None)
    if lr_action is None or lr_goto is None or lr_productions is None:
        return

    target_prods = set()
    indent_prods = set()
    for idx, prod in enumerate(lr_productions):
        if len(prod) < 2:
            continue
        lhs = prod[1]
        if lhs in _NODEDENT_LHS:
            target_prods.add(-idx)
        elif lhs == "indent_tok":
            indent_prods.add(-idx)

    nodedent_shift_targets = set()
    for goto_map in lr_goto.values():
        if "indent_tok" in goto_map:
            indent_state = goto_map["indent_tok"]
            str_act = lr_action.get(indent_state, {}).get("STRING")
            if str_act is not None and str_act > 0:
                nodedent_shift_targets.add(str_act)

    for actions in lr_action.values():
        str_act = actions.get("STRING")
        if str_act is None:
            continue
        if (
            str_act in nodedent_shift_targets
            or str_act in target_prods
            or (str_act in indent_prods and "FSTRING_START" not in actions)
        ):
            for tok in _FSTRING_TOKENS:
                if tok not in actions:
                    actions[tok] = str_act

    parser_table._xontrib_tmux_fstring_patched = True


def _remove_hook() -> None:
    """Removes any installed ``_ParserTableImportHook``."""
    global _hook_instance
    if _hook_instance is not None:
        try:
            sys.meta_path.remove(_hook_instance)
        except ValueError:
            pass
        _hook_instance = None


def patch_xonsh_parser() -> None:
    """Patches ``xonsh.parser_table`` and ``BaseParser``."""
    pt = sys.modules.get("xonsh.parser_table")
    if pt is None:
        try:
            import xonsh.parser_table as pt  # type: ignore
        except ImportError:
            return
    patch_parser_table(pt)
    _patch_base_parser()
    _remove_hook()


class _ParserTableImportHook(MetaPathFinder, Loader):
    """Patches ``xonsh.parser_table`` when imported, then removes itself."""

    _in_progress = False

    def find_spec(
        self,
        fullname: str,
        path: Optional[Sequence[str]],
        target: Optional[ModuleType] = None,
    ) -> Optional[ModuleSpec]:
        if fullname != "xonsh.parser_table" or self._in_progress:
            return None

        self._in_progress = True
        try:
            for finder in sys.meta_path:
                if finder is self:
                    continue
                find_spec = getattr(finder, "find_spec", None)
                if find_spec is None:
                    continue
                spec = find_spec(fullname, path, target)
                if spec is not None and spec.loader is not None:
                    orig_loader = spec.loader

                    class _WrappedLoader(Loader):
                        def create_module(
                            self, s: ModuleSpec
                        ) -> Optional[ModuleType]:
                            create = getattr(orig_loader, "create_module", None)
                            return create(s) if create is not None else None

                        def exec_module(self, module: ModuleType) -> None:
                            spec.loader = orig_loader
                            orig_loader.exec_module(module)
                            _remove_hook()
                            if (
                                _peer_socket_path is not None
                                or not _is_autoload_disabled()
                            ):
                                patch_parser_table(module)
                                _patch_base_parser()
                            if _peer_socket_path is not None:
                                _attach_peer_events()

                        def __getattr__(self, name: str) -> Any:
                            return getattr(orig_loader, name)

                    spec.loader = _WrappedLoader()
                    return spec
            return None
        finally:
            self._in_progress = False


_hook_instance: Optional[_ParserTableImportHook] = None


def install_parser_patch_hook() -> None:
    """Installs the import hook and captures peer startup configuration."""
    global _hook_instance
    _capture_peer_env()
    is_peer = _peer_socket_path is not None
    if is_peer:
        _ensure_startup_atexit()
    if not is_peer and _is_autoload_disabled():
        return
    pt = sys.modules.get("xonsh.parser_table")
    if pt is not None:
        patch_parser_table(pt)
        _patch_base_parser()
        if is_peer:
            _attach_peer_events()
        return
    if _hook_instance is None:
        _hook_instance = _ParserTableImportHook()
        sys.meta_path.insert(0, _hook_instance)


_capture_peer_env()
