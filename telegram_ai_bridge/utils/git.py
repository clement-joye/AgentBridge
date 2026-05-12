from __future__ import annotations

import subprocess
from pathlib import Path


def git_info(cwd: str) -> tuple[str | None, str | None, int]:
    p = Path(cwd)
    if not p.exists():
        return None, None, 0

    root = _run(["git", "rev-parse", "--show-toplevel"], cwd)
    if not root:
        return None, None, 0
    repo_name = Path(root).name
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd)
    dirty = _run(["git", "status", "--porcelain"], cwd)
    dirty_count = len(dirty.splitlines()) if dirty else 0
    return repo_name, branch, dirty_count


def git_root(cwd: str) -> str | None:
    return _run(["git", "rev-parse", "--show-toplevel"], cwd)


def changed_files(cwd: str) -> list[str]:
    dirty = _run(["git", "status", "--porcelain"], cwd)
    if not dirty:
        return []
    out: list[str] = []
    for line in dirty.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if path:
            out.append(path)
    return out


def diff_text(cwd: str, max_chars: int = 12000) -> str:
    text = _run(["git", "diff", "--", "."], cwd)
    if text is None:
        return "Unable to read git diff."
    if not text:
        return "No diff."
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 20] + "\n...[truncated]"


def worktree_add(repo_root: str, worktree_path: str, branch: str | None) -> tuple[bool, str]:
    """Create a git worktree at *worktree_path*.

    If *branch* is given and already exists, check it out there.
    If *branch* is given but does not exist yet, create it with ``-b``.
    If *branch* is None, a new branch named after the worktree directory is created.
    Returns (success, message).
    """
    path = Path(worktree_path)
    if path.exists():
        return False, f"Path already exists: {worktree_path}"

    target_branch = branch or path.name

    # Try checking out an existing branch first.
    ok, out = _run_result(["git", "worktree", "add", worktree_path, target_branch], repo_root)
    if ok:
        return True, f"Worktree created at {worktree_path} on branch '{target_branch}'."

    # Branch doesn't exist yet — create it.
    ok, out = _run_result(["git", "worktree", "add", "-b", target_branch, worktree_path], repo_root)
    if ok:
        return True, f"Worktree created at {worktree_path} on new branch '{target_branch}'."

    return False, f"git worktree add failed: {out.strip()}"


def worktree_remove(repo_root: str, worktree_path: str) -> tuple[bool, str]:
    """Remove a git worktree and its directory (with --force)."""
    ok, out = _run_result(["git", "worktree", "remove", "--force", worktree_path], repo_root)
    if ok:
        return True, f"Worktree removed: {worktree_path}"
    return False, f"git worktree remove failed: {out.strip()}"


def _run(cmd: list[str], cwd: str) -> str | None:
    try:
        res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            return None
        return res.stdout.strip()
    except Exception:
        return None


def _run_result(cmd: list[str], cwd: str) -> tuple[bool, str]:
    try:
        res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
        output = (res.stdout + res.stderr).strip()
        return res.returncode == 0, output
    except Exception as exc:
        return False, str(exc)
