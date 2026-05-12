from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from telegram_ai_bridge.main import _is_dir_writable


class MainOpsTests(unittest.TestCase):
    def test_is_dir_writable(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "state"
            self.assertTrue(_is_dir_writable(p))


if __name__ == "__main__":
    unittest.main()
