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
