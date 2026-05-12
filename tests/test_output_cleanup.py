from __future__ import annotations

import unittest

from telegram_ai_bridge.sessions.runner import _looks_like_terminal_garbage
from telegram_ai_bridge.utils.output_cleanup import remove_prompt_echo


class OutputCleanupTests(unittest.TestCase):
    def test_remove_prompt_echo_from_start_of_reply(self) -> None:
        prompt = "Can you check the tests?"
        reply = "Can you check the tests?\n\nThe failing test is in auth."
        self.assertEqual(remove_prompt_echo(reply, prompt), "The failing test is in auth.")

    def test_remove_prompt_echo_handles_multiline_prompt(self) -> None:
        prompt = "First line\nSecond line"
        reply = "First line\nSecond line\n\nAnswer below"
        self.assertEqual(remove_prompt_echo(reply, prompt), "Answer below")

    def test_detect_terminal_garbage_repeated_characters(self) -> None:
        self.assertTrue(_looks_like_terminal_garbage("MMMMMMMM"))
        self.assertTrue(_looks_like_terminal_garbage("--------"))
        self.assertFalse(_looks_like_terminal_garbage("Modified files"))


if __name__ == "__main__":
    unittest.main()
