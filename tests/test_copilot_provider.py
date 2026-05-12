from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from telegram_ai_bridge.models import LocalSession
from telegram_ai_bridge.providers.copilot import CopilotProvider


class CopilotProviderTests(unittest.TestCase):
    def test_copilot_commands(self) -> None:
        provider = CopilotProvider()
        session = LocalSession(
            index=0,
            provider="copilot",
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
        self.assertEqual(provider.resume_command(session), ["copilot", "--silent", "--no-color", "--resume=sess-1"])
        self.assertEqual(provider.new_command("/tmp/project"), ["copilot", "--silent", "--no-color"])

    def test_discover_sessions_parses_workspace_and_events(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            session_dir = root / "123e4567-e89b-12d3-a456-426614174000"
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / "workspace.yaml").write_text(
                "\n".join(
                    [
                        "id: 123e4567-e89b-12d3-a456-426614174000",
                        "cwd: /tmp/copilot-project",
                        "updated_at: 2026-05-07T11:07:42.511Z",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            lines = [
                {
                    "type": "session.start",
                    "timestamp": "2026-05-07T11:06:57.775Z",
                    "data": {
                        "sessionId": "123e4567-e89b-12d3-a456-426614174000",
                        "selectedModel": "claude-sonnet-4.6",
                        "context": {"cwd": "/tmp/copilot-project"},
                    },
                },
                {
                    "type": "user.message",
                    "timestamp": "2026-05-07T11:07:10.000Z",
                    "data": {"content": "Fix the bug"},
                },
                {
                    "type": "assistant.message",
                    "timestamp": "2026-05-07T11:07:20.000Z",
                    "data": {"content": "Applied fix and updated tests."},
                },
                {
                    "type": "session.model_change",
                    "timestamp": "2026-05-07T11:07:42.000Z",
                    "data": {"newModel": "claude-haiku-4.5"},
                },
            ]
            (session_dir / "events.jsonl").write_text(
                "".join(json.dumps(item) + "\n" for item in lines),
                encoding="utf-8",
            )

            sessions = CopilotProvider(root).discover_sessions(limit=5)

            self.assertEqual(len(sessions), 1)
            session = sessions[0]
            self.assertEqual(session.provider, "copilot")
            self.assertEqual(session.session_id, "123e4567-e89b-12d3-a456-426614174000")
            self.assertEqual(session.cwd, "/tmp/copilot-project")
            self.assertEqual(session.last_user_prompt, "Fix the bug")
            self.assertEqual(session.last_final_answer, "Applied fix and updated tests.")
            self.assertEqual(session.model, "claude-haiku-4.5")

    def test_resume_command_uses_session_id(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            session_dir = root / "abc"
            session_dir.mkdir(parents=True, exist_ok=True)
            (session_dir / "workspace.yaml").write_text("cwd: /tmp/project\n", encoding="utf-8")
            (session_dir / "events.jsonl").write_text("", encoding="utf-8")
            session = CopilotProvider(root).discover_sessions(limit=1)[0]
            self.assertEqual(CopilotProvider().resume_command(session), ["copilot", "--silent", "--no-color", "--resume=abc"])


if __name__ == "__main__":
    unittest.main()
