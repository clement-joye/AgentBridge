from __future__ import annotations

from datetime import datetime
from html import escape
import re

from ..models import ActiveSession, LocalSession


def fmt_time(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M")


def render_sessions(sessions: list[LocalSession]) -> str:
    if not sessions:
        return "No sessions found."
    lines = ["Latest sessions", ""]
    for s in sessions:
        dirty = f"{s.git_dirty_count} modified files" if s.git_dirty_count else "clean"
        lines.append(f"{s.index}. {escape(s.provider.title())} - {escape(s.repo_name or 'unknown')} - {escape(s.branch or '-')}")
        details = "\n".join(
            [
                f"Path: {s.cwd}",
                f"Last active: {fmt_time(s.last_active_at)}",
                f"Git: {dirty}",
                f"Model: {s.model or 'default'}",
            ]
        )
        if s.last_user_prompt:
            details += f"\nLast prompt: {s.last_user_prompt[:160]}"
        lines.append(f"<pre><code>{escape(details)}</code></pre>")
        lines.append("")
    lines.append("<i>Indices are valid until the next /last call.</i>")
    return "\n".join(lines).strip()


def render_command_list() -> str:
    commands = [
        ("start", "Confirm the bridge is running"),
        ("help", "Show the command list"),
        ("ping", "Reply with the host name"),
        ("last", "List recent sessions from all providers"),
        ("last_codex", "List recent Codex sessions only"),
        ("last_copilot", "List recent Copilot sessions only"),
        ("use", "Bind a listed session; provide a session number, and optionally an alias"),
        ("new", "Start a new session; provide a session number to copy a listed session"),
        ("active", "Show the current chat binding"),
        ("sessions", "List saved chat session bindings"),
        ("close", "Close the active session; provide an alias or index to target one"),
        ("abort", "Stop the active runner session"),
        ("status", "Show bot, session, and safety status"),
        ("model", "View or set the active model; provide a name or `clear`"),
        ("killswitch", "Enable or disable prompt handling; provide `on` or `off`"),
        ("trace", "Enable or disable trace output; provide `on` or `off`"),
        ("git", "Show git repository details"),
        ("files", "List changed files in the active repo"),
        ("diff", "Show the current git diff"),
        ("history", "Show recent prompt/response pairs; optionally provide a count"),
        ("reload", "Hot-reload the config file without restarting"),
        ("worktree", "Create a new session in a git worktree; provide index, optional alias and branch"),
    ]
    lines = [f"{name} - {desc}" for name, desc in commands]
    lines.append("")
    lines.append("Tip: prefix any message with @alias to send it to a named session, e.g. @work fix the bug.")
    return "\n".join(lines)


def render_active(session: ActiveSession) -> str:
    return (
        "Active session\n\n"
        f"Provider: {session.provider.title()}\n"
        f"Path: {session.cwd}\n"
        f"Mode: {session.mode}\n"
        f"Status: {session.status}\n"
        f"Model: {session.model or 'default'}"
    )


def chunk_text(text: str, chunk_size: int = 3500) -> list[str]:
    clean = text.strip()
    if not clean:
        return ["No output."]
    chunks: list[str] = []
    current = clean
    while current:
        if len(current) <= chunk_size:
            chunks.append(current)
            break
        split = current.rfind("\n", 0, chunk_size)
        if split <= 0:
            split = chunk_size
        chunks.append(current[:split].rstrip())
        current = current[split:].lstrip("\n")
    return chunks


def fence_code(text: str, language: str = "") -> str:
    # Avoid accidental fence termination in payload text.
    escaped = re.sub(r"```", "`\u200b``", text)
    return f"```{language}\n{escaped}\n```"
