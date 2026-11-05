"""Tests for the Updates feature. No network, no real git changes: command execution and the
GitHub lookup are replaced with fakes. Run: python3 -m unittest discover -s tests -v
"""
import json
import socket
import sys
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import switchboard as sb  # noqa: E402
import updates as up  # noqa: E402


class FakeRun:
    """Replacement for updates.run_command. `script` maps a substring of the joined command
    to (rc, output); the first match wins. Every call is recorded."""

    def __init__(self, script=None, default=(0, "")):
        self.script, self.default, self.calls = script or [], default, []

    def __call__(self, argv, cwd=None, timeout=55, shell=False, env=None):
        text = argv if isinstance(argv, str) else " ".join(argv)
        self.calls.append(text)
        for key, res in self.script:
            if key in text:
                return res
        return self.default


class Patched(unittest.TestCase):
    def patch(self, fake=None, fetch=None):
        self._old = (up.run_command, up.fetch_json)
        if fake is not None:
            up.run_command = fake
        if fetch is not None:
            up.fetch_json = fetch
        self.addCleanup(self._restore)

    def _restore(self):
        up.run_command, up.fetch_json = self._old


def backends(**over):
    b = {
        "git": {"id": "git", "name": "GitApp", "kind": "process", "update": {"kind": "git", "path": "/tmp"}},
        "pip": {"id": "pip", "name": "PipApp", "kind": "process", "update": {"kind": "pip", "python": "py", "package": "My_Pkg"}},
        "rel": {"id": "rel", "name": "Rel", "kind": "systemd", "use_pkexec": True,
                "update": {"kind": "github_release", "repo": "a/b", "version_command": "tool --version",
                           "apply_command": "install-tool"}},
        "dock": {"id": "dock", "name": "Dock", "kind": "docker", "container": "c1",
                 "update": {"kind": "docker", "image": "img:main", "apply_command": "recreate"}},
        "plain": {"id": "plain", "name": "Plain", "kind": "process"},
    }
    b.update(over)
    return b


class Helpers(unittest.TestCase):
    def test_parse_version(self):
        self.assertEqual(up.parse_version("ollama version is 0.5.12"), (0, 5, 12))
        self.assertEqual(up.parse_version("v1.2.3-rc1"), (1, 2, 3))
        self.assertIsNone(up.parse_version("no digits here"))
        self.assertIsNone(up.parse_version("7"))

    def test_behind_count(self):
        self.assertEqual(up.parse_behind("3\n"), 3)
        self.assertEqual(up.parse_behind("warning\n0"), 0)
        self.assertIsNone(up.parse_behind("fatal: bad"))

    def test_pip_outdated(self):
        text = 'WARNING: something\n[{"name": "my-pkg", "version": "1.0.0", "latest_version": "1.2.0"}]'
        self.assertEqual(up.parse_pip_outdated(text, "My_Pkg"), ("1.0.0", "1.2.0"))
        self.assertIsNone(up.parse_pip_outdated("[]", "x"))
        with self.assertRaises(ValueError):
            up.parse_pip_outdated("boom", "x")

    def test_pip_index(self):
        text = "foo (2.0.1)\nAvailable versions: 2.0.1, 2.0.0\n  INSTALLED: 2.0.0\n  LATEST:    2.0.1"
        self.assertEqual(up.parse_pip_index(text), ("2.0.0", "2.0.1"))

    def test_decisions(self):
        self.assertEqual(up.decide_git(2)[0], up.AVAILABLE)
        self.assertEqual(up.decide_git(0)[0], up.UP_TO_DATE)
        self.assertEqual(up.decide_git(None)[0], up.UNKNOWN)
        self.assertEqual(up.decide_version((0, 5, 1), (0, 5, 2))[0], up.AVAILABLE)
        self.assertEqual(up.decide_version((0, 5, 2), (0, 5, 2))[0], up.UP_TO_DATE)
        self.assertEqual(up.decide_version((0, 6), (0, 5, 2))[0], up.UP_TO_DATE)
        self.assertEqual(up.decide_version(None, (1, 0))[0], up.UNKNOWN)
        self.assertEqual(up.decide_image("sha:b", "sha:a")[0], up.AVAILABLE)
        self.assertEqual(up.decide_image("sha:a", "sha:a")[0], up.UP_TO_DATE)
        self.assertEqual(up.decide_image("sha:a", "")[0], up.UNKNOWN)

    def test_tail(self):
        text = "\n".join(str(i) for i in range(100))
        self.assertEqual(len(up.tail_lines(text).splitlines()), 40)
        self.assertTrue(up.tail_lines(text).endswith("99"))


class Checks(Patched):
    def test_git_behind(self):
        self.patch(FakeRun([("rev-parse --is-inside", (0, "true")), ("rev-list", (0, "4"))]))
        self.assertEqual(up.run_check(backends()["git"]), (up.AVAILABLE, "4 commits behind"))

    def test_git_not_a_checkout_is_no_source(self):
        self.patch(FakeRun([("rev-parse --is-inside", (128, "fatal: not a git repository"))]))
        self.assertEqual(up.run_check(backends()["git"])[0], up.NO_SOURCE)

    def test_git_missing_folder_is_no_source(self):
        b = backends()["git"]
        b["update"]["path"] = "/nonexistent/folder/x"
        self.patch(FakeRun())
        self.assertEqual(up.run_check(b)[0], up.NO_SOURCE)

    def test_git_fetch_failure_is_error(self):
        self.patch(FakeRun([("rev-parse --is-inside", (0, "true")), ("fetch", (1, "network down"))]))
        self.assertEqual(up.run_check(backends()["git"])[0], up.ERROR)

    def test_pip_available_and_current(self):
        out = '[{"name": "my-pkg", "version": "1.0", "latest_version": "1.1"}]'
        self.patch(FakeRun([("list --outdated", (0, out))]))
        self.assertEqual(up.run_check(backends()["pip"])[0], up.AVAILABLE)
        self.patch(FakeRun([("list --outdated", (0, "[]"))]))
        self.assertEqual(up.run_check(backends()["pip"])[0], up.UP_TO_DATE)

    def test_release_check_uses_installed_vs_tag(self):
        self.patch(FakeRun([("tool --version", (0, "tool version is 0.5.1"))]), lambda url: {"tag_name": "v0.5.2"})
        self.assertEqual(up.run_check(backends()["rel"])[0], up.AVAILABLE)
        self.patch(FakeRun([("tool --version", (0, "tool version is 0.5.2"))]), lambda url: {"tag_name": "v0.5.2"})
        self.assertEqual(up.run_check(backends()["rel"])[0], up.UP_TO_DATE)

    def test_network_exception_becomes_error(self):
        def boom(url):
            raise urllib.error.URLError("offline")
        self.patch(FakeRun([("tool --version", (0, "0.1.0"))]), boom)
        self.assertEqual(up.run_check(backends()["rel"])[0], up.ERROR)

    def test_docker_compares_image_ids(self):
        self.patch(FakeRun([("docker pull", (0, "ok")), ("image inspect", (0, "sha256:new")),
                            ("docker inspect", (0, "sha256:old"))]))
        self.assertEqual(up.run_check(backends()["dock"])[0], up.AVAILABLE)

    def test_hung_check_times_out(self):
        gate = threading.Event()
        self.addCleanup(gate.set)

        def slow(b):
            gate.wait(5)
            return up.UP_TO_DATE, "late"
        self.assertEqual(up._with_timeout(lambda: slow({}), 0.05), (up.ERROR, "check timed out"))


class StepRunner(Patched):
    def steps(self, *specs):
        return [up.Step(n, lambda r=r: r) for n, r in specs]

    def test_success_path_reports_each_step(self):
        seen = []
        ok, name, reason, out = up.run_steps(
            self.steps(("pull", (True, "a", "pulled")), ("install", (True, "b", "built")), ("verify", (True, "c", "v2"))),
            lambda i, st, note: seen.append((i, st)))
        self.assertTrue(ok)
        self.assertEqual(seen, [(0, "running"), (0, "ok"), (1, "running"), (1, "ok"), (2, "running"), (2, "ok")])

    def test_failure_stops_later_steps(self):
        ran = []
        steps = [up.Step("pull", lambda: (True, "", "")),
                 up.Step("install", lambda: (False, "make: error", "exit 2: make: error")),
                 up.Step("verify", lambda: ran.append("verify") or (True, "", ""))]
        ok, name, reason, out = up.run_steps(steps)
        self.assertFalse(ok)
        self.assertEqual(name, "install")
        self.assertIn("exit 2", reason)
        self.assertEqual(ran, [])

    def test_exception_in_a_step_is_a_failure(self):
        def boom():
            raise RuntimeError("kaput")
        ok, name, reason, _ = up.run_steps([up.Step("pull", boom)])
        self.assertFalse(ok)
        self.assertIn("kaput", reason)

    def test_timeout_is_reported(self):
        self.patch(FakeRun(default=(124, "command timed out")))
        ok, name, reason, _ = up.run_steps([up.cmd_step("install", ["slow"], timeout=7)])
        self.assertFalse(ok)
        self.assertEqual(reason, "timed out after 7 s")

    def test_pip_verify_mismatch_is_failure(self):
        self.patch(FakeRun([("pip show", (0, "Name: x\nVersion: 1.0.0"))]))
        ok, _, reason = up.verify_pip("py", "x", {"latest": (1, 2, 0), "installed": (1, 0, 0)})
        self.assertFalse(ok)
        self.assertIn("expected 1.2.0, found 1.0.0", reason)
        ok, _, _ = up.verify_pip("py", "x", {"latest": (1, 0, 0)})
        self.assertTrue(ok)

    def test_release_verify_mismatch_is_failure(self):
        self.patch(FakeRun([("tool --version", (0, "tool 0.5.1"))]))
        ok, _, reason = up.verify_release(backends()["rel"]["update"], {"latest": (0, 5, 2)})
        self.assertFalse(ok)
        self.assertIn("expected 0.5.2", reason)

    def test_git_verify_needs_head_at_upstream(self):
        self.patch(FakeRun([("rev-parse HEAD", (0, "aaaaaaaa1")), ("rev-parse @{u}", (0, "bbbbbbbb2"))]))
        ok, _, reason = up.verify_git("/tmp", {"kind": "git", "path": "/tmp"})
        self.assertFalse(ok)
        self.assertIn("not at upstream", reason)
        self.patch(FakeRun([("rev-parse", (0, "same1234")), ("log -1", (0, "same123 msg"))]))
        self.assertTrue(up.verify_git("/tmp", {"kind": "git", "path": "/tmp"})[0])

    def test_docker_verify_container_must_use_new_image(self):
        b = backends()["dock"]
        self.patch(FakeRun([("image inspect", (0, "sha:new")), ("docker inspect", (0, "sha:old"))]))
        self.assertFalse(up.verify_docker(b["update"], b)[0])
        self.patch(FakeRun([("image inspect", (0, "sha:new")), ("docker inspect", (0, "sha:new"))]))
        self.assertTrue(up.verify_docker(b["update"], b)[0])

    def test_custom_steps_override_and_still_end_with_verify(self):
        b = backends()["git"]
        b["update"]["steps"] = [{"name": "pull", "command": "./go.sh", "cwd": "/tmp", "timeout": 5}]
        names = [s.name for s in up.build_steps(b, {})]
        self.assertEqual(names, ["pull", "verify"])
        b["update"] = {"kind": "git", "path": "/tmp", "script": "/opt/up.sh"}
        self.assertEqual([s.name for s in up.build_steps(b, {})], ["install", "verify"])

    def test_bad_custom_steps_are_rejected(self):
        b = backends()["git"]
        for bad in ([{"name": "explode", "command": "x"}], [{"name": "pull"}], []):
            b["update"]["steps"] = bad
            with self.assertRaises(ValueError):
                up.build_steps(b, {})

    def test_default_steps_per_kind(self):
        bk = backends()
        self.assertEqual([s.name for s in up.build_steps(bk["git"], {})], ["pull", "verify"])
        bk["git"]["update"]["post_command"] = "make"
        self.assertEqual([s.name for s in up.build_steps(bk["git"], {})], ["pull", "install", "verify"])
        self.assertEqual([s.name for s in up.build_steps(bk["pip"], {})], ["pull", "verify"])
        self.assertEqual([s.name for s in up.build_steps(bk["rel"], {})], ["install", "verify"])
        self.assertEqual([s.name for s in up.build_steps(bk["dock"], {})], ["pull", "install", "verify"])


class UpdaterTests(Patched):
    def make(self, running=(), bk=None, log=None):
        bk = bk or backends()
        return up.Updater(lambda: bk, lambda bid: bid in running, log)

    def seed(self, u, bid, status=up.AVAILABLE, meta=None):
        u._store(bid, status, "x", meta=meta or {})

    def test_entries_without_update_object_are_not_listed(self):
        u = self.make()
        self.assertNotIn("plain", [i["id"] for i in u.snapshot()["items"]])

    def test_apply_refuses_while_running_for_git_pip_docker(self):
        for bid in ("git", "pip", "dock"):
            fake = FakeRun()
            self.patch(fake)
            u = self.make(running=(bid,))
            self.seed(u, bid)
            code, obj = u.apply(bid, background=False)
            self.assertEqual(code, 409)
            self.assertEqual(obj["error"], f"Stop {backends()[bid]['name']} first, then update it.")
            self.assertEqual(fake.calls, [])  # nothing ran

    def test_ollama_style_release_may_apply_while_running(self):
        fake = FakeRun([("tool --version", (0, "tool 0.5.2")), ("install-tool", (0, "installed"))])
        self.patch(fake, lambda url: {"tag_name": "0.5.2"})
        u = self.make(running=("rel",))
        self.seed(u, "rel", meta={"latest": (0, 5, 2)})
        self.assertEqual(u.apply("rel", background=False)[0], 202)
        item = next(i for i in u.snapshot()["items"] if i["id"] == "rel")
        self.assertEqual(item["status"], up.UP_TO_DATE)
        self.assertTrue(any("pkexec" in c and "install-tool" in c for c in fake.calls))

    def test_unknown_id_is_rejected(self):
        u = self.make()
        self.assertEqual(u.apply("nope", background=False)[0], 404)
        self.assertEqual(u.apply("plain", background=False)[0], 404)

    def test_apply_needs_an_available_update(self):
        u = self.make()
        self.seed(u, "git", status=up.UP_TO_DATE)
        self.assertEqual(u.apply("git", background=False)[0], 409)

    def test_dirty_git_tree_is_refused(self):
        self.patch(FakeRun([("status --porcelain", (0, " M file.py"))]))
        u = self.make()
        self.seed(u, "git")
        code, obj = u.apply("git", background=False)
        self.assertEqual(code, 409)
        self.assertIn("local changes", obj["error"])

    def test_docker_without_recreate_command_does_nothing(self):
        bk = backends()
        del bk["dock"]["update"]["apply_command"]
        fake = FakeRun()
        self.patch(fake)
        u = self.make(bk=bk)
        self.seed(u, "dock")
        code, obj = u.apply("dock", background=False)
        self.assertEqual((code, obj["error"]), (409, "no recreate command set in backends.json"))
        self.assertEqual(fake.calls, [])

    def test_only_one_apply_at_a_time(self):
        u = self.make()
        self.seed(u, "pip")
        u._apply_lock.acquire()
        self.addCleanup(u._apply_lock.release)
        self.patch(FakeRun())
        code, obj = u.apply("pip", background=False)
        self.assertEqual(code, 409)
        self.assertIn("Another update", obj["error"])

    def test_full_pip_success_runs_pull_then_verify_and_logs(self):
        logs = []
        fake = FakeRun([("pip show", (0, "Version: 1.2.0")), ("list --outdated", (0, "[]")),
                        ("install --upgrade", (0, "Successfully installed"))])
        self.patch(fake)
        u = self.make(log=lambda bid, text: logs.append((bid, text)))
        self.seed(u, "pip", meta={"installed": (1, 0), "latest": (1, 2, 0)})
        self.assertEqual(u.apply("pip", background=False)[0], 202)
        item = next(i for i in u.snapshot()["items"] if i["id"] == "pip")
        self.assertEqual(item["status"], up.UP_TO_DATE)
        self.assertIn("pull ok", item["progress"])
        self.assertIn("verify ok", item["progress"])
        self.assertEqual(logs[0][0], "pip")
        self.assertLess(fake.calls.index(next(c for c in fake.calls if "install --upgrade" in c)),
                        fake.calls.index(next(c for c in fake.calls if "pip show" in c)))

    def test_verify_mismatch_marks_error_and_never_claims_success(self):
        fake = FakeRun([("pip show", (0, "Version: 1.0.0")), ("install --upgrade", (0, "ok"))])
        self.patch(fake)
        u = self.make()
        self.seed(u, "pip", meta={"installed": (1, 0), "latest": (1, 2, 0)})
        u.apply("pip", background=False)
        item = next(i for i in u.snapshot()["items"] if i["id"] == "pip")
        self.assertEqual(item["status"], up.ERROR)
        self.assertIn("verify failed", item["detail"])
        self.assertIn("verify failed", item["progress"])
        self.assertTrue(item["can_apply"])  # incomplete: the pipeline can be run again

    def test_failed_pull_stops_and_nothing_else_runs(self):
        fake = FakeRun([("pull --ff-only", (1, "fatal: Not possible to fast-forward"))])
        self.patch(fake)
        u = self.make()
        self.seed(u, "git", meta={})
        u.apply("git", background=False)
        item = next(i for i in u.snapshot()["items"] if i["id"] == "git")
        self.assertEqual(item["status"], up.ERROR)
        self.assertTrue(item["detail"].startswith("pull failed"))
        self.assertFalse(any("rev-parse HEAD" in c for c in fake.calls))
        self.assertFalse(item["can_apply"] and "incomplete" in item["detail"])

    def test_incomplete_update_is_not_reported_up_to_date_by_a_recheck(self):
        fake = FakeRun([("pip show", (0, "Version: 1.0.0")), ("install --upgrade", (0, "ok")),
                        ("list --outdated", (0, "[]"))])
        self.patch(fake)
        u = self.make()
        self.seed(u, "pip", meta={"installed": (1, 0), "latest": (1, 2, 0)})
        u.apply("pip", background=False)
        u._check_one("pip")
        item = next(i for i in u.snapshot()["items"] if i["id"] == "pip")
        self.assertEqual(item["status"], up.ERROR)
        self.assertIn("Last update stopped at verify", item["detail"])

    def test_check_all_one_at_a_time_and_caches(self):
        order = []

        def fake(argv, **kw):
            order.append(argv[0] if isinstance(argv, list) else argv)
            return 0, ""
        self.patch(fake, lambda url: {"tag_name": "1.0.0"})
        u = self.make()
        self.assertTrue(u.check_all())
        snap = u.snapshot()
        self.assertFalse(snap["checking"])
        self.assertTrue(all(i["checked_at"] for i in snap["items"]))


class HttpApi(Patched):
    def test_request_body_commands_are_never_executed(self):
        fake = FakeRun()
        self.patch(fake)
        bk = backends()
        u = up.Updater(lambda: bk, lambda bid: False)
        u._store("pip", up.UP_TO_DATE, "x")  # not applicable, so nothing may run
        old = sb.UPDATER
        sb.UPDATER = u
        self.addCleanup(lambda: setattr(sb, "UPDATER", old))
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        srv = ThreadingHTTPServer(("127.0.0.1", port), sb.Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)

        def post(path, body=None):
            req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body or {}).encode(),
                                         method="POST", headers={"Content-Type": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

        evil = {"command": "touch /tmp/pwned", "apply_command": "rm -rf /", "shell": True, "steps": [{"name": "pull", "command": "id"}]}
        self.assertEqual(post("/api/updates/apply/nope", evil)[0], 404)
        self.assertEqual(post("/api/updates/apply/pip", evil)[0], 409)
        self.assertEqual(post("/api/updates/apply/plain", evil)[0], 404)
        self.assertEqual(fake.calls, [])
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/updates", timeout=5) as r:
            data = json.loads(r.read())
        self.assertEqual(set(data), {"checking", "applying", "items"})
        self.assertEqual({i["id"] for i in data["items"]}, {"git", "pip", "rel", "dock"})
        for key in ("id", "name", "status", "detail", "checked_at", "running", "can_apply"):
            self.assertIn(key, data["items"][0])


if __name__ == "__main__":
    unittest.main()
