from __future__ import annotations

import unittest

from telegram_ai_bridge.utils.formatting import chunk_text, fence_code


class FormattingTests(unittest.TestCase):
    def test_chunk_text_splits_long_payload(self) -> None:
        text = "a" * 9000
        chunks = chunk_text(text, chunk_size=3500)
        self.assertGreater(len(chunks), 2)
        self.assertEqual("".join(chunks), text)

    def test_fence_code_escapes_backtick_fence(self) -> None:
        payload = "line1\n```\nline2"
        fenced = fence_code(payload, "text")
        self.assertTrue(fenced.startswith("```text\n"))
        self.assertTrue(fenced.endswith("\n```"))
        self.assertIn("`\u200b``", fenced)


if __name__ == "__main__":
    unittest.main()
