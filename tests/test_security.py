from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from telegram_ai_bridge.config import BridgeConfig, ProviderConfig
from telegram_ai_bridge.utils.security import contains_destructive_intent, is_allowed_cwd


class SecurityTests(unittest.TestCase):
    def _config(self, allowed_roots: list[Path], blocked_paths: list[Path]) -> BridgeConfig:
        return BridgeConfig(
            telegram_bot_token="x",
            allowed_user_ids={1},
            allowed_chat_ids={1},
            security_alert_chat_ids=set(),
            allowed_repo_roots=allowed_roots,
            blocked_paths=blocked_paths,
            codex=ProviderConfig(enabled=True),
            copilot=ProviderConfig(enabled=False),
            state_dir=Path(tempfile.gettempdir()),
            runner_timeout_seconds=180,
            audit_enabled=True,
            deny_destructive_prompts=True,
        )

    def test_detects_destructive_patterns(self) -> None:
        self.assertTrue(contains_destructive_intent("please run rm -rf tmp"))
        self.assertTrue(contains_destructive_intent("mkfs.ext4 /dev/sda"))
        self.assertTrue(contains_destructive_intent("dd if=/dev/zero of=/dev/sda"))

    def test_allows_safe_prompt(self) -> None:
        self.assertFalse(contains_destructive_intent("run tests and summarize failures"))

    def test_allowed_cwd_within_root(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            repo = root / "repo"
            repo.mkdir()
            cfg = self._config([root.resolve()], [])
            self.assertTrue(is_allowed_cwd(cfg, str(repo)))

    def test_blocked_path_wins(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            blocked = root / "secrets"
            blocked.mkdir()
            cfg = self._config([root.resolve()], [blocked.resolve()])
            self.assertFalse(is_allowed_cwd(cfg, str(blocked)))

    def test_allowed_root_overrides_blocked_parent(self) -> None:
        """allowed_repo_roots must take precedence over a blocked ancestor path."""
        with tempfile.TemporaryDirectory() as d:
            parent = Path(d)
            repos = parent / "repos"
            repos.mkdir()
            project = repos / "myproject"
            project.mkdir()
            # parent is blocked, but repos/ is explicitly allowed
            cfg = self._config([repos.resolve()], [parent.resolve()])
            self.assertTrue(is_allowed_cwd(cfg, str(project)))

    def test_path_outside_allowed_roots_denied(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            allowed = root / "repos"
            allowed.mkdir()
            other = root / "other"
            other.mkdir()
            cfg = self._config([allowed.resolve()], [])
            self.assertFalse(is_allowed_cwd(cfg, str(other)))

    def test_no_allowed_roots_and_not_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            cfg = self._config([], [])
            self.assertTrue(is_allowed_cwd(cfg, str(root)))


if __name__ == "__main__":
    unittest.main()
