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

"""Monkeypatches ``xonsh`` and ``send()`` onto libtmux objects."""

from collections.abc import Sequence
import inspect
from typing import Any

import libtmux  # type: ignore

from .attach import attach
from .commands import get_pane_xonsh
from .pane_text import pane_text
from .parser_patch import install_parser_patch_hook, patch_xonsh_parser
from .peer import PeerMacro
from .protocol import capture_vars
from .result import PeerResult


install_parser_patch_hook()
patch_xonsh_parser()


def _send(
    self: Any,
    block: str | None = None,
    /,
    capture: Sequence[str] | None = None,
    record: bool = True,
) -> PeerResult | PeerMacro:
    """Sends a xonsh block to the target or active pane's cooperating peer."""
    pane = self if isinstance(self, libtmux.Pane) else self.active_pane
    peer = pane.xonsh if pane is not None else None
    if peer is None:
        raise RuntimeError("Target pane has no peer xonsh")

    captured: dict[str, str] | None = None
    if capture is not None:
        frame = inspect.currentframe()
        caller_frame = frame.f_back if frame is not None else None
        del frame
        try:
            captured = capture_vars(caller_frame, capture)
        finally:
            caller_frame = None
    return peer._send_from_caller(block, captured=captured, record=record)


libtmux.Pane.xonsh = property(get_pane_xonsh)
libtmux.Pane.text = pane_text
libtmux.Session.send = _send
libtmux.Window.send = _send
libtmux.Pane.send = _send

libtmux.Session.attach = attach
_attach = attach
