from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch
from unittest.mock import Mock

from telegram_ai_bridge.models import LocalSession
from telegram_ai_bridge.sessions.runner import (
    AgentRunner,
    RunnerResult,
    _extract_codex_terminal_reply,
    _parse_codex_exec_output,
    _parse_codex_terminal_turn,
)


class _StubProvider:
    name = "codex"


class _WarmReuseRunner(AgentRunner):
    def __init__(self) -> None:
        super().__init__()
        self.closed_keys: list[str] = []

    def close_session(self, chat_key: str) -> None:
        self.closed_keys.append(chat_key)

    def _changed_files(self, cwd: str) -> list[str]:
        return []

    def _run_pexpect(self, chat_key, provider, session, prompt, mode):  # type: ignore[override]
        _ = (chat_key, provider, session, prompt, mode)
        return RunnerResult(ok=True, output="ok", changed_files=[])

    def _run_codex_exec(self, provider, session, prompt, mode, progress_cb=None):  # type: ignore[override]
        _ = (provider, session, prompt, mode, progress_cb)
        return RunnerResult(ok=True, output="ok", changed_files=[])


class _StubChild:
    def __init__(self, chunks: list[str] | None = None) -> None:
        self._chunks = list(chunks or [])

    def sendline(self, prompt: str) -> None:
        _ = prompt

    def send(self, text: str) -> None:
        _ = text

    def read_nonblocking(self, size: int, timeout: float) -> str:
        _ = (size, timeout)
        if self._chunks:
            return self._chunks.pop(0)
        raise self.timeout_exc()

    def isalive(self) -> bool:
        return True

    def close(self, force: bool = True) -> None:
        _ = force

    def timeout_exc(self) -> Exception:
        return TimeoutError("no more chunks")


class RunnerTests(unittest.TestCase):
    def test_send_prompt_keeps_codex_session_warm(self) -> None:
        runner = _WarmReuseRunner()
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
            model="gpt-5.5",
        )
        with patch("telegram_ai_bridge.sessions.runner.pexpect", object()):
            result = runner.send_prompt("1:default", _StubProvider(), session, "hello", "resumed")

        self.assertTrue(result.ok)
        self.assertEqual(runner.closed_keys, [])

    def test_collect_codex_turn_window_returns_matching_turn_final_message(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "session.jsonl"
            prelude = [
                {
                    "timestamp": "2026-05-11T17:31:40.686Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "older-turn",
                        "last_agent_message": "older answer",
                    },
                }
            ]
            path.write_text("".join(json.dumps(item) + "\n" for item in prelude), encoding="utf-8")
            start_offset = path.stat().st_size

            events = [
                {
                    "timestamp": "2026-05-11T17:32:42.140Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "current-turn"},
                },
                {
                    "timestamp": "2026-05-11T17:32:42.153Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "Current prompt"},
                },
                {
                    "timestamp": "2026-05-11T17:32:48.638Z",
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "message": "working on it"},
                },
                {
                    "timestamp": "2026-05-11T17:32:48.640Z",
                    "type": "response_item",
                    "payload": {"type": "function_call", "name": "exec_command"},
                },
                {
                    "timestamp": "2026-05-11T17:33:15.846Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "current-turn",
                        "last_agent_message": "current answer",
                    },
                },
            ]
            with path.open("a", encoding="utf-8") as fh:
                for item in events:
                    fh.write(json.dumps(item) + "\n")

            result = AgentRunner()._collect_codex_turn_window(path, start_offset, "Current prompt", 3)

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result["final_text"], "current answer")
            self.assertEqual(result["progress_messages"], ["Progress: working on it"])
            self.assertEqual(result["tool_calls"], ["exec_command"])
            self.assertTrue(result["prompt_matched"])

    def test_collect_codex_turn_window_classifies_activity_without_exposing_reasoning(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "session.jsonl"
            path.write_text("", encoding="utf-8")
            events = [
                {
                    "timestamp": "2026-05-11T17:32:42.140Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "current-turn"},
                },
                {
                    "timestamp": "2026-05-11T17:32:42.153Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "Current prompt"},
                },
                {
                    "timestamp": "2026-05-11T17:32:43.000Z",
                    "type": "response_item",
                    "payload": {"type": "reasoning", "summary": [{"text": "private"}]},
                },
                {
                    "timestamp": "2026-05-11T17:32:44.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "exec_command",
                        "arguments": json.dumps({"cmd": "sed -n '1,20p' telegram_ai_bridge/sessions/runner.py"}),
                    },
                },
                {
                    "timestamp": "2026-05-11T17:32:45.000Z",
                    "type": "response_item",
                    "payload": {
                        "type": "function_call",
                        "name": "spawn_agent",
                        "arguments": json.dumps({"message": "Inspect runner.py", "agent_type": "explorer"}),
                    },
                },
                {
                    "timestamp": "2026-05-11T17:33:15.846Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "current-turn",
                        "last_agent_message": "complete final answer",
                    },
                },
            ]
            path.write_text("".join(json.dumps(item) + "\n" for item in events), encoding="utf-8")

            result = AgentRunner()._collect_codex_turn_window(path, 0, "Current prompt", 3)

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result["final_text"], "complete final answer")
            self.assertEqual(result["reasoning_events"], ["thinking"])
            self.assertEqual(result["tool_calls"], ["exec_command: telegram_ai_bridge/sessions/runner.py"])
            self.assertEqual(result["subagent_events"], ["spawn_agent"])

    def test_parse_codex_exec_output_returns_final_and_activity(self) -> None:
        lines = [
            {
                "type": "session_meta",
                "payload": {"id": "sess-1", "cwd": "/tmp/project"},
            },
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": "Current prompt"},
            },
            {
                "type": "response_item",
                "payload": {"type": "reasoning", "summary": [{"text": "private"}]},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": json.dumps({"cmd": "sed -n '1,20p' telegram_ai_bridge/sessions/runner.py"}),
                },
            },
            {
                "type": "event_msg",
                "payload": {"type": "agent_message", "message": "working"},
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "task_complete",
                    "last_agent_message": "Final answer",
                },
            },
        ]
        text = "\n".join(json.dumps(item) for item in lines)

        result = _parse_codex_exec_output(text, "Current prompt")

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.final_text, "Final answer")
        self.assertEqual(result.detected_session_id, "sess-1")
        self.assertEqual(result.reasoning_events, ["thinking"])
        self.assertEqual(result.tool_calls, ["exec_command: telegram_ai_bridge/sessions/runner.py"])
        self.assertEqual(result.progress_messages, ["Progress: working"])
        self.assertTrue(result.prompt_matched)

    def test_parse_codex_exec_output_handles_exec_event_stream(self) -> None:
        lines = [
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "turn.started"},
            {
                "type": "item.started",
                "item": {
                    "id": "item_0",
                    "type": "command_execution",
                    "command": "/usr/bin/bash -lc \"sed -n '1,20p' README.md\"",
                    "status": "in_progress",
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "id": "item_0",
                    "type": "command_execution",
                    "command": "/usr/bin/bash -lc \"sed -n '1,20p' README.md\"",
                    "aggregated_output": "# README",
                    "exit_code": 0,
                    "status": "completed",
                },
            },
            {
                "type": "item.completed",
                "item": {
                    "id": "item_1",
                    "type": "agent_message",
                    "text": "```markdown\n# README\n```",
                },
            },
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 0,
                    "output_tokens": 10,
                    "reasoning_output_tokens": 0,
                },
            },
        ]
        text = "\n".join(json.dumps(item) for item in lines)

        result = _parse_codex_exec_output(text, "show readme")

        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(result.detected_session_id, "thread-1")
        self.assertEqual(result.final_text, "```markdown\n# README\n```")
        self.assertEqual(result.tool_calls, ["command: README.md ok"])

    def test_run_codex_exec_skips_git_repo_check(self) -> None:
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
            model="gpt-5.5",
        )
        runner = AgentRunner(timeout_seconds=1)

        proc_mock = Mock()
        proc_mock.stdin = Mock()
        proc_mock.stdout = iter([])
        proc_mock.stderr = Mock()
        proc_mock.stderr.readlines = Mock(return_value=[])
        proc_mock.returncode = 0
        proc_mock.wait = Mock(return_value=0)

        with patch("telegram_ai_bridge.sessions.runner.subprocess.Popen", return_value=proc_mock) as popen:
            runner._run_codex_exec(_StubProvider(), session, "hello", "resumed")

        cmd = popen.call_args.args[0]
        self.assertTrue(cmd[0].endswith("codex"))
        self.assertEqual(cmd[1:5], ["exec", "resume", "--json", "--skip-git-repo-check"])
        self.assertIn("-m", cmd)
        self.assertIn("gpt-5.5", cmd)
        self.assertEqual(cmd[-2:], ["sess-1", "-"])

    def test_collect_codex_turn_window_ignores_nonmatching_completed_turns(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "session.jsonl"
            path.write_text("", encoding="utf-8")
            start_offset = 0
            events = [
                {
                    "timestamp": "2026-05-11T17:32:42.140Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "current-turn"},
                },
                {
                    "timestamp": "2026-05-11T17:32:42.153Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "Current prompt"},
                },
                {
                    "timestamp": "2026-05-11T17:32:48.638Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "other-turn",
                        "last_agent_message": "wrong answer",
                    },
                },
                {
                    "timestamp": "2026-05-11T17:33:15.846Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "current-turn",
                        "last_agent_message": "right answer",
                    },
                },
            ]
            path.write_text("".join(json.dumps(item) + "\n" for item in events), encoding="utf-8")

            result = AgentRunner()._collect_codex_turn_window(path, start_offset, "Current prompt", 3)

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result["final_text"], "right answer")

    def test_collect_codex_turn_window_ignores_startup_turn_before_matching_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "session.jsonl"
            path.write_text("", encoding="utf-8")
            start_offset = 0
            events = [
                {
                    "timestamp": "2026-05-11T17:32:40.000Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "startup-turn"},
                },
                {
                    "timestamp": "2026-05-11T17:32:40.500Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "startup-turn",
                        "last_agent_message": "startup answer",
                    },
                },
                {
                    "timestamp": "2026-05-11T17:32:42.140Z",
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": "current-turn"},
                },
                {
                    "timestamp": "2026-05-11T17:32:42.153Z",
                    "type": "event_msg",
                    "payload": {"type": "user_message", "message": "Current prompt"},
                },
                {
                    "timestamp": "2026-05-11T17:32:48.638Z",
                    "type": "event_msg",
                    "payload": {"type": "agent_message", "message": "working on current turn"},
                },
                {
                    "timestamp": "2026-05-11T17:33:15.846Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "task_complete",
                        "turn_id": "current-turn",
                        "last_agent_message": "current answer",
                    },
                },
            ]
            path.write_text("".join(json.dumps(item) + "\n" for item in events), encoding="utf-8")

            result = AgentRunner()._collect_codex_turn_window(path, start_offset, "Current prompt", 3)

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result["final_text"], "current answer")
            self.assertEqual(result["progress_messages"], ["Progress: working on current turn"])
            self.assertTrue(result["prompt_matched"])

    def test_extract_codex_terminal_reply_returns_last_assistant_block(self) -> None:
        raw = """
>_ OpenAI Codex
you > hello
assistant > I’m looking at the repo.
assistant > The fix is in the prompt matching path.
user >
"""
        result = _extract_codex_terminal_reply(raw, "hello")

        self.assertEqual(result, "The fix is in the prompt matching path.")

    def test_parse_codex_terminal_turn_ignores_previous_turns(self) -> None:
        raw = """
you > old prompt
assistant > Old answer
user >
you > current prompt
assistant > Current answer
user >
"""
        result = _parse_codex_terminal_turn(raw, "current prompt")

        self.assertTrue(result.prompt_matched)
        self.assertTrue(result.completed)
        self.assertEqual(result.final_text, "Current answer")

    def test_parse_codex_terminal_turn_does_not_fall_back_to_old_user_text(self) -> None:
        raw = """
you > old prompt
assistant > Old answer
user >
"""
        result = _parse_codex_terminal_turn(raw, "new prompt")

        self.assertFalse(result.prompt_matched)
        self.assertFalse(result.completed)
        self.assertIsNone(result.final_text)

    def test_extract_codex_terminal_reply_keeps_multiline_assistant_block(self) -> None:
        raw = """
you > check this
assistant > Summary
  line one
  line two
user >
"""
        result = _extract_codex_terminal_reply(raw, "check this")

        self.assertEqual(result, "Summary line one line two")

    def test_parse_codex_terminal_turn_handles_tui_prompt_and_activity(self) -> None:
        raw = """
>_ OpenAI Codex
› explain the change
Thinking
exec_command: telegram_ai_bridge/sessions/runner.py
spawn_agent
The bridge now reads the terminal output first.
It only uses JSONL if it is already available.
›
"""
        result = _parse_codex_terminal_turn(raw, "explain the change")

        self.assertTrue(result.prompt_matched)
        self.assertTrue(result.completed)
        self.assertEqual(
            result.final_text,
            "The bridge now reads the terminal output first.\nIt only uses JSONL if it is already available.",
        )
        self.assertEqual(result.reasoning_events, ["thinking"])
        self.assertEqual(result.tool_calls, ["exec_command: telegram_ai_bridge/sessions/runner.py"])
        self.assertEqual(result.subagent_events, ["subagent"])

    def test_run_pexpect_prefers_extracted_terminal_reply_for_codex(self) -> None:
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
        runner = AgentRunner(timeout_seconds=1, quiet_window_seconds=0)
        child = _StubChild(["you > hello\n", "assistant > structured final answer\n", "user >\n"])

        with (
            patch.object(runner, "_ensure_child", return_value=child),
            patch.object(runner, "_drain_stale_output", return_value=None),
            patch("telegram_ai_bridge.sessions.runner.pexpect") as fake_pexpect,
        ):
            fake_pexpect.TIMEOUT = type("FakeTimeout", (Exception,), {})
            fake_pexpect.EOF = type("FakeEOF", (Exception,), {})
            child.timeout_exc = fake_pexpect.TIMEOUT
            result = runner._run_pexpect("1:default", _StubProvider(), session, "hello", "resumed")

        self.assertTrue(result.ok)
        self.assertEqual(result.output, "structured final answer")
        self.assertEqual(result.progress_messages, [])
        self.assertEqual(result.tool_calls, [])
        self.assertTrue(result.prompt_matched)

    def test_run_pexpect_ignores_stale_prompt_markers_and_waits_for_matching_turn(self) -> None:
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
        runner = AgentRunner(timeout_seconds=1, quiet_window_seconds=0)
        child = _StubChild(
            [
                "user >\n",
                "you > current prompt\n",
                "assistant > New answer\n",
                "user >\n",
            ]
        )

        with (
            patch.object(runner, "_ensure_child", return_value=child),
            patch.object(runner, "_drain_stale_output", return_value=None),
            patch("telegram_ai_bridge.sessions.runner.pexpect") as fake_pexpect,
        ):
            fake_pexpect.TIMEOUT = type("FakeTimeout", (Exception,), {})
            fake_pexpect.EOF = type("FakeEOF", (Exception,), {})
            child.timeout_exc = fake_pexpect.TIMEOUT
            result = runner._run_pexpect("1:default", _StubProvider(), session, "current prompt", "resumed")

        self.assertTrue(result.ok)
        self.assertTrue(result.prompt_matched)
        self.assertEqual(result.output, "New answer")

    def test_run_pexpect_surfaces_startup_error_detail(self) -> None:
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
        runner = AgentRunner()
        runner._start_errors["1:default"] = "Failed to start session process: missing binary"

        with patch.object(runner, "_ensure_child", return_value=None):
            result = runner._run_pexpect("1:default", _StubProvider(), session, "hello", "resumed")

        self.assertFalse(result.ok)
        self.assertEqual(result.output, "Failed to start session process: missing binary")


if __name__ == "__main__":
    unittest.main()
