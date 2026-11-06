"""Tests for the Updates feature. No network, no real git changes: command execution and the
GitHub lookup are replaced with fakes. Run: python3 -m unittest discover -s tests -v
"""
import json
import os
import socket
import tempfile as tempfile_module
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
        bk["git"]["update"]["check_timeout"] = 10 ** 9
        self.assertEqual(up.check_budget(bk["git"]), 86400)  # capped at a day

    def test_bad_check_timeout_is_a_config_error_not_a_crash(self):
        for bad in (float("inf"), float("nan"), 0, -3, "junk", None, True, [60]):
            bk = backends()
            bk["git"]["update"]["check_timeout"] = bad
            with self.assertRaises(ValueError):
                up.check_budget(bk["git"])
            fake = FakeRun()
            self.patch(fake)
            u = up.Updater(lambda: bk, lambda bid: False)
            u._check_one("git")
            row = next(i for i in u.snapshot()["items"] if i["id"] == "git")
            self.assertEqual(row["status"], up.ERROR, bad)
            self.assertIn("check_timeout", row["detail"])
            self.assertEqual(fake.calls, [])
            u._store("git", up.AVAILABLE, "x")
            code, obj = u.apply("git", background=False)
            self.assertEqual(code, 409)
            self.assertIn("check_timeout", obj["error"])

    def test_stale_first_check_does_not_leave_a_checking_placeholder(self):
        u = up.Updater(lambda: backends(), lambda bid: False)
        real = up.run_check

        def straddling(b, meta=None, budget=60):
            u._epoch += 1
            return up.UP_TO_DATE, "stale"
        up.run_check = straddling
        self.addCleanup(setattr, up, "run_check", real)
        u._check_one("dock")
        row = next(i for i in u.snapshot()["items"] if i["id"] == "dock")
        self.assertEqual(row["status"], up.UNKNOWN)

    def test_docker_pull_command_timeout_is_at_least_the_budget(self):
        seen = {}

        def fake(argv, **kw):
            if "pull" in argv:
                seen["t"] = kw.get("timeout")
            return 0, "sha256:x"
        self.patch(fake)
        bk = backends()
        bk["dock"]["update"]["check_timeout"] = 7200
        up.run_check(bk["dock"], budget=up.check_budget(bk["dock"]))
        self.assertGreater(seen["t"], 3600)

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


def gpu_line(n, name):
    return (f'Nov 05 12:00:0{n} host ollama[1]: time=2026-11-05T12:00:0{n} level=INFO source=types.go '
            f'msg="inference compute" id=GPU-{n} library=CUDA compute=7.0 name=CUDA{n} description="{name}" total="16 GiB"')


TWO = "\n".join(["Nov 05 12:00:00 host ollama[1]: Listening on 127.0.0.1:11434", gpu_line(0, "Tesla V100"), gpu_line(1, "GeForce RTX 3070")])
ONE = "\n".join(["Nov 05 12:00:00 host ollama[1]: Listening on 127.0.0.1:11434", gpu_line(0, "Tesla V100"),
                  'Nov 05 12:00:01 host ollama[1]: level=INFO msg="skipping CUDA device" id=GPU-1 reason="compute capability"'])
SMI2 = "GPU 0: Tesla V100 (UUID: GPU-0)\nGPU 1: GeForce RTX 3070 (UUID: GPU-1)"
GC = ("ollama", 60.0, "Ollama")


class GpuCheck(Patched):
    def setUp(self):
        super().setUp()
        self.sleeps = []
        self.now = [0.0]

        def fake_sleep(seconds):
            self.sleeps.append(seconds)
            self.now[0] += seconds  # the fake clock only moves when the code sleeps
        self._old = (up._sleep, up._clock)
        up._sleep, up._clock = fake_sleep, lambda: self.now[0]
        self.addCleanup(lambda: (setattr(up, "_sleep", self._old[0]), setattr(up, "_clock", self._old[1])))

    def test_both_gpus_found_passes(self):
        fake = FakeRun([("systemctl", (0, "active")), ("journalctl", (0, TWO)), ("nvidia-smi", (0, SMI2))])
        self.patch(fake)
        ok, out, text = up.gpu_verify(GC, "2026-11-05 12:00:00")
        self.assertTrue(ok)
        self.assertEqual(text, "2 GPUs found")
        self.assertIn("Tesla V100", out)
        self.assertTrue(any("--since 2026-11-05 12:00:00" in c and "-u ollama" in c and "--no-pager" in c for c in fake.calls))
        self.assertLess(next(i for i, c in enumerate(fake.calls) if "systemctl is-active ollama" in c),
                        next(i for i, c in enumerate(fake.calls) if "journalctl" in c))
        self.assertFalse(any("sudo" in c for c in fake.calls))

    def test_a_missing_gpu_fails_with_a_clear_message_and_names_what_was_found(self):
        self.patch(FakeRun([("systemctl", (0, "active")), ("journalctl", (0, ONE)), ("nvidia-smi", (0, SMI2))]))
        ok, out, text = up.gpu_verify(("ollama", 0, "Ollama"), "2026-11-05 12:00:00")
        self.assertFalse(ok)
        self.assertEqual(text, "Ollama sees 1 of 2 GPUs after the update. The old version may need to be reinstalled.")
        self.assertIn('description="Tesla V100"', out)
        self.assertIn("skipping CUDA device", out)

    def test_unreadable_journal_is_a_failure_not_a_pass(self):
        for res in ((1, "journalctl: command not found"), (127, "command not found"),
                    (0, "Hint: You are currently not seeing messages from other users and the system.\n"
                        "No journal files were opened due to insufficient permissions.")):
            self.patch(FakeRun([("systemctl", (0, "active")), ("journalctl", res), ("nvidia-smi", (0, SMI2))]))
            ok, _, text = up.gpu_verify(GC, "2026-11-05 12:00:00")
            self.assertFalse(ok)
            self.assertIn("could not read the service journal", text)
        self.assertEqual(self.sleeps, [])  # no point polling an unreadable journal

    def test_no_nvidia_smi_still_needs_one_gpu_line(self):
        self.patch(FakeRun([("systemctl", (0, "active")), ("journalctl", (0, ONE)), ("nvidia-smi", (127, "command not found"))]))
        ok, _, text = up.gpu_verify(GC, "2026-11-05 12:00:00")
        self.assertTrue(ok)
        self.assertEqual(text, "1 GPU found")
        self.patch(FakeRun([("systemctl", (0, "active")), ("journalctl", (0, "Nov 05 host ollama[1]: Listening")), ("nvidia-smi", (127, ""))]))
        ok, _, text = up.gpu_verify(("ollama", 0, "Ollama"), "2026-11-05 12:00:00")
        self.assertFalse(ok)
        self.assertIn("no GPU", text)

    def test_polling_waits_then_succeeds(self):
        answers = ["Nov 05 host ollama[1]: starting", "Nov 05 host ollama[1]: starting", TWO]

        def fake(argv, **kw):
            if argv[0] == "nvidia-smi":
                return 0, SMI2
            if argv[0] == "systemctl":
                return 0, "active"
            return 0, answers.pop(0) if len(answers) > 1 else answers[0]
        self.patch(fake)
        ok, _, text = up.gpu_verify(GC, "2026-11-05 12:00:00")
        self.assertTrue(ok)
        self.assertEqual(len(self.sleeps), 2)

    def test_timeout_fails_after_polling_for_the_whole_wait(self):
        self.patch(FakeRun([("systemctl", (0, "active")), ("journalctl", (0, "Nov 05 host ollama[1]: starting")), ("nvidia-smi", (0, SMI2))]))
        ok, _, text = up.gpu_verify(("ollama", 6.0, "Ollama"), "2026-11-05 12:00:00")
        self.assertFalse(ok)
        self.assertEqual(text, "Ollama sees 0 of 2 GPUs after the update. The old version may need to be reinstalled.")
        self.assertEqual(self.sleeps, [2, 2, 2])  # 6 s wait, polled every 2 s against one deadline

    def test_a_restart_repeating_the_same_gpu_lines_counts_each_gpu_once(self):
        self.patch(FakeRun([("systemctl", (0, "active")), ("journalctl", (0, TWO + "\n" + TWO)), ("nvidia-smi", (0, SMI2 + "\nGPU 2: x"))]))
        ok, _, text = up.gpu_verify(("ollama", 0, "Ollama"), "2026-11-05 12:00:00")
        self.assertFalse(ok)  # 2 distinct GPUs of 3

    def test_pipeline_stamps_before_install_and_checks_after_the_version(self):
        bk = backends()
        bk["rel"]["update"]["gpu_check"] = {"unit": "ollama", "wait": 0}
        fake = FakeRun([("tool --version", (0, "tool 0.5.2")), ("install-tool", (0, "installed")),
                        ("systemctl", (0, "active")), ("journalctl", (0, TWO)), ("nvidia-smi", (0, SMI2))])
        self.patch(fake)
        ok, name, reason, out = up.run_steps(up.build_steps(bk["rel"], {"latest": (0, 5, 2)}))
        self.assertTrue(ok, reason)
        order = [c for c in fake.calls if "install-tool" in c or "tool --version" in c or "journalctl" in c]
        self.assertTrue(order[0].endswith("install-tool"))
        self.assertIn("tool --version", order[1])
        self.assertIn("journalctl", order[2])
        self.assertIn("GPU check", out)

    def test_pipeline_fails_verify_when_a_gpu_is_missing(self):
        bk = backends()
        bk["rel"]["update"]["gpu_check"] = {"unit": "ollama", "wait": 0}
        self.patch(FakeRun([("tool --version", (0, "tool 0.5.2")), ("install-tool", (0, "ok")),
                            ("systemctl", (0, "active")), ("journalctl", (0, ONE)), ("nvidia-smi", (0, SMI2))]))
        ok, name, reason, _ = up.run_steps(up.build_steps(bk["rel"], {"latest": (0, 5, 2)}))
        self.assertEqual((ok, name), (False, "verify"))
        self.assertIn("1 of 2 GPUs", reason)

    def test_version_failure_skips_the_gpu_check(self):
        bk = backends()
        bk["rel"]["update"]["gpu_check"] = {"unit": "ollama"}
        fake = FakeRun([("tool --version", (0, "tool 0.5.1")), ("install-tool", (0, "ok"))])
        self.patch(fake)
        ok, name, reason, _ = up.run_steps(up.build_steps(bk["rel"], {"latest": (0, 5, 2)}))
        self.assertFalse(ok)
        self.assertFalse(any("journalctl" in c for c in fake.calls))

    def test_bad_gpu_check_settings_are_rejected(self):
        for bad in ({}, {"unit": "a b; rm"}, "ollama", {"unit": "ollama", "wait": "soon"}):
            bk = backends()
            bk["rel"]["update"]["gpu_check"] = bad
            with self.assertRaises(ValueError):
                up.build_steps(bk["rel"], {})


    def test_permission_hint_with_exit_zero_is_an_unreadable_journal(self):
        hint = ("Hint: You are currently not seeing messages from other users and the system.\n"
                "      Users in groups 'adm', 'systemd-journal' can see all messages.\n"
                "      Pass -q to turn off this notice.\n-- No entries --")
        self.patch(FakeRun([("systemctl", (0, "active")), ("journalctl", (0, hint)), ("nvidia-smi", (0, SMI2))]))
        ok, _, text = up.gpu_verify(GC, "@1")
        self.assertFalse(ok)
        self.assertEqual(text, "could not read the service journal (add your user to the systemd-journal group)")

    def test_service_not_running_is_reported_as_such(self):
        fake = FakeRun([("systemctl", (3, "inactive")), ("journalctl", (0, TWO)), ("nvidia-smi", (0, SMI2))])
        self.patch(fake)
        ok, out, text = up.gpu_verify(("ollama", 4.0, "Ollama"), "@1")
        self.assertFalse(ok)
        self.assertEqual(text, "service not running after the update")
        self.assertFalse(any("journalctl" in c for c in fake.calls))

    def test_a_service_that_is_still_starting_gets_time(self):
        states = ["activating", "activating", "active"]

        def fake(argv, **kw):
            if argv[0] == "systemctl":
                return 0, states.pop(0) if len(states) > 1 else states[0]
            return (0, SMI2) if argv[0] == "nvidia-smi" else (0, TWO)
        self.patch(fake)
        self.assertTrue(up.gpu_verify(GC, "@1")[0])

    def test_journalctl_calls_get_a_short_timeout(self):
        seen = []

        def fake(argv, **kw):
            seen.append((argv[0], kw.get("timeout")))
            return (0, "active") if argv[0] == "systemctl" else ((0, SMI2) if argv[0] == "nvidia-smi" else (0, TWO))
        self.patch(fake)
        up.gpu_verify(GC, "@1")
        self.assertIn(("journalctl", 10), seen)

    def test_the_owners_v100_and_rtx_3070_pass_two_of_two(self):
        v100 = ('Nov 05 12:00:02 host ollama[1]: time=2026-11-05T12:00:02.000-05:00 level=INFO source=types.go:42 '
                'msg="inference compute" id=1 filter_id=1 library=CUDA compute=7.0 name=CUDA1 '
                'description="Tesla PG500-216" libdirs=ollama,cuda_v12 driver=13.0 pci_id=0000:02:00.0 '
                'type=discrete total="31.7 GiB" available="31.4 GiB"')
        rtx = ('Nov 05 12:00:02 host ollama[1]: time=2026-11-05T12:00:02.100-05:00 level=INFO source=types.go:42 '
               'msg="inference compute" id=0 filter_id=0 library=CUDA compute=8.6 name=CUDA0 '
               'description="NVIDIA GeForce RTX 3070" libdirs=ollama,cuda_v13 driver=13.0 pci_id=0000:01:00.0 '
               'type=discrete total="8.0 GiB" available="7.1 GiB"')
        skip = ('Nov 05 12:00:01 host ollama[1]: time=2026-11-05T12:00:01.900-05:00 level=INFO source=runner.go:120 '
                'msg="skipping CUDA device" name="Tesla PG500-216" cc=700 libdir=cuda_v13 '
                'reason="compute capability 7.0 is not supported by the cuda_v13 libraries"')
        journal = "\n".join(["Nov 05 12:00:00 host ollama[1]: Listening on 127.0.0.1:11434", skip, v100, rtx])
        self.patch(FakeRun([("systemctl", (0, "active")), ("journalctl", (0, journal)), ("nvidia-smi", (0, SMI2))]))
        ok, out, text = up.gpu_verify(("ollama", 0, "Ollama"), "@1")
        self.assertTrue(ok, out)
        self.assertEqual(text, "2 GPUs found")
        self.assertIn("Tesla PG500-216", out)

    def test_wait_is_capped_and_non_finite_values_are_rejected(self):
        self.assertEqual(up.parse_gpu_check({"gpu_check": {"unit": "o", "wait": 99999}})[1], 600)
        self.assertEqual(up.parse_gpu_check({"gpu_check": {"unit": "o", "wait": -5}})[1], 0)
        for bad in (float("inf"), float("nan"), "x", True):
            with self.assertRaises(ValueError):
                up.parse_gpu_check({"gpu_check": {"unit": "o", "wait": bad}})

    def test_pipeline_records_the_install_time_in_epoch_form(self):
        bk = backends()
        bk["rel"]["update"]["gpu_check"] = {"unit": "ollama", "wait": 0}
        fake = FakeRun([("tool --version", (0, "tool 0.5.2")), ("install-tool", (0, "ok")),
                        ("systemctl", (0, "active")), ("journalctl", (0, TWO)), ("nvidia-smi", (0, SMI2))])
        self.patch(fake)
        up.run_steps(up.build_steps(bk["rel"], {"latest": (0, 5, 2)}))
        call = next(c for c in fake.calls if "journalctl" in c)
        self.assertRegex(call, r"--since @\d{9,}")

    def test_checked_in_ollama_entry_has_a_gpu_check(self):
        cfg = json.loads((Path(__file__).resolve().parent.parent / "backends.json").read_text())
        ollama = next(b for b in cfg["backends"] if b["id"] == "ollama")
        self.assertEqual(ollama["update"]["gpu_check"], {"unit": "ollama"})
        self.assertIsNone(up.parse_gpu_check({"kind": "git"}))


class LiveOutput(Patched):
    def test_buffer_keeps_only_the_last_200_lines(self):
        b = up.OutputBuffer()
        b.feed("".join(f"line {n}\n" for n in range(1000)))
        self.assertEqual(b.line_count(), 200)
        self.assertEqual(b.tail().splitlines()[-1], "line 999")
        self.assertEqual(len(b.tail().splitlines()), 60)  # the API tail
        self.assertEqual(b.tail(500).splitlines()[0], "line 800")

    def test_chunks_that_split_a_line_are_joined(self):
        b = up.OutputBuffer()
        b.feed("hel")
        b.feed("lo\nwor")
        b.feed("ld\nrest")
        self.assertEqual(b.tail().splitlines(), ["hello", "world", "rest"])
        b.finish()
        self.assertTrue(b.finished)
        self.assertIsNotNone(b.finished_at)
        self.assertEqual(b.tail().splitlines()[-1], "rest")

    def test_a_bare_carriage_return_replaces_the_line_like_a_progress_bar(self):
        b = up.OutputBuffer()
        b.feed("start\n10%\r20%\r30%\r")
        self.assertEqual(b.line_count(), 1)  # only \n stores a line
        self.assertEqual(b.tail().splitlines(), ["start", "30%"])
        b.feed("40%\r")
        b.feed("done\n")
        self.assertEqual(b.tail().splitlines(), ["start", "done"])

    def test_crlf_split_across_chunks_adds_no_blank_line(self):
        b = up.OutputBuffer()
        b.feed("one\r")
        b.feed("\ntwo\r\n")
        b.feed("three\r")
        b.feed("\n")
        self.assertEqual(b.tail().splitlines(), ["one", "two", "three"])
        self.assertEqual(b.line_count(), 3)

    def test_multibyte_character_split_between_reads_is_whole(self):
        b = up.OutputBuffer()
        import codecs
        dec = codecs.getincrementaldecoder("utf-8")("replace")
        b.feed(dec.decode(b"caf\xc3"))
        b.feed(dec.decode(b"\xa9\n"))
        self.assertEqual(b.tail(), "caf\u00e9")

    def test_a_real_command_printing_a_split_character_shows_it_whole(self):
        b = up.OutputBuffer()
        up._ctx.sink = b
        try:
            rc, out = up.run_command(["sh", "-c", "printf 'caf\\303'; sleep 0.8; printf '\\251\\n'"], timeout=10)
        finally:
            up._ctx.sink = None
        self.assertEqual(rc, 0)
        self.assertEqual(b.tail(), "caf\u00e9")
        self.assertEqual(out, "caf\u00e9")

    def test_a_command_that_prints_faster_than_we_read_still_times_out(self):
        import time
        from unittest import mock
        made = []
        real = tempfile_module.TemporaryFile

        def spy(*a, **k):
            f = real(*a, **k)
            made.append(f)
            return f
        b = up.OutputBuffer()
        up._ctx.sink = b
        t0 = time.monotonic()
        try:
            with mock.patch.object(up.tempfile, "TemporaryFile", spy):
                rc, out = up.run_command(["sh", "-c", "yes"], timeout=2)
        finally:
            up._ctx.sink = None
        # 124 when the timeout fires first; 125 when it wrote past the output-size limit first
        # (`yes` can write hundreds of MB per second). Either way it is stopped promptly.
        self.assertIn(rc, (124, 125))
        self.assertLess(time.monotonic() - t0, 6)
        self.assertTrue(made and all(f.closed for f in made), "the temp file must be closed (and so removed)")
        self.assertLessEqual(b.line_count(), up.LIVE_LINES)

    def test_a_chatty_but_slow_command_times_out_with_124(self):
        import time
        b = up.OutputBuffer()
        up._ctx.sink = b
        t0 = time.monotonic()
        try:
            rc, out = up.run_command(["sh", "-c", "while :; do echo tick; sleep 0.01; done"], timeout=1)
        finally:
            up._ctx.sink = None
        self.assertEqual((rc, out), (124, "command timed out"))
        self.assertLess(time.monotonic() - t0, 5)
        self.assertIn("tick", b.tail())

    def test_catching_up_skips_ahead_and_never_starts_inside_a_character(self):
        import codecs
        with tempfile_module.TemporaryFile() as f:
            f.write(b"x" * (up.PUMP_LAG * 3) + "\u00e9".encode() * 10)
            f.flush()
            b = up.OutputBuffer()
            dec = codecs.getincrementaldecoder("utf-8")("replace")
            pos = up._pump(f, 0, b, dec)
            self.assertEqual(pos, os.fstat(f.fileno()).st_size)
            self.assertNotIn("\ufffd", b.tail())
            # a position inside a multi-byte character is moved past it
            self.assertEqual(up._skip_continuation(f, up.PUMP_LAG * 3 + 1, pos), up.PUMP_LAG * 3 + 2)

    def test_every_step_gets_a_marker_in_order(self):
        b = up.OutputBuffer()
        steps = [up.Step("pull", lambda: (True, "", "pulled")), up.Step("install", lambda: (True, "", "")),
                 up.Step("verify", lambda: (False, "", "version mismatch"))]
        up.run_steps(steps, out=b)
        lines = b.tail().splitlines()
        self.assertEqual(lines, ["== pull started ==", "== pull ok: pulled ==", "== install started ==",
                                 "== install ok ==", "== verify started ==", "== verify failed: version mismatch =="])

    def test_output_streams_while_the_command_is_still_running(self):
        import time
        b = up.OutputBuffer()
        result = {}
        step = up.cmd_step("install", ["sh", "-c", "echo first; sleep 2; echo second"], timeout=20)
        th = threading.Thread(target=lambda: result.update(r=up.run_steps([step], out=b)))
        th.start()
        seen_early = False
        for _ in range(30):
            if "first" in b.tail() and th.is_alive():
                seen_early = True
                break
            time.sleep(0.1)
        th.join(20)
        self.assertTrue(seen_early, "the first line should be visible before the command ends")
        self.assertTrue(result["r"][0])
        self.assertIn("second", b.tail())
        self.assertEqual(result["r"][3].count("first"), 1)  # the final result is still complete

    def test_timeout_still_kills_the_group_and_keeps_what_was_printed(self):
        b = up.OutputBuffer()
        up._ctx.sink = b
        try:
            rc, out = up.run_command(["sh", "-c", "echo hi; sleep 30 & sleep 30"], timeout=1)
        finally:
            up._ctx.sink = None
        self.assertEqual((rc, out), (124, "command timed out"))
        self.assertIn("hi", b.tail())

    def test_hostile_output_is_capped_and_cleaned(self):
        b = up.OutputBuffer()
        b.feed("a" * 50000 + "\n")
        b.feed("\x1b[31mred\x1b[0m \x00\x07bell\x1b]0;title\x07 ok\n")
        b.feed("<script>alert(1)</script> <img src=x onerror=y>\n")
        lines = b.tail().splitlines()
        self.assertEqual(len(lines[0]), up.LIVE_LINE_CHARS)
        self.assertEqual(lines[1], "red bell ok")
        self.assertEqual(lines[2], "<script>alert(1)</script> <img src=x onerror=y>")  # kept as plain text
        self.assertNotIn("\x1b", b.tail())
        body = json.dumps({"output_tail": b.tail()})  # what the API sends: JSON-escaped text
        self.assertEqual(json.loads(body)["output_tail"], b.tail())

    def test_a_runaway_command_cannot_blow_up_memory(self):
        b = up.OutputBuffer()
        up._ctx.sink = b
        try:
            rc, out = up.run_command(["sh", "-c", "head -c 5000000 /dev/zero | tr '\\0' 'a'; echo done"], timeout=30)
        finally:
            up._ctx.sink = None
        self.assertEqual(rc, 0)
        self.assertLessEqual(len(out), up.MAX_RESULT_BYTES + 10)
        self.assertLessEqual(b.line_count(), up.LIVE_LINES)
        self.assertLessEqual(len(b.tail(1000)), up.LIVE_LINES * (up.LIVE_LINE_CHARS + 1))

    def test_updater_exposes_the_tail_during_and_after_an_update_until_the_next_one(self):
        gate, started = threading.Event(), threading.Event()
        self.addCleanup(gate.set)

        def fake(argv, **kw):
            text = " ".join(argv)
            if "install --upgrade" in text:
                up.emit("Collecting pkg")  # what a streaming command would add
                started.set()
                gate.wait(5)
                return 0, "ok"
            if "pip show" in text:
                return 0, "Version: 1.2.0"
            return 0, "[]"
        self.patch(fake)
        bk = backends()
        u = up.Updater(lambda: bk, lambda bid: False)
        u._store("pip", up.AVAILABLE, "x", meta={"installed": (1, 0), "latest": (1, 2, 0)})
        self.assertEqual(next(i for i in u.snapshot()["items"] if i["id"] == "pip")["output_tail"], "")
        self.assertEqual(u.apply("pip")[0], 202)
        self.assertTrue(started.wait(5))
        row = next(i for i in u.snapshot()["items"] if i["id"] == "pip")
        self.assertTrue(row["applying"])
        self.assertIn("== pull started ==", row["output_tail"])
        self.assertIn("Collecting pkg", row["output_tail"])
        gate.set()
        for _ in range(100):
            if u.snapshot()["applying"] is None:
                break
            threading.Event().wait(0.05)
        row = next(i for i in u.snapshot()["items"] if i["id"] == "pip")
        self.assertFalse(row["applying"])
        self.assertIn("== verify ok", row["output_tail"])
        self.assertTrue(u._out["pip"].finished)
        self.assertIsNone(next(i for i in u.snapshot()["items"] if i["id"] == "git")["output_at"])
        self.assertIsNotNone(row["output_at"])
        self.assertLessEqual(row["checked_at"], row["output_at"] + 2)  # so the page shows the live tail
        # the next update starts with a fresh buffer
        u._store("pip", up.AVAILABLE, "x", meta={"installed": (1, 0), "latest": (1, 2, 0)})
        gate.clear()
        u.apply("pip")
        self.assertFalse(u._out["pip"].finished)  # a fresh buffer, not the old finished one
        gate.set()
        for _ in range(100):
            if u.snapshot()["applying"] is None:
                break
            threading.Event().wait(0.05)

    def test_the_page_shows_output_as_text_and_polls_fast_while_updating(self):
        html = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text()
        a = html.index("function outText")
        b = html.index("async function pollUpdates")
        a2 = html.index("function buildUpdRow")
        section = html[a:b] + html[a2:html.index("\n}\n", a2)]
        self.assertNotIn("innerHTML", section)
        self.assertIn("pre.textContent = text", section)
        self.assertRegex(html, r"updOpen \? \(upd\.applying \? 1500 : 3000\) : \(upd\.applying \|\| upd\.checking\) \? 3000 : 60000")
        self.assertIn("output_at", html)


class Thresholds(Patched):
    def test_git_threshold_decisions(self):
        self.assertEqual(up.decide_git(1, 50), (up.NEWER, "1 commit behind (below the 50 commit threshold)"))
        self.assertEqual(up.decide_git(49, 50)[0], up.NEWER)
        self.assertEqual(up.decide_git(50, 50), (up.AVAILABLE, "50 commits behind"))
        self.assertEqual(up.decide_git(51, 50)[0], up.AVAILABLE)
        self.assertEqual(up.decide_git(0, 50)[0], up.UP_TO_DATE)
        self.assertEqual(up.decide_git(1)[0], up.AVAILABLE)  # default threshold is 1
        self.assertEqual(up.decide_git(None, 50)[0], up.UNKNOWN)

    def test_git_threshold_setting_must_be_a_whole_number_of_one_or_more(self):
        self.assertEqual(up.git_threshold({}), 1)
        self.assertEqual(up.git_threshold({"min_commits_behind": 20}), 20)
        for bad in (0, -1, 1.5, "5", True, None, [3]):
            with self.assertRaises(ValueError):
                up.git_threshold({"min_commits_behind": bad})
            b = backends()["git"]
            b["update"]["min_commits_behind"] = bad
            self.patch(FakeRun([("rev-parse --is-inside", (0, "true")), ("rev-list", (0, "9"))]))
            status, detail = up.run_check(b)
            self.assertEqual(status, up.ERROR)
            self.assertIn("min_commits_behind", detail)

    def test_release_threshold_decisions(self):
        d = up.decide_release
        self.assertEqual(d("ollama version is 0.40.14", "v0.40.15", "minor"),
                         (up.NEWER, "0.40.14 to 0.40.15 (patch release, below the minor threshold)"))
        self.assertEqual(d("0.40.14", "v0.41.0", "minor"), (up.AVAILABLE, "0.40.14 to 0.41.0"))
        self.assertEqual(d("0.40.14", "0.41.0", "major")[0], up.NEWER)
        self.assertEqual(d("0.40.14", "1.0.0", "major")[0], up.AVAILABLE)
        self.assertEqual(d("0.40.14", "0.40.15")[0], up.AVAILABLE)  # default is patch
        self.assertEqual(d("0.40.14", "0.40.15", "patch")[0], up.AVAILABLE)
        self.assertEqual(d("0.40.15", "0.40.15", "minor")[0], up.UP_TO_DATE)
        self.assertEqual(d("0.41.2", "0.40.15", "minor")[0], up.UP_TO_DATE)  # installed is newer
        self.assertEqual(d("1.2", "1.2.1", "minor")[0], up.NEWER)  # a missing part counts as 0

    def test_release_setting_must_be_patch_minor_or_major(self):
        self.assertEqual(up.release_threshold({}), "patch")
        with self.assertRaises(ValueError):
            up.release_threshold({"min_release": "huge"})
        b = backends()["rel"]
        b["update"]["min_release"] = "huge"
        self.patch(FakeRun([("tool --version", (0, "0.1.0"))]), lambda url: {"tag_name": "9.9.9"})
        status, detail = up.run_check(b)
        self.assertEqual(status, up.ERROR)
        self.assertIn("min_release", detail)

    def test_prereleases_are_never_offered_and_junk_is_ignored(self):
        d = up.decide_release
        self.assertEqual(d("0.40.14", "v0.40.15-rc1", "patch")[0], up.UP_TO_DATE)
        self.assertEqual(d("0.40.14", "0.41.0-beta.2", "patch")[0], up.UP_TO_DATE)
        self.assertEqual(d("0.40.14", "latest", "patch")[0], up.UNKNOWN)
        self.assertEqual(d("0.40.14", "", "patch")[0], up.UNKNOWN)
        self.assertEqual(d("no version here", "0.40.15", "patch")[0], up.UNKNOWN)
        self.assertEqual(d("0.40.15+build7", "v0.40.15+build9", "patch")[0], up.UP_TO_DATE)  # build metadata ignored
        # the final release is newer than its own release candidate
        self.assertEqual(d("0.40.15-rc1", "0.40.15", "patch")[0], up.AVAILABLE)
        self.assertEqual(up.parse_release("v0.40.15-rc1"), ((0, 40, 15), True))
        self.assertEqual(up.parse_release("Warning: x\nclient version is 0.40.15"), ((0, 40, 15), False))

    def test_check_end_to_end_for_both_kinds(self):
        b = backends()["git"]
        b["update"]["min_commits_behind"] = 50
        self.patch(FakeRun([("rev-parse --is-inside", (0, "true")), ("rev-list", (0, "3"))]))
        self.assertEqual(up.run_check(b), (up.NEWER, "3 commits behind (below the 50 commit threshold)"))
        r = backends()["rel"]
        r["update"]["min_release"] = "minor"
        self.patch(FakeRun([("tool --version", (0, "tool version is 0.40.14"))]), lambda url: {"tag_name": "v0.40.15"})
        status, detail = up.run_check(r)
        self.assertEqual(status, up.NEWER)
        self.assertIn("patch release, below the minor threshold", detail)

    def test_pip_and_docker_have_no_threshold(self):
        out = '[{"name": "my-pkg", "version": "1.0.0", "latest_version": "1.0.1"}]'
        self.patch(FakeRun([("list --outdated", (0, out))]))
        self.assertEqual(up.run_check(backends()["pip"])[0], up.AVAILABLE)

    def test_a_row_below_the_threshold_can_still_be_updated(self):
        fake = FakeRun()
        self.patch(fake)
        u = up.Updater(lambda: backends(), lambda bid: False)
        u._store("git", up.NEWER, "1 commit behind (below the 50 commit threshold)")
        row = next(i for i in u.snapshot()["items"] if i["id"] == "git")
        self.assertTrue(row["can_apply"])
        self.assertIsNone(row["reason"])
        self.assertEqual(u.apply("git", background=False)[0], 202)
        row = next(i for i in u.snapshot()["items"] if i["id"] == "git")
        self.assertIn("pull ok", row["progress"])
        self.assertIn("verify ok", row["progress"])  # same pipeline as any other update
        # it is still refused while the backend runs
        u2 = up.Updater(lambda: backends(), lambda bid: True)
        u2._store("git", up.NEWER, "x")
        self.assertEqual(u2.apply("git", background=False)[0], 409)

    def test_badge_and_update_all_ignore_rows_below_the_threshold(self):
        import shutil
        import subprocess
        if not shutil.which("node"):
            self.skipTest("node not installed")
        html = (Path(__file__).resolve().parent.parent / "static" / "index.html").read_text()
        a = html.index('const NEWER = ')
        b = html.index("\n", html.index("const updateAllTargets"))
        src = html[a:b + 1]
        script = src + """
const items = [
  {id: "a", status: "update available", can_apply: true},
  {id: "b", status: "newer version exists", can_apply: true},
  {id: "c", status: "up to date", can_apply: false},
  {id: "d", status: "error", can_apply: true},   // unfinished update: still offered
  {id: "e", status: "error", can_apply: false},
  {id: "f", status: "update available", can_apply: false},
];
console.log(JSON.stringify({badge: badgeCount(items), all: updateAllTargets(items).map(i => i.id)}));
"""
        r = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), {"badge": 2, "all": ["a", "d", "f"]})

    def test_checked_in_thresholds(self):
        cfg = {b["id"]: b for b in json.loads((Path(__file__).resolve().parent.parent / "backends.json").read_text())["backends"]}
        self.assertEqual(cfg["llamacpp-qwen"]["update"]["min_commits_behind"], 50)
        self.assertEqual(cfg["strata-flash-next"]["update"]["min_commits_behind"], 50)
        self.assertEqual(cfg["comfyui"]["update"]["min_commits_behind"], 20)
        self.assertEqual(cfg["ollama"]["update"]["min_release"], "minor")
        for bid in ("litellm", "open-webui"):
            self.assertNotIn("min_commits_behind", cfg[bid]["update"])
            self.assertNotIn("min_release", cfg[bid]["update"])


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
        for key in ("id", "name", "status", "detail", "checked_at", "running", "can_apply", "output_tail"):
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
        for _ in range(200):  # let the background check finish so it cannot leak into the next test
            if not u._checking:
                break
            threading.Event().wait(0.05)
        self.assertFalse(u._checking)

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
