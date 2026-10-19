"""Tests for the chat box's reasoning-effort choice. Run: python3 -m unittest discover tests"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import switchboard as sb  # noqa: E402

OLLAMA = {"id": "ollama", "name": "Ollama", "openai_base": "http://127.0.0.1:11434/v1"}
LLAMA = {"id": "llamacpp-qwen", "name": "llama.cpp", "openai_base": "http://127.0.0.1:8888/v1"}


class Effort(unittest.TestCase):
    def test_default_changes_nothing(self):
        for choice in (None, "", "bogus"):
            req = {"messages": [], "effort": choice}
            sb.apply_effort(OLLAMA, req)
            self.assertEqual(req, {"messages": []})

    def test_ollama_uses_reasoning_effort(self):
        req = {"messages": [], "effort": "low"}
        sb.apply_effort(OLLAMA, req)
        self.assertEqual(req, {"messages": [], "reasoning_effort": "low"})
        req = {"messages": [], "effort": "xhigh"}
        sb.apply_effort(OLLAMA, req)
        self.assertEqual(req, {"messages": [], "reasoning_effort": "high"})

    def test_llama_server_uses_template_kwargs(self):
        req = {"messages": [], "effort": "xhigh"}
        sb.apply_effort(LLAMA, req)
        self.assertEqual(req, {"messages": [], "chat_template_kwargs": {"reasoning_effort": "xhigh"}})

    def test_existing_template_kwargs_are_kept(self):
        req = {"effort": "low", "chat_template_kwargs": {"enable_thinking": True}}
        sb.apply_effort(LLAMA, req)
        self.assertEqual(req["chat_template_kwargs"], {"enable_thinking": True, "reasoning_effort": "low"})


if __name__ == "__main__":
    unittest.main()
