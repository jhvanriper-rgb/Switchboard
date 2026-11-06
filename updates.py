"""Update checks and one-click updates for Switchboard backends.

Standard library only. Everything that is ever executed comes from backends.json (the
"update" object of an entry), chosen by backend id. Nothing here takes command text from a
request. Checks are read-only with respect to running software (git fetch and docker pull
only download). Updates are applied only when apply() is called for one id.

All command execution goes through run_command() and all web lookups through fetch_json(),
so tests can replace those two functions.
"""
import codecs
import json
import math
import os
import re
import shlex
import signal
import subprocess
import tempfile
import threading
import time
import urllib.request

CHECK_TIMEOUT = 60  # seconds per entry (git, pip, github_release)
DOCKER_CHECK_TIMEOUT = 1800  # docker pull of a multi-GB image is slow
DOWNLOADING = "Downloading the image: this can take several minutes."
CMD_TIMEOUT = 55  # seconds for one command inside a check
APPLY_TIMEOUT = 3600
POST_TIMEOUT = 7200
DETAIL_LINES = 40

UP_TO_DATE = "up to date"
AVAILABLE = "update available"
NEWER = "newer version exists"  # newer, but below the entry's threshold: no badge, still updatable
CHECKING = "checking"
UNKNOWN = "unknown"
NO_SOURCE = "no update source"
ERROR = "error"

KINDS = ("git", "git_tags", "pip", "github_release", "docker")
# A backend must be stopped before it is updated, unless its update object sets
# "update_while_running": true (Ollama: its installer restarts the service itself).

NO_SUCH = re.compile(r"no such (object|container)", re.I)
_VERSION_RE = re.compile(r"\d+(?:\.\d+)+", re.ASCII)  # ASCII digits only


# ---------- the two replaceable edges ----------
LIVE_LINES = 200  # lines kept per backend
LIVE_TAIL = 60  # lines the API returns
LIVE_LINE_CHARS = 2000
MAX_RESULT_BYTES = 2_000_000  # most of one command's output kept for the result
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")
_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


class OutputBuffer:
    """The recent output of one backend's update: the last LIVE_LINES lines, plus a line
    at the start of every step. Bounded: long lines are cut (the first LIVE_LINE_CHARS
    characters are kept), control characters and terminal escape codes are removed, and old
    lines fall off the front."""

    def __init__(self):
        self._lock = threading.Lock()
        self._lines = []
        self._partial = ""  # the line being written; a bare \r starts it over (progress bars)
        self._held_cr = False  # a trailing \r waits for the next chunk, which may begin with \n
        self.finished = False
        self.finished_at = None

    @staticmethod
    def _clean(line):
        return _CTRL.sub("", _ANSI.sub("", line))[:LIVE_LINE_CHARS]

    def _push(self, line):
        self._lines.append(self._clean(line))
        if len(self._lines) > LIVE_LINES:
            del self._lines[:len(self._lines) - LIVE_LINES]

    def feed(self, text):
        """Add raw command output in any chunking. \\n (or \\r\\n) ends a line; a bare \\r
        replaces the current line, like a terminal."""
        with self._lock:
            if self._held_cr:
                text, self._held_cr = "\r" + text, False
            if text.endswith("\r"):
                text, self._held_cr = text[:-1], True
            for part in re.split(r"(\r\n|\n|\r)", text):
                if part in ("\n", "\r\n"):
                    self._push(self._partial)
                    self._partial = ""
                elif part == "\r":
                    self._partial = ""
                else:
                    self._partial = (self._partial + part)[:LIVE_LINE_CHARS]

    def marker(self, text):
        with self._lock:
            self._held_cr = False
            if self._partial:
                self._push(self._partial)
                self._partial = ""
            self._push(text)

    def finish(self):
        with self._lock:
            self._held_cr = False
            if self._partial:
                self._push(self._partial)
                self._partial = ""
            self.finished = True
            self.finished_at = time.time()

    def tail(self, n=LIVE_TAIL):
        with self._lock:
            lines = self._lines + ([self._clean(self._partial)] if self._partial else [])
            return "\n".join(lines[-n:])

    def line_count(self):
        with self._lock:
            return len(self._lines)


_ctx = threading.local()  # per thread: shared check deadline, and the live output sink


def emit(text):
    """Add a line to the live output of the update running in this thread (if any)."""
    sink = getattr(_ctx, "sink", None)
    if sink is not None:
        sink.marker(text)


class _quiet:
    """Keep repeated polling output (journal reads) out of the live output."""

    def __enter__(self):
        self.old = getattr(_ctx, "sink", None)
        _ctx.sink = None

    def __exit__(self, *a):
        _ctx.sink = self.old


def _kill_group(p):
    """Stop a command and everything it started: SIGTERM, a short wait, then SIGKILL."""
    try:
        pgid = os.getpgid(p.pid)
    except OSError:
        pgid = None
    for sig, wait in ((signal.SIGTERM, 2), (signal.SIGKILL, 2)):
        try:
            if pgid is not None:
                os.killpg(pgid, sig)
            else:
                p.send_signal(sig)
        except OSError:
            pass
        try:
            p.wait(timeout=wait)
            break
        except subprocess.TimeoutExpired:
            continue
    if pgid is not None:  # make sure no grandchild survives the leader
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass


PUMP_BYTES = 1_000_000  # most one pass reads
PUMP_SECONDS = 0.2  # longest one pass runs
PUMP_LAG = 256 * 1024  # if the reader falls this far behind, skip ahead
MAX_OUTPUT_FILE = 200_000_000  # a command that writes more than this is stopped


def _skip_continuation(outf, pos, size):
    """First position at or after pos that is not a UTF-8 continuation byte."""
    while pos < size:
        b = os.pread(outf.fileno(), 1, pos)
        if not b or (b[0] & 0xC0) != 0x80:
            break
        pos += 1
    return pos


def _pump(outf, pos, sink, dec):
    """Feed output written since `pos` to the sink and return the new position. One pass is
    capped (about 1 MB or 0.2 s), and a reader more than PUMP_LAG behind jumps ahead, so a
    command that prints faster than we read cannot keep us here. pread does not move the file
    offset the child is writing at."""
    size = os.fstat(outf.fileno()).st_size
    if size - pos > PUMP_LAG:
        pos = _skip_continuation(outf, size - PUMP_LAG, size)
        dec.reset()
        sink.feed("\n")
    t0, used = time.monotonic(), 0
    while used < PUMP_BYTES and time.monotonic() - t0 < PUMP_SECONDS:
        chunk = os.pread(outf.fileno(), 65536, pos)
        if not chunk:
            break
        pos += len(chunk)
        used += len(chunk)
        sink.feed(dec.decode(chunk))
    return pos


def run_command(argv, cwd=None, timeout=CMD_TIMEOUT, shell=False, env=None):
    """Run one command in its own process group, return (returncode, combined output).
    On timeout the whole group is killed and (124, "command timed out") comes back. A command
    that writes more than MAX_OUTPUT_FILE bytes is stopped too (125). If this thread has a
    live-output sink, output is passed to it while the command runs. Never raises."""
    p = None
    sink = getattr(_ctx, "sink", None)
    dec = codecs.getincrementaldecoder("utf-8")("replace")
    try:
        full_env = {**os.environ, **(env or {})}
        # Output goes to a temp file, not a pipe: a background process that outlives the
        # command would otherwise hold a pipe open and look like a timeout. The result depends
        # only on the command itself exiting.
        with tempfile.TemporaryFile() as outf:
            p = subprocess.Popen(argv, cwd=cwd, shell=shell, stdout=outf, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, env=full_env, start_new_session=True)
            end = time.monotonic() + max(0.01, timeout)
            pos = 0
            killed = None
            while True:
                left = end - time.monotonic()
                if left <= 0:
                    killed = (124, "command timed out")
                    break
                try:
                    p.wait(timeout=min(0.2, left))
                    break
                except subprocess.TimeoutExpired:
                    pass
                if sink:
                    pos = _pump(outf, pos, sink, dec)
                if os.fstat(outf.fileno()).st_size > MAX_OUTPUT_FILE:
                    killed = (125, "command printed too much output and was stopped")
                    break
            if killed:
                _kill_group(p)
            if sink:
                _pump(outf, pos, sink, dec)  # one last bounded pass
                tail = dec.decode(b"", final=True)
                if tail:
                    sink.feed(tail)
            if killed:
                return killed
            size = os.fstat(outf.fileno()).st_size
            start = max(0, size - MAX_RESULT_BYTES)  # bounded, even for runaway output
            if start:
                start = _skip_continuation(outf, start, size)
            data = os.pread(outf.fileno(), size - start, start)
            return p.returncode, data.decode("utf-8", "replace").strip()
    except FileNotFoundError as e:
        return 127, f"command not found: {e.filename or e}"
    except Exception as e:  # noqa: BLE001 - must never escape
        if p is not None and p.poll() is None:
            _kill_group(p)
        return 1, str(e)


def _cmd(argv, **kw):
    """run_command with the current check's shared deadline applied (if any)."""
    dl = getattr(_ctx, "deadline", None)
    if dl is not None:
        left = dl - time.monotonic()
        if left <= 0:
            return 124, "command timed out"
        kw["timeout"] = min(kw.get("timeout", CMD_TIMEOUT), left)
    return run_command(argv, **kw)


def fetch_json(url, timeout=20):
    req = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": "switchboard-update-check"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


# ---------- pure helpers ----------
def parse_version(text):
    """First version number in text as a tuple of ints, or None."""
    m = _VERSION_RE.search(text or "")
    return _numbers(m.group(0)) if m else None


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


def git_threshold(u):
    """"min_commits_behind" of a git update object: a whole number of 1 or more (default 1).
    Raises ValueError for anything else."""
    v = u.get("min_commits_behind", 1)
    if isinstance(v, bool) or not isinstance(v, int) or v < 1:
        raise ValueError('"min_commits_behind" in backends.json must be a whole number of 1 or more')
    return v


def decide_git(behind, min_behind=1):
    if behind is None:
        return UNKNOWN, "could not count commits behind"
    text = f"{behind} commit{'s' if behind != 1 else ''} behind"
    if behind >= min_behind and behind > 0:
        return AVAILABLE, text
    if behind > 0:
        return NEWER, f"{text} (below the {min_behind} commit threshold)"
    return UP_TO_DATE, "no new commits"


RELEASE_LEVELS = {"patch": 0, "minor": 1, "major": 2}
_RELEASE_RE = re.compile(r"v?(\d+(?:\.\d+)+)(-[0-9A-Za-z][0-9A-Za-z.-]*)?", re.ASCII)
# -rc1, -beta.2, -alpha, -pre1, -dev mark a pre-release; other suffixes (-cuda, -r2) do not
_PRERELEASE_RE = re.compile(r"-(?:rc|alpha|beta|pre|dev)[0-9.]*(?:-|$)", re.I)
MAX_COMPONENT_DIGITS = 9


def release_threshold(u):
    """"min_release" of a github_release update object: patch, minor or major (default patch)."""
    v = u.get("min_release", "patch")
    if not isinstance(v, str) or v not in RELEASE_LEVELS:
        raise ValueError('"min_release" in backends.json must be "patch", "minor" or "major"')
    return v


def _numbers(text):
    """The numbers of a dotted version as a tuple, or None if a part is absurdly long."""
    parts = text.split(".")
    if any(len(p) > MAX_COMPONENT_DIGITS for p in parts):
        return None
    return tuple(int(p) for p in parts)


def parse_release(text):
    """(numbers, is_prerelease) for the first version in text, or None (also for absurdly
    long numbers). Only a suffix such as -rc1, -beta.2, -alpha, -pre or -dev marks a
    pre-release; other suffixes like -cuda do not. Build metadata after + is ignored."""
    m = _RELEASE_RE.search(str(text) if text is not None else "")
    if not m:
        return None
    nums = _numbers(m.group(1))
    if nums is None:
        return None
    return nums, bool(m.group(2) and _PRERELEASE_RE.match(m.group(2)))


def release_class(old, new):
    """Which part of the version changed: "major", "minor" or "patch". Missing parts count as 0."""
    a, b = (tuple(old) + (0, 0, 0))[:3], (tuple(new) + (0, 0, 0))[:3]
    if b[0] != a[0]:
        return "major"
    if b[1] != a[1]:
        return "minor"
    return "patch"


def _strip_zeros(nums):
    """1.2.3.0 equals 1.2.3: drop trailing zero parts before comparing."""
    n = list(nums)
    while len(n) > 1 and n[-1] == 0:
        n.pop()
    return tuple(n)


def decide_release(installed_text, tag_text, min_release="patch"):
    """Status for a GitHub release check. A pre-release tag is never offered; text that does not
    parse is ignored (unknown)."""
    lat = parse_release(tag_text)
    if lat is None:
        return UNKNOWN, "could not read the latest version"
    if lat[1]:
        return UP_TO_DATE, f"the latest release ({str(tag_text).strip()}) is a pre-release, which is not offered"
    inst = parse_release(installed_text)
    if inst is None:
        return UNKNOWN, f"latest is {fmt_version(lat[0])}; installed version not readable"
    # a final release is newer than its own release candidates
    if (_strip_zeros(lat[0]), 1) <= (_strip_zeros(inst[0]), 0 if inst[1] else 1):
        return UP_TO_DATE, f"version {fmt_version(inst[0])}"
    text = f"{fmt_version(inst[0])} to {fmt_version(lat[0])}"
    cls = release_class(inst[0], lat[0])
    if RELEASE_LEVELS[cls] >= RELEASE_LEVELS[min_release]:
        return AVAILABLE, text
    return NEWER, f"{text} ({cls} release, below the {min_release} threshold)"


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
    return _cmd(["git", "-C", path, *args], env={"GIT_TERMINAL_PROMPT": "0"}, **kw)


def check_git(u):
    try:
        min_behind = git_threshold(u)
    except ValueError as e:
        return ERROR, f"Bad settings in backends.json: {e}"
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
    return decide_git(parse_behind(out), min_behind)


# ---------- git_tags: a checkout that follows release tags (vMAJOR.MINOR.PATCH) ----------
_PART = r"(0|[1-9][0-9]{0,8})"  # ASCII digits, no leading zeros, bounded
_STABLE_TAG_RE = re.compile(rf"v{_PART}\.{_PART}\.{_PART}")  # [0-9] only: v01.2.3, v0.39.02 and v\u0669.0.0 are not releases
MAX_TAG_LINES = 50000  # more tag names than this are ignored (hostile or runaway output)
MAX_TAG_CHARS = 64


def newest_stable_tag(text):
    """(tag, numbers) for the newest tag shaped exactly vMAJOR.MINOR.PATCH in `git tag --list`
    output, or None. Tags with a suffix (-rc1) and tags that are not versions are ignored."""
    best = None
    for n, line in enumerate((text or "").splitlines()):
        if n >= MAX_TAG_LINES:
            break
        tag = line.strip()
        m = _STABLE_TAG_RE.fullmatch(tag) if len(tag) <= MAX_TAG_CHARS else None
        if m:
            nums = tuple(int(x) for x in m.groups())
            if best is None or nums > best[1]:
                best = (tag, nums)
    return best


def git_tags_remote(u):
    """Optional "remote" name for the fetch; validated because it goes on a command line."""
    v = u.get("remote")
    if v is None:
        return None
    if not isinstance(v, str) or not re.fullmatch(r"[\w][\w.-]*", v):
        raise ValueError('"remote" in backends.json must be a plain remote name such as "origin"')
    return v


MAX_IGNORE_PATHS = 50
MAX_IGNORE_CHARS = 200


def ignore_dirty_paths(u):
    """The "ignore_dirty_paths" list of a git_tags update object: plain paths, relative to the
    repository, whose tracked changes do not count as local changes. Raises ValueError for
    anything else (not a list, too many or too long entries, absolute paths, "..", a leading
    "-", control characters)."""
    v = u.get("ignore_dirty_paths")
    if v is None:
        return []
    err = ('"ignore_dirty_paths" in backends.json must be a short list of plain paths relative to '
           'the repository, such as ["models/"]')
    if not isinstance(v, list) or len(v) > MAX_IGNORE_PATHS:
        raise ValueError(err)
    out = []
    for p in v:
        if (not isinstance(p, str) or not p.strip("/") or len(p) > MAX_IGNORE_CHARS or p.startswith(("/", "-"))
                or any(ord(c) < 32 or ord(c) == 127 for c in p) or ".." in p.split("/") or "\\" in p):
            raise ValueError(err)
        out.append(p.strip("/"))
    return out


def under_ignored(path, ignored):
    """Is this repository path one of the ignored paths, or inside one? "models" matches
    models/x but not models2/x."""
    return any(path == p or path.startswith(p + "/") for p in ignored)


def parse_status_z(text):
    """Paths in `git status --porcelain -z` output. A rename or copy lists two paths."""
    paths, parts = [], (text or "").split("\0")
    # the command runner strips the output, which can eat the leading space of the first entry (" D path")
    if parts and len(parts[0]) >= 3 and parts[0][1] == " " and parts[0][2] != " ":
        parts[0] = " " + parts[0]
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        paths.append([entry[3:]])
        if entry[0] in "RC" or entry[1] in "RC":
            if i < len(parts):
                paths[-1].append(parts[i])
                i += 1
    return paths


def blocking_changes(path, ignored):
    """(rc, output, changes): the tracked changes in the working tree that are not under an ignored
    path. Untracked files and directories never count. A rename counts unless both its paths
    are ignored."""
    rc, out = _git(path, "status", "--porcelain", "-z", "--untracked-files=no")
    if rc != 0:
        return rc, out, []
    return rc, out, [p for p in parse_status_z(out) if not all(under_ignored(x, ignored) for x in p)]


def tag_fetch_args(remote):
    """git arguments that fetch only the version tags, forcing them to match the remote. A plain
    `git fetch --tags` stops with "would clobber existing tag" when any tag, such as a moving
    `latest`, changes upstream. Tags that do not start with v are never fetched, and the output
    is kept so a failure has a reason."""
    return ["fetch", "--no-tags", remote or "origin", "+refs/tags/v*:refs/tags/v*"]


def move_guard(path, tag):
    """May HEAD be moved to `tag` without going backwards or sideways? Returns
    (ok, reason, is_error): is_error is True when git could not answer. Allowed when HEAD is
    already at the tag, when HEAD is exactly on an older stable release tag, or when HEAD is an
    ancestor of the tag."""
    rc, head = _git(path, "rev-parse", "HEAD")
    rc2, want = _git(path, "rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}")
    if rc != 0 or rc2 != 0:
        return False, f"could not find {tag} in this checkout", True
    if head.strip() == want.strip():
        return True, "", False
    cur = current_release(path)
    m = _STABLE_TAG_RE.fullmatch(cur[0]) if cur and cur[1] else None
    t = _STABLE_TAG_RE.fullmatch(tag)
    if m and t and tuple(int(x) for x in m.groups()) < tuple(int(x) for x in t.groups()):
        return True, "", False  # a move from one stable release to a newer one
    rc3, out = _git(path, "merge-base", "--is-ancestor", "HEAD", f"refs/tags/{tag}^{{commit}}")
    if rc3 == 0:
        return True, "", False
    if rc3 == 1:
        return False, (f"HEAD is not behind {tag} (it is past it, or on another branch), so moving to it "
                       "could go backwards or sideways"), False
    return False, f"could not compare HEAD with {tag}: {tail_lines(out, 2)}", True


def current_release(path):
    """(tag, exact) for the checked-out version, or None. `exact` is True when HEAD is exactly on
    a version tag (the newest one if several); otherwise the nearest version tag behind HEAD."""
    rc, out = _git(path, "tag", "--points-at", "HEAD")
    if rc == 0:
        found = [(parse_release(ln.strip()), ln.strip()) for ln in out.splitlines()[:200]
                 if len(ln.strip()) <= MAX_TAG_CHARS]
        found = [(p[0], t) for p, t in found if p and re.fullmatch(r"v[0-9]+(?:\.[0-9]+)+(?:-[0-9A-Za-z.-]+)?", t)]
        if found:
            return max(found, key=lambda f: (not f[0][1], f[0][0]))[1], True  # a stable tag beats a release candidate
    rc, out = _git(path, "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*")
    if rc == 0 and out.strip() and parse_release(out.strip()):
        return out.strip().splitlines()[-1][:MAX_TAG_CHARS], False
    return None


def check_git_tags(u, meta=None):
    meta = {} if meta is None else meta
    try:
        min_release = release_threshold(u)
        remote = git_tags_remote(u)
        ignored = ignore_dirty_paths(u)
    except ValueError as e:
        return ERROR, f"Bad settings in backends.json: {e}"
    path = os.path.expanduser(u.get("path", ""))
    if not path or not os.path.isdir(path):
        return NO_SOURCE, "folder not found"
    rc, out = _git(path, "rev-parse", "--is-inside-work-tree")
    if rc != 0 or out.strip() != "true":
        return NO_SOURCE, "not a git checkout"
    rc, out = _git(path, *tag_fetch_args(remote))
    if rc != 0:
        return ERROR, "git fetch failed: " + (tail_lines(out, 3) or f"exit {rc}")
    rc, out = _git(path, "tag", "--list")
    if rc != 0:
        return ERROR, "could not list tags: " + tail_lines(out, 3)
    newest = newest_stable_tag(out)
    if newest is None:
        return UNKNOWN, "no release tags like v1.2.3 found in this repository"
    cur = current_release(path)
    if cur is None:
        return UNKNOWN, f"the checked-out commit is not on or after any release tag (newest release is {newest[0]})"
    tag, exact = cur
    meta.update(tag=newest[0], latest=newest[1], installed=(parse_release(tag) or ((),))[0])
    status, detail = decide_release(tag, newest[0], min_release)
    if not exact:
        rc, desc = _git(path, "describe", "--tags", "--always")
        note = f"HEAD is not exactly on a release tag: {last_line(desc) or tag}"
        if status in (AVAILABLE, NEWER):
            # the nearest tag behind HEAD says nothing about the newest release's history: it may
            # sit on another branch, and moving there would be a downgrade
            ok, reason, is_error = move_guard(path, newest[0])
            if not ok:
                if is_error:
                    return UNKNOWN, reason
                return UP_TO_DATE, f"HEAD is past the newest release {newest[0]} ({note})"
        detail += f" ({note})"
    if status in (AVAILABLE, NEWER):
        rc, out, changes = blocking_changes(path, ignored)
        if rc == 0 and changes:
            detail += " (the working tree has local changes, so an update is refused until they are committed or stashed)"
    return status, detail


def check_pip(u, meta=None):
    meta = {} if meta is None else meta
    py, pkg = u.get("python"), u.get("package")
    if not py or not pkg:
        return ERROR, 'pip update needs "python" and "package"'
    py = os.path.expanduser(py)
    rc, out = _cmd([py, "-m", "pip", "list", "--outdated", "--format=json"])
    if rc == 0:
        try:
            hit = parse_pip_outdated(out, pkg)
            if hit is None:
                return UP_TO_DATE, f"{pkg} is current"
            meta.update(installed=parse_version(hit[0]), latest=parse_version(hit[1]))
            return AVAILABLE, f"{hit[0]} to {hit[1]}"
        except ValueError:
            pass  # fall through to the index lookup
    rc2, out2 = _cmd([py, "-m", "pip", "index", "versions", pkg])
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
    try:
        min_release = release_threshold(u)
    except ValueError as e:
        return ERROR, f"Bad settings in backends.json: {e}"
    argv, sh = split_cmd(vc, bool(u.get("shell")))
    rc, out = _cmd(argv, shell=sh)
    installed = parse_version(out) if rc == 0 else None
    dl = getattr(_ctx, "deadline", None)
    if dl is not None and dl - time.monotonic() <= 0:
        return ERROR, "check timed out"  # run_check rewrites this with the budget
    data = fetch_json(f"https://api.github.com/repos/{repo}/releases/latest")
    raw = data.get("tag_name") if isinstance(data, dict) else None
    if raw is None or isinstance(raw, (dict, list, bool)) or not str(raw).strip():
        return UNKNOWN, "GitHub did not return a release tag"
    tag = str(raw).strip()  # a number such as 1.5 becomes "1.5"
    lat = parse_release(tag)
    meta.update(installed=installed, latest=lat[0] if lat else None)
    return decide_release(out if rc == 0 else "", tag, min_release)


def check_docker(u, b, meta=None):
    meta = {} if meta is None else meta
    image, container = u.get("image"), b.get("container")
    if not image or not container:
        return ERROR, 'docker update needs "image" and a "container" on the entry'
    rc, out = _cmd(["docker", "pull", image],
                   timeout=max(APPLY_TIMEOUT, getattr(_ctx, "budget", None) or 0))  # the check's deadline is the real limit
    if rc != 0:
        return ERROR, "docker pull failed: " + tail_lines(out, 3)
    rc, new_id = _cmd(["docker", "image", "inspect", "--format", "{{.Id}}", image])
    rc2, cid = _cmd(["docker", "inspect", "--format", "{{.Image}}", container])
    meta["new_id"] = new_id.strip() if rc == 0 else ""
    if rc2 != 0 and not NO_SUCH.search(cid or ""):
        return ERROR, "could not inspect the container: " + last_line(cid)
    return decide_image(meta["new_id"], cid.strip() if rc2 == 0 else "")


MAX_CHECK_TIMEOUT = 86400


def check_budget(b, override=None):
    """Seconds one check of this entry may take: the update object's "check_timeout", else
    `override` (tests), else 1800 for docker and 60 for everything else. Raises ValueError for
    a check_timeout that is not a finite number above 0; values above a day are capped."""
    u = b.get("update") or {}
    if "check_timeout" in u:
        v = u["check_timeout"]
        try:
            v = float(v) if not isinstance(v, bool) else float("nan")
        except (TypeError, ValueError):
            v = float("nan")
        if not math.isfinite(v) or v <= 0:
            raise ValueError('"check_timeout" in backends.json must be a number of seconds above 0')
        return min(v, MAX_CHECK_TIMEOUT)
    if override is not None:
        return override
    return DOCKER_CHECK_TIMEOUT if u.get("kind") == "docker" else CHECK_TIMEOUT


def timed_out_text(budget):
    return f"check timed out after {budget:g} seconds. Raise check_timeout in backends.json."


def run_check(b, meta=None, budget=CHECK_TIMEOUT):
    """One entry's check, as (status, detail). Never raises. meta, if given, is filled with
    what the verify step needs (expected version, new image id). All of the check's commands
    share one deadline of `budget` seconds."""
    u = b.get("update")
    deadline = time.monotonic() + budget
    _ctx.deadline, _ctx.budget = deadline, budget
    try:
        kind = u.get("kind")
        if kind == "git":
            res = check_git(u)
        elif kind == "git_tags":
            res = check_git_tags(u, meta)
        elif kind == "pip":
            res = check_pip(u, meta)
        elif kind == "github_release":
            res = check_github_release(u, meta)
        elif kind == "docker":
            res = check_docker(u, b, meta)
        else:
            res = (ERROR, f"unknown update kind {kind!r}")
    except Exception as e:  # noqa: BLE001
        res = (ERROR, str(e)[:200] or type(e).__name__)
    finally:
        _ctx.deadline = _ctx.budget = None
    if res[0] == ERROR and time.monotonic() >= deadline:
        return ERROR, timed_out_text(budget)
    return res


# ---------- update steps: pull, install, verify ----------
class Step:
    """One named step. run() returns (ok, output, text): text is the reason on failure and a
    short note on success."""

    def __init__(self, name, run, safe_on_fail=False):
        # safe_on_fail: if this step fails nothing has changed (git pull --ff-only, docker pull)
        self.name, self.run, self.safe_on_fail = name, run, safe_on_fail


RECREATE_MISSING = "No recreate command set in backends.json."
APPLY_MISSING = "No apply command set in backends.json."
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


def cmd_step(name, argv, cwd=None, shell=False, timeout=STEP_TIMEOUT, env=None, safe_on_fail=False):
    def run():
        rc, out = run_command(argv, cwd=cwd, shell=shell, timeout=timeout, env=env)
        return cmd_result(rc, out, timeout)
    return Step(name, run, safe_on_fail)


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
                      timeout=APPLY_TIMEOUT, safe_on_fail=True)]
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


def git_tags_steps(b, u, meta):
    path = os.path.expanduser(u.get("path", ""))
    remote = git_tags_remote(u)
    ignored = ignore_dirty_paths(u)
    state = {}

    def tag():
        t = meta.get("tag")
        if not isinstance(t, str) or not _STABLE_TAG_RE.fullmatch(t):
            raise ValueError("no release tag was chosen; press Check again first")
        return t

    def pull():
        # Fetch the version tags again, make sure the move is not backwards or sideways, then
        # check the chosen tag out. Nothing is reset or cleaned: git itself refuses to overwrite
        # local changes, and the precheck already refuses a dirty tree.
        pull_step.safe_on_fail = True
        try:
            t = tag()
        except ValueError as e:
            return False, str(e), str(e)
        rc, out, changes = blocking_changes(path, ignored)  # the same rule as the precheck, right before the move
        if rc != 0:
            return False, out, f"could not read the git status: {last_line(out)}"
        if changes:
            return False, "\n".join(p[0] for p in changes[:20]), "the working tree has local changes. Commit or stash them first."
        # On a retry after a failed install HEAD is already on the new tag; the marker remembers
        # what was checked out before the first attempt.
        rc, prev = _git(path, "describe", "--tags", "--always")
        first = (meta.get("_incomplete") or {}).get("prev")
        state["prev"] = first or last_line(prev) or "an unknown commit"
        meta["prev"] = state["prev"]
        log = [f"previous checkout: {state['prev']}"]
        rc, out = run_command(["git", "-C", path, *tag_fetch_args(remote)], env={"GIT_TERMINAL_PROMPT": "0"},
                              timeout=APPLY_TIMEOUT)
        log.append("$ git " + " ".join(tag_fetch_args(remote)) + "\n" + out)
        ok, _, reason = cmd_result(rc, out, APPLY_TIMEOUT)
        if not ok:
            return False, "\n".join(log), f"{reason} (nothing was changed; {state['prev']} is still checked out)"
        allowed, why, _ = move_guard(path, t)
        if not allowed:
            log.append(why)
            return False, "\n".join(log), f"{why} (nothing was changed; {state['prev']} is still checked out)"
        if ignored:
            # git would replace a symlink that stands where the release has tracked files, so a
            # release that touches an ignored path is not checked out at all
            rc, names = _git(path, "diff", "--name-only", "-z", "HEAD", f"refs/tags/{t}")
            hit = [n for n in names.split("\0") if n and under_ignored(n, ignored)] if rc == 0 else []
            if rc != 0 or hit:
                why = (f"the release {t} changes files under an ignored path ({hit[0]}); update by hand"
                       if hit else f"could not compare {state['prev']} with {t}: {last_line(names)}")
                log.append(why)
                return False, "\n".join(log), f"{why} (nothing was changed; {state['prev']} is still checked out)."
        args = ["-c", "advice.detachedHead=false", "checkout", "--detach", f"refs/tags/{t}"]
        rc, out = run_command(["git", "-C", path, *args], env={"GIT_TERMINAL_PROMPT": "0"}, timeout=APPLY_TIMEOUT)
        log.append("$ git " + " ".join(args) + "\n" + out)
        ok, _, reason = cmd_result(rc, out, APPLY_TIMEOUT)
        if not ok:
            if rc == 124:  # timed out: git may have been part-way through writing files
                pull_step.safe_on_fail = False
                return False, "\n".join(log), (f"checkout timed out; the working tree may be only partly updated "
                                               f"(it was on {state['prev']})")
            hint = ""
            if ignored and ("beyond a symbolic link" in out or any(p in out for p in ignored)):
                hint = " The new release changes files under an ignored path; update by hand."
            return False, "\n".join(log), f"{reason} (git refused; nothing was changed; {state['prev']} is still checked out).{hint}"
        return True, "\n".join(log), f"now on {t}"

    pull_step = Step("pull", pull, safe_on_fail=True)
    steps = [pull_step]
    parts = []
    # Like the git kind, requirements.txt is looked for when the steps are built, so a file that
    # the checkout itself brings in is not installed on the first update.
    req = os.path.join(path, "requirements.txt")
    if u.get("python") and os.path.isfile(req):
        parts.append(([os.path.expanduser(u["python"]), "-m", "pip", "install", "-r", req], path, False, APPLY_TIMEOUT))
    if u.get("post_command"):
        argv, sh = split_cmd(u["post_command"], bool(u.get("post_shell")))
        parts.append((argv, os.path.expanduser(u.get("post_cwd", path)), sh, POST_TIMEOUT))
    if parts:
        run_parts = _seq(parts)

        def install():
            ok, out, text = run_parts()
            if not ok:
                text += (f" (the new release {meta.get('tag')} is checked out; before the update it was "
                         f"{state.get('prev', 'unknown')}; its dependencies may be only partly installed)")
            return ok, out, text
        steps.append(Step("install", install))
    steps.append(Step("verify", lambda: verify_git_tags(path, u, meta)))
    return steps


def verify_git_tags(path, u, meta):
    t = meta.get("tag")
    if not isinstance(t, str) or not _STABLE_TAG_RE.fullmatch(t):
        return False, "", "no release tag was chosen to verify against"
    rc, head = _git(path, "rev-parse", "HEAD")
    rc2, want = _git(path, "rev-parse", "--verify", "--quiet", f"refs/tags/{t}^{{commit}}")
    if rc != 0 or rc2 != 0:
        return False, head + "\n" + want, f"could not compare HEAD with {t}"
    if head.strip() != want.strip():
        return False, f"HEAD {head.strip()}\n{t} {want.strip()}", f"HEAD {head.strip()[:8]} is not on {t}"
    rc3, points = _git(path, "tag", "--points-at", "HEAD")
    out = f"HEAD is exactly on {t}\n{points}"
    if u.get("verify_command"):
        argv, sh = split_cmd(u["verify_command"], bool(u.get("verify_shell")))
        rc4, o4 = run_command(argv, cwd=path, shell=sh, timeout=STEP_TIMEOUT)
        ok, o4, reason = cmd_result(rc4, o4, STEP_TIMEOUT)
        out += f"\n$ {u['verify_command']}\n{o4}"
        if not ok:
            return False, out, "verify command failed: " + reason
    return True, out, f"on {t}"


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
    if rc2 != 0 and not NO_SUCH.search(cid or ""):
        return False, cid, "could not inspect the container: " + last_line(cid)
    if rc2 == 0 and cid.strip() != new:
        return False, f"image {new}\ncontainer {cid.strip()}", "the container still runs the old image"
    note = "container runs the new image" if rc2 == 0 else "image downloaded; no container exists yet"
    return True, f"image {new}\n{note}", note


def docker_steps(b, u, meta):
    steps = [cmd_step("pull", ["docker", "pull", u["image"]], timeout=APPLY_TIMEOUT, safe_on_fail=True)]
    if u.get("apply_command"):
        argv, sh = split_cmd(u["apply_command"], bool(u.get("shell")))
        steps.append(cmd_step("install", argv, shell=sh, timeout=APPLY_TIMEOUT))
    steps.append(Step("verify", lambda: verify_docker(u, b)))
    return steps


_DEFAULT_STEPS = {"git": git_steps, "git_tags": git_tags_steps, "pip": pip_steps, "github_release": release_steps, "docker": docker_steps}


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


GPU_POLL_SECONDS = 2
_sleep = time.sleep  # tests replace these two
_clock = time.monotonic
MAX_GPU_WAIT = 600
JOURNAL_TIMEOUT = 10
JOURNAL_READ_FAILED = "could not read the service journal"
JOURNAL_NEEDS_GROUP = JOURNAL_READ_FAILED + " (add your user to the systemd-journal group)"


def parse_gpu_check(u):
    """(unit, wait_seconds, label) from the update object's "gpu_check", or None if absent.
    Raises ValueError on a mistake in backends.json."""
    gc = u.get("gpu_check")
    if gc is None:
        return None
    if not isinstance(gc, dict) or not re.fullmatch(r"[\w@.:-]+", str(gc.get("unit", ""))):
        raise ValueError('"gpu_check" needs a "unit", like {"unit": "ollama"}')
    try:
        w = gc.get("wait", 60)
        wait = float(w) if not isinstance(w, bool) else float("nan")
    except (TypeError, ValueError):
        wait = float("nan")
    if not math.isfinite(wait):
        raise ValueError('"gpu_check" wait must be a number of seconds')
    unit = str(gc["unit"])
    return unit, min(max(0.0, wait), MAX_GPU_WAIT), str(gc.get("name") or unit[:1].upper() + unit[1:])


def journal_unreadable(rc, out):
    """Why the journal could not be read (a message), or None if it was read."""
    out = out or ""
    if "not seeing messages from other users" in out or "insufficient permissions" in out \
            or "No journal files were opened" in out:
        return JOURNAL_NEEDS_GROUP  # journalctl exits 0 here and prints "-- No entries --"
    if rc != 0:
        return JOURNAL_READ_FAILED
    return None


def gpu_lines(journal):
    """The distinct 'inference compute' lines (one usable GPU each); a restart repeats them, so
    lines with the same id= count once."""
    seen, found = set(), []
    for ln in (journal or "").splitlines():
        if "inference compute" not in ln:
            continue
        m = re.search(r"\bid=(\S+)", ln)
        key = m.group(1) if m else ln
        if key not in seen:
            seen.add(key)
            found.append(ln.strip())
    return found


def nvidia_gpu_count():
    """Number of GPUs `nvidia-smi -L` lists, or None when nvidia-smi is missing or fails."""
    rc, out = run_command(["nvidia-smi", "-L"], timeout=CMD_TIMEOUT)
    if rc != 0:
        return None
    return sum(1 for ln in out.splitlines() if ln.startswith("GPU "))


def gpu_verify(gc, since):
    """Check that the service is up and sees every GPU after an update. Polls against one
    deadline of `wait` seconds. Returns (ok, output, text)."""
    unit, wait, label = gc
    expected = nvidia_gpu_count()
    deadline = _clock() + wait
    journal, found, active = "", [], True
    emit(f"checking that {label} sees its GPUs (nvidia-smi lists {expected if expected is not None else 'unknown'})")
    while True:
        with _quiet():  # the same journal text every 2 seconds would only bury the real output
            rc, state = run_command(["systemctl", "is-active", unit], timeout=JOURNAL_TIMEOUT)
        active = state.strip() == "active"
        if active:
            with _quiet():
                rc, journal = run_command(["journalctl", "-u", unit, "--since", since, "--no-pager"],
                                          timeout=JOURNAL_TIMEOUT)
            why = journal_unreadable(rc, journal)
            if why:
                return False, journal, why
            found = gpu_lines(journal)
            need = max(1, expected or 0)
            emit(f"{label} reports {len(found)} GPU(s) so far")
            if len(found) >= need:
                names = "\n".join(re.sub(r"^.*?(name=)", r"\1", ln) for ln in found)
                return True, f"{label} sees {len(found)} GPU(s) (nvidia-smi lists {expected if expected is not None else 'unknown'}):\n{names}", \
                    f"{len(found)} GPU{'s' if len(found) != 1 else ''} found"
        left = deadline - _clock()
        if left <= 0:
            break
        _sleep(min(GPU_POLL_SECONDS, left))
    if not active:
        return False, f"systemctl is-active {unit}: {state.strip() or '(no answer)'}", "service not running after the update"
    skipped = [ln.strip() for ln in journal.splitlines() if "skipping CUDA device" in ln]
    detail = f"{label} reported these GPUs since {since}:\n" + ("\n".join(re.sub(r"^.*?(name=)", r"\1", ln) for ln in found) or "(none)")
    if skipped:
        detail += "\nSkipped by " + label + ":\n" + "\n".join(skipped)
    if expected is None:
        reason = f"{label} reported no GPU after the update (waited {wait:g} s). The old version may need to be reinstalled."
    else:
        reason = f"{label} sees {len(found)} of {expected} GPUs after the update. The old version may need to be reinstalled."
    return False, detail, reason


def add_gpu_check(steps, u):
    """If the update object has "gpu_check", remember the time just before the install step
    (the installer restarts the service) and run the GPU check after the verify step passes."""
    gc = parse_gpu_check(u)
    if gc is None:
        return steps
    state = {}
    target = next((s for s in steps if s.name == "install"), steps[0])
    first_run = target.run

    def stamped():
        state["since"] = f"@{int(time.time())}"  # epoch form: no time zone to get wrong
        return first_run()
    target.run = stamped
    verify = steps[-1]
    version_check = verify.run

    def verify_with_gpus():
        ok, out, text = version_check()
        if not ok:
            return ok, out, text
        since = state.get("since") or f"@{int(time.time()) - 600}"
        ok2, out2, text2 = gpu_verify(gc, since)
        return ok2, f"{out}\n--- GPU check ---\n{out2}", text2
    verify.run = verify_with_gpus
    return steps


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
    else:
        steps = defaults
    return add_gpu_check(steps, u)


def run_steps(steps, progress=None, out=None):
    """Run steps in order, stop at the first failure. progress(index, state, note) is called
    live with state running, ok or failed. If `out` (an OutputBuffer) is given, each step's
    output streams into it while it runs, after a "== name started ==" line. Returns
    (ok, failed_name, reason, full_output)."""
    log = []
    old_sink = getattr(_ctx, "sink", None)
    _ctx.sink = out
    try:
        for i, step in enumerate(steps):
            if progress:
                progress(i, "running", "")
            if out is not None:
                out.marker(f"== {step.name} started ==")
            try:
                ok, text_out, text = step.run()
            except Exception as e:  # noqa: BLE001
                ok, text_out, text = False, str(e), str(e)[:200] or type(e).__name__
            log.append(f"--- {step.name} ({'ok' if ok else 'failed'}) ---\n{text_out}")
            if out is not None:
                out.marker(f"== {step.name} {'ok' if ok else 'failed'}: {text} ==" if text else f"== {step.name} {'ok' if ok else 'failed'} ==")
            if progress:
                progress(i, "ok" if ok else "failed", text)
            if not ok:
                return False, step.name, text, "\n".join(log)
        return True, None, "", "\n".join(log)
    finally:
        _ctx.sink = old_sink


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
    if kind in ("git", "git_tags"):
        path = os.path.expanduser(u.get("path", ""))
        try:
            ignored = ignore_dirty_paths(u) if kind == "git_tags" else []
        except ValueError as e:
            return f"Bad update settings for {b['name']}: {e}"
        rc, out, changes = blocking_changes(path, ignored)  # untracked files never block
        if rc != 0:
            return f"Could not read git status for {b['name']}: {tail_lines(out, 2)}"
        if changes:
            return f"{b['name']} has local changes in {path}. Commit or stash them first."
    if kind == "git_tags":
        # moving to a tag leaves the current commit behind; refuse if nothing else would keep it
        path = os.path.expanduser(u.get("path", ""))
        rc, out = _git(path, "for-each-ref", "--contains", "HEAD", "--count=1", "--format=%(refname)",
                       "refs/heads", "refs/remotes", "refs/tags")
        if rc != 0:
            return f"Could not check whether the current commit of {b['name']} is held by a branch or tag: {tail_lines(out, 2)}"
        if not out.strip():
            return (f"{b['name']} is on a commit that is not on any branch or tag in {path}. "
                    "Moving to a release would leave it behind. Create a branch for it first.")
    if not custom and kind in ("docker", "github_release") and not u.get("apply_command"):
        return RECREATE_MISSING if kind == "docker" else APPLY_MISSING
    try:
        check_budget(b)
        build_steps(b, {})
    except (ValueError, KeyError) as e:
        return f"Bad update settings for {b['name']}: {e}"
    return None


# ---------- the cache and the background work ----------
def _with_timeout(fn, seconds, message="check timed out"):
    box = {}

    def work():
        try:
            box["r"] = fn()
        except Exception as e:  # noqa: BLE001
            box["r"] = (ERROR, str(e)[:200] or type(e).__name__)

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(seconds)
    return box.get("r") if "r" in box else (ERROR, message)


def progress_text(steps):
    names = {"pending": "pending", "running": "running", "ok": "ok", "failed": "failed"}
    return " · ".join(f"{s['name']} {names[s['state']]}" for s in steps)


class Updater:
    def __init__(self, get_backends, is_running=lambda bid: False, log_writer=None, check_timeout=None):
        self._get = get_backends
        self._is_running = is_running
        self._log = log_writer
        self._timeout = check_timeout  # overrides the kind defaults (tests); None = use them
        self._lock = threading.RLock()
        self._cache = {}  # id -> status, detail, checked_at, meta, steps, incomplete
        self._checking = False
        self._applying = None  # id being updated
        self._out = {}  # id -> OutputBuffer of the latest update (kept until the next one starts)
        self._epoch = 0  # bumped when an update finishes; older check results are dropped
        self._apply_lock = threading.Lock()

    # -- helpers
    def entries(self):
        return {bid: b for bid, b in self._get().items() if isinstance(b.get("update"), dict)}

    def _running(self, bid):
        try:
            return bool(self._is_running(bid))
        except Exception:  # noqa: BLE001
            return True  # fail safe: if we cannot tell, assume it is running

    def _store(self, bid, status, detail, **extra):
        with self._lock:
            old = self._cache.get(bid) or {}
            self._cache[bid] = {"status": status, "detail": detail, "checked_at": time.time(),
                                "meta": extra.get("meta", {}), "steps": extra.get("steps", []),
                                "incomplete": extra.get("incomplete", old.get("incomplete"))}

    def _stop_reason(self, b):
        if not b["update"].get("update_while_running") and self._running(b["id"]):
            return f"Stop {b['name']} first, then update it."
        return None

    @staticmethod
    def _applicable(c):
        return c["status"] in (AVAILABLE, NEWER) or (c["status"] == ERROR and bool(c.get("incomplete")))

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
                    u = b["update"]
                    reason = self._stop_reason(b)
                    if not reason and checking:
                        reason = "A check is running."
                    if not reason and applying:
                        reason = "Another update is running."
                    if not reason and u.get("steps") is None and not u.get("script") and not u.get("apply_command"):
                        if u.get("kind") == "docker":
                            reason = RECREATE_MISSING
                        elif u.get("kind") == "github_release":
                            reason = APPLY_MISSING
                steps = list(c.get("steps") or [])
                items.append({"id": bid, "name": b["name"], "status": c["status"], "detail": c["detail"],
                              "checked_at": c["checked_at"], "running": running,
                              "can_apply": self._applicable(c) and applying != bid and reason is None,
                              "reason": reason, "applying": applying == bid, "steps": steps,
                              "output_tail": self._out[bid].tail() if bid in self._out else "",
                              "output_at": self._out[bid].finished_at if bid in self._out else None,
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
            epoch = self._epoch
            if bid not in self._cache:
                self._cache[bid] = {"status": CHECKING, "detail": "", "checked_at": None, "meta": {}, "steps": []}
        meta = {}
        try:
            budget = check_budget(b, self._timeout)
        except ValueError as e:
            with self._lock:
                if self._applying is None and self._epoch == epoch:
                    self._store(bid, ERROR, f"Bad settings in backends.json: {e}")
            return
        with self._lock:
            before = dict(self._cache.get(bid) or {})
            if b["update"].get("kind") == "docker":  # the pull can take minutes: say so, not "error"
                self._cache[bid] = {**before, "status": CHECKING, "detail": DOWNLOADING}
        status, detail = _with_timeout(lambda: run_check(b, meta, budget), budget + 5, timed_out_text(budget))
        with self._lock:
            if self._applying is not None or self._epoch != epoch:
                if before.get("checked_at") is None:  # never checked: do not leave a "checking" placeholder
                    self._cache[bid] = {**before, "status": UNKNOWN, "detail": "not checked yet"}
                else:
                    self._cache[bid] = before
                return  # an update ran meanwhile; this result may be stale
            old = self._cache.get(bid) or {}
            inc = old.get("incomplete")
            if inc and status == UP_TO_DATE:
                status = ERROR
                detail = f"Last update stopped at {inc['step']}: {inc['reason']}\nFix that, then click Update to run the steps again."
            self._store(bid, status, detail, meta=meta, steps=old.get("steps", []) if inc else [])

    def _claim_check(self, startup):
        """Under the lock: mark a check as running and return the ids to check, or None if a
        check or an update is already going."""
        with self._lock:
            if self._checking or self._applying:
                return None
            self._checking = True
            todo = []
            for bid, b in self.entries().items():
                if startup and b["update"].get("check_at_startup") is False:
                    self._cache.setdefault(bid, {"status": UNKNOWN, "checked_at": None, "meta": {}, "steps": [],
                                                 "detail": "Not checked at startup. Press Check again."})
                    continue
                self._cache.setdefault(bid, {"status": CHECKING, "detail": "", "checked_at": None, "meta": {}, "steps": []})
                todo.append(bid)
            return todo

    def _run_checks(self, todo):
        try:
            for bid in todo:
                self._check_one(bid)
        finally:
            with self._lock:
                self._checking = False

    def check_all(self, startup=False):
        """Check every entry one at a time (blocking). Returns False if a run was already going
        or an update is running. At startup, entries with "check_at_startup": false are skipped."""
        todo = self._claim_check(startup)
        if todo is None:
            return False
        self._run_checks(todo)
        return True

    def check_all_async(self, startup=False):
        """Start a background check. Returns (code, body). The check is claimed under the lock
        before this returns, so an update cannot slip in between."""
        with self._lock:
            if self._applying:
                return 409, {"error": "An update is running. Check again when it finishes."}
            if self._checking:
                return 202, {"ok": True}
            todo = self._claim_check(startup)
            threading.Thread(target=self._run_checks, args=(todo,), daemon=True).start()
        return 202, {"ok": True}

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
        with self._lock:
            if self._checking:
                return 409, {"error": "A check is running, try again in a moment."}
            if not self._apply_lock.acquire(blocking=False):
                return 409, {"error": "Another update is running. Wait for it to finish."}
            self._applying = bid
            self._out[bid] = OutputBuffer()
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
                    run_check(b, meta, check_budget(b, self._timeout))  # read-only; fills in what verify compares with
                meta["_incomplete"] = inc  # lets a step see an earlier unfinished attempt
                steps = build_steps(b, meta)
            except Exception as e:  # noqa: BLE001
                self._store(bid, ERROR, f"Could not start the update: {e}")
                return
            prog = [{"name": s.name, "state": "pending", "note": ""} for s in steps]
            with self._lock:
                self._cache[bid] = {"status": CHECKING, "detail": progress_text(prog), "checked_at": time.time(),
                                    "meta": meta, "steps": prog, "incomplete": inc}
            ok, name, reason, out = run_steps(steps, lambda i, st, note: self._set_progress(bid, prog, i, st, note),
                                              out=self._out.get(bid))
            if self._log:
                try:
                    self._log(bid, f"=== update {time.ctime()} ({'ok' if ok else 'failed at ' + str(name)}) ===\n{out}\n")
                except Exception:  # noqa: BLE001
                    pass
            tail = tail_lines(out)
            if not ok:
                idx = next((i for i, s in enumerate(prog) if s["state"] == "failed"), 0)
                safe = steps[idx].safe_on_fail if idx < len(steps) else False
                if safe:  # nothing changed, so there is nothing half-done to finish later
                    self._store(bid, ERROR, f"{name} failed: {reason}\nNothing was changed. Press Check again to retry.\n{tail}",
                                steps=prog, incomplete=inc)
                else:
                    marker = {"step": name, "reason": reason}
                    if meta.get("prev"):  # what was there before the update, kept across retries
                        marker["prev"] = meta["prev"]
                    self._store(bid, ERROR, f"{name} failed: {reason}\n{tail}", steps=prog, incomplete=marker)
                return
            meta2 = {}
            budget = check_budget(b, self._timeout)
            status, detail = _with_timeout(lambda: run_check(b, meta2, budget), budget + 5, timed_out_text(budget))
            done = f"Updated and verified ({progress_text(prog)})."
            if status == ERROR:  # the update itself passed verify; only the recheck failed
                status = UP_TO_DATE
                done += f" The check afterward failed: {detail}"
                detail = ""
            self._store(bid, status, done + ("\n" + detail if detail else "") + "\n" + tail,
                        meta=meta2, steps=prog, incomplete=None)
        finally:
            if bid in self._out:
                self._out[bid].finish()
            with self._lock:
                self._applying = None
                self._epoch += 1
            self._apply_lock.release()

