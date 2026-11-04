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

from importlib.abc import Loader, MetaPathFinder
from importlib.machinery import ModuleSpec
import sys
from types import ModuleType
from typing import Any, Optional, Sequence


_FSTRING_TOKENS = ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END")
_NODEDENT_LHS = ("nodedent", "any_dedent_tok", "any_dedent_toks")


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


def patch_xonsh_parser() -> None:
    """Patches ``xonsh.parser_table`` if loaded or imports and patches it."""
    pt = sys.modules.get("xonsh.parser_table")
    if pt is None:
        try:
            import xonsh.parser_table as pt  # type: ignore
        except ImportError:
            return
    patch_parser_table(pt)


class _ParserTableImportHook(MetaPathFinder, Loader):
    """Patches ``xonsh.parser_table`` immediately when it is imported."""

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
                            orig_loader.exec_module(module)
                            patch_parser_table(module)

                    spec.loader = _WrappedLoader()
                    return spec
            return None
        finally:
            self._in_progress = False


_hook_installed = False


def install_parser_patch_hook() -> None:
    """Installs the ``sys.meta_path`` hook and patches ``xonsh.parser_table`` if already loaded."""
    global _hook_installed
    pt = sys.modules.get("xonsh.parser_table")
    if pt is not None:
        patch_parser_table(pt)
    if not _hook_installed:
        sys.meta_path.insert(0, _ParserTableImportHook())
        _hook_installed = True
