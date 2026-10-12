# Switchboard

A small local web app for switching between local model backends (Ollama, llama.cpp, Strata, Maya, ComfyUI and others) one at a time, watching GPU and memory use, and chatting with whatever is running.

Standard library only (Python 3). No packages to install.

## Run

    python3 switchboard.py

Then open http://127.0.0.1:8090.

Options: `--config PATH` (default `backends.json`), `--host`, `--port`.

## Features

- Start, stop and switch backends. Backends in the `gpu` group run one at a time; starting one stops the others.
- Live GPU and system memory gauges and a 3-minute history chart.
- Chat with the running backend. For Ollama, pick any installed model from a dropdown, see which one is loaded (size, context, GPU/CPU split), and unload it to free memory.
- Per-backend logs.

## Configure

Edit `backends.json`. Each entry has an `id`, `name`, `group`, and a `kind` of `systemd`, `docker` or `process`. Entries with a `health` URL are probed to decide whether they are up; entries with an `openai_base` URL can be chatted with. The paths in the included file are examples from the author's machine; change them to match yours.
