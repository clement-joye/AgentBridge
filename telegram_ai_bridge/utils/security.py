from __future__ import annotations

from pathlib import Path
import re

from ..config import BridgeConfig


def is_authorized_user(config: BridgeConfig, user_id: int | None, chat_id: int | None) -> bool:
    if user_id is None or chat_id is None:
        return False
    return user_id in config.allowed_user_ids and chat_id in config.allowed_chat_ids


def is_allowed_cwd(config: BridgeConfig, cwd: str) -> bool:
    path = Path(cwd).expanduser().resolve()
    if not path.exists() or not path.is_dir():
        return False

    # Find the most-specific allowed root that covers this path.
    covering_root: Path | None = None
    if config.allowed_repo_roots:
        for root in config.allowed_repo_roots:
            if path == root or root in path.parents:
                if covering_root is None or len(root.parts) > len(covering_root.parts):
                    covering_root = root
        if covering_root is None:
            return False  # not under any allowed root

    # Check blocked paths.
    for blocked in config.blocked_paths:
        if path == blocked or blocked in path.parents:
            if covering_root is not None and blocked in covering_root.parents:
                # blocked is an ancestor of the allowed root — allowed root overrides it.
                continue
            return False

    return True


RISKY_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)\brm\s+-rf\b"),
    re.compile(r"(?i)\bmkfs(\.[a-z0-9]+)?\b"),
    re.compile(r"(?i)\bdd\s+if="),
    re.compile(r"(?i)\bshutdown\b"),
    re.compile(r"(?i)\breboot\b"),
    re.compile(r"(?i)\bchown\s+-R\b"),
    re.compile(r"(?i)\bchmod\s+-R\s+777\b"),
)


def contains_destructive_intent(text: str) -> bool:
    payload = text.strip()
    if not payload:
        return False
    for pattern in RISKY_PATTERNS:
        if pattern.search(payload):
            return True
    return False
