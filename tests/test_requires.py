"""Tests for "requires": starting a backend starts what it needs, stopping that stops it.

Uses throwaway python http servers as fake backends. Run: python3 -m unittest discover tests
"""
import json
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import switchboard as sb  # noqa: E402


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def fake(bid, group, port, **extra):
    return {"id": bid, "name": bid, "group": group, "kind": "process", "port": port,
            "command": f"{sys.executable} -m http.server {port} --bind 127.0.0.1",
            "health": f"http://127.0.0.1:{port}/", **extra}


class Requires(unittest.TestCase):
    def setUp(self):
        self.ports = [free_port() for _ in range(3)]
        cfg = {"port": 0, "backends": [
            fake("ollama", "gpu", self.ports[0]),
            fake("llama", "gpu", self.ports[1]),
            fake("harness", "other", self.ports[2], requires=["ollama"]),
        ]}
        d = tempfile.mkdtemp()
        path = Path(d) / "b.json"
        path.write_text(json.dumps(cfg))
        sb.STATE.clear()
        sb.load_config(path)

    def tearDown(self):
        for bid in list(sb.BACKENDS):
            try:
                sb._stop_blocking(bid)
            except Exception:
                pass

    def up(self, bid):
        return sb.ready(sb.BACKENDS[bid])

    def test_starting_the_harness_starts_ollama_first_and_keeps_it(self):
        sb.switch_to("harness")
        self.assertEqual(sb.STATE["harness"]["phase"], "running")
        self.assertTrue(self.up("ollama"))
        self.assertTrue(self.up("harness"))

    def test_harness_does_not_stop_a_running_ollama(self):
        sb.switch_to("ollama")
        sb.switch_to("harness")
        self.assertTrue(self.up("ollama"))
        self.assertTrue(self.up("harness"))

    def test_stopping_ollama_stops_the_harness(self):
        sb.switch_to("harness")
        sb.stop_with_dependents("ollama")
        self.assertFalse(self.up("harness"))
        self.assertFalse(self.up("ollama"))

    def test_switching_to_another_gpu_backend_stops_ollama_and_the_harness(self):
        sb.switch_to("harness")
        sb.switch_to("llama")
        self.assertTrue(self.up("llama"))
        self.assertFalse(self.up("ollama"))
        self.assertFalse(self.up("harness"))

    def test_stopping_the_harness_leaves_ollama_running(self):
        sb.switch_to("harness")
        sb.stop_with_dependents("harness")
        self.assertFalse(self.up("harness"))
        self.assertTrue(self.up("ollama"))

    def test_a_loop_is_an_error_not_a_hang(self):
        sb.BACKENDS["ollama"]["requires"] = ["harness"]
        sb.switch_to("harness")
        self.assertEqual(sb.STATE["harness"]["phase"], "error")

    def test_unknown_requirement_is_ignored(self):
        sb.BACKENDS["harness"]["requires"] = ["nope"]
        self.assertEqual(sb.requires_of(sb.BACKENDS["harness"]), [])

    def test_dependents_include_indirect_ones(self):
        sb.BACKENDS["llama"]["requires"] = ["harness"]
        self.assertEqual(set(sb.dependents_of("ollama")), {"harness", "llama"})


if __name__ == "__main__":
    unittest.main()
