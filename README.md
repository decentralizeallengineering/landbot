# landbot

[![tests](https://github.com/decentralizeallengineering/landbot/actions/workflows/tests.yml/badge.svg)](https://github.com/decentralizeallengineering/landbot/actions/workflows/tests.yml) ![license](https://img.shields.io/badge/license-Apache--2.0-blue)

A local merge queue for code written by AI agents. It needs no CI service. Python 3.10+, standard library only, plus git.

Agents push branches named `ready/<name>`. `landbot run` merges them onto main in a temporary worktree, runs your gate command once on the whole batch, and lands the batch if it is green. If it is red, landbot halves the batch and re-gates until the bad refs are found. Landed refs are renamed `landed/<name>`. Bad refs are renamed `rejected/<name>` and their gate output is saved to `rejects/<name>.log`. Branches are taken oldest first, so a single bad branch cannot block the others.

## Install

    pip install .

or run `python3 landbot.py` directly.

## Configure

Copy `landbot.toml.example` to `landbot.toml` in the repo root. Keys: `gate` (command, default `pytest -q`), `main` (default `main`), `batch_size` (default 8), `timeout` (seconds, default 600). Add `.pause` and `rejects/` to the repo's `.gitignore`.

- `touch .pause` stops runs (exit code 3); delete it to resume.
- A lock file in the git directory prevents two runs at once (exit code 4). A lock left by a dead process is taken over.
- Landing moves main to the gated batch commit with a fast-forward. If main is checked out in the repo, the working tree is updated too.

## Example

    git push origin my-branch:ready/fix-parser   # or: git branch ready/fix-parser
    landbot run --repo /srv/agent-repo
    landbot status --repo /srv/agent-repo

The `systemd/` directory has a oneshot service, a path unit that fires when a `ready/*` branch appears, and a five-minute timer. Edit the paths, then copy them to `/etc/systemd/system/` and enable the path or timer unit. The path unit only sees loose refs; run `git config gc.packRefs false` if the repo packs refs, or use the timer.

## Tests

    python3 -m pytest -q
