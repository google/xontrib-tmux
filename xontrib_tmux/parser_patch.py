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

"""Patches xonsh's LALR parser table so f-strings parse inside with! blocks.

In Python 3.12+ (PEP 701), f-strings are tokenized into ``FSTRING_START``,
``FSTRING_MIDDLE``, and ``FSTRING_END`` tokens rather than a single ``STRING``
token.  xonsh's ``_attach_nodedent_base_rules`` excludes those three tokens
from the ``nodedent`` rule used by ``with!`` macro suites (``rawsuite``),
causing any ``f"..."`` inside a multiline ``with!`` block to raise
``SyntaxError``.

Because ``xonsh`` parses ``rc.xsh`` during startup *before* autoloading
xontribs, this module also provides a lightweight ``sys.meta_path`` import
hook installed via ``xontrib_tmux.pth`` so ``xonsh.parser_table`` is patched
as soon as it is imported.
"""

import builtins
from importlib.abc import Loader, MetaPathFinder
from importlib.machinery import ModuleSpec
import io
import os
import sys
import tokenize
from types import ModuleType
from typing import Any, Optional, Sequence


_FSTRING_TOKENS = ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END")
_NODEDENT_LHS = ("nodedent", "any_dedent_tok", "any_dedent_toks")


def _is_autoload_disabled() -> bool:
    """Returns True if xontrib autoloading is disabled for ``tmux``."""
    xsh = getattr(builtins, "__xonsh__", None)
    env = getattr(xsh, "env", None)
    if env is not None and "XONTRIBS_AUTOLOAD_DISABLED" in env:
        disabled = env.get("XONTRIBS_AUTOLOAD_DISABLED")
        if disabled is True:
            return True
        if isinstance(disabled, (list, tuple, set, frozenset)) and "tmux" in disabled:
            return True
    raw = os.environ.get("XONTRIBS_AUTOLOAD_DISABLED", "")
    if raw.lower() in ("1", "true", "yes"):
        return True
    if "tmux" in [item.strip() for item in raw.split(",")]:
        return True
    return False


def _find_unterminated_fstring(src: str) -> Optional[str]:
    """Returns an error message if ``src`` contains an unterminated f-string."""
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
    """Patches ``xonsh.parsers.base.BaseParser`` for regenerated tables and f-string checks."""
    base_mod = sys.modules.get("xonsh.parsers.base")
    if base_mod is None:
        try:
            import xonsh.parsers.base as base_mod  # type: ignore
        except ImportError:
            return
    base_parser = getattr(base_mod, "BaseParser", None)
    if base_parser is None or getattr(base_parser, "_xontrib_tmux_patched", False):
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
            prod.func = p_rawsuite_indent.__get__(parser_obj, type(parser_obj))


def patch_parser_table(parser_table: Any) -> None:
    """Adds FSTRING token transitions to ``nodedent`` states in ``parser_table``."""
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
    """Removes any installed ``_ParserTableImportHook`` from ``sys.meta_path``."""
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
    """Patches ``xonsh.parser_table`` once when it is imported, then removes itself."""

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
                        def create_module(self, s: ModuleSpec) -> Optional[ModuleType]:
                            create = getattr(orig_loader, "create_module", None)
                            return create(s) if create is not None else None

                        def exec_module(self, module: ModuleType) -> None:
                            spec.loader = orig_loader
                            orig_loader.exec_module(module)
                            _remove_hook()
                            if not _is_autoload_disabled():
                                patch_parser_table(module)
                                _patch_base_parser()

                        def __getattr__(self, name: str) -> Any:
                            return getattr(orig_loader, name)

                    spec.loader = _WrappedLoader()
                    return spec
            return None
        finally:
            self._in_progress = False


_hook_instance: Optional[_ParserTableImportHook] = None


def install_parser_patch_hook() -> None:
    """Installs the ``sys.meta_path`` hook or patches ``xonsh.parser_table`` if already loaded."""
    global _hook_instance
    if _is_autoload_disabled():
        return
    pt = sys.modules.get("xonsh.parser_table")
    if pt is not None:
        patch_parser_table(pt)
        _patch_base_parser()
        return
    if _hook_instance is None:
        _hook_instance = _ParserTableImportHook()
        sys.meta_path.insert(0, _hook_instance)
