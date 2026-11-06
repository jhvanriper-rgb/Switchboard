# Switchboard

A small local web app for switching between local model backends (Ollama, llama.cpp, Strata, Maya, ComfyUI and others) one at a time, watching GPU and memory use, and chatting with whatever is running.

Standard library only (Python 3). No packages to install.

![Switchboard showing backends, GPU gauges, history chart and chat](docs/screenshot.png)

## Run

    python3 switchboard.py

Then open http://127.0.0.1:8090.

Options: `--config PATH` (default `backends.json`), `--host`, `--port`.

## Features

- Start, stop and switch backends. Backends in the `gpu` group run one at a time; starting one stops the others.
- Live GPU and system memory gauges and a 3-minute history chart.
- Chat with the running backend. For Ollama, pick any installed model from a dropdown, see which one is loaded (size, context, GPU/CPU split), and unload it to free memory.
- Per-backend logs.
- An **Updates** button in the header, next to "Stop all models". A badge shows how many backends have an update available. The panel lists each backend with its status, when it was last checked, a per-row **Update** button, **Check again**, and **Update all**. Nothing is ever updated without a click: Switchboard checks at startup (read-only, one backend at a time, in the background) and when you press "Check again", but it only applies an update when you press an Update button.

## Configure

Edit `backends.json`. Each entry has an `id`, `name`, `group`, and a `kind` of `systemd`, `docker` or `process`. Entries with a `health` URL are probed to decide whether they are up; entries with an `openai_base` URL can be chatted with. The paths in the included file are examples from the author's machine; change them to match yours.

### Updates

Add an optional `update` object to any backend in `backends.json`. Backends without one do not appear in the Updates panel. Checks are read-only for the running software (`git fetch` and `docker pull` only download). Each check has a 60 second limit and shows "check timed out" if it hangs.

Small gaps need not raise the badge, since an update can cost a long rebuild or a service restart. A `git` update object may set `"min_commits_behind"` (a whole number of 1 or more, default 1), and a `github_release` object may set `"min_release"` (`"patch"`, `"minor"` or `"major"`, default `"patch"`). Below the threshold the row shows the neutral status "newer version exists" with a detail such as "1 commit behind (below the 50 commit threshold)" or "0.40.14 to 0.40.15 (patch release, below the minor threshold)". Such a row does not count in the badge and is skipped by Update all, but its Update button works, so you can update anyway; the update steps are the same. A pre-release tag such as `0.40.15-rc1` is never offered, and `pip` and `docker` entries have no threshold. The checked-in file uses 50 commits for llama.cpp and Strata, 20 for ComfyUI, and `"minor"` for Ollama.

Any update object may set `"check_timeout": <seconds>`, the most one check (all of its commands together) may take. The default is 60 for `git`, `pip` and `github_release`, and 1800 for `docker`. It must be a finite number above 0 and is capped at 86400; anything else shows a settings error in the row. A check that runs out reports "error: check timed out after N seconds. Raise check_timeout in backends.json."

Kinds:

- `{"kind": "git", "path": "/dir", "branch": "optional"}`. Check: `git fetch`, then count commits behind the upstream. A folder that is not a git checkout shows "no update source". Optional keys: `python` (a venv's python; if the repo has a `requirements.txt` it is installed with it), `post_command` (runs after a successful pull, in `post_cwd` or the repo; `post_shell: true` for pipes), `verify_command` (must exit 0 after the update).
- `{"kind": "git_tags", "path": "/dir"}`. For a checkout that follows release tags instead of a branch (for example ComfyUI left on `v0.39.2`, where the `git` kind finds no upstream). Check: `git fetch --tags`, then the newest stable tag shaped exactly `vMAJOR.MINOR.PATCH` (tags with a suffix such as `-rc1`, and tags that are not versions, are ignored) is compared with the checked-out version: the version tag exactly at HEAD, or else the nearest one behind HEAD (the detail then says HEAD is not exactly on a release tag). It reports up to date, update available (from and to versions), or unknown with a plain reason (no release tags, or HEAD is not on or after one). `min_release` (`patch`, `minor` or `major`, default `patch`) works as for `github_release`, so small gaps show "newer version exists" and do not raise the badge. Optional keys: `remote` (a remote name for the fetch; git's default otherwise), `python` (a venv's python; if the checkout has a `requirements.txt` it is installed with it), `post_command` / `post_cwd` / `post_shell` (your own install command, run after the checkout), and `verify_command`. Update: the steps are **pull** (fetch the tags and `git checkout --detach` the tag chosen by the check), **install** (requirements and `post_command`, if configured) and **verify** (HEAD is exactly on that tag, then `verify_command`). It refuses while the working tree has local changes and while HEAD is a commit that is on no branch or tag, since moving would leave it behind; it never runs `git reset --hard` or `git clean`. A failed checkout changes nothing. If the install fails after the checkout, the row reports an error that says the new release is checked out, what was checked out before, and that its dependencies may be only partly installed; the row can then be updated again once you fix the cause.
- `{"kind": "pip", "python": "/path/to/python", "package": "name"}`. Check: `pip list --outdated` (falls back to `pip index versions`). The checked-in file uses `"python": "python3"` for LiteLLM, but the system Python on Debian and Ubuntu refuses `pip install` (PEP 668), so set `"python"` to the virtualenv's python (for example `/home/james/litellm/venv/bin/python`) before you try to update it.
- `{"kind": "github_release", "repo": "owner/name", "version_command": "ollama --version", "apply_command": "...", "shell": false}`. Check: the first version number in the command output against the latest release tag on GitHub. Entries with `use_pkexec` run the apply command through `pkexec`. The checked-in Ollama entry looks like this:

      "update": {"kind": "github_release", "repo": "ollama/ollama", "version_command": "ollama --version",
                 "apply_command": "curl -fsSL https://ollama.com/install.sh | sh", "shell": true,
                 "update_while_running": true}
- `{"kind": "docker", "image": "repo/name:tag", "apply_command": "..."}`. Check: `docker pull` (the running container is untouched), then compare image ids. `apply_command` is the command you supply to recreate the container; without it the Update button stays disabled and says "No recreate command set in backends.json." `docker pull` downloads the whole image, so the checked-in Open WebUI entry sets `"check_at_startup": false`: it is checked only when you press Check again. While the pull runs the row shows "checking" with "Downloading the image: this can take several minutes." and the other rows keep working.

Optional `"gpu_check": {"unit": "ollama", "wait": 60}` (the checked-in Ollama entry sets `{"unit": "ollama"}`; `wait` defaults to 60 seconds and is capped at 600, `name` is the label used in messages and defaults to the unit name) adds a GPU check to the verify step of any systemd-backed entry. Switchboard notes the time just before the install step, because the installer restarts the service. After the normal version check passes it first checks `systemctl is-active <unit>` (if the service is not running, verify fails with "service not running after the update"), then reads `journalctl -u <unit> --since @<epoch seconds> --no-pager` (no sudo, 10 second limit per call), polling until one `wait` second deadline, and counts the distinct lines containing "inference compute", one per usable GPU. It compares that with the GPUs `nvidia-smi -L` lists. If the service sees fewer, verify fails with, for example, "Ollama sees 1 of 2 GPUs after the update. The old version may need to be reinstalled.", and the output names the GPUs it found plus any "skipping CUDA device" lines. If `nvidia-smi` is missing, the comparison is skipped but at least one GPU line is still required. If the journal cannot be read (journalctl missing, or your user lacks permission, for example not in the `systemd-journal` or `adm` group), verify fails with "could not read the service journal" (plus "add your user to the systemd-journal group" when journalctl says it cannot show other users' messages); it never counts as a pass.

Output is live: while an update runs, the row's Output section opens by itself and shows the last lines of what the commands print, with a line such as "== install started ==" at the start of every step. It follows the newest line unless you scroll up, and it stays open while the panel refreshes (every 1.5 seconds during an update). Switchboard keeps the last 200 lines per backend in memory (long lines are cut, control characters and terminal color codes are removed, and the text is shown as plain text), and the API returns the last 60 as `output_tail`, during the update and after it, until the next update of that backend starts.

Each update is a pipeline of steps run in order, stopping at the first failure: **pull** (get the new version), **install** (make it usable), **verify** (prove it works). The panel shows progress live ("pull ok · install running"). Defaults per kind:

- git: `git pull --ff-only` (a failed pull leaves the repo unchanged), install requirements and `post_command` if set, then verify that HEAD equals the upstream (plus `verify_command` if set).
- pip: `pip install --upgrade`, then `pip show` must report at least the newest version.
- github_release: `apply_command`, then `version_command` must report at least the release version.
- docker: `docker pull`, `apply_command`, then the image id must match the downloaded image and the container (if it exists) must report it.

To override the defaults, give the update object `"steps": [{"name": "pull|install|verify", "command": "...", "cwd": "...", "shell": false, "timeout": 900, "pkexec": false}]`, or `"script": "/path/to/script.sh"` as shorthand for one install step. A verify step is always added at the end if your list does not end with one, so an update is never reported as done without a passing verify. A failed step marks the row "error" with the step name and the last 40 lines of output; if the failure came after the pull, the row can be run again once you fix the cause. Full output goes to the backend's log (for process backends).

Rules: requests never carry commands, only `backends.json` does. Only one update runs at a time. A running backend must be stopped first ("Stop X first, then update it."), unless its update object sets `"update_while_running": true`; only Ollama does, because its installer restarts the service itself. If Switchboard cannot tell whether a backend is running, it assumes it is. An update and a check never run at the same time. An update is refused if a git working tree has local changes.

Security: Switchboard only answers requests whose `Host` is `127.0.0.1`, `localhost` or `[::1]` (or the `--host` value) with its own port, refuses a foreign `Origin`, and requires the header `X-Switchboard: 1` on every POST. The page sends it for you; if you script the API with curl, add `-H 'X-Switchboard: 1'`. Binding to `--host 0.0.0.0` therefore only works when you browse to `0.0.0.0:<port>` itself.

API: `GET /api/updates`, `POST /api/updates/check`, `POST /api/updates/apply/<id>`.

## License

MIT. See [LICENSE](LICENSE).
