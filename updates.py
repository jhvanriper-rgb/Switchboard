"""Update checks and one-click updates for Switchboard backends.

Standard library only. Everything that is ever executed comes from backends.json (the
"update" object of an entry), chosen by backend id. Nothing here takes command text from a
request. Checks are read-only with respect to running software (git fetch and docker pull
only download). Updates are applied only when apply() is called for one id.

All command execution goes through run_command() and all web lookups through fetch_json(),
so tests can replace those two functions.
"""
import json
import os
import re
import shlex
import subprocess
import threading
import time
import urllib.request

CHECK_TIMEOUT = 60  # seconds per entry
CMD_TIMEOUT = 55  # seconds for one command inside a check
APPLY_TIMEOUT = 3600
POST_TIMEOUT = 7200
DETAIL_LINES = 40

UP_TO_DATE = "up to date"
AVAILABLE = "update available"
CHECKING = "checking"
UNKNOWN = "unknown"
NO_SOURCE = "no update source"
ERROR = "error"

KINDS = ("git", "pip", "github_release", "docker")
# These must be stopped before updating. Ollama's installer restarts its own service.
NEEDS_STOP = ("git", "pip", "docker")

_VERSION_RE = re.compile(r"\d+(?:\.\d+)+")


# ---------- the two replaceable edges ----------
def run_command(argv, cwd=None, timeout=CMD_TIMEOUT, shell=False, env=None):
    """Run one command, return (returncode, combined output). Never raises."""
    try:
        full_env = {**os.environ, **(env or {})}
        r = subprocess.run(argv, cwd=cwd, shell=shell, capture_output=True, text=True,
                           timeout=timeout, stdin=subprocess.DEVNULL, env=full_env)
        return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()
    except subprocess.TimeoutExpired:
        return 124, "command timed out"
    except FileNotFoundError as e:
        return 127, f"command not found: {e.filename or e}"
    except Exception as e:  # noqa: BLE001 - must never escape
        return 1, str(e)


def fetch_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "switchboard-update-check"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ---------- pure helpers ----------
def parse_version(text):
    """First version number in text as a tuple of ints, or None."""
    m = _VERSION_RE.search(text or "")
    return tuple(int(x) for x in m.group(0).split(".")) if m else None


def fmt_version(t):
    return ".".join(str(x) for x in t)


def norm_name(name):
    return re.sub(r"[-_.]+", "-", str(name)).lower()


def parse_behind(text):
    """Output of `git rev-list --count`, as an int, or None if it is not a count."""
    t = (text or "").strip().splitlines()
    t = t[-1].strip() if t else ""
    return int(t) if t.isdigit() else None


def parse_pip_outdated(text, package):
    """(installed, latest) for package from `pip list --outdated --format=json`, or None
    when the package is not listed as outdated. Raises ValueError when there is no JSON."""
    lines = (text or "").splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.lstrip().startswith("[")), None)
    if start is None:
        raise ValueError("pip printed no JSON list")
    data, _ = json.JSONDecoder().raw_decode("\n".join(lines[start:]).lstrip())
    want = norm_name(package)
    for row in data:
        if isinstance(row, dict) and norm_name(row.get("name", "")) == want:
            return str(row.get("version", "?")), str(row.get("latest_version", "?"))
    return None


def parse_pip_index(text):
    """(installed, latest) from `pip index versions`; installed may be None."""
    inst = re.search(r"INSTALLED:\s*(\S+)", text or "")
    latest = re.search(r"LATEST:\s*(\S+)", text or "")
    if not latest:
        m = re.match(r"\s*\S+\s+\(([^)]+)\)", text or "")
        latest = m
    if not latest:
        return None
    return (inst.group(1) if inst else None), latest.group(1)


def tail_lines(text, n=DETAIL_LINES):
    return "\n".join((text or "").splitlines()[-n:])


def decide_git(behind):
    if behind is None:
        return UNKNOWN, "could not count commits behind"
    if behind > 0:
        return AVAILABLE, f"{behind} commit{'s' if behind != 1 else ''} behind"
    return UP_TO_DATE, "no new commits"


def decide_version(installed, latest):
    """installed/latest are version tuples or None."""
    if latest is None:
        return UNKNOWN, "could not read the latest version"
    if installed is None:
        return UNKNOWN, f"latest is {fmt_version(latest)}; installed version not readable"
    if latest > installed:
        return AVAILABLE, f"{fmt_version(installed)} to {fmt_version(latest)}"
    return UP_TO_DATE, f"version {fmt_version(installed)}"


def decide_image(new_id, container_id):
    if not new_id:
        return UNKNOWN, "could not read the image id"
    if not container_id:
        return UNKNOWN, "container not found, so there is nothing to compare"
    if new_id != container_id:
        return AVAILABLE, "newer image downloaded; recreate the container to use it"
    return UP_TO_DATE, "container runs the latest image"


def split_cmd(text, shell):
    """(argv_or_string, shell_flag) for a command that comes from the config file."""
    return (text, True) if shell else (shlex.split(text), False)


# ---------- per-kind checks: return (status, detail) ----------
def _git(path, *args, **kw):
    return run_command(["git", "-C", path, *args], env={"GIT_TERMINAL_PROMPT": "0"}, **kw)


def check_git(u):
    path = os.path.expanduser(u.get("path", ""))
    if not path or not os.path.isdir(path):
        return NO_SOURCE, "folder not found"
    rc, out = _git(path, "rev-parse", "--is-inside-work-tree")
    if rc != 0 or out.strip() != "true":
        return NO_SOURCE, "not a git checkout"
    branch = u.get("branch")
    if branch:
        if not re.fullmatch(r"[\w./-]+", branch) or branch.startswith("-"):
            return ERROR, "bad branch name in backends.json"
        rc, out = _git(path, "fetch", "--quiet", "origin", branch)
        upstream = f"origin/{branch}"
    else:
        rc, out = _git(path, "fetch", "--quiet")
        upstream = "@{u}"
    if rc != 0:
        return ERROR, "git fetch failed: " + tail_lines(out, 3)
    rc, out = _git(path, "rev-list", "--count", f"HEAD..{upstream}")
    if rc != 0:
        return NO_SOURCE, "no upstream branch to compare with"
    return decide_git(parse_behind(out))


def check_pip(u, meta=None):
    meta = {} if meta is None else meta
    py, pkg = u.get("python"), u.get("package")
    if not py or not pkg:
        return ERROR, 'pip update needs "python" and "package"'
    py = os.path.expanduser(py)
    rc, out = run_command([py, "-m", "pip", "list", "--outdated", "--format=json"])
    if rc == 0:
        try:
            hit = parse_pip_outdated(out, pkg)
            if hit is None:
                return UP_TO_DATE, f"{pkg} is current"
            meta.update(installed=parse_version(hit[0]), latest=parse_version(hit[1]))
            return AVAILABLE, f"{hit[0]} to {hit[1]}"
        except ValueError:
            pass  # fall through to the index lookup
    rc2, out2 = run_command([py, "-m", "pip", "index", "versions", pkg])
    if rc2 != 0:
        return ERROR, "pip check failed: " + tail_lines(out2 or out, 3)
    got = parse_pip_index(out2)
    if not got:
        return UNKNOWN, "could not read pip's answer"
    inst, latest = got
    meta.update(installed=parse_version(inst), latest=parse_version(latest))
    return decide_version(parse_version(inst), parse_version(latest))


def check_github_release(u, meta=None):
    meta = {} if meta is None else meta
    repo = u.get("repo", "")
    if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo):
        return ERROR, 'github_release needs "repo" as owner/name'
    vc = u.get("version_command")
    if not vc:
        return ERROR, 'github_release needs "version_command"'
    argv, sh = split_cmd(vc, bool(u.get("shell")))
    rc, out = run_command(argv, shell=sh)
    installed = parse_version(out) if rc == 0 else None
    tag = fetch_json(f"https://api.github.com/repos/{repo}/releases/latest").get("tag_name", "")
    meta.update(installed=installed, latest=parse_version(tag))
    return decide_version(installed, parse_version(tag))


def check_docker(u, b, meta=None):
    meta = {} if meta is None else meta
    image, container = u.get("image"), b.get("container")
    if not image or not container:
        return ERROR, 'docker update needs "image" and a "container" on the entry'
    rc, out = run_command(["docker", "pull", image])
    if rc != 0:
        return ERROR, "docker pull failed: " + tail_lines(out, 3)
    rc, new_id = run_command(["docker", "image", "inspect", "--format", "{{.Id}}", image])
    rc2, cid = run_command(["docker", "inspect", "--format", "{{.Image}}", container])
    meta["new_id"] = new_id.strip() if rc == 0 else ""
    return decide_image(meta["new_id"], cid.strip() if rc2 == 0 else "")


def run_check(b, meta=None):
    """One entry's check, as (status, detail). Never raises. meta, if given, is filled with
    what the verify step needs (expected version, new image id)."""
    u = b.get("update")
    try:
        kind = u.get("kind")
        if kind == "git":
            return check_git(u)
        if kind == "pip":
            return check_pip(u, meta)
        if kind == "github_release":
            return check_github_release(u, meta)
        if kind == "docker":
            return check_docker(u, b, meta)
        return ERROR, f"unknown update kind {kind!r}"
    except Exception as e:  # noqa: BLE001
        return ERROR, str(e)[:200] or type(e).__name__




# ---------- update steps: pull, install, verify ----------
class Step:
    """One named step. run() returns (ok, output, text): text is the reason on failure and a
    short note on success."""

    def __init__(self, name, run):
        self.name, self.run = name, run


STEP_NAMES = ("pull", "install", "verify")
STEP_TIMEOUT = 900


def last_line(text):
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1][:200] if lines else ""


def cmd_result(rc, out, timeout):
    """Turn one command's result into a step result."""
    if rc == 124 and "timed out" in (out or ""):
        return False, out, f"timed out after {timeout} s"
    if rc != 0:
        return False, out, f"exit {rc}: {last_line(out)}"
    return True, out, last_line(out)


def cmd_step(name, argv, cwd=None, shell=False, timeout=STEP_TIMEOUT, env=None):
    def run():
        rc, out = run_command(argv, cwd=cwd, shell=shell, timeout=timeout, env=env)
        return cmd_result(rc, out, timeout)
    return Step(name, run)


def git_upstream(u):
    return f"origin/{u['branch']}" if u.get("branch") else "@{u}"


def _seq(parts):
    """Run several (argv, cwd, shell, timeout) commands in order, stop at the first failure."""
    def run():
        log = []
        for argv, cwd, sh, t in parts:
            rc, out = run_command(argv, cwd=cwd, shell=sh, timeout=t)
            log.append(f"$ {argv if isinstance(argv, str) else ' '.join(argv)}\n{out}")
            ok, _, reason = cmd_result(rc, out, t)
            if not ok:
                return False, "\n".join(log), reason
        return True, "\n".join(log), "done"
    return run


def git_steps(b, u, meta):
    path = os.path.expanduser(u.get("path", ""))
    pull_args = ["pull", "--ff-only"] + (["origin", u["branch"]] if u.get("branch") else [])
    steps = [cmd_step("pull", ["git", "-C", path, *pull_args], env={"GIT_TERMINAL_PROMPT": "0"},
                      timeout=APPLY_TIMEOUT)]
    parts = []
    req = os.path.join(path, "requirements.txt")
    if u.get("python") and os.path.isfile(req):
        parts.append(([os.path.expanduser(u["python"]), "-m", "pip", "install", "-r", req], path, False, APPLY_TIMEOUT))
    if u.get("post_command"):
        argv, sh = split_cmd(u["post_command"], bool(u.get("post_shell")))
        parts.append((argv, os.path.expanduser(u.get("post_cwd", path)), sh, POST_TIMEOUT))
    if parts:
        steps.append(Step("install", _seq(parts)))
    steps.append(Step("verify", lambda: verify_git(path, u)))
    return steps


def verify_git(path, u):
    rc, head = _git(path, "rev-parse", "HEAD")
    rc2, up = _git(path, "rev-parse", git_upstream(u))
    if rc != 0 or rc2 != 0:
        return False, head + "\n" + up, "could not compare HEAD with upstream"
    if head.strip() != up.strip():
        return False, f"HEAD {head.strip()}\nupstream {up.strip()}", f"HEAD {head.strip()[:8]} is not at upstream {up.strip()[:8]}"
    _, log = _git(path, "log", "-1", "--oneline")
    out = f"HEAD matches upstream\n{log}"
    if u.get("verify_command"):
        argv, sh = split_cmd(u["verify_command"], bool(u.get("verify_shell")))
        rc3, o3 = run_command(argv, cwd=path, shell=sh, timeout=STEP_TIMEOUT)
        ok, o3, reason = cmd_result(rc3, o3, STEP_TIMEOUT)
        out += f"\n$ {u['verify_command']}\n{o3}"
        if not ok:
            return False, out, "verify command failed: " + reason
    return True, out, last_line(log) or "HEAD matches upstream"


def verify_pip(py, pkg, meta):
    rc, out = run_command([py, "-m", "pip", "show", pkg], timeout=CMD_TIMEOUT)
    if rc != 0:
        return False, out, f"pip show failed: {last_line(out)}"
    m = re.search(r"^Version:\s*(\S+)", out, re.M)
    new = parse_version(m.group(1)) if m else None
    exp, old = meta.get("latest"), meta.get("installed")
    if new is None:
        return False, out, "could not read the installed version"
    if exp is not None:
        if new < exp:
            return False, out, f"expected {fmt_version(exp)}, found {fmt_version(new)}"
    elif old is not None and new <= old:
        return False, out, f"version is still {fmt_version(new)}"
    return True, out, f"version {fmt_version(new)}"


def pip_steps(b, u, meta):
    py = os.path.expanduser(u["python"])
    return [cmd_step("pull", [py, "-m", "pip", "install", "--upgrade", u["package"]], timeout=APPLY_TIMEOUT),
            Step("verify", lambda: verify_pip(py, u["package"], meta))]


def verify_release(u, meta):
    argv, sh = split_cmd(u["version_command"], bool(u.get("shell")))
    rc, out = run_command(argv, shell=sh)
    new = parse_version(out) if rc == 0 else None
    exp = meta.get("latest")
    if new is None:
        return False, out, f"could not read the installed version: {last_line(out)}"
    if exp is None:
        return False, out, "could not read the release version to compare with"
    if new < exp:
        return False, out, f"expected {fmt_version(exp)}, found {fmt_version(new)}"
    return True, out, f"version {fmt_version(new)}"


def release_steps(b, u, meta):
    argv, sh = _apply_argv(b, u)
    return [cmd_step("install", argv, shell=sh, timeout=APPLY_TIMEOUT),
            Step("verify", lambda: verify_release(u, meta))]


def verify_docker(u, b):
    rc, new = run_command(["docker", "image", "inspect", "--format", "{{.Id}}", u["image"]], timeout=CMD_TIMEOUT)
    new = new.strip()
    if rc != 0 or not new:
        return False, new, "could not read the downloaded image id"
    rc2, cid = run_command(["docker", "inspect", "--format", "{{.Image}}", b["container"]], timeout=CMD_TIMEOUT)
    if rc2 == 0 and cid.strip() != new:
        return False, f"image {new}\ncontainer {cid.strip()}", "the container still runs the old image"
    note = "container runs the new image" if rc2 == 0 else "image downloaded; no container exists yet"
    return True, f"image {new}\n{note}", note


def docker_steps(b, u, meta):
    steps = [cmd_step("pull", ["docker", "pull", u["image"]], timeout=APPLY_TIMEOUT)]
    if u.get("apply_command"):
        argv, sh = split_cmd(u["apply_command"], bool(u.get("shell")))
        steps.append(cmd_step("install", argv, shell=sh, timeout=APPLY_TIMEOUT))
    steps.append(Step("verify", lambda: verify_docker(u, b)))
    return steps


_DEFAULT_STEPS = {"git": git_steps, "pip": pip_steps, "github_release": release_steps, "docker": docker_steps}


def custom_steps(b, u):
    """Steps from the "steps" list (or "script") of the update object. Raises ValueError on a
    mistake in backends.json."""
    raw = u.get("steps")
    if raw is None and u.get("script"):
        raw = [{"name": "install", "command": u["script"], "shell": False}]
    if not isinstance(raw, list) or not raw:
        raise ValueError('"steps" in backends.json must be a non-empty list')
    home = os.path.expanduser(u.get("path") or "~")
    out = []
    for i, s in enumerate(raw, 1):
        if not isinstance(s, dict) or s.get("name") not in STEP_NAMES:
            raise ValueError(f"step {i} in backends.json needs a name of pull, install or verify")
        if not s.get("command"):
            raise ValueError(f"step {i} ({s['name']}) in backends.json has no command")
        try:
            timeout = int(s.get("timeout", STEP_TIMEOUT))
            argv, sh = split_cmd(str(s["command"]), bool(s.get("shell")))
        except ValueError as e:
            raise ValueError(f"step {i} ({s['name']}) in backends.json: {e}") from e
        if s.get("pkexec"):
            argv = ["pkexec", "sh", "-c", argv] if sh else ["pkexec"] + argv
            sh = False
        out.append(cmd_step(s["name"], argv, cwd=os.path.expanduser(s.get("cwd", home)), shell=sh, timeout=timeout))
    return out


def build_steps(b, meta):
    """Ordered steps for one entry. The last step is always a verify step."""
    u = b["update"]
    kind = u.get("kind")
    if kind not in _DEFAULT_STEPS:
        raise ValueError(f"unknown update kind {kind!r}")
    defaults = _DEFAULT_STEPS[kind](b, u, meta)
    if u.get("steps") is not None or u.get("script"):
        steps = custom_steps(b, u)
        if steps[-1].name != "verify":
            steps.append(defaults[-1])  # a custom update still has to prove it worked
        return steps
    return defaults


def run_steps(steps, progress=None):
    """Run steps in order, stop at the first failure. progress(index, state, note) is called
    live with state running, ok or failed. Returns (ok, failed_name, reason, full_output)."""
    log = []
    for i, step in enumerate(steps):
        if progress:
            progress(i, "running", "")
        try:
            ok, out, text = step.run()
        except Exception as e:  # noqa: BLE001
            ok, out, text = False, str(e), str(e)[:200] or type(e).__name__
        log.append(f"--- {step.name} ({'ok' if ok else 'failed'}) ---\n{out}")
        if progress:
            progress(i, "ok" if ok else "failed", text)
        if not ok:
            return False, step.name, text, "\n".join(log)
    return True, None, "", "\n".join(log)


def _apply_argv(b, u):
    argv, sh = split_cmd(u["apply_command"], bool(u.get("shell")))
    if b.get("use_pkexec"):
        argv = ["pkexec", "sh", "-c", argv] if sh else ["pkexec"] + argv
        sh = False
    return argv, sh


def precheck_apply(b):
    """Refusal message for an apply that cannot go ahead (nothing has run yet), else None."""
    u, kind = b["update"], b["update"].get("kind")
    if kind not in _DEFAULT_STEPS:
        return f"unknown update kind {kind!r}"
    custom = u.get("steps") is not None or u.get("script")
    if kind == "git":
        path = os.path.expanduser(u.get("path", ""))
        rc, out = _git(path, "status", "--porcelain", "--untracked-files=no")
        if rc != 0:
            return f"Could not read git status for {b['name']}: {tail_lines(out, 2)}"
        if out.strip():
            return f"{b['name']} has local changes in {path}. Commit or stash them first."
    if not custom and kind in ("docker", "github_release") and not u.get("apply_command"):
        return "no recreate command set in backends.json" if kind == "docker" else "no apply command set in backends.json"
    try:
        build_steps(b, {})
    except (ValueError, KeyError) as e:
        return f"Bad update settings for {b['name']}: {e}"
    return None


# ---------- the cache and the background work ----------
def _with_timeout(fn, seconds):
    box = {}

    def work():
        try:
            box["r"] = fn()
        except Exception as e:  # noqa: BLE001
            box["r"] = (ERROR, str(e)[:200] or type(e).__name__)

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(seconds)
    return box.get("r") if "r" in box else (ERROR, "check timed out")


def progress_text(steps):
    names = {"pending": "pending", "running": "running", "ok": "ok", "failed": "failed"}
    return " · ".join(f"{s['name']} {names[s['state']]}" for s in steps)


class Updater:
    def __init__(self, get_backends, is_running=lambda bid: False, log_writer=None, check_timeout=CHECK_TIMEOUT):
        self._get = get_backends
        self._is_running = is_running
        self._log = log_writer
        self._timeout = check_timeout
        self._lock = threading.RLock()
        self._cache = {}  # id -> status, detail, checked_at, meta, steps, incomplete
        self._checking = False
        self._applying = None  # id being updated
        self._apply_lock = threading.Lock()

    # -- helpers
    def entries(self):
        return {bid: b for bid, b in self._get().items() if isinstance(b.get("update"), dict)}

    def _running(self, bid):
        try:
            return bool(self._is_running(bid))
        except Exception:  # noqa: BLE001
            return False

    def _store(self, bid, status, detail, **extra):
        with self._lock:
            old = self._cache.get(bid) or {}
            self._cache[bid] = {"status": status, "detail": detail, "checked_at": time.time(),
                                "meta": extra.get("meta", {}), "steps": extra.get("steps", []),
                                "incomplete": extra.get("incomplete", old.get("incomplete"))}

    def _stop_reason(self, b):
        if b["update"].get("kind") in NEEDS_STOP and self._running(b["id"]):
            return f"Stop {b['name']} first, then update it."
        return None

    @staticmethod
    def _applicable(c):
        return c["status"] == AVAILABLE or (c["status"] == ERROR and bool(c.get("incomplete")))

    # -- reading
    def snapshot(self):
        with self._lock:
            checking, applying = self._checking, self._applying
            items = []
            for bid, b in self.entries().items():
                c = self._cache.get(bid) or {"status": CHECKING if checking else UNKNOWN,
                                             "detail": "" if checking else "not checked yet",
                                             "checked_at": None, "steps": []}
                running = self._running(bid)
                reason = None
                if self._applicable(c) and applying != bid:
                    reason = self._stop_reason(b)
                    if applying and not reason:
                        reason = "Another update is running."
                    u = b["update"]
                    if (not reason and u.get("kind") in ("docker", "github_release") and not u.get("apply_command")
                            and u.get("steps") is None and not u.get("script")):
                        reason = "No apply command set in backends.json."
                steps = list(c.get("steps") or [])
                items.append({"id": bid, "name": b["name"], "status": c["status"], "detail": c["detail"],
                              "checked_at": c["checked_at"], "running": running,
                              "can_apply": self._applicable(c) and applying != bid and reason is None,
                              "reason": reason, "applying": applying == bid, "steps": steps,
                              "progress": progress_text(steps) if steps else ""})
            return {"checking": checking, "applying": applying, "items": items}

    # -- checking
    def _check_one(self, bid):
        b = self.entries().get(bid)
        if not b:
            return
        with self._lock:
            if self._applying == bid:
                return
            if bid not in self._cache:
                self._cache[bid] = {"status": CHECKING, "detail": "", "checked_at": None, "meta": {}, "steps": []}
        meta = {}
        status, detail = _with_timeout(lambda: run_check(b, meta), self._timeout)
        with self._lock:
            if self._applying == bid:
                return
            old = self._cache.get(bid) or {}
            inc = old.get("incomplete")
            if inc and status == UP_TO_DATE:
                status = ERROR
                detail = f"Last update stopped at {inc['step']}: {inc['reason']}\nFix that, then click Update to run the steps again."
            self._store(bid, status, detail, meta=meta, steps=old.get("steps", []) if inc else [])

    def check_all(self):
        """Check every entry one at a time (blocking). Returns False if a run was already going."""
        with self._lock:
            if self._checking:
                return False
            self._checking = True
            for bid in self.entries():
                self._cache.setdefault(bid, {"status": CHECKING, "detail": "", "checked_at": None, "meta": {}, "steps": []})
        try:
            for bid in list(self.entries()):
                self._check_one(bid)
        finally:
            with self._lock:
                self._checking = False
        return True

    def check_all_async(self):
        with self._lock:
            if self._checking:
                return False
        threading.Thread(target=self.check_all, daemon=True).start()
        return True

    # -- applying
    def apply(self, bid, background=True):
        """Start one update. Returns (http_code, json_body)."""
        b = self.entries().get(bid)
        if not b:
            return 404, {"error": "unknown backend or nothing to update"}
        with self._lock:
            cached = self._cache.get(bid)
        if not cached or not self._applicable(cached):
            return 409, {"error": f"No update is available for {b['name']}. Check again first."}
        stop = self._stop_reason(b)
        if stop:
            return 409, {"error": stop}
        try:
            refusal = precheck_apply(b)
        except Exception as e:  # noqa: BLE001
            refusal = str(e)[:200]
        if refusal:
            return 409, {"error": refusal}
        if not self._apply_lock.acquire(blocking=False):
            return 409, {"error": "Another update is running. Wait for it to finish."}
        with self._lock:
            self._applying = bid
        if background:
            threading.Thread(target=self._apply_job, args=(bid, b), daemon=True).start()
        else:
            self._apply_job(bid, b)
        return 202, {"ok": True}

    def _set_progress(self, bid, steps, i, state, note):
        with self._lock:
            steps[i]["state"], steps[i]["note"] = state, note
            c = self._cache.get(bid) or {}
            c.update(steps=[dict(s) for s in steps], detail=progress_text(steps), checked_at=time.time())
            self._cache[bid] = c

    def _apply_job(self, bid, b):
        try:
            with self._lock:
                old = self._cache.get(bid) or {}
                meta = dict(old.get("meta") or {})
                inc = old.get("incomplete")
            try:
                if not meta:
                    run_check(b, meta)  # read-only; fills in what the verify step compares with
                steps = build_steps(b, meta)
            except Exception as e:  # noqa: BLE001
                self._store(bid, ERROR, f"Could not start the update: {e}")
                return
            prog = [{"name": s.name, "state": "pending", "note": ""} for s in steps]
            with self._lock:
                self._cache[bid] = {"status": CHECKING, "detail": progress_text(prog), "checked_at": time.time(),
                                    "meta": meta, "steps": prog, "incomplete": inc}
            ok, name, reason, out = run_steps(steps, lambda i, st, note: self._set_progress(bid, prog, i, st, note))
            if self._log:
                try:
                    self._log(bid, f"=== update {time.ctime()} ({'ok' if ok else 'failed at ' + str(name)}) ===\n{out}\n")
                except Exception:  # noqa: BLE001
                    pass
            tail = tail_lines(out)
            if not ok:
                idx = next((i for i, s in enumerate(prog) if s["name"] == name), 0)
                marker = {"step": name, "reason": reason} if idx > 0 else inc
                self._store(bid, ERROR, f"{name} failed: {reason}\n{tail}", steps=prog, incomplete=marker)
                return
            meta2 = {}
            status, detail = _with_timeout(lambda: run_check(b, meta2), self._timeout)
            self._store(bid, status, (detail + "\n\n" if detail else "") + f"Updated and verified ({progress_text(prog)}).\n" + tail,
                        meta=meta2, steps=prog, incomplete=None)
        finally:
            with self._lock:
                self._applying = None
            self._apply_lock.release()
