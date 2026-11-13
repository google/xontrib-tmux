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

"""Monkeypatches methods onto libtmux Session, Window, and Pane."""

from typing import Any, List, Optional

import libtmux  # type: ignore

from .attach import attach
from .block import send
from .pane_text import pane_text
from .parser_patch import install_parser_patch_hook, patch_xonsh_parser


install_parser_patch_hook()
patch_xonsh_parser()


def _send(
    self: Any,
    inline_cmds: Optional[str] = None,
    capture: Optional[List[str]] = None,
) -> Any:
    """Sends commands to the active or target tmux pane."""
    pane = self if isinstance(self, libtmux.Pane) else self.active_pane
    return send(pane, inline_cmds, capture=capture)


libtmux.Session.send = _send
libtmux.Window.send = _send
libtmux.Pane.send = _send
libtmux.Pane.text = pane_text

libtmux.Session.attach = attach
_attach = attach
