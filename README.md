# xontrib-tmux

[xontrib](https://xon.sh/xontrib.html) for the [xonsh shell](https://xon.sh/)
which lets you create, arrange, and populate tmux sessions
right from your shell or a script.

Example GitHub Issue dashboard:
```xonsh
@aliases.register
def _triage(args):
    issue = args[0]
    # Create a tmux session and capture the 'issue' var
    with! tmuxs(f"gh-{issue}").send(capture=['issue']):
        # Open the issue details
        gh issue view @(issue)
        # Open claude in a separate panel
        with! tmuxp().send(capture=['issue']):
            agy -p @(f"Diagnose GitHub issue {issue}")
```

## Installation

```xsh
xpip install xontrib-tmux
```

## Usage

This xontrib will get loaded automatically for interactive sessions.
To stop this, set

```xsh
$XONTRIBS_AUTOLOAD_DISABLED = ["tmux", ]
# if you have set this for other xontribs, you should append the value
```

### Reading pane text

Call `pane.text(scrollback=False) -> str` on any `libtmux.Pane` to capture clean
plain text (wrapped rows joined with `-p -J`, ANSI escape sequences omitted,
per-line trailing whitespace and trailing blank padding rows stripped, with no
trailing newline):

- `pane.text()` or `pane.text(scrollback=0)`: captures only the visible pane.
- `pane.text(scrollback=True)`: captures the full scrollback history plus the
  visible pane.
- `pane.text(scrollback=N)`: captures the last `N` scrollback lines plus the
  visible pane (`N > 0`).
- Raises `ValueError` for negative `scrollback`, `TypeError` for non-`bool`/non-`int`,
  and `libtmux.exc.LibTmuxException` if the pane is closed or `capture-pane` fails.

### Attaching and switching sessions

Call `session.attach(...)` on any `libtmux.Session` to view that session:

- Outside tmux (when this shell's controlling terminal is not `$TMUX_PANE`),
  runs `tmux attach` with server options (`-L`, `-S`, `-f`, `-2`/`-8`) and
  optional `exit_` (`-x`) or `flags_`. Raises `libtmux.exc.LibTmuxException` on
  non-zero exit.
- Already inside tmux (detected by comparing the process's controlling terminal
  against `$TMUX_PANE`'s `pane_tty`), automatically switches an active client on
  the caller's tmux server using `tmux switch-client` (preferring the most
  recently active client on the caller's session). Passing `exit_`, `flags_`, or
  positional args inside tmux raises `ValueError`, and switching to a session
  on a different tmux server raises `xontrib_tmux.attach.DifferentTmuxServerError`.

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

### Background xonsh peers (`RemoteXonsh`)

`RemoteXonsh` spawns a background interactive xonsh peer on a hidden
pseudo-terminal (`pty.openpty()`) and communicates with it over a local Unix
socket (`peer_server.py`), with **no dependency on `tmux`** (it works even when
`tmux` is not installed or running).

- `peer = RemoteXonsh()` starts an isolated interactive peer; it can be used
  as a context manager (`with RemoteXonsh() as peer: ...`) and also terminates
  the child process on `peer.kill()`, when garbage-collected after any
  in-flight blocks finish, or at interpreter exit.
- `peer.send(block=None, /, record=True)`:
  - Call with a source string (`peer.send("x = 1\necho @(x)")`) to run a block
    and return a `PeerResult`.
  - Use as a xonsh macro (`with! peer.send() as r:` or `with! peer.send():`) to
    capture the block body without running it in the caller, returning a
    `PeerResult` bound to `as` when provided.
- `PeerResult`:
  - `rtn`: return code of the last executed statement (or first failing command).
  - `out`, `err`, `lines`: captured stdout/stderr strings and output lines (or
    `None` / `[]` when `record=False`).
  - `commands`: list of `CommandRecord` entries (`args`, `rtn`, `out`, `err`,
    where `args` is `list[str]`).
  - `future`: `concurrent.futures.Future[PeerResult]`. Calling
    `result.future.cancel()` cancels locally queued blocks immediately and asks
    the peer to drop blocks still waiting on its queue or prompt; blocks
    already submitted to the peer's shell return `False`.
  - `end()`: waits for completion and returns `self` (raising any transport
    or peer runtime error; unread exceptions are warned/printed cleanly when the
    result is discarded).
  - `bool(result)` is `True` when `rtn == 0` and no exception occurred.
- `peer.env`, `peer.ctx`, `peer.lastcmd`, and `peer.kill()`: inspect or mutate
  the peer's environment variables, Python namespace, and last command
  (`EnvProxy`, `CtxProxy`, and `LastCmdProxy`, guarding against self-wait
  deadlocks on the peer's own main thread), or terminate the peer explicitly.

Blocks sent to a peer run one at a time as whole entries in arrival order, with
the sender's submission order preserved and `env`/`ctx` operations serialized
strictly between blocks.

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

## Development

- activate [pre-commit](https://github.com/pre-commit/pre-commit) hooks
```sh
# install pre-commit plugins and activate the commit hook
pre-commit install
pre-commit autoupdate
```

## Releasing your package

- Bump the version of your package.
- Create a GitHub release (The release notes are automatically generated as a draft release after each push).
- And publish with `poetry publish --build`, `uv publish`, or `twine`

## Credits

This package was created with [xontrib template](https://github.com/xonsh/xontrib-template).

## Disclaimer

This is not an officially supported Google product.

Eligibility for the [Google Open Source Software Vulnerability Rewards Program](https://bughunters.google.com/open-source-security) is determined by the [Google Open Source Software Vulnerability Reward Program Rules](https://bughunters.google.com/about/rules/open-source/google-open-source-software-vulnerability-reward-program-rules).
