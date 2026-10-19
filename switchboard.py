#!/usr/bin/env python3
"""Switchboard: start/stop local model backends one at a time, watch the GPUs, chat.

Standard library only. Run:  python3 switchboard.py   then open http://127.0.0.1:8090
"""
import argparse
import json
import os
import shlex
import signal
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG_DIR = Path.home() / ".switchboard" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

lock = threading.RLock()
CONFIG = {}
BACKENDS = {}
STATE = {}  # id -> dict(proc, phase, since, error)
HISTORY = deque(maxlen=150)  # one sample per 2 s


# ---------- config ----------
def load_config(path):
    global CONFIG, BACKENDS
    CONFIG = json.loads(Path(path).read_text())
    BACKENDS = {b["id"]: b for b in CONFIG["backends"]}
    for bid in BACKENDS:
        STATE.setdefault(bid, {"proc": None, "phase": "stopped", "since": None, "error": None})


def configured(b):
    return not (b["kind"] == "process" and "TODO" in b.get("command", "TODO"))


def requires_of(b):
    """Ids of the backends this one cannot work without (optional "requires" list)."""
    r = b.get("requires") or []
    return [r] if isinstance(r, str) else [x for x in r if x in BACKENDS]


def dependents_of(bid):
    """Ids of the backends that need bid, directly or through another backend."""
    out, todo = [], [bid]
    while todo:
        cur = todo.pop()
        for oid, ob in BACKENDS.items():
            if cur in requires_of(ob) and oid not in out and oid != bid:
                out.append(oid)
                todo.append(oid)
    return out


# ---------- probing ----------
def http_ok(url, timeout=1.5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500
    except Exception:
        return False


def run(cmd, timeout=15):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout + r.stderr).strip()
    except Exception as e:
        return 1, str(e)


def sudo(b, cmd):
    if b.get("use_pkexec"):
        return ["pkexec"] + cmd  # graphical password prompt, like the old tray app
    return (["sudo", "-n"] + cmd) if b.get("use_sudo") else cmd


def is_active(b):
    """Is this backend actually up right now (whoever started it)?"""
    kind = b["kind"]
    if kind == "systemd":
        rc, out = run(["systemctl", "is-active", b["unit"]])
        return out.strip() == "active"
    if kind == "docker":
        rc, out = run(["docker", "inspect", "-f", "{{.State.Running}}", b["container"]])
        return rc == 0 and out.strip() == "true"
    st = STATE[b["id"]]
    p = st["proc"]
    if p is not None and p.poll() is None:
        return True
    return False


def ready(b):
    return http_ok(b["health"]) if b.get("health") else is_active(b)


def snapshot(bid):
    """Current view of one backend, reconciling manager state with reality."""
    b = BACKENDS[bid]
    with lock:
        st = STATE[bid]
        phase = st["phase"]
        active = is_active(b)
        up = ready(b)
        external = False
        if phase in ("starting", "stopping"):
            pass
        elif active and up:
            phase = "running"
        elif up and not active:
            phase, external = "running", True  # something else is serving this port
        elif active and not up:
            phase = "starting" if phase == "starting" else "unresponsive"
        else:
            if phase in ("running", "unresponsive"):
                phase = "error" if st["proc"] is not None and st["proc"].returncode not in (0, None, -15) else "stopped"
                if phase == "error" and not st["error"]:
                    st["error"] = f"process exited with code {st['proc'].returncode}; see logs"
            elif phase != "error":
                phase = "stopped"
        st["phase"] = phase
        return {
            "id": bid,
            "name": b["name"],
            "subtitle": b.get("subtitle", ""),
            "group": b.get("group", "gpu"),
            "kind": b["kind"],
            "port": b.get("port"),
            "phase": phase,
            "external": external,
            "configured": configured(b),
            "since": st["since"],
            "error": st["error"],
            "chat": bool(b.get("openai_base")),
            "open_url": b.get("open_url"),
            "slow_start": bool(b.get("slow_start")),
            "requires": [BACKENDS[r]["name"] for r in requires_of(b)],
        }


# ---------- start / stop ----------
def _set(bid, **kw):
    with lock:
        STATE[bid].update(kw)


def _stop_blocking(bid):
    b = BACKENDS[bid]
    kind = b["kind"]
    _set(bid, phase="stopping", error=None)
    try:
        if kind == "systemd":
            rc, out = run(sudo(b, ["systemctl", "stop", b["unit"]]), 60)
            if rc != 0:
                raise RuntimeError(f"systemctl stop failed: {out[:200]} (passwordless sudo for systemctl needed)")
        elif kind == "docker":
            rc, out = run(["docker", "stop", b["container"]], 60)
            if rc != 0:
                raise RuntimeError(f"docker stop failed: {out[:200]}")
        else:
            p = STATE[bid]["proc"]
            if p is not None and p.poll() is None:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGTERM)
                except ProcessLookupError:
                    pass
                for _ in range(60):  # up to 30 s for a clean exit
                    if p.poll() is not None:
                        break
                    time.sleep(0.5)
                if p.poll() is None:
                    try:
                        os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    p.wait(timeout=10)
            elif http_ok(b.get("health", "")) if b.get("health") else False:
                raise RuntimeError("serving, but not started by Switchboard; stop it yourself")
        _set(bid, phase="stopped", proc=None, since=None)
    except Exception as e:
        _set(bid, phase="error", error=str(e))
        raise


def _start_blocking(bid):
    b = BACKENDS[bid]
    kind = b["kind"]
    _set(bid, phase="starting", error=None, since=time.time())
    if kind == "systemd":
        rc, out = run(sudo(b, ["systemctl", "start", b["unit"]]), 60)
        if rc != 0:
            raise RuntimeError(f"systemctl start failed: {out[:200]}")
    elif kind == "docker":
        rc, out = run(["docker", "start", b["container"]], 60)
        if rc != 0:
            raise RuntimeError(f"docker start failed: {out[:200]}")
    else:
        logf = open(LOG_DIR / f"{bid}.log", "ab", buffering=0)
        logf.write(f"\n=== start {time.ctime()} ===\n".encode())
        proc = subprocess.Popen(
            b["command"] if b.get("shell") else shlex.split(b["command"]),
            shell=bool(b.get("shell")),
            cwd=os.path.expanduser(b.get("cwd", "~")),
            stdout=logf, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            start_new_session=True,
            env={**os.environ, **b.get("env", {})},
        )
        _set(bid, proc=proc)
    deadline = time.time() + (3600 if b.get("slow_start") else 300)
    while time.time() < deadline:
        if kind == "process":
            p = STATE[bid]["proc"]
            if p.poll() is not None:
                raise RuntimeError(f"exited during startup (code {p.returncode}); see logs")
        if ready(b):
            _set(bid, phase="running")
            return
        time.sleep(1.5)
    raise RuntimeError("did not become ready in time; see logs")


def stop_with_dependents(bid):
    """Stop everything that needs bid first (it would only crash-loop without it), then bid."""
    for did in dependents_of(bid):
        d = BACKENDS[did]
        if is_active(d) or STATE[did]["phase"] in ("starting", "running"):
            try:
                _stop_blocking(did)
            except Exception:
                pass  # keep going; the error shows on that backend
    _stop_blocking(bid)


def switch_to(bid, _seen=()):
    """Start any backends bid requires, stop everything else in its exclusive group, then start bid."""
    b = BACKENDS[bid]
    try:
        for rid in requires_of(b):
            if rid in _seen or rid == bid:
                raise RuntimeError("backends require each other in a loop; check backends.json")
            rb = BACKENDS[rid]
            if not configured(rb):
                raise RuntimeError(f"{rb['name']} is needed first but is not configured")
            if not ready(rb):
                _set(rid, phase="starting", error=None, since=time.time())
                switch_to(rid, _seen + (bid,))
                if STATE[rid]["phase"] == "error" or not ready(rb):
                    raise RuntimeError(f"{rb['name']} is needed first and did not start: {STATE[rid]['error'] or 'see logs'}")
        if b.get("group", "gpu") == "gpu":
            keep = set(_seen) | set(requires_of(b))
            for oid, ob in BACKENDS.items():
                if oid != bid and oid not in keep and ob.get("group", "gpu") == "gpu":
                    if is_active(ob) or STATE[oid]["phase"] in ("starting", "running"):
                        stop_with_dependents(oid)
                    elif ob.get("health") and http_ok(ob["health"]):
                        raise RuntimeError(f"{ob['name']} is running outside Switchboard; stop it first")
            time.sleep(2)  # let VRAM drain
        _start_blocking(bid)
    except Exception as e:
        _set(bid, phase="error", error=str(e))


def bg(fn, *a):
    t = threading.Thread(target=fn, args=a, daemon=True)
    t.start()


def api_start(bid):
    b = BACKENDS.get(bid)
    if not b:
        return 404, {"error": "unknown backend"}
    if not configured(b):
        return 400, {"error": "not configured: edit backends.json"}
    with lock:
        if STATE[bid]["phase"] in ("starting", "stopping"):
            return 409, {"error": "busy"}
        if b.get("group", "gpu") == "gpu" and any(
                STATE[o]["phase"] in ("starting", "stopping")
                for o in BACKENDS if o != bid and BACKENDS[o].get("group", "gpu") == "gpu"):
            return 409, {"error": "another backend is switching"}
        STATE[bid]["phase"] = "starting"
        STATE[bid]["since"] = time.time()
    bg(switch_to, bid)
    return 202, {"ok": True}


def api_stop(bid):
    if bid not in BACKENDS:
        return 404, {"error": "unknown backend"}
    with lock:
        if STATE[bid]["phase"] == "stopping":
            return 409, {"error": "busy"}
    def job():
        try:
            stop_with_dependents(bid)
        except Exception:
            pass
    bg(job)
    return 202, {"ok": True}


def api_stop_all():
    def job():
        for bid, b in BACKENDS.items():
            if b.get("group", "gpu") == "gpu" and (is_active(b) or STATE[bid]["phase"] == "starting"):
                try:
                    stop_with_dependents(bid)
                except Exception:
                    pass
    bg(job)
    return 202, {"ok": True}


# ---------- stats ----------
def read_gpus():
    rc, out = run(["nvidia-smi",
                   "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit",
                   "--format=csv,noheader,nounits"], 5)
    if rc != 0:
        return []
    gpus = []
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 8:
            continue

        def f(x):
            try:
                return float(x)
            except ValueError:
                return None
        gpus.append({"index": int(parts[0]), "name": parts[1], "util": f(parts[2]),
                     "mem_used": f(parts[3]), "mem_total": f(parts[4]), "temp": f(parts[5]),
                     "power": f(parts[6]), "power_limit": f(parts[7])})
    return gpus


def read_mem():
    info = {}
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, v = line.split(":", 1)
            info[k] = int(v.split()[0]) / 1048576  # GB
    except Exception:
        return None
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", 0)
    return {"total": round(total, 1), "used": round(total - avail, 1), "avail": round(avail, 1)}


def read_cpu_load():
    try:
        return os.getloadavg()[0] / (os.cpu_count() or 1) * 100
    except Exception:
        return None


def sampler():
    while True:
        HISTORY.append({"t": time.time(), "gpus": read_gpus(), "mem": read_mem(), "cpu": read_cpu_load()})
        time.sleep(2)


# ---------- logs ----------
def read_logs(bid, n=200):
    b = BACKENDS.get(bid)
    if not b:
        return "unknown backend"
    if b["kind"] == "systemd":
        rc, out = run(["journalctl", "-u", b["unit"], "-n", str(n), "--no-pager"], 10)
        return out
    if b["kind"] == "docker":
        rc, out = run(["docker", "logs", "--tail", str(n), b["container"]], 10)
        return out
    p = LOG_DIR / f"{bid}.log"
    if not p.exists():
        return "(no log yet)"
    with open(p, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 60000))
        data = f.read().decode("utf-8", "replace")
    return "\n".join(data.splitlines()[-n:])


# ---------- chat proxy ----------
def live_chat_backend():
    for bid, b in BACKENDS.items():
        if b.get("openai_base") and snapshot(bid)["phase"] == "running":
            return b
    return None


# ---------- models: what the running chat backend offers and has loaded ----------
_MODELS_CACHE = {}  # backend id -> (time, payload)


def get_json(url, timeout=3):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


def ollama_root(b):
    """Base URL of Ollama's native API, or None for backends that are not Ollama."""
    if b.get("ollama_api"):
        return b["ollama_api"].rstrip("/")
    if b["id"] == "ollama" and b.get("openai_base"):
        base = b["openai_base"].rstrip("/")
        return base[:-3] if base.endswith("/v1") else base
    return None


def model_info(b):
    """Installed and loaded models for one chat backend (cached for 2 s)."""
    hit = _MODELS_CACHE.get(b["id"])
    if hit and time.time() - hit[0] < 2:
        return hit[1]
    root = ollama_root(b)
    out = {"backend": b["id"], "name": b["name"], "default": b.get("chat_model"),
           "models": [], "loaded": [], "can_switch": bool(root), "can_unload": bool(root)}
    try:
        if root:
            for m in get_json(root + "/api/tags").get("models", []):
                out["models"].append({"id": m["name"], "size": m.get("size")})
            for m in get_json(root + "/api/ps").get("models", []):
                out["loaded"].append({"id": m["name"], "size": m.get("size"), "vram": m.get("size_vram"),
                                      "context": m.get("context_length"), "expires": m.get("expires_at")})
        else:
            # llama.cpp, Strata and Maya serve a single model; just report its name
            ids = [m["id"] for m in get_json(b["openai_base"].rstrip("/") + "/models").get("data", [])]
            out["models"] = [{"id": i} for i in ids]
            out["loaded"] = [{"id": i} for i in ids]
    except Exception as e:
        out["error"] = str(e)
    _MODELS_CACHE[b["id"]] = (time.time(), out)
    return out


def api_models():
    b = live_chat_backend()
    if not b:
        return {"backend": None, "models": [], "loaded": [], "can_switch": False, "can_unload": False}
    return model_info(b)


def api_unload(raw):
    """Free an Ollama model's memory now instead of waiting for its keep-alive timer."""
    b = live_chat_backend()
    root = ollama_root(b) if b else None
    if not root:
        return 409, {"error": "the running backend does not support unloading"}
    try:
        model = json.loads(raw).get("model")
    except Exception:
        model = None
    if not model:
        return 400, {"error": "no model given"}
    up = urllib.request.Request(root + "/api/generate", data=json.dumps({"model": model, "keep_alive": 0}).encode(),
                                headers={"Content-Type": "application/json"})
    try:
        urllib.request.urlopen(up, timeout=30).read()
    except Exception as e:
        return 502, {"error": f"unload failed: {e}"}
    _MODELS_CACHE.pop(b["id"], None)
    return 200, {"ok": True}


# ---------- HTTP ----------
class Handler(BaseHTTPRequestHandler):
    server_version = "Switchboard/1.0"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        q = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        if path in ("/", "/index.html"):
            body = (HERE / "static" / "index.html").read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/state":
            self._json(200, {"backends": [snapshot(b) for b in BACKENDS]})
        elif path == "/api/models":
            self._json(200, api_models())
        elif path == "/api/stats":
            self._json(200, {"history": list(HISTORY)[-int(q.get("n", 90)):]})
        elif path == "/api/logs":
            self._json(200, {"log": read_logs(q.get("id", ""), int(q.get("n", 200)))})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if self.path.startswith("/api/start/"):
            code, obj = api_start(self.path.rsplit("/", 1)[1])
            return self._json(code, obj)
        if self.path.startswith("/api/stop/"):
            code, obj = api_stop(self.path.rsplit("/", 1)[1])
            return self._json(code, obj)
        if self.path == "/api/stop-all":
            code, obj = api_stop_all()
            return self._json(code, obj)
        if self.path == "/api/unload":
            code, obj = api_unload(raw)
            return self._json(code, obj)
        if self.path == "/api/chat":
            return self.chat(raw)
        self._json(404, {"error": "not found"})

    def chat(self, raw):
        b = live_chat_backend()
        if not b:
            return self._json(409, {"error": "no chat-capable model is running"})
        try:
            req = json.loads(raw)
        except Exception:
            return self._json(400, {"error": "bad json"})
        req.setdefault("model", b.get("chat_model", "default"))
        req["stream"] = True
        up = urllib.request.Request(b["openai_base"].rstrip("/") + "/chat/completions",
                                    data=json.dumps(req).encode(),
                                    headers={"Content-Type": "application/json", "Authorization": "Bearer none"})
        try:
            resp = urllib.request.urlopen(up, timeout=3600)
        except urllib.error.HTTPError as e:
            return self._json(502, {"error": f"backend returned {e.code}: {e.read()[:300].decode('utf-8', 'replace')}"})
        except Exception as e:
            return self._json(502, {"error": f"backend unreachable: {e}"})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Backend", b["name"])
        self.end_headers()
        try:
            while True:
                chunk = resp.read1(4096) if hasattr(resp, "read1") else resp.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass  # browser stopped generation
        finally:
            resp.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(HERE / "backends.json"))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int)
    a = ap.parse_args()
    load_config(a.config)
    port = a.port or CONFIG.get("port", 8090)
    threading.Thread(target=sampler, daemon=True).start()
    srv = ThreadingHTTPServer((a.host, port), Handler)
    srv.daemon_threads = True
    print(f"Switchboard on http://{a.host}:{port}  (config: {a.config})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nleaving managed models running; use the UI or `kill` to stop them")


if __name__ == "__main__":
    main()
