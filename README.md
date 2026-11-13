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
