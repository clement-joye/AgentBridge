from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from telegram_ai_bridge.models import LocalSession
from telegram_ai_bridge.providers.codex import CodexProvider
from telegram_ai_bridge.sessions.runner import AgentRunner


class CodexProviderTests(unittest.TestCase):
    def test_codex_commands_disable_alternate_screen(self) -> None:
        session = LocalSession(
            index=0,
            provider="codex",
            session_id="sess-1",
            cwd="/tmp/project",
            repo_name=None,
            branch=None,
            last_active_at=datetime.now(timezone.utc),
            last_user_prompt=None,
            last_final_answer=None,
            resumable=True,
            source_path=None,
        )
        provider = CodexProvider()

        self.assertEqual(provider.resume_command(session), ["codex", "resume", "--no-alt-screen", "sess-1"])
        self.assertEqual(provider.new_command("/tmp/project"), ["codex", "--no-alt-screen"])

    def test_parse_session_jsonl_uses_task_complete_final_not_commentary(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "rollout.jsonl"
            lines = [
                {
                    "timestamp": "2026-05-11T16:42:15.843Z",
                    "type": "session_meta",
                    "payload": {"id": "sess-1", "cwd": "/tmp/project"},
                },
                {
                    "timestamp": "2026-05-11T16:42:16.000Z",
                    "type": "turn_context",
                    "payload": {"model": "gpt-5.5"},
                },
                {
                    "timestamp": "2026-05-11T16:42:16.100Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "First prompt"},
                },
                {
                    "timestamp": "2026-05-11T16:42:17.000Z",
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "message": "I’m working on it"},
                },
                {
                    "timestamp": "2026-05-11T16:42:18.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "commentary",
                        "content": [{"type": "output_text", "text": "I’m working on it"}],
                    },
                },
                {
                    "timestamp": "2026-05-11T16:42:19.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "turn-1",
                        "last_agent_message": "Actual final answer",
                    },
                },
            ]
            path.write_text("".join(json.dumps(item) + "\n" for item in lines), encoding="utf-8")

            session = CodexProvider(Path(d))._parse_session_jsonl_file(path)

            self.assertIsNotNone(session)
            assert session is not None
            self.assertEqual(session.last_user_prompt, "First prompt")
            self.assertEqual(session.last_final_answer, "Actual final answer")
            self.assertEqual(session.model, "gpt-5.5")

    def test_runner_fallback_prefers_task_complete_final(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "rollout.jsonl"
            lines = [
                {
                    "timestamp": "2026-05-11T16:42:15.843Z",
                    "type": "session_meta",
                    "payload": {"id": "sess-1", "cwd": "/tmp/project"},
                },
                {
                    "timestamp": "2026-05-11T16:42:16.000Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "Prompt to match"},
                },
                {
                    "timestamp": "2026-05-11T16:42:17.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "message",
                        "role": "assistant",
                        "phase": "commentary",
                        "content": [{"type": "output_text", "text": "Commentary only"}],
                    },
                },
                {
                    "timestamp": "2026-05-11T16:42:18.000Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "turn-1",
                        "last_agent_message": "Final answer only",
                    },
                },
            ]
            path.write_text("".join(json.dumps(item) + "\n" for item in lines), encoding="utf-8")

            result = AgentRunner()._extract_codex_assistant_text(path, None, "Prompt to match")

            self.assertEqual(result, "Final answer only")


if __name__ == "__main__":
    unittest.main()
