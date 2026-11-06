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


ORIGINALS = (up.run_command, up.fetch_json)


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
    def setUp(self):
        self.addCleanup(self._restore)

    def patch(self, fake=None, fetch=None):
        if fake is not None:
            up.run_command = fake
        if fetch is not None:
            up.fetch_json = fetch
        self.addCleanup(self._restore)

    def _restore(self):
        up.run_command, up.fetch_json = ORIGINALS


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

    def test_docker_check_container_errors_other_than_missing_are_errors(self):
        self.patch(FakeRun([("docker pull", (0, "ok")), ("image inspect", (0, "sha256:new")),
                            ("docker inspect", (1, "Cannot connect to the Docker daemon"))]))
        self.assertEqual(up.run_check(backends()["dock"])[0], up.ERROR)
        self.patch(FakeRun([("docker pull", (0, "ok")), ("image inspect", (0, "sha256:new")),
                            ("docker inspect", (1, "Error: No such object: c1"))]))
        self.assertEqual(up.run_check(backends()["dock"])[0], up.UNKNOWN)

    def test_all_commands_of_a_check_share_one_deadline(self):
        calls = []

        def slow(argv, **kw):
            calls.append(kw.get("timeout"))
            return 124, "command timed out"
        self.patch(slow)
        status, detail = up.run_check(backends()["pip"], budget=30)
        self.assertEqual(status, up.ERROR)
        # every command got at most what was left of the 30 s budget
        self.assertTrue(all(t is not None and t <= 30 for t in calls))
        # an already-spent budget runs nothing at all
        calls.clear()
        self.assertEqual(up.run_check(backends()["pip"], budget=0),
                         (up.ERROR, "check timed out after 0 seconds. Raise check_timeout in backends.json."))
        self.assertEqual(calls, [])

    def test_hung_check_times_out(self):
        gate = threading.Event()
        self.addCleanup(gate.set)

        def slow(b):
            gate.wait(5)
            return up.UP_TO_DATE, "late"
        self.assertEqual(up._with_timeout(lambda: slow({}), 0.05), (up.ERROR, "check timed out"))


class CheckBudget(Patched):
    def test_defaults_and_per_entry_override(self):
        bk = backends()
        self.assertEqual(up.check_budget(bk["git"]), 60)
        self.assertEqual(up.check_budget(bk["pip"]), 60)
        self.assertEqual(up.check_budget(bk["rel"]), 60)
        self.assertEqual(up.check_budget(bk["dock"]), 1800)
        bk["dock"]["update"]["check_timeout"] = 3600
        bk["git"]["update"]["check_timeout"] = 5
        self.assertEqual(up.check_budget(bk["dock"]), 3600)
        self.assertEqual(up.check_budget(bk["git"]), 5)
        bk["git"]["update"]["check_timeout"] = "junk"
        self.assertEqual(up.check_budget(bk["git"]), 60)

    def test_the_entrys_budget_is_the_shared_deadline(self):
        seen = []

        def fake(argv, **kw):
            seen.append(kw.get("timeout"))
            return 0, "sha256:x"
        self.patch(fake)
        bk = backends()
        up.run_check(bk["dock"], budget=up.check_budget(bk["dock"]))
        self.assertTrue(all(t <= 1800 for t in seen), seen)
        self.assertTrue(any(t > 1700 for t in seen), seen)  # the pull may use the whole budget

    def test_docker_row_says_downloading_and_other_rows_finish_meanwhile(self):
        import time
        in_pull, gate = threading.Event(), threading.Event()
        self.addCleanup(gate.set)

        def fake(argv, **kw):
            text = " ".join(argv)
            if "docker pull" in text:
                in_pull.set()
                gate.wait(10)
            return 0, ""
        self.patch(fake, lambda url: {"tag_name": "1.0.0"})
        u = up.Updater(lambda: backends(), lambda bid: False)
        self.assertEqual(u.check_all_async()[0], 202)
        self.assertTrue(in_pull.wait(5))
        t0 = time.monotonic()
        snap = u.snapshot()
        self.assertLess(time.monotonic() - t0, 1)  # the panel is not blocked by the pull
        rows = {i["id"]: i for i in snap["items"]}
        self.assertTrue(snap["checking"])
        for bid in ("git", "pip", "rel"):
            self.assertIsNotNone(rows[bid]["checked_at"], bid)
            self.assertNotEqual(rows[bid]["status"], up.CHECKING, bid)
        self.assertEqual(rows["dock"]["status"], up.CHECKING)
        self.assertEqual(rows["dock"]["detail"], "Downloading the image: this can take several minutes.")
        gate.set()
        for _ in range(100):
            if not u.snapshot()["checking"]:
                break
            time.sleep(0.05)
        done = next(i for i in u.snapshot()["items"] if i["id"] == "dock")
        self.assertNotEqual(done["status"], up.CHECKING)
        self.assertNotEqual(done["detail"], up.DOWNLOADING)

    def test_a_real_docker_timeout_is_an_error_with_advice(self):
        bk = backends()
        bk["dock"]["update"]["check_timeout"] = 0.05
        gate = threading.Event()
        self.addCleanup(gate.set)

        def fake(argv, **kw):
            gate.wait(2)
            return 124, "command timed out"
        self.patch(fake)
        u = up.Updater(lambda: bk, lambda bid: False)
        u._check_one("dock")
        row = next(i for i in u.snapshot()["items"] if i["id"] == "dock")
        self.assertEqual(row["status"], up.ERROR)
        self.assertEqual(row["detail"], "check timed out after 0.05 seconds. Raise check_timeout in backends.json.")


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

    def test_docker_verify_no_container_only_passes_for_no_such_object(self):
        b = backends()["dock"]
        self.patch(FakeRun([("image inspect", (0, "sha:new")), ("docker inspect", (1, "Error: No such object: c1"))]))
        self.assertTrue(up.verify_docker(b["update"], b)[0])
        self.patch(FakeRun([("image inspect", (0, "sha:new")), ("docker inspect", (1, "permission denied"))]))
        ok, _, reason = up.verify_docker(b["update"], b)
        self.assertFalse(ok)
        self.assertIn("could not inspect", reason)

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

    def test_release_refused_while_running_without_the_flag(self):
        fake = FakeRun()
        self.patch(fake)
        u = self.make(running=("rel",))
        self.seed(u, "rel", meta={"latest": (0, 5, 2)})
        code, obj = u.apply("rel", background=False)
        self.assertEqual((code, obj["error"]), (409, "Stop Rel first, then update it."))
        self.assertEqual(fake.calls, [])

    def test_update_while_running_flag_allows_apply_while_running(self):
        fake = FakeRun([("tool --version", (0, "tool 0.5.2")), ("install-tool", (0, "installed"))])
        self.patch(fake, lambda url: {"tag_name": "0.5.2"})
        bk = backends()
        bk["rel"]["update"]["update_while_running"] = True
        u = self.make(running=("rel",), bk=bk)
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
        self.assertEqual((code, obj["error"]), (409, "No recreate command set in backends.json."))
        snap = next(i for i in u.snapshot()["items"] if i["id"] == "dock")
        self.assertEqual(snap["reason"], "No recreate command set in backends.json.")
        self.assertFalse(snap["can_apply"])
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
        self.assertFalse(item["can_apply"])  # nothing changed, so there is nothing to finish
        self.assertIn("Press Check again to retry.", item["detail"])

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
        active = []
        overlap = []

        def fake(argv, **kw):
            text = argv if isinstance(argv, str) else " ".join(argv)
            kind = ("rel" if "tool --version" in text else "pip" if "pip" in text
                    else "dock" if "docker" in text else "git")
            if not order or order[-1] != kind:
                order.append(kind)
            active.append(kind)
            if len(set(active)) > 1:
                overlap.append(kind)
            active.clear()
            return 0, ""
        self.patch(fake, lambda url: {"tag_name": "1.0.0"})
        u = self.make()
        self.assertTrue(u.check_all())
        self.assertEqual(order, ["git", "pip", "rel", "dock"])  # one entry at a time, in file order
        self.assertEqual(overlap, [])
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
        u._store("git", up.AVAILABLE, "x")  # applicable, but running, so nothing may run
        u._is_running = lambda bid: True
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
                                         method="POST", headers={"Content-Type": "application/json", "X-Switchboard": "1"})
            try:
                with urllib.request.urlopen(req, timeout=5) as r:
                    return r.status, json.loads(r.read())
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read())

        evil = {"command": "touch /tmp/pwned", "apply_command": "rm -rf /", "shell": True, "steps": [{"name": "pull", "command": "id"}]}
        self.assertEqual(post("/api/updates/apply/nope", evil)[0], 404)
        self.assertEqual(post("/api/updates/apply/pip", evil)[0], 409)
        self.assertEqual(post("/api/updates/apply/git", evil)[0], 409)
        self.assertEqual(post("/api/updates/apply/plain", evil)[0], 404)
        self.assertEqual(fake.calls, [])
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/updates", timeout=5) as r:
            data = json.loads(r.read())
        self.assertEqual(set(data), {"checking", "applying", "items"})
        self.assertEqual({i["id"] for i in data["items"]}, {"git", "pip", "rel", "dock"})
        for key in ("id", "name", "status", "detail", "checked_at", "running", "can_apply"):
            self.assertIn(key, data["items"][0])


def alive(pid):
    """Is the process still running (a zombie that only waits to be reaped does not count)?"""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return False
    return stat.rsplit(")", 1)[1].split()[0] != "Z"


class RealProcessGroup(unittest.TestCase):
    def test_timeout_kills_the_whole_process_group_and_returns_promptly(self):
        import tempfile
        import time
        with tempfile.TemporaryDirectory() as d:
            pidfile = Path(d) / "pid"
            t0 = time.monotonic()
            rc, out = up.run_command(["sh", "-c", f"sleep 30 & echo $! > {pidfile}; sleep 30"], timeout=1)
            took = time.monotonic() - t0
            self.assertEqual(rc, 124)
            self.assertEqual(out, "command timed out")
            self.assertLess(took, 8)
            pid = int(pidfile.read_text())
            for _ in range(40):
                if not alive(pid):
                    break
                time.sleep(0.1)
            self.assertFalse(alive(pid), "the background child survived the timeout")

    def test_leader_exiting_normally_is_not_a_timeout_even_if_a_child_keeps_running(self):
        import tempfile
        import time
        with tempfile.TemporaryDirectory() as d:
            pidfile = Path(d) / "pid"
            t0 = time.monotonic()
            rc, out = up.run_command(["sh", "-c", f"sleep 38 & echo $! > {pidfile}; echo started; exit 0"], timeout=10)
            took = time.monotonic() - t0
            pid = int(pidfile.read_text())
            try:
                self.assertEqual(rc, 0)
                self.assertEqual(out, "started")
                self.assertLess(took, 5)
                self.assertTrue(alive(pid), "the group must not be killed when the leader exits normally")
            finally:
                import os
                import signal
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass

    def test_normal_command_returns_output(self):
        rc, out = up.run_command(["sh", "-c", "echo hi; echo err 1>&2; exit 3"])
        self.assertEqual(rc, 3)
        self.assertIn("hi", out)
        self.assertIn("err", out)

    def test_missing_program_is_a_result_not_an_exception(self):
        rc, out = up.run_command(["definitely-not-a-program-xyz"])
        self.assertEqual(rc, 127)


class RealGit(unittest.TestCase):
    """Uses real git on repositories made in a temp dir. No network (the remote is a local path)."""

    def setUp(self):
        import shutil
        import subprocess
        import tempfile
        if not shutil.which("git"):
            self.skipTest("git not installed")
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
                    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
                    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
        import os
        from unittest import mock
        self._sub = subprocess
        hermetic = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_TERMINAL_PROMPT": "0"}
        patcher = mock.patch.dict(os.environ, {**self.env, **hermetic})
        patcher.start()  # also applies to the git commands updates.py runs
        self.addCleanup(patcher.stop)
        self.environ = dict(os.environ)
        self.remote = self.dir / "remote.git"
        self.g(self.dir, "init", "-q", "--bare", str(self.remote))
        self.g(self.dir, "symbolic-ref", "HEAD", "refs/heads/main", cwd_git=self.remote)
        self.app = self.clone("app")
        self.commit(self.app, "a.txt", "one")
        self.g(self.app, "push", "-q", "origin", "HEAD:main")
        self.g(self.app, "branch", "--set-upstream-to=origin/main")
        self.other = self.clone("other")

    def g(self, cwd, *args, cwd_git=None):
        r = self._sub.run(["git", *args] if cwd_git is None else ["git", "-C", str(cwd_git), *args],
                          cwd=str(cwd), env=self.environ, capture_output=True, text=True)
        if r.returncode != 0:
            self.fail(f"git {args} failed: {r.stderr}")
        return r.stdout.strip()

    def clone(self, name):
        path = self.dir / name
        self.g(self.dir, "init", "-q", str(path))
        self.g(path, "checkout", "-q", "-b", "main")
        self.g(path, "remote", "add", "origin", str(self.remote))
        return path

    def commit(self, repo, name, text):
        (repo / name).write_text(text)
        self.g(repo, "add", name)
        self.g(repo, "commit", "-q", "-m", f"change {name} {text}")

    def updater(self):
        bk = {"app": {"id": "app", "name": "App", "kind": "process",
                      "update": {"kind": "git", "path": str(self.app)}}}
        return up.Updater(lambda: bk, lambda bid: False)

    def publish_from_other(self, name="b.txt", text="two"):
        self.g(self.other, "pull", "-q", "origin", "main")
        self.commit(self.other, name, text)
        self.g(self.other, "push", "-q", "origin", "HEAD:main")

    def snap(self, u):
        return u.snapshot()["items"][0]

    def test_clean_fast_forward_succeeds(self):
        self.publish_from_other()
        u = self.updater()
        u.check_all()
        self.assertEqual(self.snap(u)["status"], up.AVAILABLE)
        self.assertEqual(self.snap(u)["detail"], "1 commit behind")
        self.assertEqual(u.apply("app", background=False)[0], 202)
        item = self.snap(u)
        self.assertEqual(item["status"], up.UP_TO_DATE)
        self.assertIn("verify ok", item["progress"])
        self.assertEqual(self.g(self.app, "rev-parse", "HEAD"), self.g(self.remote, "rev-parse", "main"))

    def test_diverged_history_fails_and_leaves_head_unchanged(self):
        self.commit(self.app, "local.txt", "mine")
        self.publish_from_other()
        before = self.g(self.app, "rev-parse", "HEAD")
        u = self.updater()
        u.check_all()
        self.assertEqual(self.snap(u)["status"], up.AVAILABLE)
        self.assertEqual(u.apply("app", background=False)[0], 202)
        item = self.snap(u)
        self.assertEqual(item["status"], up.ERROR)
        self.assertIn("pull failed", item["detail"])
        self.assertIn("Press Check again to retry.", item["detail"])
        self.assertFalse(item["can_apply"])
        self.assertEqual(self.g(self.app, "rev-parse", "HEAD"), before)

    def test_dirty_tree_is_refused_and_nothing_changes(self):
        self.publish_from_other()
        u = self.updater()
        u.check_all()
        (self.app / "a.txt").write_text("edited by hand")
        before = self.g(self.app, "rev-parse", "HEAD")
        code, obj = u.apply("app", background=False)
        self.assertEqual(code, 409)
        self.assertIn("local changes", obj["error"])
        self.assertEqual(self.g(self.app, "rev-parse", "HEAD"), before)

    def test_not_a_checkout_is_no_update_source(self):
        plain = self.dir / "plain"
        plain.mkdir()
        self.assertEqual(up.check_git({"kind": "git", "path": str(plain)})[0], up.NO_SOURCE)


class SafetyRules(Patched):
    def test_async_check_is_claimed_before_it_returns(self):
        gate = threading.Event()
        self.addCleanup(gate.set)

        def blocked(argv, **kw):
            gate.wait(5)
            return 0, ""
        self.patch(blocked, lambda url: {"tag_name": "1.0.0"})
        u = self.make()
        u._store("git", up.AVAILABLE, "x")
        self.assertEqual(u.check_all_async()[0], 202)
        self.assertTrue(u._checking)  # claimed under the lock, before the thread got going
        code, obj = u.apply("git", background=False)
        self.assertEqual((code, obj["error"]), (409, "A check is running, try again in a moment."))
        gate.set()

    def make(self, **kw):
        return up.Updater(lambda: backends(), kw.get("is_running", lambda bid: False))

    def test_unreadable_running_state_counts_as_running(self):
        def boom(bid):
            raise RuntimeError("docker is down")
        fake = FakeRun()
        self.patch(fake)
        u = self.make(is_running=boom)
        u._store("git", up.AVAILABLE, "x")
        code, obj = u.apply("git", background=False)
        self.assertEqual(code, 409)
        self.assertIn("Stop GitApp first", obj["error"])
        self.assertTrue(next(i for i in u.snapshot()["items"] if i["id"] == "git")["running"])
        self.assertEqual(fake.calls, [])

    def test_apply_refused_while_a_check_is_running(self):
        fake = FakeRun()
        self.patch(fake)
        u = self.make()
        u._store("pip", up.AVAILABLE, "x")
        u._checking = True
        code, obj = u.apply("pip", background=False)
        self.assertEqual((code, obj["error"]), (409, "A check is running, try again in a moment."))
        item = next(i for i in u.snapshot()["items"] if i["id"] == "pip")
        self.assertFalse(item["can_apply"])
        self.assertEqual(fake.calls, [])

    def test_check_refused_while_an_update_is_running(self):
        u = self.make()
        u._applying = "pip"
        self.assertFalse(u.check_all())
        self.assertEqual(u.check_all_async()[0], 409)

    def test_a_check_that_straddles_an_update_is_not_stored(self):
        u = self.make()
        u._store("git", up.AVAILABLE, "before")
        real = up.run_check

        def straddling(b, meta=None, budget=60):
            u._epoch += 1  # an update finished while this check was running
            return up.UP_TO_DATE, "stale"
        up.run_check = straddling
        self.addCleanup(setattr, up, "run_check", real)
        u._check_one("git")
        self.assertEqual(u._cache["git"]["detail"], "before")

    def test_failure_after_the_pull_keeps_the_update_open_but_a_failed_pull_does_not(self):
        bk = backends()
        bk["git"]["update"]["post_command"] = "make"
        fake = FakeRun([("pull --ff-only", (0, "ok")), ("make", (2, "boom"))])
        self.patch(fake)
        u = up.Updater(lambda: bk, lambda bid: False)
        u._store("git", up.AVAILABLE, "x")
        u.apply("git", background=False)
        item = next(i for i in u.snapshot()["items"] if i["id"] == "git")
        self.assertEqual(item["status"], up.ERROR)
        self.assertIn("install failed", item["detail"])
        self.assertTrue(item["can_apply"])

    def test_failed_recheck_after_a_verified_update_still_counts_as_success(self):
        fake = FakeRun([("pip show", (0, "Version: 1.2.0")), ("install --upgrade", (0, "ok")),
                        ("list --outdated", (1, "network down")), ("index versions", (1, "network down"))])
        self.patch(fake)
        u = self.make()
        u._store("pip", up.AVAILABLE, "x", meta={"installed": (1, 0), "latest": (1, 2, 0)})
        u.apply("pip", background=False)
        item = next(i for i in u.snapshot()["items"] if i["id"] == "pip")
        self.assertEqual(item["status"], up.UP_TO_DATE)
        self.assertTrue(item["detail"].startswith("Updated and verified"))
        self.assertIn("check afterward failed", item["detail"])

    def test_startup_skips_entries_that_opt_out(self):
        bk = backends()
        bk["dock"]["update"]["check_at_startup"] = False
        fake = FakeRun()
        self.patch(fake, lambda url: {"tag_name": "1.0.0"})
        u = up.Updater(lambda: bk, lambda bid: False)
        u.check_all(startup=True)
        self.assertFalse(any("docker" in c for c in fake.calls))
        item = next(i for i in u.snapshot()["items"] if i["id"] == "dock")
        self.assertEqual(item["status"], up.UNKNOWN)
        u.check_all()  # "Check again" does check it
        self.assertTrue(any("docker pull" in c for c in fake.calls))


class RequestGuard(Patched):
    def setUp(self):
        self.patch(FakeRun())
        bk = backends()
        old = sb.UPDATER
        sb.UPDATER = up.Updater(lambda: bk, lambda bid: False)
        self.addCleanup(lambda: setattr(sb, "UPDATER", old))
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), sb.Handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)

    def req(self, method, path, headers=None, host="default"):
        import http.client
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.putrequest(method, path, skip_host=True)
        c.putheader("Host", f"127.0.0.1:{self.port}" if host == "default" else host)
        for k, v in (headers or {}).items():
            c.putheader(k, v)
        c.putheader("Content-Length", "2")
        c.endheaders(b"{}")
        r = c.getresponse()
        body = r.read()
        c.close()
        return r.status, body

    def test_good_requests_pass(self):
        self.assertEqual(self.req("GET", "/api/updates")[0], 200)
        good = {"X-Switchboard": "1"}
        self.assertEqual(self.req("POST", "/api/updates/apply/nope", good)[0], 404)
        for host in (f"localhost:{self.port}", f"[::1]:{self.port}"):
            self.assertEqual(self.req("GET", "/api/updates", host=host)[0], 200)
        origin = {"X-Switchboard": "1", "Origin": f"http://127.0.0.1:{self.port}"}
        self.assertEqual(self.req("POST", "/api/updates/apply/nope", origin)[0], 404)

    def test_query_string_is_stripped_and_ids_are_unquoted(self):
        good = {"X-Switchboard": "1"}
        self.assertEqual(self.req("POST", "/api/updates/apply/nope?x=1", good)[0], 404)
        self.assertEqual(self.req("POST", "/api/updates/apply/gi%74?x=1", good)[0], 409)  # found "git"
        self.assertEqual(self.req("POST", "/api/start/nope%20x?y=2", good)[0], 404)

    def test_bad_host_is_refused_for_get_and_post(self):
        for host in ("evil.example.com", f"evil.example.com:{self.port}", "127.0.0.1:1", ""):
            self.assertEqual(self.req("GET", "/api/updates", host=host)[0], 403, host)
            self.assertEqual(self.req("POST", "/api/updates/apply/nope", {"X-Switchboard": "1"}, host=host)[0], 403, host)

    def test_bad_origin_is_refused(self):
        for origin in ("http://evil.example.com", "null", f"http://localhost:{self.port}", f"https://127.0.0.1:{self.port}"):
            code, _ = self.req("POST", "/api/updates/apply/nope", {"X-Switchboard": "1", "Origin": origin})
            self.assertEqual(code, 403, origin)
            self.assertEqual(self.req("GET", "/api/updates", {"Origin": origin})[0], 403, origin)

    def test_post_without_the_header_is_refused_on_every_endpoint(self):
        for path in ("/api/updates/check", "/api/updates/apply/pip", "/api/start/x", "/api/stop/x",
                     "/api/stop-all", "/api/unload", "/api/chat"):
            self.assertEqual(self.req("POST", path)[0], 403, path)
        self.assertEqual(self.req("POST", "/api/updates/check", {"X-Switchboard": "0"})[0], 403)

    def test_the_page_sends_the_header_on_every_post(self):
        html = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text()
        self.assertEqual(html.count('method: "POST"'), html.count("X-Switchboard"))
        self.assertGreaterEqual(html.count("X-Switchboard"), 3)


if __name__ == "__main__":
    unittest.main()
