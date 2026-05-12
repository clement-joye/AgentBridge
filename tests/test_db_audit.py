from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from telegram_ai_bridge.db import Database


class DbAuditTests(unittest.TestCase):
    def test_audit_event_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "bridge.sqlite3"
            db = Database(db_path)
            db.log_audit_event("test_event", "detail", chat_id=123, user_id=456)

            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT event_type, detail, chat_id, user_id FROM audit_events ORDER BY id DESC LIMIT 1"
                ).fetchone()
            finally:
                conn.close()

            assert row is not None
            self.assertEqual(row[0], "test_event")
            self.assertEqual(row[1], "detail")
            self.assertEqual(row[2], "123")
            self.assertEqual(row[3], "456")


if __name__ == "__main__":
    unittest.main()
