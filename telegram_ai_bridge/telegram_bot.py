from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from html import escape
import logging
from pathlib import Path
import queue
import socket

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction
from telegram.error import NetworkError, RetryAfter, TelegramError, TimedOut
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from .config import BridgeConfig
from .db import Database
from .models import LocalSession
from .providers.codex import CodexProvider
from .providers.copilot import CopilotProvider
from .providers.base import SessionProvider
from .sessions.active import ActiveSessionManager
from .sessions.discovery import SessionDiscoveryService
from .sessions.registry import SessionRegistry
from .sessions.runner import AgentRunner
from .utils.formatting import chunk_text, fence_code, render_active, render_command_list, render_sessions
from .utils.git import changed_files, diff_text, git_info, git_root, worktree_add, worktree_remove
from .utils.output_cleanup import remove_prompt_echo
from .utils.security import contains_destructive_intent, is_allowed_cwd, is_authorized_user

logger = logging.getLogger(__name__)
io_logger = logging.getLogger("telegram_ai_bridge.io")


def _split_markdown_fenced_blocks(text: str) -> list[tuple[str, bool]]:
    parts: list[tuple[str, bool]] = []
    current_text: list[str] = []
    current_code: list[str] = []
    in_code = False

    for line in text.splitlines():
        if line.startswith("```"):
            if in_code:
                parts.append(("\n".join(current_code).strip("\n"), True))
                current_code = []
                in_code = False
            else:
                if current_text:
                    parts.append(("\n".join(current_text).strip("\n"), False))
                    current_text = []
                in_code = True
            continue
        if in_code:
            current_code.append(line)
        else:
            current_text.append(line)

    if in_code:
        # Unclosed fences are still best displayed as code.
        parts.append(("\n".join(current_code).strip("\n"), True))
    elif current_text:
        parts.append(("\n".join(current_text).strip("\n"), False))

    return [(part, is_code) for part, is_code in parts if part.strip()]


def _killswitch_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Turn on", callback_data="killswitch:on"),
        InlineKeyboardButton("Turn off", callback_data="killswitch:off"),
    ]])


def _trace_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Turn on", callback_data="trace:on"),
        InlineKeyboardButton("Turn off", callback_data="trace:off"),
    ]])


def _model_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Clear model override", callback_data="model:clear"),
    ]])


class BridgeBot:
    def __init__(self, config: BridgeConfig, config_path: str | Path | None = None) -> None:
        self.config = config
        self._config_path: Path | None = Path(config_path) if config_path else None
        self.db = Database(config.state_dir / "bridge.sqlite3")
        self.registry = SessionRegistry(self.db)
        self.active = ActiveSessionManager(self.db)
        self.runner = AgentRunner(timeout_seconds=config.runner_timeout_seconds)
        self.kill_switch_enabled = self.db.get_kill_switch()

        providers: list[SessionProvider] = []
        if config.codex.enabled:
            providers.append(CodexProvider())
        if config.copilot.enabled:
            providers.append(CopilotProvider())
        self.providers = {p.name: p for p in providers}
        
        # Always include all providers for discovery, even if disabled
        # The 'enabled' flag only controls whether new sessions can be created/resumed
        discovery_providers: list[SessionProvider] = []
        discovery_providers.append(CodexProvider())
        discovery_providers.append(CopilotProvider())
        self.discovery = SessionDiscoveryService(discovery_providers)

    def build(self) -> Application:
        builder = Application.builder().token(self.config.telegram_bot_token)
        builder = builder.post_shutdown(self.on_shutdown)
        app = builder.build()
        app.add_error_handler(self.on_error)
        app.add_handler(CommandHandler("start", self.start))
        app.add_handler(CommandHandler("help", self.help))
        app.add_handler(CommandHandler("ping", self.ping))
        app.add_handler(CommandHandler("last", self.last))
        app.add_handler(CommandHandler("last_codex", self.last_codex))
        app.add_handler(CommandHandler("last_copilot", self.last_copilot))
        app.add_handler(CommandHandler("use", self.use))
        app.add_handler(CommandHandler("new", self.new))
        app.add_handler(CommandHandler("active", self.active_cmd))
        app.add_handler(CommandHandler("sessions", self.sessions_cmd))
        app.add_handler(CommandHandler("close", self.close))
        app.add_handler(CommandHandler("abort", self.abort))
        app.add_handler(CommandHandler("status", self.status))
        app.add_handler(CommandHandler("model", self.model_cmd))
        app.add_handler(CommandHandler("killswitch", self.killswitch_cmd))
        app.add_handler(CommandHandler("trace", self.trace_cmd))
        app.add_handler(CommandHandler("git", self.git_cmd))
        app.add_handler(CommandHandler("files", self.files_cmd))
        app.add_handler(CommandHandler("diff", self.diff_cmd))
        app.add_handler(CommandHandler("history", self.history_cmd))
        app.add_handler(CommandHandler("reload", self.reload_cmd))
        app.add_handler(CommandHandler("worktree", self.worktree_cmd))
        app.add_handler(CallbackQueryHandler(self.handle_callback))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.handle_text))
        if app.job_queue is not None:
            app.job_queue.run_repeating(self.housekeeping, interval=60, first=10)
        else:
            logger.warning("JobQueue not available — install python-telegram-bot[job-queue] for background housekeeping")
        logger.info("Bridge handlers registered. Waiting for Telegram updates.")
        return app

    async def on_shutdown(self, application: Application) -> None:
        _ = application
        self.runner.close_all_sessions()

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        logger.exception("Telegram handler error for update=%r", update, exc_info=context.error)

    async def housekeeping(self, context: ContextTypes.DEFAULT_TYPE | None) -> None:
        _ = context
        removed = self.runner.cleanup_dead_sessions()
        if removed:
            logger.info("Cleaned %d dead runner sessions", len(removed))
        for session in self.active.list_all():
            alias = session.alias or "default"
            chat_key = f"{session.telegram_chat_id}:{alias}"
            has_live = self.runner.has_live_session(chat_key)
            if session.status == "running" and not has_live:
                session.status = "error"
                session.last_message_at = datetime.now(timezone.utc)
                self.db.upsert_binding(session)

    def _authorized(self, update: Update) -> bool:
        user_id = update.effective_user.id if update.effective_user else None
        chat_id = update.effective_chat.id if update.effective_chat else None
        return is_authorized_user(self.config, user_id, chat_id)

    async def _deny_if_unauthorized(self, update: Update) -> bool:
        if self._authorized(update):
            return False
        chat_id = update.effective_chat.id if update.effective_chat else None
        user_id = update.effective_user.id if update.effective_user else None
        logger.warning("Unauthorized update denied user_id=%s chat_id=%s", user_id, chat_id)
        if self.config.audit_enabled:
            self.db.log_audit_event(
                event_type="unauthorized_access",
                detail="request denied by allowlist",
                chat_id=chat_id,
                user_id=user_id,
            )
        await self._reply_text(
            update,
            "Request denied by allowlist. Check `allowed_user_ids` and `allowed_chat_ids` in `config.toml`.",
        )
        return True

    async def _reply_text(
        self, update: Update, text: str, retries: int = 2, parse_mode: str | None = None
    ) -> bool:
        message = update.message
        if message is None:
            logger.warning("No Telegram message object available for reply")
            return False
        self._log_telegram_payload(update, text, label="telegram_send")
        attempt = 0
        while True:
            try:
                await message.reply_text(text, parse_mode=parse_mode)
                logger.info("Telegram reply sent chat_id=%s chars=%d", self._chat_id(update), len(text))
                return True
            except RetryAfter as exc:
                attempt += 1
                if attempt > retries:
                    logger.error(
                        "Telegram reply failed after retry-after wait chat_id=%s chars=%d error=%s",
                        self._chat_id(update),
                        len(text),
                        exc,
                    )
                    return False
                logger.warning(
                    "Telegram reply retry-after chat_id=%s attempt=%d wait=%s",
                    self._chat_id(update),
                    attempt,
                    exc.retry_after,
                )
                await asyncio.sleep(float(exc.retry_after) + 0.25)
            except (TimedOut, NetworkError) as exc:
                attempt += 1
                if attempt > retries:
                    logger.error(
                        "Telegram reply failed after retries chat_id=%s chars=%d error=%s",
                        self._chat_id(update),
                        len(text),
                        exc,
                    )
                    return False
                logger.warning(
                    "Telegram reply attempt failed chat_id=%s attempt=%d error=%s",
                    self._chat_id(update),
                    attempt,
                    exc,
                )
                await asyncio.sleep(0.75 * attempt)
            except TelegramError as exc:
                logger.error(
                    "Telegram reply failed chat_id=%s chars=%d error=%s",
                    self._chat_id(update),
                    len(text),
                    exc,
                )
                if parse_mode:
                    logger.warning(
                        "Telegram formatted reply failed; retrying as plain text chat_id=%s parse_mode=%s",
                        self._chat_id(update),
                        parse_mode,
                    )
                    return await self._reply_text(update, text, retries=retries, parse_mode=None)
                return False

    async def _reply_chunks(
        self, update: Update, text: str, chunk_size: int = 3800, parse_mode: str | None = None
    ) -> bool:
        chunks = chunk_text(text, chunk_size=chunk_size)
        logger.info(
            "Telegram reply prepared chat_id=%s chunks=%d total_chars=%d",
            self._chat_id(update),
            len(chunks),
            len(text),
        )
        self._log_telegram_payload(update, text, label="telegram_full_reply")
        for index, chunk in enumerate(chunks, start=1):
            logger.info(
                "Telegram reply chunk chat_id=%s chunk=%d/%d chars=%d",
                self._chat_id(update),
                index,
                len(chunks),
                len(chunk),
            )
            if not await self._reply_text(update, chunk, parse_mode=parse_mode):
                logger.error(
                    "Telegram reply chunk failed chat_id=%s chunk=%d/%d",
                    self._chat_id(update),
                    index,
                    len(chunks),
                )
                return False
        return True

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        _ = context
        self._log_update(update, "start")
        if await self._deny_if_unauthorized(update):
            return
        await self._reply_text(update, "Telegram AI Bridge is running. Use /help.")

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        _ = context
        self._log_update(update, "help")
        if await self._deny_if_unauthorized(update):
            return
        await self._reply_text(update, render_command_list())

    async def ping(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        _ = context
        self._log_update(update, "ping")
        if await self._deny_if_unauthorized(update):
            return
        await self._reply_text(update, f"Bridge is running on {socket.gethostname()}")

    async def last(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        _ = context
        self._log_update(update, "last")
        if await self._deny_if_unauthorized(update):
            return
        if self.kill_switch_enabled:
            await self._reply_text(update, "Kill switch is enabled. Command denied.")
            return
        chat_id = update.effective_chat.id
        sessions = self._latest_sessions(chat_id, None)
        await self._reply_text(update, render_sessions(sessions), parse_mode="HTML")

    async def last_codex(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        _ = context
        self._log_update(update, "last_codex")
        if await self._deny_if_unauthorized(update):
            return
        if self.kill_switch_enabled:
            await self._reply_text(update, "Kill switch is enabled. Command denied.")
            return
        chat_id = update.effective_chat.id
        sessions = self._latest_sessions(chat_id, "codex")
        await self._reply_text(update, render_sessions(sessions), parse_mode="HTML")

    async def last_copilot(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        _ = context
        self._log_update(update, "last_copilot")
        if await self._deny_if_unauthorized(update):
            return
        if self.kill_switch_enabled:
            await self._reply_text(update, "Kill switch is enabled. Command denied.")
            return
        chat_id = update.effective_chat.id
        sessions = self._latest_sessions(chat_id, "copilot")
        await self._reply_text(update, render_sessions(sessions), parse_mode="HTML")

    def _latest_sessions(self, chat_id: int, provider: str | None) -> list[LocalSession]:
        sessions = self.discovery.latest(limit=10, provider_name=provider)
        self.registry.store_last(chat_id, sessions)
        return sessions

    async def use(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._log_update(update, "use")
        if await self._deny_if_unauthorized(update):
            return
        if self.kill_switch_enabled:
            await self._reply_text(update, "Kill switch is enabled. Command denied.")
            return
        chat_id = update.effective_chat.id
        if not context.args:
            await self._reply_text(update, "Usage: /use <index>")
            return
        alias = "default"
        if len(context.args) >= 3 and context.args[1].lower() == "as":
            alias = context.args[2].strip()
        elif len(context.args) == 2:
            await self._reply_text(update, "Usage: /use <index> as <alias>")
            return
        try:
            idx = int(context.args[0])
        except ValueError:
            await self._reply_text(update, "Index must be a number.")
            return

        session = self.registry.get_indexed(chat_id, idx)
        if not session:
            await self._reply_text(update, "Session index not found. Run /last to refresh the session list.")
            return
        if not is_allowed_cwd(self.config, session.cwd):
            await self._reply_text(update, "Session path is blocked by policy.")
            return
        self.runner.close_session(f"{chat_id}:{alias}")
        active = self.active.bind(chat_id, session, alias=alias, make_default=True)
        await self._reply_text(
            update,
            f"Using {session.provider.title()} session {idx}"
            + (f" as {alias}" if alias != "default" else "")
            + "\n\n"
            f"Repo: {session.repo_name or 'unknown'}\n"
            f"Path: {session.cwd}\n"
            f"Branch: {session.branch or '-'}\n"
            f"Model: {active.model or 'default'}\n"
            f"Status: {active.status}\n\n"
            "Send your next message to continue this session."
        )

    async def new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._log_update(update, "new")
        if await self._deny_if_unauthorized(update):
            return
        if self.kill_switch_enabled:
            await self._reply_text(update, "Kill switch is enabled. Command denied.")
            return
        chat_id = update.effective_chat.id

        target_provider: str | None = None
        target_cwd: str | None = None
        target_model: str | None = None
        source_label = "active session"

        if context.args:
            try:
                idx = int(context.args[0])
            except ValueError:
                await self._reply_text(update, "Usage: /new or /new <index>")
                return
            source = self.registry.get_indexed(chat_id, idx)
            if not source:
                await self._reply_text(update, "Session index not found. Run /last to refresh the session list.")
                return
            target_provider = source.provider
            target_cwd = source.cwd
            target_model = source.model
            source_label = f"session {idx}"
        else:
            current = self.active.get(chat_id)
            if not current:
                await self._reply_text(update, "No active session. Use /new 1 or /use 1 first.")
                return
            target_provider = current.provider
            target_cwd = current.cwd
            target_model = current.model

        if not target_provider or not target_cwd:
            await self._reply_text(update, "Unable to determine session target.")
            return
        if target_provider not in self.providers:
            await self._reply_text(update, "Provider unavailable.")
            return
        if not is_allowed_cwd(self.config, target_cwd):
            await self._reply_text(update, "Target path is blocked by policy.")
            return

        default_alias = self.active.get_default_alias(chat_id) or "default"
        self.runner.close_session(f"{chat_id}:{default_alias}")
        active = self.active.bind_new(
            chat_id, target_provider, target_cwd, alias=default_alias, make_default=True, model=target_model
        )
        await self._reply_text(
            update,
            f"Started new {target_provider.title()} session from {source_label}.\n\n"
            f"Path: {active.cwd}\n"
            f"Mode: {active.mode}\n"
            f"Model: {active.model or 'default'}\n"
            f"Status: {active.status}\n\n"
            "Send your next message to begin."
        )

    async def active_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        _ = context
        self._log_update(update, "active")
        if await self._deny_if_unauthorized(update):
            return
        chat_id = update.effective_chat.id
        session = self.active.get(chat_id)
        if not session:
            await update.message.reply_text("No active session.")
            return
        await update.message.reply_text(render_active(session))

    async def sessions_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        _ = context
        self._log_update(update, "sessions")
        if await self._deny_if_unauthorized(update):
            return
        chat_id = update.effective_chat.id
        sessions = self.active.list_for_chat(chat_id)
        if not sessions:
            await update.message.reply_text("No active sessions.")
            return
        default_alias = self.active.get_default_alias(chat_id)
        lines = ["Active sessions", ""]
        for i, session in enumerate(sessions, start=1):
            alias = session.alias or "default"
            key = f"{chat_id}:{alias}"
            runner_state = "live" if self.runner.has_live_session(key) else "idle"
            marker = " (default)" if alias == default_alias else ""
            lines.append(
                f"{i}. {alias}{marker} - {session.provider.title()} - {session.cwd} - "
                f"{session.status} ({runner_state}) - model: {session.model or 'default'}"
            )
        await update.message.reply_text("\n".join(lines))

    async def close(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._log_update(update, "close")
        if await self._deny_if_unauthorized(update):
            return
        chat_id = update.effective_chat.id
        target_alias: str | None = None
        if context.args:
            token = context.args[0].strip()
            sessions = self.active.list_for_chat(chat_id)
            if token.isdigit():
                idx = int(token)
                if idx < 1 or idx > len(sessions):
                    await update.message.reply_text("Session index not found.")
                    return
                target_alias = sessions[idx - 1].alias or "default"
            else:
                target_alias = token
                if not self.active.get(chat_id, alias=target_alias):
                    await update.message.reply_text("Session alias not found.")
                    return
        else:
            current = self.active.get(chat_id)
            if not current:
                await update.message.reply_text("No active session.")
                return
            target_alias = current.alias or "default"
        self.runner.close_session(f"{chat_id}:{target_alias}")
        self.active.close(chat_id, alias=target_alias)
        if self.active.get_default_alias(chat_id) == target_alias:
            remaining = self.active.list_for_chat(chat_id)
            if remaining:
                self.active.set_default(chat_id, remaining[0].alias or "default")
        # Clean up any managed worktree for this alias.
        wt = self.db.get_worktree(chat_id, target_alias)
        wt_note = ""
        if wt:
            ok, msg = worktree_remove(wt["repo_root"], wt["worktree_path"])
            self.db.delete_worktree(chat_id, target_alias)
            wt_note = f"\nWorktree {'removed' if ok else 'removal failed — ' + msg}: {wt['worktree_path']}"
        await update.message.reply_text("Active session closed." + wt_note)

    async def abort(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        _ = context
        self._log_update(update, "abort")
        if await self._deny_if_unauthorized(update):
            return
        chat_id = update.effective_chat.id
        session = self.active.get(chat_id)
        if not session:
            await update.message.reply_text("No active session.")
            return
        alias = session.alias or "default"
        aborted = self.runner.abort_session(f"{chat_id}:{alias}")
        session.status = "ready"
        session.last_message_at = datetime.now(timezone.utc)
        self.db.upsert_binding(session)
        if aborted:
            await update.message.reply_text("Current task aborted. Session binding kept.")
        else:
            await update.message.reply_text("No running local process to abort. Session binding kept.")

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        _ = context
        self._log_update(update, "status")
        if await self._deny_if_unauthorized(update):
            return
        chat_id = update.effective_chat.id
        session = self.active.get(chat_id)
        if not session:
            await update.message.reply_text("No active session.")
            return
        alias = session.alias or "default"
        runner_state = "live" if self.runner.has_live_session(f"{chat_id}:{alias}") else "idle"
        await update.message.reply_text(
            "Session status\n\n"
            f"Alias: {alias}\n"
            f"Provider: {session.provider.title()}\n"
            f"Mode: {session.mode}\n"
            f"State: {session.status}\n"
            f"Process: {runner_state}\n"
            f"Model: {session.model or 'default'}\n"
            f"Path: {session.cwd}\n"
            f"Kill switch: {'on' if self.kill_switch_enabled else 'off'}"
        )

    async def model_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._log_update(update, "model")
        if await self._deny_if_unauthorized(update):
            return
        chat_id = update.effective_chat.id
        session = self.active.get(chat_id)
        if not session:
            await update.message.reply_text("No active session.")
            return
        alias = session.alias or "default"
        if not context.args:
            await update.message.reply_text(
                "Model\n\n"
                f"Alias: {alias}\n"
                f"Provider: {session.provider.title()}\n"
                f"Current: {session.model or 'default'}\n\n"
                "Usage: /model <model-name> or /model clear",
                reply_markup=_model_keyboard(),
            )
            return
        value = " ".join(context.args).strip()
        if not value:
            await update.message.reply_text("Usage: /model <model-name> or /model clear")
            return
        if value.lower() in {"clear", "default", "none"}:
            session.model = None
            self.db.upsert_binding(session)
            self._audit(update, "model_changed", f"alias={alias}; model=default")
            await update.message.reply_text(f"Model override cleared for {alias}.")
            return
        session.model = value
        self.db.upsert_binding(session)
        self._audit(update, "model_changed", f"alias={alias}; model={value}")
        await update.message.reply_text(f"Model for {alias} set to {value}.")

    async def killswitch_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._log_update(update, "killswitch")
        if await self._deny_if_unauthorized(update):
            return
        if not context.args:
            state = "on" if self.kill_switch_enabled else "off"
            await update.message.reply_text(f"Kill switch is {state}.", reply_markup=_killswitch_keyboard())
            return
        value = context.args[0].strip().lower()
        if value not in {"on", "off"}:
            await update.message.reply_text("Usage: /killswitch <on|off>")
            return
        self.kill_switch_enabled = value == "on"
        self.db.set_kill_switch(self.kill_switch_enabled)
        if self.kill_switch_enabled:
            self.runner.close_all_sessions()
            for active in self.active.list_all():
                active.status = "error"
                active.last_message_at = datetime.now(timezone.utc)
                self.db.upsert_binding(active)
        self._audit(update, "killswitch_changed", f"value={value}")
        await update.message.reply_text(f"Kill switch is now {value}.")

    async def trace_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._log_update(update, "trace")
        if await self._deny_if_unauthorized(update):
            return
        chat_id = update.effective_chat.id
        if not context.args:
            current = self.db.get_trace_mode(chat_id)
            await update.message.reply_text(f"Trace mode is {current}.", reply_markup=_trace_keyboard())
            return
        value = context.args[0].strip().lower()
        if value not in {"on", "off"}:
            await update.message.reply_text("Usage: /trace <on|off>")
            return
        self.db.set_trace_mode(chat_id, value)
        self._audit(update, "trace_changed", f"value={value}")
        await update.message.reply_text(f"Trace mode set to {value}.")

    async def git_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        _ = context
        self._log_update(update, "git")
        if await self._deny_if_unauthorized(update):
            return
        chat_id = update.effective_chat.id
        session = self.active.get(chat_id)
        if not session:
            await update.message.reply_text("No active session.")
            return
        repo, branch, dirty = git_info(session.cwd)
        root = git_root(session.cwd)
        if not repo:
            await update.message.reply_text("No git repository found for active session path.")
            return
        status_line = "clean" if dirty == 0 else f"{dirty} modified files"
        await update.message.reply_text(
            "Git status\n\n"
            f"Repo: {repo}\n"
            f"Branch: {branch or '-'}\n"
            f"Path: {root or session.cwd}\n"
            f"Status: {status_line}"
        )

    async def files_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        _ = context
        self._log_update(update, "files")
        if await self._deny_if_unauthorized(update):
            return
        chat_id = update.effective_chat.id
        session = self.active.get(chat_id)
        if not session:
            await update.message.reply_text("No active session.")
            return
        files = changed_files(session.cwd)
        if not files:
            await update.message.reply_text("No changed files.")
            return
        lines = ["Changed files", ""]
        lines.extend(f"- {path}" for path in files[:200])
        if len(files) > 200:
            lines.append("")
            lines.append(f"...and {len(files) - 200} more")
        for chunk in chunk_text("\n".join(lines)):
            await update.message.reply_text(chunk)

    async def diff_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        _ = context
        self._log_update(update, "diff")
        if await self._deny_if_unauthorized(update):
            return
        chat_id = update.effective_chat.id
        session = self.active.get(chat_id)
        if not session:
            await update.message.reply_text("No active session.")
            return
        diff = diff_text(session.cwd)
        if diff in {"No diff.", "Unable to read git diff."}:
            await update.message.reply_text(diff)
            return
        payload = "Diff summary\n\n" + fence_code(diff, "diff")
        for chunk in chunk_text(payload, chunk_size=3400):
            await update.message.reply_text(chunk)

    async def history_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        self._log_update(update, "history")
        if await self._deny_if_unauthorized(update):
            return
        chat_id = update.effective_chat.id
        session = self.active.get(chat_id)
        alias = (session.alias or "default") if session else "default"
        limit = 5
        if context.args:
            try:
                limit = max(1, min(int(context.args[0]), 20))
            except ValueError:
                await update.message.reply_text("Usage: /history [n]  — show last n exchanges (default 5, max 20)")
                return
        rows = self.db.get_history(chat_id, alias, limit=limit * 2)
        if not rows:
            await update.message.reply_text(f"No history for alias '{alias}'.")
            return
        lines = [f"<b>History</b> (alias: {escape(alias)}, last {min(len(rows), limit * 2)} entries)", ""]
        for row in rows:
            role_label = "You" if row["role"] == "user" else "Agent"
            ts = row["created_at"][:16].replace("T", " ")
            content = escape(row["content"][:300])
            lines.append(f"<b>{role_label}</b> <i>{ts}</i>\n{content}")
            lines.append("")
        await self._reply_chunks(update, "\n".join(lines).strip(), chunk_size=3800, parse_mode="HTML")

    async def reload_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        _ = context
        self._log_update(update, "reload")
        if await self._deny_if_unauthorized(update):
            return
        if not self._config_path:
            await update.message.reply_text("Config path not set — cannot reload.")
            return
        try:
            from .config import load_config as _load_config
            new_cfg = _load_config(self._config_path)
        except Exception as exc:
            await update.message.reply_text(f"Reload failed — keeping current config.\n\n{escape(str(exc))}", parse_mode="HTML")
            return
        old_cfg = self.config
        self.config = new_cfg
        changed: list[str] = []
        for field in ("allowed_user_ids", "allowed_chat_ids", "allowed_repo_roots", "blocked_paths",
                      "codex", "copilot", "state_dir", "runner_timeout_seconds", "audit_enabled",
                      "deny_destructive_prompts"):
            if getattr(old_cfg, field) != getattr(new_cfg, field):
                changed.append(field)
        summary = "Config reloaded."
        if changed:
            summary += f"\nChanged fields: {', '.join(changed)}"
        self._audit(update, "config_reloaded", f"changed={changed}")
        await update.message.reply_text(summary)

    async def worktree_cmd(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Create a git worktree from a listed session and start a fresh agent session in it.

        Usage: /worktree <index> [as <alias>] [branch <name>]
        """
        self._log_update(update, "worktree")
        if await self._deny_if_unauthorized(update):
            return
        if self.kill_switch_enabled:
            await self._reply_text(update, "Kill switch is enabled. Command denied.")
            return

        USAGE = "Usage: /worktree <index> [as <alias>] [branch <name>]"
        if not context.args:
            await update.message.reply_text(USAGE)
            return

        # --- Parse arguments ---
        args = list(context.args)
        try:
            idx = int(args.pop(0))
        except ValueError:
            await update.message.reply_text(USAGE)
            return

        alias = "default"
        branch: str | None = None
        i = 0
        while i < len(args):
            token = args[i].lower()
            if token == "as" and i + 1 < len(args):
                alias = args[i + 1].strip()
                i += 2
            elif token == "branch" and i + 1 < len(args):
                branch = args[i + 1].strip()
                i += 2
            else:
                await update.message.reply_text(f"Unexpected argument: {args[i]}\n{USAGE}")
                return

        chat_id = update.effective_chat.id

        # --- Resolve source session ---
        source = self.registry.get_indexed(chat_id, idx)
        if not source:
            await update.message.reply_text("Session index not found. Run /last to refresh the session list.")
            return

        if source.provider not in self.providers:
            await update.message.reply_text(f"Provider '{source.provider}' is not enabled in your config.")
            return

        # --- Find git root of the source session ---
        repo_root = git_root(source.cwd)
        if not repo_root:
            await update.message.reply_text(
                "The source session path is not inside a git repository. Worktrees require git."
            )
            return

        # --- Build worktree path ---
        repo_path = Path(repo_root)
        worktree_path = str(repo_path.parent / f"{repo_path.name}-worktrees" / alias)

        # --- Security check on the worktree path ---
        # The worktree dir doesn't exist yet; check its parent instead.
        worktree_parent = str(Path(worktree_path).parent)
        Path(worktree_parent).mkdir(parents=True, exist_ok=True)
        if not is_allowed_cwd(self.config, worktree_parent):
            await update.message.reply_text(
                f"Worktree path is not covered by your policy.\n\n"
                f"Planned path: {worktree_path}\n\n"
                f"The parent directory ({worktree_parent}) is not under any path in "
                f"`allowed_repo_roots` or is explicitly blocked.\n\n"
                f"To fix this, add the parent to `allowed_repo_roots` in your config:\n"
                f"  allowed_repo_roots = [\"{worktree_parent}\"]"
            )
            return

        # --- Create the worktree ---
        ok, msg = worktree_add(repo_root, worktree_path, branch)
        if not ok:
            await update.message.reply_text(f"Failed to create worktree.\n\n{msg}")
            return

        # --- Bind new agent session ---
        effective_branch = branch or Path(worktree_path).name
        self.db.save_worktree(chat_id, alias, repo_root, worktree_path, effective_branch)
        active = self.active.bind_new(
            chat_id, source.provider, worktree_path, alias=alias, make_default=True, model=source.model
        )
        self._audit(update, "worktree_created", f"alias={alias}; path={worktree_path}; branch={effective_branch}")
        await update.message.reply_text(
            f"Worktree session ready.\n\n"
            f"Provider: {source.provider.title()}\n"
            f"Alias: {alias}\n"
            f"Path: {worktree_path}\n"
            f"Branch: {effective_branch}\n"
            f"Model: {active.model or 'default'}\n\n"
            f"Send your next message (or prefix with @{alias}) to start working.\n"
            f"Use /close {alias} when done — the worktree will be removed automatically."
        )

    async def handle_callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        _ = context
        query = update.callback_query
        if query is None:
            return
        if not self._authorized(update):
            await query.answer(text="Unauthorized", show_alert=True)
            return
        await query.answer()
        data = query.data or ""
        chat_id = update.effective_chat.id

        if data in ("killswitch:on", "killswitch:off"):
            value = data.split(":")[1]
            self.kill_switch_enabled = value == "on"
            self.db.set_kill_switch(self.kill_switch_enabled)
            if self.kill_switch_enabled:
                self.runner.close_all_sessions()
                for active in self.active.list_all():
                    active.status = "error"
                    active.last_message_at = datetime.now(timezone.utc)
                    self.db.upsert_binding(active)
            self._audit(update, "killswitch_changed", f"value={value}")
            await query.edit_message_text(f"Kill switch is now {value}.", reply_markup=_killswitch_keyboard())

        elif data in ("trace:on", "trace:off"):
            value = data.split(":")[1]
            self.db.set_trace_mode(chat_id, value)
            self._audit(update, "trace_changed", f"value={value}")
            await query.edit_message_text(f"Trace mode is now {value}.", reply_markup=_trace_keyboard())

        elif data == "model:clear":
            session = self.active.get(chat_id)
            if session:
                alias = session.alias or "default"
                session.model = None
                self.db.upsert_binding(session)
                self._audit(update, "model_changed", f"alias={alias}; model=default")
                await query.edit_message_text(f"Model override cleared for {alias}.")
            else:
                await query.edit_message_text("No active session.")

        else:
            logger.warning("Unknown callback_data=%r", data)
            await query.edit_message_text("Unknown action.")

    async def handle_text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        _ = context
        self._log_update(update, "text")
        if await self._deny_if_unauthorized(update):
            return
        chat_id = update.effective_chat.id
        message = update.message.text or ""
        alias_from_prefix: str | None = None
        if message.startswith("@"):
            first, _, rest = message.partition(" ")
            alias_from_prefix = first[1:].strip()
            if alias_from_prefix:
                message = rest.strip()
        if self.kill_switch_enabled:
            self._audit(update, "message_denied_killswitch", "text message denied")
            await self._reply_text(update, "Kill switch is enabled. Prompt forwarding is disabled.")
            return
        if self.config.deny_destructive_prompts and contains_destructive_intent(message):
            self._audit(update, "message_denied_destructive", message[:500])
            await self._reply_text(
                update,
                "Prompt blocked by safety policy due to destructive command intent."
            )
            return

        active_session = self.active.get(chat_id, alias=alias_from_prefix)
        if not active_session:
            await self._reply_text(update, "No active session. Run /last then /use <n>.")
            return
        if alias_from_prefix and not message:
            await self._reply_text(update, "Alias selected but prompt is empty.")
            return

        provider = self.providers.get(active_session.provider)
        if not provider:
            await self._reply_text(update, "Provider unavailable.")
            return
        if active_session.status == "running":
            await self._reply_text(update, "Session is currently running. Wait for completion or use /status.")
            return

        session = LocalSession(
            index=0,
            provider=active_session.provider,
            session_id=active_session.session_id,
            cwd=active_session.cwd,
            repo_name=None,
            branch=None,
            last_active_at=datetime.now(timezone.utc),
            last_user_prompt=None,
            last_final_answer=None,
            resumable=True,
            source_path=None,
            model=active_session.model,
        )
        active_session.status = "running"
        active_session.last_message_at = datetime.now(timezone.utc)
        self.db.upsert_binding(active_session)
        alias = active_session.alias or "default"
        chat_key = f"{chat_id}:{alias}"

        logger.info(
            "Forwarding prompt to runner chat_id=%s alias=%s provider=%s mode=%s session_id=%s model=%s chars=%d",
            chat_id,
            alias,
            active_session.provider,
            active_session.mode,
            active_session.session_id,
            active_session.model or "default",
            len(message),
        )

        # Thread-safe queue: runner thread puts progress strings; asyncio drainer sends them live.
        progress_q: queue.Queue[str] = queue.Queue()

        def progress_cb(msg: str) -> None:
            progress_q.put(msg)

        async def _live_feedback() -> None:
            """Send TYPING indicator every 4 s and drain progress messages as they arrive."""
            typing_interval = 4.0
            last_typing = 0.0
            while True:
                now = asyncio.get_event_loop().time()
                # Drain any queued progress items.
                while True:
                    try:
                        item = progress_q.get_nowait()
                        html = f"<i>In progress</i>\n<code>{escape(item)}</code>"
                        await self._reply_text(update, html, parse_mode="HTML")
                    except queue.Empty:
                        break
                # Send typing action on the interval.
                if now - last_typing >= typing_interval:
                    try:
                        await update.effective_chat.send_action(ChatAction.TYPING)
                    except Exception:
                        pass
                    last_typing = now
                await asyncio.sleep(0.5)

        feedback_task = asyncio.create_task(_live_feedback())
        try:
            result = await asyncio.to_thread(
                self.runner.send_prompt, chat_key, provider, session, message, active_session.mode, progress_cb
            )
        finally:
            feedback_task.cancel()
            try:
                await feedback_task
            except asyncio.CancelledError:
                pass
            # Drain any remaining progress items the task didn't reach.
            while True:
                try:
                    item = progress_q.get_nowait()
                    html = f"<i>In progress</i>\n<code>{escape(item)}</code>"
                    await self._reply_text(update, html, parse_mode="HTML")
                except queue.Empty:
                    break

        logger.info(
            "Runner completed chat_id=%s alias=%s ok=%s timed_out=%s prompt_matched=%s output_chars=%d "
            "changed_files=%d progress=%d tools=%d reasoning=%d subagents=%d detected_session_id=%s",
            chat_id,
            alias,
            result.ok,
            result.timed_out,
            result.prompt_matched,
            len(result.output or ""),
            len(result.changed_files or []),
            len(result.progress_messages or []),
            len(result.tool_calls or []),
            len(result.reasoning_events or []),
            len(result.subagent_events or []),
            result.detected_session_id,
        )
        active_session.last_message_at = datetime.now(timezone.utc)
        if active_session.mode == "new" and active_session.session_id.startswith("pending:"):
            if result.detected_session_id:
                active_session.session_id = result.detected_session_id
                active_session.mode = "resumed"
        active_session.status = "ready" if result.ok else "error"
        self.db.upsert_binding(active_session)
        trace_mode = self.db.get_trace_mode(chat_id)

        if result.ok:
            sent = await self._send_agent_result(
                update=update,
                result=result,
                prompt=message,
                trace_mode=trace_mode,
                provider=active_session.provider,
                mode=active_session.mode,
                session_id=active_session.session_id,
                alias=alias,
                process_live=self.runner.has_live_session(chat_key),
                live_progress=True,
            )
            answer = (result.output or "").strip()
            self.db.append_history(chat_id, alias, "user", message)
            self.db.append_history(chat_id, alias, "assistant", answer[:500])
        else:
            detail = result.output or "No details provided."
            if result.timed_out:
                detail += "\n\nThe local command hit the timeout window."
            if trace_mode == "on":
                detail += (
                    "\n\nTrace:\n"
                    f"- provider: {active_session.provider}\n"
                    f"- mode: {active_session.mode}\n"
                    f"- session_id: {active_session.session_id}"
                )
            reply = "Task failed.\n\nDetails:\n" + detail
            sent = await self._reply_chunks(update, reply, chunk_size=3800)
        self._audit(update, "prompt_forwarded", f"ok={result.ok}; mode={active_session.mode}")
        logger.info("Telegram reply flow finished chat_id=%s sent=%s", chat_id, sent)

    def _audit(self, update: Update, event_type: str, detail: str) -> None:
        if not self.config.audit_enabled:
            return
        chat_id = update.effective_chat.id if update.effective_chat else None
        user_id = update.effective_user.id if update.effective_user else None
        self.db.log_audit_event(event_type=event_type, detail=detail, chat_id=chat_id, user_id=user_id)

    def _log_update(self, update: Update, route: str) -> None:
        chat_id = update.effective_chat.id if update.effective_chat else None
        user_id = update.effective_user.id if update.effective_user else None
        text = getattr(update.message, "text", "") if update.message else ""
        preview = text[:80].replace("\n", " ")
        logger.info("Update route=%s user_id=%s chat_id=%s text=%r", route, user_id, chat_id, preview)
        if route == "text":
            io_logger.info("INPUT chat_id=%s user_id=%s chars=%d text=%r", chat_id, user_id, len(text), text)

    def _chat_id(self, update: Update) -> int | None:
        chat = getattr(update, "effective_chat", None)
        return chat.id if chat else None

    def _user_id(self, update: Update) -> int | None:
        user = getattr(update, "effective_user", None)
        return user.id if user else None

    def _log_telegram_payload(self, update: Update, text: str, *, label: str) -> None:
        io_logger.info(
            "%s chat_id=%s user_id=%s chars=%d\n----- BEGIN TELEGRAM PAYLOAD -----\n%s\n----- END TELEGRAM PAYLOAD -----",
            label,
            self._chat_id(update),
            self._user_id(update),
            len(text),
            text,
        )

    async def _send_agent_result(
        self,
        *,
        update: Update,
        result,
        prompt: str,
        trace_mode: str,
        provider: str,
        mode: str,
        session_id: str,
        alias: str,
        process_live: bool,
        live_progress: bool = False,
    ) -> bool:
        for message in self._render_agent_activity_messages(result, live_progress=live_progress):
            if not await self._reply_text(update, message, parse_mode="HTML"):
                return False

        changed_files = self._render_changed_files_message(result)
        if changed_files and not await self._reply_chunks(update, changed_files, chunk_size=3800, parse_mode="HTML"):
            return False

        if trace_mode == "on":
            trace = (
                "Trace\n"
                f"- prompt_matched: {result.prompt_matched}\n"
                f"- provider: {provider}\n"
                f"- mode: {mode}\n"
                f"- session_id: {session_id}\n"
                f"- alias: {alias}\n"
                f"- process_live: {process_live}"
            )
            if not await self._reply_text(update, trace):
                return False

        answer = remove_prompt_echo(result.output or "No output", prompt).strip() or "No output"
        return await self._reply_answer(update, answer)

    def _render_agent_activity_messages(self, result, *, live_progress: bool = False) -> list[str]:
        messages: list[str] = []
        if result.reasoning_events:
            messages.append(f"<i>Thinking</i>\nInternal reasoning completed: {len(result.reasoning_events)} step(s).")
        # tool_calls and progress_messages are sent live when live_progress is True;
        # fall back to batch rendering for providers that don't stream (e.g. copilot).
        if not live_progress and result.tool_calls:
            messages.extend(f"<i>Function call</i>\n<code>{escape(item)}</code>" for item in result.tool_calls[:12])
        if result.subagent_events:
            messages.extend(f"<i>Sub agent</i>\n<code>{escape(item)}</code>" for item in result.subagent_events[:8])
        if not live_progress and result.progress_messages:
            messages.extend(f"<i>Progress</i>\n{escape(item)}" for item in result.progress_messages[-5:])
        return messages

    def _render_changed_files_message(self, result) -> str | None:
        if result.changed_files:
            files = [f"- <code>{escape(path)}</code>" for path in result.changed_files[:30]]
            if len(result.changed_files) > 30:
                files.append(f"...and {len(result.changed_files) - 30} more")
            return "<i>Changed files</i>\n" + "\n".join(files)
        return None

    async def _reply_answer(self, update: Update, answer: str) -> bool:
        for text, is_code in _split_markdown_fenced_blocks(answer):
            if is_code:
                for chunk in chunk_text(text, chunk_size=3400):
                    html = f"<pre><code>{escape(chunk)}</code></pre>"
                    if not await self._reply_text(update, html, parse_mode="HTML"):
                        return False
                continue
            if text.strip() and not await self._reply_chunks(update, text.strip(), chunk_size=3800):
                return False
        return True
