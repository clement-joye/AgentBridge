from __future__ import annotations

import unittest
from datetime import datetime, timezone

from telegram_ai_bridge.models import LocalSession
from telegram_ai_bridge.sessions.discovery import SessionDiscoveryService


class _FakeProvider:
    def __init__(self, name: str, sessions: list[LocalSession]) -> None:
        self.name = name
        self._sessions = sessions

    def discover_sessions(self, limit: int) -> list[LocalSession]:
        return self._sessions[:limit]


class DiscoveryTests(unittest.TestCase):
    def test_latest_filters_provider_before_limit(self) -> None:
        now = datetime.now(timezone.utc)
        codex_sessions = [
            LocalSession(
                index=0,
                provider="codex",
                session_id=f"codex-{i}",
                cwd="/tmp/codex",
                repo_name=None,
                branch=None,
                last_active_at=now,
                last_user_prompt=None,
                last_final_answer=None,
                resumable=True,
                source_path=None,
            )
            for i in range(10)
        ]
        copilot_sessions = [
            LocalSession(
                index=0,
                provider="copilot",
                session_id="copilot-1",
                cwd="/tmp/copilot",
                repo_name=None,
                branch=None,
                last_active_at=now,
                last_user_prompt=None,
                last_final_answer=None,
                resumable=True,
                source_path=None,
            )
        ]
        discovery = SessionDiscoveryService(
            [
                _FakeProvider("codex", codex_sessions),
                _FakeProvider("copilot", copilot_sessions),
            ]
        )

        sessions = discovery.latest(limit=10, provider_name="copilot")

        self.assertEqual([s.provider for s in sessions], ["copilot"])
        self.assertEqual([s.session_id for s in sessions], ["copilot-1"])


if __name__ == "__main__":
    unittest.main()
