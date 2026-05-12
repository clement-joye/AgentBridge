from __future__ import annotations

from datetime import datetime, timezone
import tempfile
import unittest
from pathlib import Path

from telegram_ai_bridge.db import Database
from telegram_ai_bridge.models import ActiveSession


class DbBindingsTests(unittest.TestCase):
    def test_bindings_and_default_alias(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            db = Database(Path(d) / "bridge.sqlite3")
            s1 = ActiveSession(
                telegram_chat_id="1",
                provider="codex",
                session_id="a",
                cwd="/tmp/a",
                process_id=None,
                alias="app",
                mode="resumed",
                status="ready",
                created_at=datetime.now(timezone.utc),
                last_message_at=datetime.now(timezone.utc),
                model="gpt-5.5",
            )
            s2 = ActiveSession(
                telegram_chat_id="1",
                provider="codex",
                session_id="b",
                cwd="/tmp/b",
                process_id=None,
                alias="api",
                mode="resumed",
                status="ready",
                created_at=datetime.now(timezone.utc),
                last_message_at=datetime.now(timezone.utc),
            )
            db.upsert_binding(s1)
            db.upsert_binding(s2)
            db.set_default_alias(1, "api")

            rows = db.list_bindings(1)
            self.assertEqual(len(rows), 2)
            self.assertEqual(db.get_default_alias(1), "api")
            hit = db.get_binding(1, "app")
            self.assertIsNotNone(hit)
            self.assertEqual(hit.session_id, "a")
            self.assertEqual(hit.model, "gpt-5.5")


if __name__ == "__main__":
    unittest.main()
