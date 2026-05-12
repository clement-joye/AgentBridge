from __future__ import annotations

from collections.abc import Iterable

from ..models import LocalSession
from ..providers.base import SessionProvider
from ..utils.git import git_info


class SessionDiscoveryService:
    def __init__(self, providers: Iterable[SessionProvider]) -> None:
        self.providers = list(providers)

    def latest(self, limit: int = 10, provider_name: str | None = None) -> list[LocalSession]:
        all_sessions: list[LocalSession] = []
        for provider in self.providers:
            if provider_name is not None and provider.name != provider_name:
                continue
            all_sessions.extend(provider.discover_sessions(limit=limit))

        for s in all_sessions:
            repo, branch, dirty = git_info(s.cwd)
            s.repo_name = repo
            s.branch = branch
            s.git_dirty_count = dirty

        ordered = sorted(all_sessions, key=lambda s: s.last_active_at, reverse=True)[:limit]
        for i, s in enumerate(ordered, start=1):
            s.index = i
        return ordered
