# xontrib-tmux

[xontrib](https://xon.sh/xontrib.html) for the [xonsh shell](https://xon.sh/)
which lets you create, arrange, and populate tmux sessions and drive background
xonsh peers right from your shell or a script.

Example GitHub Issue dashboard:

```xonsh
@aliases.register
def _triage(args):
    issue = args[0]
    name = f"gh-{issue}"
    fresh = not libtmux.Server().has_session(name)
    work = xsess(name)
    if fresh:
        with! work.send(capture=["issue"], record=False):
            gh issue view @(issue)
            with! xpane().send(capture=["issue"], record=False):
                claude -p @(f"Diagnose GitHub issue {issue}")
    work.attach()
```

## Architecture

`xontrib-tmux` is organized into two layers:

### 1. Peer layer (`RemoteXonsh`)

`RemoteXonsh` spawns a background interactive xonsh peer on a hidden
pseudo-terminal (`pty.openpty()`) and communicates with it over a local Unix
socket (`peer_server.py`), with **no dependency on `tmux`** (it works even when
`tmux` is not installed or running).

- `peer = RemoteXonsh()` starts an isolated interactive peer; it can be used
  as a context manager (`with RemoteXonsh() as peer: ...`) and also terminates
  the child process on `peer.kill()`, when garbage-collected after any
  in-flight blocks finish, or at interpreter exit.
- `peer.send(block=None, /, capture=None, record=True)`:
  - Call with a source string (`peer.send("x = 1\necho @(x)")`) to run a block
    and return a `PeerResult`.
  - Use as a xonsh macro (`with! peer.send(...) as r:` or `with! peer.send():`)
    to capture the block body without running it in the caller, returning a
    `PeerResult` bound to `as` when provided.
- `PeerResult`:
  - `rtn`: return code of the last executed statement (or first failing command).
  - `out`, `err`, `lines`: captured stdout/stderr strings and output lines.
  - `commands`: list of `CommandRecord` entries (`args`, `rtn`, `out`, `err`,
    where `args` is `list[str]`).
  - `future`: `concurrent.futures.Future[PeerResult]`.
  - `end()`: waits for completion and returns `self` (raising any transport
    or peer runtime error; unread exceptions are warned/printed cleanly when the
    result is discarded).
  - `bool(result)` is `True` when `rtn == 0` and no exception occurred.
- `peer.env`, `peer.ctx`, `peer.lastcmd`, and `peer.kill()`: inspect or mutate
  the peer's environment variables, Python namespace, and last command, or
  terminate the peer explicitly.

Blocks sent to a peer — even from multiple concurrent sender processes — run
one at a time as whole entries in arrival order, with each sender's own
submission order preserved and `env`/`ctx` operations serialized strictly
between blocks.

### 2. tmux layer (`xsess`, `xwin`, `xpane`, `Pane.text`, `Session.attach`)

- `xsess(name=None, **kwargs) -> libtmux.Session`: attaches to an existing
  session with that name only if its active pane holds a `RemoteXonsh` peer
  (otherwise raises), or creates a new session running a peer bound to your
  calling xonsh interpreter (`sys.executable`).
- `xwin(name=None, session=None, **kwargs) -> libtmux.Window`: creates a new
  window running a `RemoteXonsh` peer in `session` (default: current tmux
  session).
- `xpane(pane=None, **kwargs) -> libtmux.Pane`: splits the caller's own tmux
  pane (or `pane` if passed as positional argument, forwarding keyword options
  to `pane.split(**kwargs)`) into a pane running a `RemoteXonsh` peer.
- `pane.xonsh`: property returning the `RemoteXonsh | None` peer attached to a
  pane (`None` if the pane has no peer or its peer has exited, cleaning up any
  stale socket file). `pane.send(...)`, `window.send(...)`, `session.send(...)`,
  and `xsess(name)` (when an existing session's active pane holds no peer) raise
  `RuntimeError`. Use `Pane.send_keys()` to type into non-peer panes.
- `session.send(...)`, `window.send(...)`, `pane.send(...)`: delegate to the
  active/target pane's `RemoteXonsh.send(...)` (supporting both string form and
  `with! ... .send(...)` macro blocks).
- `pane.text(scrollback=False) -> str`: captures rendered pane text (or
  `scrollback=True` for full scrollback, or `scrollback=N` for the last `N`
  lines of history).
- `session.attach(...)`: attaches from a bare terminal using tmux `attach-session`
  or calls `switch-client` automatically when already inside tmux (`exit_` and
  `flags_` raise `ValueError` inside tmux).

## Scoped captures (`capture=`)

Both `peer.send("...", capture=...)` and `with! ... .send(capture=...):` carry
captured variables with by-value closure semantics:

- Before the block executes, the peer pushes any existing `ctx` bindings for
  the captured names onto a restore stack and installs the captured values.
- When the block finishes — whether normally, via a failing command, via a
  Python exception, or via interrupt — prior `ctx` bindings are restored (and
  names that did not exist before are deleted).
- Rebinding a captured variable inside the block updates only that block's
  copy and is discarded when the block ends.
- Assigning to any **non-captured** name persists in the peer (`send("x = 2")`
  sets `x` for good). To persist a captured value intentionally, assign it to
  another name inside the block or set `peer.ctx["name"] = value` /
  `pane.xonsh.ctx["name"] = value`.
- Functions defined inside a block look up free names in `ctx` dynamically when
  called (after the block's temporary bindings have been popped, unless closed
  over or rebound separately).

### Target shell visibility and secrets warning

When `capture=` is non-empty, the entry submitted to the peer is prefixed with a
comment line summarizing the captured names and repr/summaries, for example:

```python
# captured: x=42, issue='gh-17', cfg=<dict, 12 items>
```

This comment is visible in the peer's tmux pane and recorded in
`__xonsh__.history` (with no effect on command execution, `rtn`, `commands`,
`out`, or `err`).

When a captured block exits — whether normally, via a failing command, via an
exception, or via interrupt — the peer writes one terminal comment line after
command output closes and before drawing the next prompt:

```python
# restored: x='before'; unset: issue, cfg
```

Prior `ctx` bindings appear under `restored:` (using the same short safe
`repr` or `<type, N items>` summaries) and newly introduced captured names
appear under `unset:`, joined by `"; "` with any empty half omitted (for
example, `# restored: x='before'` or `# unset: issue`). Nothing is emitted for capture-free blocks.
This line is terminal output only: it does not appear in `result.out`,
`result.err`, or `result.commands`, does not affect `rtn`, is not recorded in
shell history, and is discarded with terminal output on hidden peers.

**No secrecy is promised**: anyone viewing the tmux pane or reading history can
see captured and restored names and values. **Do not capture secrets** (tokens,
passwords, private keys); pass those via `$ENV` or files instead.

## `$TMUX` server resolution

When `$TMUX` is set in the environment, `xsess()` targets that tmux server. If
the `$TMUX` socket is dead, missing, or invalid, `xsess()` raises
`RuntimeError` naming the bad `$TMUX` value and recommending clearing `TMUX`
(rather than silently falling back). It falls back to the default tmux server
only when `$TMUX` is unset.

## Window names

By default (`$XONTRIB_TMUX_RENAME_WINDOW = True`), an interactive xonsh shell
running inside a tmux pane renames its window to `xonsh` on load and after
each command, and to the first word of the command while running. Set
`$XONTRIB_TMUX_RENAME_WINDOW = False` (or `"0"` / `"false"`) to leave window
names untouched — either in your `.xonshrc` (works with both autoload and
`xontrib load tmux`) or at runtime from the prompt:

```xonsh
$XONTRIB_TMUX_RENAME_WINDOW = False
@events.on_chdir
def _win_cwd(olddir, newdir, **kw): tmux rename-window @(newdir.rsplit("/", 1)[-1] or "/")
```

## Known issues

- **Exit status `0` reported for a failing subprocess when recording is on**:
  With `record=True` (the default for sent blocks, which enables
  `$XONSH_CAPTURE_ALWAYS`), xonsh 0.24 can occasionally report exit status `0`
  for a subprocess command that actually exited non-zero (measured at ~3 per
  1,000 commands under load). This is an upstream race in xonsh's threaded
  subprocess handling (`PopenThread` / `proc_untraced_waitpid` racing with
  `subprocess.Popen`'s `_waitpid_lock` and hitting `ECHILD`), not specific to
  this plugin. Passing `record=False` leaves `$XONSH_CAPTURE_ALWAYS` disabled
  and avoids xonsh's threaded subprocess path (measured `0/1,000` failures
  with `record=False`, vs. `3/1,000` with `$XONSH_CAPTURE_ALWAYS` enabled), at
  the cost of not recording `out`/`err`/per-command output.

## Changes for existing users

| Before | After |
| --- | --- |
| `tmuxs(name)`, `tmuxw(name)`, `tmuxp()` | `xsess(name)`, `xwin(name)`, `xpane()`; they still return a libtmux `Session`, `Window`, and `Pane` |
| `tmuxp()` splits the window's active pane and forwards positional arguments to `split()` | `xpane()` splits the caller's own pane; its positional argument is the pane to split, and options are keywords |
| New panes run the first `xonsh` on `$PATH` | They run the calling shell's interpreter (`sys.executable`) |
| `tmuxs(name)` returns an existing session whatever it runs; `tmuxs(name, existing=False)` raises on an existing name | `xsess(name)` returns an existing session only if its active pane holds a peer, and raises otherwise |
| `send()` types into whatever the target pane runs, including a xonsh the plugin did not start | `send()` reaches only a peer started by `xsess()`, `xwin()`, `xpane()`, or `RemoteXonsh()`, and raises where there is none; `Pane.send_keys()` types into other panes |
| A block reaches the other shell by being typed into its pane | It is handed to the peer over a local socket, and the peer submits it to itself |
| `send()` returns a future that completes when the keys have been typed | `send()` returns a `PeerResult` that is filled in when the block has finished |
| `with! x.send() as m` binds an object whose `future` completes on delivery | It binds the `PeerResult` |
| Each statement of a block is its own entry: later statements run after a failed one, and each is a history entry and a window-name change | A block is one entry: a failing command stops the rest, and there is one history entry and one window-name change |
| Commands in a sent block run as if typed | They are recorded unless sent with `record=False`, which avoids pty/tty capture side effects for interactive TUIs |
| A block sent to a busy shell is typed at once and read at its next prompt | It waits in the peer until the peer's prompt is idle |
| With `capture=`, a typed line loads the variables in the other shell; the string form of `send()` ignores `capture=` | The variables travel with the block in both forms, are scoped to the block like a by-value closure (saving and restoring previous `ctx` bindings), are logged in a leading `# captured: ...` comment line visible in the pane and history, and print a `# restored: ...; unset: ...` terminal line when restored |
| When `$TMUX` is set to a dead or invalid server, server lookups fall back to the default server | `xsess()` raises `RuntimeError` naming the bad `$TMUX` and recommending clearing `TMUX`; it falls back to the default server only when `$TMUX` is unset |
| When `$XONTRIB_TMUX_READY_TIMEOUT` passes, the block is typed anyway | The block fails with `RuntimeError` |
| Delivery failures are printed on the sender's standard error | They are raised by the `PeerResult`, and printed only when nobody reads the result |
| `Session.attach()` inside tmux fails | It switches the client; `exit_` and `flags_` raise `ValueError` there |

## Installation

Requires Python >= 3.11 and `xonsh >= 0.24.0`.

```xsh
xpip install xontrib-tmux
```

## Usage

This xontrib will get loaded automatically for interactive sessions.
To stop this, set:

```xsh
$XONTRIBS_AUTOLOAD_DISABLED = ["tmux", ]
# if you have set this for other xontribs, you should append the value
```

## Development

- Activate [pre-commit](https://github.com/pre-commit/pre-commit) hooks:
```sh
# install pre-commit plugins and activate the commit hook
pre-commit install
pre-commit autoupdate
```

## Releasing your package

- Bump the version of your package.
- Create a GitHub release (The release notes are automatically generated as a draft release after each push).
- And publish with `poetry publish --build`, `uv publish`, or `twine`.

## Credits

This package was created with [xontrib template](https://github.com/xonsh/xontrib-template).

## Disclaimer

This is not an officially supported Google product.

Eligibility for the [Google Open Source Software Vulnerability Rewards Program](https://bughunters.google.com/open-source-security) is determined by the [Google Open Source Software Vulnerability Reward Program Rules](https://bughunters.google.com/about/rules/open-source/google-open-source-software-vulnerability-reward-program-rules).
