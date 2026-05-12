from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from telegram_ai_bridge.db import Database
from telegram_ai_bridge.utils.git import worktree_add, worktree_remove


def _make_git_repo(path: Path) -> None:
    """Initialise a minimal git repo with one commit."""
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t.com"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "Test"], check=True, capture_output=True)
    (path / "README.md").write_text("hello")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-m", "init"], check=True, capture_output=True)


class WorktreeGitTests(unittest.TestCase):
    def test_add_and_remove_new_branch(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            _make_git_repo(repo)
            wt_path = str(Path(d) / "wt-alias")
            ok, msg = worktree_add(str(repo), wt_path, "feature-branch")
            self.assertTrue(ok, msg)
            self.assertTrue(Path(wt_path).is_dir())
            ok2, msg2 = worktree_remove(str(repo), wt_path)
            self.assertTrue(ok2, msg2)
            self.assertFalse(Path(wt_path).is_dir())

    def test_add_and_remove_default_branch(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            repo = Path(d) / "repo"
            repo.mkdir()
            _make_git_repo(repo)
            wt_path = str(Path(d) / "wt-default")
            ok, msg = worktree_add(str(repo), wt_path, None)
            self.assertTrue(ok, msg)
            self.assertTrue(Path(wt_path).is_dir())
            ok2, _ = worktree_remove(str(repo), wt_path)
            self.assertTrue(ok2)

    def test_add_fails_for_nonexistent_repo(self) -> None:
        ok, msg = worktree_add("/nonexistent/repo", "/tmp/wt-fail", "branch")
        self.assertFalse(ok)
        self.assertTrue(len(msg) > 0)


class WorktreeDbTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.db = Database(Path(self._tmpdir.name) / "bridge.sqlite3")

    def tearDown(self) -> None:
        self._tmpdir.cleanup()

    def test_save_and_retrieve(self) -> None:
        self.db.save_worktree(1, "feat", "/repo", "/repo-worktrees/feat", "feat-branch")
        row = self.db.get_worktree(1, "feat")
        self.assertIsNotNone(row)
        self.assertEqual(row["repo_root"], "/repo")
        self.assertEqual(row["worktree_path"], "/repo-worktrees/feat")
        self.assertEqual(row["branch"], "feat-branch")

    def test_not_found_returns_none(self) -> None:
        self.assertIsNone(self.db.get_worktree(99, "missing"))

    def test_delete_removes_row(self) -> None:
        self.db.save_worktree(2, "wt", "/r", "/wt", None)
        self.assertIsNotNone(self.db.get_worktree(2, "wt"))
        self.db.delete_worktree(2, "wt")
        self.assertIsNone(self.db.get_worktree(2, "wt"))

    def test_different_chats_isolated(self) -> None:
        self.db.save_worktree(10, "same-alias", "/r", "/wt10", "b")
        self.db.save_worktree(20, "same-alias", "/r", "/wt20", "b")
        self.assertEqual(self.db.get_worktree(10, "same-alias")["worktree_path"], "/wt10")
        self.assertEqual(self.db.get_worktree(20, "same-alias")["worktree_path"], "/wt20")


if __name__ == "__main__":
    unittest.main()
