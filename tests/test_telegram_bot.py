from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
import sys
from types import SimpleNamespace
from types import ModuleType


class _TelegramError(Exception):
    pass


class _TimedOut(_TelegramError):
    pass


class _NetworkError(_TelegramError):
    pass


class _RetryAfter(_TelegramError):
    def __init__(self, retry_after: float = 0) -> None:
        super().__init__(f"retry after {retry_after}")
        self.retry_after = retry_after


telegram_module = ModuleType("telegram")
telegram_module.Update = object
telegram_module.InlineKeyboardButton = lambda text, callback_data=None: SimpleNamespace(text=text, callback_data=callback_data)
telegram_module.InlineKeyboardMarkup = lambda buttons: SimpleNamespace(buttons=buttons)
telegram_error_module = ModuleType("telegram.error")
telegram_error_module.NetworkError = _NetworkError
telegram_error_module.RetryAfter = _RetryAfter
telegram_error_module.TelegramError = _TelegramError
telegram_error_module.TimedOut = _TimedOut
telegram_ext_module = ModuleType("telegram.ext")
telegram_ext_module.Application = object
telegram_ext_module.CallbackQueryHandler = object
telegram_ext_module.CommandHandler = object
telegram_ext_module.ContextTypes = SimpleNamespace(DEFAULT_TYPE=object)
telegram_ext_module.MessageHandler = object
telegram_ext_module.filters = SimpleNamespace(TEXT=object(), COMMAND=object())
telegram_constants_module = ModuleType("telegram.constants")
telegram_constants_module.ChatAction = SimpleNamespace(TYPING="typing")

sys.modules.setdefault("telegram", telegram_module)
sys.modules.setdefault("telegram.error", telegram_error_module)
sys.modules.setdefault("telegram.ext", telegram_ext_module)
sys.modules.setdefault("telegram.constants", telegram_constants_module)

from telegram_ai_bridge.config import BridgeConfig, ProviderConfig
from telegram_ai_bridge.models import ActiveSession, LocalSession
from telegram_ai_bridge.sessions.runner import RunnerResult
from telegram_ai_bridge.telegram_bot import BridgeBot, _split_markdown_fenced_blocks


class _FakeMessage:
    def __init__(self, outcomes: list[object]) -> None:
        self.outcomes = list(outcomes)
        self.sent: list[str] = []
        self.parse_modes: list[str | None] = []

    async def reply_text(self, text: str, parse_mode: str | None = None) -> None:
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
        self.sent.append(text)
        self.parse_modes.append(parse_mode)


class _CaptureRegistry:
    def __init__(self) -> None:
        self.last_chat_id: int | None = None
        self.last_sessions: list[LocalSession] = []

    def store_last(self, chat_id: int, sessions: list[LocalSession]) -> None:
        self.last_chat_id = chat_id
        self.last_sessions = list(sessions)


class _DiscoveryStub:
    def __init__(self, sessions: list[LocalSession]) -> None:
        self.sessions = sessions
        self.last_provider_name: str | None = None
        self.last_limit: int | None = None

    def latest(self, limit: int = 10, provider_name: str | None = None) -> list[LocalSession]:
        self.last_limit = limit
        self.last_provider_name = provider_name
        sessions = self.sessions
        if provider_name is not None:
            sessions = [session for session in sessions if session.provider == provider_name]
        return sessions[:limit]


class TelegramBotTests(unittest.IsolatedAsyncioTestCase):
    def _make_bot(self, state_dir: Path) -> BridgeBot:
        config = BridgeConfig(
            telegram_bot_token="test-token",
            allowed_user_ids={1},
            allowed_chat_ids={1},
            allowed_repo_roots=[],
            blocked_paths=[],
            codex=ProviderConfig(enabled=False),
            copilot=ProviderConfig(enabled=False),
            state_dir=state_dir,
            runner_timeout_seconds=30,
            audit_enabled=False,
            deny_destructive_prompts=True,
        )
        return BridgeBot(config)

    async def test_reply_text_retries_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            bot = self._make_bot(Path(d))
            update = SimpleNamespace(message=_FakeMessage([_TimedOut(), object()]))

            ok = await bot._reply_text(update, "hello")

            self.assertTrue(ok)
            self.assertEqual(update.message.sent, ["hello"])

    async def test_reply_text_returns_false_after_retries(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            bot = self._make_bot(Path(d))
            update = SimpleNamespace(message=_FakeMessage([_TimedOut(), _TimedOut(), _TimedOut()]))

            ok = await bot._reply_text(update, "hello")

            self.assertFalse(ok)
            self.assertEqual(update.message.sent, [])

    async def test_help_lists_commands_without_slashes(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            bot = self._make_bot(Path(d))
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=1),
                effective_chat=SimpleNamespace(id=1),
                message=_FakeMessage([object()]),
            )
            context = SimpleNamespace(args=[])

            await bot.help(update, context)

            body = update.message.sent[0]
            self.assertIn("last_codex - List recent Codex sessions only", body)
            self.assertIn("last_copilot - List recent Copilot sessions only", body)
            self.assertIn("use - Bind a listed session; provide a session number, and optionally an alias", body)
            self.assertIn("model - View or set the active model; provide a name or `clear`", body)
            self.assertNotIn("/last_codex", body)
            self.assertNotIn("/last_copilot", body)

    async def test_unauthorized_request_replies_with_allowlist_message(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            bot = self._make_bot(Path(d))
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=999),
                effective_chat=SimpleNamespace(id=999),
                message=_FakeMessage([object()]),
            )

            denied = await bot._deny_if_unauthorized(update)

            self.assertTrue(denied)
            self.assertEqual(
                update.message.sent,
                ["Request denied by allowlist. Check `allowed_user_ids` and `allowed_chat_ids` in `config.toml`."],
            )

    async def test_use_updates_active_model_from_selected_session(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            bot = self._make_bot(Path(d))
            now = datetime.now(timezone.utc)
            bot.registry.store_last(
                1,
                [
                    LocalSession(
                        index=1,
                        provider="codex",
                        session_id="sess-1",
                        cwd=str(Path(d)),
                        repo_name="repo",
                        branch="main",
                        last_active_at=now,
                        last_user_prompt=None,
                        last_final_answer=None,
                        resumable=True,
                        source_path=None,
                        model="gpt-5.5",
                    )
                ],
            )
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=1),
                effective_chat=SimpleNamespace(id=1),
                message=_FakeMessage([object()]),
            )
            context = SimpleNamespace(args=["1"])

            await bot.use(update, context)

            active = bot.active.get(1)
            assert active is not None
            self.assertEqual(active.model, "gpt-5.5")
            self.assertIn("Model: gpt-5.5", update.message.sent[0])

    async def test_last_renders_sessions_as_html(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            bot = self._make_bot(Path(d))
            now = datetime.now(timezone.utc)
            bot.discovery = _DiscoveryStub(
                [
                    LocalSession(
                        index=1,
                        provider="copilot",
                        session_id="sess-1",
                        cwd="/tmp/project",
                        repo_name="repo",
                        branch="main",
                        last_active_at=now,
                        last_user_prompt="Use <b>tag</b> & check",
                        last_final_answer="done",
                        resumable=True,
                        source_path=None,
                        model="claude-sonnet-4.6",
                    )
                ]
            )
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=1),
                effective_chat=SimpleNamespace(id=1),
                message=_FakeMessage([object()]),
            )
            context = SimpleNamespace(args=[])

            await bot.last(update, context)

            self.assertEqual(update.message.parse_modes, ["HTML"])
            self.assertEqual(bot.discovery.last_provider_name, None)
            self.assertIn("1. Copilot - repo - main", update.message.sent[0])
            self.assertIn(
                "<pre><code>Path: /tmp/project\nLast active: ", update.message.sent[0]
            )
            self.assertIn("Last prompt: Use &lt;b&gt;tag&lt;/b&gt; &amp; check", update.message.sent[0])

    async def test_last_codex_filters_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            bot = self._make_bot(Path(d))
            now = datetime.now(timezone.utc)
            bot.discovery = _DiscoveryStub(
                [
                    LocalSession(
                        index=1,
                        provider="codex",
                        session_id="codex-1",
                        cwd="/tmp/codex",
                        repo_name="repo",
                        branch="main",
                        last_active_at=now,
                        last_user_prompt=None,
                        last_final_answer=None,
                        resumable=True,
                        source_path=None,
                        model=None,
                    ),
                    LocalSession(
                        index=2,
                        provider="copilot",
                        session_id="copilot-1",
                        cwd="/tmp/copilot",
                        repo_name="repo",
                        branch="main",
                        last_active_at=now,
                        last_user_prompt=None,
                        last_final_answer=None,
                        resumable=True,
                        source_path=None,
                        model=None,
                    ),
                ]
            )
            capture = _CaptureRegistry()
            bot.registry = capture  # type: ignore[assignment]
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=1),
                effective_chat=SimpleNamespace(id=1),
                message=_FakeMessage([object()]),
            )
            context = SimpleNamespace(args=[])

            await bot.last_codex(update, context)

            self.assertEqual(capture.last_chat_id, 1)
            self.assertEqual([s.provider for s in capture.last_sessions], ["codex"])
            self.assertEqual(bot.discovery.last_provider_name, "codex")
            self.assertEqual(update.message.parse_modes, ["HTML"])

    async def test_last_copilot_filters_sessions(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            bot = self._make_bot(Path(d))
            now = datetime.now(timezone.utc)
            bot.discovery = _DiscoveryStub(
                [
                    LocalSession(
                        index=1,
                        provider="codex",
                        session_id="codex-1",
                        cwd="/tmp/codex",
                        repo_name="repo",
                        branch="main",
                        last_active_at=now,
                        last_user_prompt=None,
                        last_final_answer=None,
                        resumable=True,
                        source_path=None,
                        model=None,
                    ),
                    LocalSession(
                        index=2,
                        provider="copilot",
                        session_id="copilot-1",
                        cwd="/tmp/copilot",
                        repo_name="repo",
                        branch="main",
                        last_active_at=now,
                        last_user_prompt=None,
                        last_final_answer=None,
                        resumable=True,
                        source_path=None,
                        model=None,
                    ),
                ]
            )
            capture = _CaptureRegistry()
            bot.registry = capture  # type: ignore[assignment]
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=1),
                effective_chat=SimpleNamespace(id=1),
                message=_FakeMessage([object()]),
            )
            context = SimpleNamespace(args=[])

            await bot.last_copilot(update, context)

            self.assertEqual(capture.last_chat_id, 1)
            self.assertEqual([s.provider for s in capture.last_sessions], ["copilot"])
            self.assertEqual(bot.discovery.last_provider_name, "copilot")
            self.assertEqual(update.message.parse_modes, ["HTML"])

    async def test_send_agent_result_splits_activity_and_final_answer(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            bot = self._make_bot(Path(d))
            long_answer = "final line\n" + ("x" * 2500)
            result = RunnerResult(
                ok=True,
                output=long_answer,
                changed_files=["telegram_ai_bridge/sessions/runner.py"],
                progress_messages=["Commentary: inspecting runner"],
                tool_calls=["exec_command: telegram_ai_bridge/sessions/runner.py"],
                reasoning_events=["thinking"],
                subagent_events=["spawn_agent"],
                prompt_matched=True,
            )
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=1),
                effective_chat=SimpleNamespace(id=1),
                message=_FakeMessage([object(), object(), object(), object(), object(), object()]),
            )

            sent = await bot._send_agent_result(
                update=update,
                result=result,
                prompt="prompt",
                trace_mode="off",
                provider="codex",
                mode="resumed",
                session_id="sess-1",
                alias="default",
                process_live=True,
            )

            self.assertTrue(sent)
            self.assertEqual(update.message.sent[0], "<i>Thinking</i>\nInternal reasoning completed: 1 step(s).")
            self.assertEqual(
                update.message.sent[1],
                "<i>Function call</i>\n<code>exec_command: telegram_ai_bridge/sessions/runner.py</code>",
            )
            self.assertEqual(update.message.sent[2], "<i>Sub agent</i>\n<code>spawn_agent</code>")
            self.assertEqual(update.message.sent[3], "<i>Progress</i>\nCommentary: inspecting runner")
            self.assertEqual(
                update.message.sent[4],
                "<i>Changed files</i>\n- <code>telegram_ai_bridge/sessions/runner.py</code>",
            )
            self.assertEqual(update.message.sent[5], long_answer)
            self.assertEqual(update.message.parse_modes[:5], ["HTML", "HTML", "HTML", "HTML", "HTML"])
            self.assertIsNone(update.message.parse_modes[5])

    async def test_reply_answer_sends_fenced_code_as_html_pre(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            bot = self._make_bot(Path(d))
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=1),
                effective_chat=SimpleNamespace(id=1),
                message=_FakeMessage([object(), object(), object()]),
            )

            sent = await bot._reply_answer(update, "Before\n```markdown\n# Title\n```\nAfter")

            self.assertTrue(sent)
            self.assertEqual(update.message.sent, ["Before", "<pre><code># Title</code></pre>", "After"])
            self.assertEqual(update.message.parse_modes, [None, "HTML", None])

    def test_split_markdown_fenced_blocks(self) -> None:
        self.assertEqual(
            _split_markdown_fenced_blocks("a\n```python\nprint(1)\n```\nb"),
            [("a", False), ("print(1)", True), ("b", False)],
        )

    async def test_model_command_sets_active_model(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            bot = self._make_bot(Path(d))
            now = datetime.now(timezone.utc)
            bot.db.upsert_binding(
                ActiveSession(
                    telegram_chat_id="1",
                    provider="codex",
                    session_id="sess-1",
                    cwd="/tmp/project",
                    process_id=None,
                    alias="default",
                    mode="resumed",
                    status="ready",
                    created_at=now,
                    last_message_at=now,
                )
            )
            bot.db.set_default_alias(1, "default")
            update = SimpleNamespace(
                effective_user=SimpleNamespace(id=1),
                effective_chat=SimpleNamespace(id=1),
                message=_FakeMessage([object()]),
            )
            context = SimpleNamespace(args=["gpt-5.5"])

            await bot.model_cmd(update, context)

            active = bot.active.get(1)
            assert active is not None
            self.assertEqual(active.model, "gpt-5.5")
            self.assertEqual(update.message.sent, ["Model for default set to gpt-5.5."])


if __name__ == "__main__":
    unittest.main()
